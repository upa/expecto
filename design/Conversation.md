# 会話履歴と Compaction 設計ドキュメント

- ステータス: 実装済み
- 親ドキュメント: `DESIGN.md` (§5.7, §5.10)
- 本ドキュメントは「LLMClient が Chat Completions のメッセージ履歴を session
  全体で保持し、長くなったら compaction で圧縮する」設計の詳細。

---

## 1. 概要 / 記憶の役割分担

`LLMClient` が Chat Completions のメッセージ履歴を所有する。履歴は system
プロンプトから始まり、session 中の全 query・tool call・tool 結果・最終応答が
連続して蓄積される:

```
[system: SYSTEM_PROMPT]                     ← 完全静的
[user: query 1 (全画面 + Goal)]
[assistant: tool_calls=...]
[tool: 実行結果] ...
[assistant: 最終 explanation]
[user: query 2 (query 間の新規出力の差分 + Goal)]
...
```

| 記憶 | 実体 | カバー範囲 |
|------|------|-----------|
| 会話記憶 | メッセージ履歴 (クライアント保持) | LLM が見た/やったことすべて |
| 画面記憶 | query ごとに送る新規出力の差分 (§3) | query 間のユーザーの手動操作 |
| 圧縮記憶 | compaction 時の引き継ぎ要約 (§5) | 履歴を捨てて新しい会話に移行するときの文脈 |

## 2. プロンプトキャッシュの不変条件

プロンプトキャッシュは厳密な prefix 一致で効く。compaction 直後を除き
**毎リクエストが前リクエストの完全な延長**になるよう、以下を守る:

- 履歴は **append-only**。過去のメッセージは一切書き換えない。assistant 応答は
  返ってきた形のまま履歴に追加する (`tool_calls` の `arguments` はモデルが
  生成した生文字列を保持)。
- `system` プロンプト・`tools` 定義・`parallel_tool_calls=False`・model は
  リクエスト間で完全固定。
- compaction リクエストも同じ system・tools で発行する (§5)。
- session の要約を system プロンプトへ注入するような方式は、要約が更新される
  たびに履歴全体のキャッシュを破壊するため採らない (要約の出番は compaction
  時のみ。§5)。

テスト `test_history_is_append_only_across_requests` がこの不変条件
(後のリクエストの messages は前のリクエストの messages を prefix に持つ) を検証する。

## 3. Executor から見たインターフェース (compaction は透過)

```python
class LLMStepper(Protocol):
    async def start_query(query, *, screen_text, new_output=None, on_delta=None) -> StepResult
    def add_tool_output(call_id: str, output: str) -> None   # 同期・ローカル追加
    async def step(on_delta=None) -> StepResult
    def finish_query() -> None    # query の実行終了通知 (同期。compaction の契機)
```

- Executor は毎 query、`screen_text` (全画面 `get_text(history_lines)`) と
  `new_output` (前回実行終了時の `mark()` 以降の差分) の**両方**を渡す。
  使い分けは LLMClient 側:
  - **会話が新規** (session 初回 / compaction 直後): 「(Session Summary +)
    Current Terminal Screen + Goal」— compaction 後も全画面文脈を失わない
  - **継続中**: 「New Terminal Output (差分) + Goal」— 過去の画面は履歴に
    あるため再送しない。差分が空ならセクション省略
- 差分は Executor 側で `new_output_max_chars` (既定 20000 文字) に cap。
  超過時は末尾 (query に近い直近の出力) を残して先頭を切り、
  `[... earlier output truncated ...]` 注記を付ける。
- Goal はメッセージ末尾 (recency)。

## 4. tool 結果の記録 (履歴の整合性)

Chat Completions は「assistant の `tool_calls` には対応する `tool`
メッセージが続く」ことを要求する。`add_tool_output()` が同期のローカル操作に
なったため、Executor は**あらゆる経路で必ず記録**できる:

- 通常実行 → settle 後の出力
- 確認ゲートで拒否 → DECLINED
- 引数不正・未知の tool → Error テキスト
- **settle 待ち中の Ctrl-C** → その時点までの画面出力 + interrupted 注記
  (`_run_tool_call` の except 節で同期記録してから re-raise)
- 実行開始前の中断 → 定型の interrupted 出力 (`execute()` 側で未処理の call を補完)

checkpoint 停止で実行が終わった場合も実行済み分は記録済みなので、
次の query はそのまま続けられる。

## 5. Compaction (LLMClient 内部・Executor から透過)

履歴は毎リクエスト全量送信・全量課金される (キャッシュ割引あり) ため、
無制限には伸ばせない。

- **契機**: query の実行が終わった直後 (explanation の表示とユーザーへの
  操作返却の後)。Executor が `execute()` の finally で同期の
  `finish_query()` を呼び (正常終了・checkpoint 停止・中断のすべてで発火)、
  LLMClient が閾値超過なら compaction を **asyncio バックグラウンドタスク**
  として起動する。タスクが実際に走るのは実行タスク完了後 = passthrough に
  戻った後なので、ユーザーを待たせない。
- **判定**: 直近レスポンスの `usage.prompt_tokens` が `compact_thresh`
  (config、既定 100000) 以上。usage を返さないバックエンド向け
  フォールバックとして、履歴の文字数 / 4 で概算する。
- **手順**: 現履歴 + COMPACT_REQUEST (user メッセージ) で
  `chat.completions.parse(response_format=HandoffSummary, tool_choice="none")`
  を 1 回呼ぶ。system・tools は通常ステップと同一に保ち、履歴 (session 中
  最大のプロンプト) の prefix キャッシュを効かせる。成功したら履歴を
  `[system]` にリセットし、summary を保持する。
- **次の query との待ち合わせ**: `start_query()` は in-flight の compaction
  タスクがあれば完了を待ってから (`[thinking...]` 表示中)、保持された
  summary を「Session Summary + 全画面 + Goal」として初回メッセージに載せる。
  query 実行中の Ctrl-C はこの待ち合わせを中断するが、compaction タスク自体は
  独立に完走し、次の query が結果を拾う。
- **失敗時**: 履歴を維持し (compaction は起きない)、次の query 終了時に再試行
  (API 障害時に文脈を捨てない)。エラーは JSONL ログに記録。バックグラウンド
  タスクは例外を外に漏らさない。

## 6. config (config.toml トップレベル)

| キー | 既定値 | 意味 |
|------|--------|------|
| `compact_thresh` | 100000 | 直近レスポンスの prompt_tokens がこれ以上なら query 終了後に compaction |
| `new_output_max_chars` | 20000 | query 間差分の最大文字数 (末尾優先で切り詰め) |

## 7. 制約 (受容した割り切り)

1. query 間に `new_output_max_chars` を超えて流れた手動出力の超過分は LLM に
   渡らない。truncation 注記で欠落を伝えるにとどめる。
2. 初回 query 前に `history_lines` を超えてスクロールした操作履歴も同様に
   渡らない。初回時点で画面 `history_lines` 行あれば実用上十分と判断。
3. reasoning 系モデルの思考トークンはターン間で引き継がれない
   (Chat Completions の仕様)。
4. compaction で失われるのは LLM 自身の中間的な思考・説明のみで、ターミナル上の
   事実は要約 + 全画面でカバーされる。

## 8. 将来拡張の候補

- トークン/コスト予算上限 (SWE-agent 型) の併設 (DESIGN.md §9)。
- 履歴中の古い tool 結果だけを間引く部分 compaction (現在は全リセットのみ)。
