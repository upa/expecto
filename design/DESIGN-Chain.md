# セッションチェーンと Compaction 設計ドキュメント

- ステータス: 実装済み
- 親ドキュメント: `DESIGN.md` (§5.7, §5.10)
- 本ドキュメントは「session 全体で 1 つの `previous_response_id` チェーンを継続し、
  長くなったら compaction で新チェーンに移行する」設計の詳細。
  旧 working summary 機構 (`DESIGN-WorkingSummary.md`) はこの設計で廃止・削除された (§7)。

---

## 1. 概要 / 記憶の役割分担

`previous_response_id` のチェーン (サーバー側に保存された LLM 会話履歴) を
**query 単位ではなく session 全体で継続**する。過去の query・実行コマンド・
その出力はすべてチェーンに残るため、LLM は session の文脈を踏まえて動ける。

| 記憶 | 実体 | カバー範囲 |
|------|------|-----------|
| 会話記憶 | `previous_response_id` チェーン | LLM が見た/やったことすべて (過去の query、tool call、tool 出力) |
| 画面記憶 | query ごとに送る新規出力の差分 (§3) | query と query の間にユーザーが手動で行った操作 |
| 圧縮記憶 | compaction 時の引き継ぎ要約 (§5) | チェーンを捨てて新チェーンに移行するときの文脈 |

前提条件: **instructions は完全静的** (`SYSTEM_PROMPT` 定数のみ)。プロンプト
キャッシュは厳密な prefix 一致であり、instructions はレンダリングされた
プロンプトの先頭に置かれるため、これが変わるとチェーン全体がキャッシュミス
になる。session の全リクエスト (全 query × 全ステップ) で同一 prefix を保つ。

## 2. チェーン状態 (Executor のインスタンス状態)

```python
_chain_id: str | None        # 現チェーン末尾の response id (None = 新規チェーン)
_chain_input_tokens: int     # 直近レスポンスの usage.input_tokens (compaction 判定)
_pending_items: list         # 次リクエストで届ける必要がある未送信アイテム (§4)
_last_mark: ScreenMark|None  # 前回実行終了時の画面マーク (query 間差分の起点)
```

`execute()` は `prev_response_id = _chain_id` から始め、各ステップの成功で
`_chain_id` / `_chain_input_tokens` を更新する。実行終了時 (finally) に
`_last_mark = screen.mark()` を取る。

## 3. query ごとのメッセージ構成

**新規チェーン (session 初回 / compaction 直後 / チェーン喪失後)**:

```
## Session Summary (compacted from the earlier conversation)   ← compaction 直後のみ
## Current Terminal Screen     ← 全画面 get_text(history_lines)
## Goal
{query}                        ← Goal は常に末尾 (recency)
```

**継続チェーン (2 回目以降の query)**:

```
## New Terminal Output (since the previous task)   ← _last_mark 以降の差分。空なら省略
## Goal
{query}
```

- 継続チェーンでは全画面を再送しない (過去の画面内容はチェーンに既にある)。
  差分は `get_text_since(_last_mark)` で取り、query 間のユーザーの手動操作を LLM に伝える。
- 差分は **文字数で cap** する: `new_output_max_chars` (既定 20000)。超過時は
  末尾 (query に近い直近の出力) を残して先頭を切り、
  `[... earlier output truncated ...]` 注記を先頭に付ける。
- `mark()` が消費する pyte の dirty set は単一コンシューマ前提だが、
  working summary 廃止により Executor が唯一のコンシューマとなり制約を満たす。

## 4. 未解決 tool call の処理 (pending items)

Responses API は、直前の応答が `function_call` のまま次のリクエストで
`function_call_output` を返さないとエラーになる。チェーンが中断をまたぐため、
以下で「未回答の call が残らない」ことを保証する:

- tool 出力は `_pending_items` に直接積みながら送信する (ステップ成功で clear)。
  **checkpoint 停止** (`continue?` に n) では組み立て済み出力がそのまま残り、
  次の query の input 先頭で届く。
- **実行中の Ctrl-C**: settle 待ち中に中断された場合、`_run_tool_call` が
  その時点までの画面出力 + `[Note] The task was interrupted by the user; the
  command may still be running.` を当該 call の output として `_pending_items`
  に積んでから CancelledError を再送出する (出力の取りこぼし防止)。
  実行開始前に中断された call には定型の interrupted 出力を補完する。
- compaction リクエスト自体も旧チェーン末尾に繋がるため、`_pending_items` が
  あれば compact の input に含めて清算してから新チェーンへ移る。

## 5. Compaction (チェーンの圧縮・更新)

チェーンは過去入力を毎ステップ再課金する (キャッシュ割引はあるが session の
長さに比例して膨らむ) ため、無制限には伸ばせない。

- **判定**: query 開始時に `_chain_input_tokens >= chain_compact_thresh`
  (config、既定 100000) なら compaction。ループ途中ではリセットしない
  (tool call の文脈を壊さないため。1 実行内の暴走は checkpoint が抑える)。
- **手順**: `[compacting...]` オーバーレイ中に、現チェーンに対して 1 回だけ
  引き継ぎ要約を要求する (`LLMClient.compact()`)。応答は Structured Outputs
  (`HandoffSummary`: environment / activities / notes)。得られた要約を
  §3 の新規チェーン形式 (Summary + 全画面 + Goal) の先頭セクションにして
  `previous_response_id=None` から再開する。
- **compact リクエストはキャッシュに揃える**: instructions・tools を step()
  と同一にし (compaction 指示は user メッセージで渡す)、`tool_choice="none"`
  で tool 呼び出しだけ禁止する。compact はチェーン全体 (session 中最大の
  プロンプト) を読むため、prefix キャッシュが効くかどうかの影響が最も大きい。
  `store=False` (この応答には誰もチェーンしない)。
- **失敗時**: 旧チェーンを保持したまま今回の query を続行し、次の query で
  再試行する (API 障害時に文脈を捨てない)。エラーは JSONL ログに記録。
- **チェーン喪失**: step が「previous response が見つからない」系の
  `BadRequestError` を返した場合 (`ChainLostError` に変換)、チェーンを捨てて
  新規チェーン形式 (要約なし、全画面のみ — 旧チェーンはもう要約できない) で
  その query を 1 回だけリトライする。

compaction で失われるのは LLM 自身の中間的な思考・説明のみで、ターミナル上の
事実は要約 + 全画面でカバーされる。

## 6. config (config.toml トップレベル)

| キー | 既定値 | 意味 |
|------|--------|------|
| `chain_compact_thresh` | 100000 | 直近レスポンスの input_tokens がこれ以上なら次 query で compaction |
| `new_output_max_chars` | 20000 | query 間差分の最大文字数 (末尾優先で切り詰め) |

## 7. 旧 working summary の廃止 (設計判断の記録)

旧設計 (`DESIGN-WorkingSummary.md`、削除済み) は「スクロールアウトした出力を
バックグラウンドで常時要約し、毎 query の instructions に注入する」機構だった。
チェーンが query 単位でリセットされる前提では session 文脈の供給源として
必須だったが、session チェーン継続によって役割がほぼ消滅した:

- session 文脈の供給 → チェーンそのものが担う
- 環境推測 (SSH 先・ベンダー CLI) の保持 → チェーン内の過去のやりとりが担う
- query 間の手動操作の記憶 → §3 の差分が担う
- チェーンリセット時の引き継ぎ → オンデマンドの compaction (§5) が担う。
  チェーン自己要約は端末出力だけの要約より忠実 (LLM 自身の目的・判断も含む)
  で、生成コストも「500 行スクロールごとに常時」から「compaction 時に 1 回」
  に下がる。

割り切り (受容したトレードオフ):

1. query 間に `new_output_max_chars` を超えて流れた手動出力の超過分は失われる
   (旧 summary は「build failed with X」のような形で残せた)。truncation 注記で
   LLM に欠落を伝えるにとどめる。
2. 初回 query 前に `history_lines` を超えてスクロールした操作履歴も同様に
   失われる。初回時点で画面 500 行あれば実用上十分と判断。

また instructions への summary 注入 (旧方式) は、summary が更新されるたびに
チェーン全体のプロンプトキャッシュを破壊するため、チェーン継続設計とは
両立しない。これが本設計移行の直接の動機である。

## 8. 将来拡張の候補

- OpenAI ネイティブの compaction (`context_management` / `/responses/compact`)
  への委譲。クライアント実装が薄くなるが、OpenAI 互換の他バックエンド
  (`base_url` 変更) では使えないため、汎用のクライアント側 compaction を先に採用した。
- トークン/コスト予算上限 (SWE-agent 型) の併設 (DESIGN.md §9)。
