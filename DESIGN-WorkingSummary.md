# Working Summary (セッションコンテキスト) 設計ドキュメント

- ステータス: v0.1 実装済み
- 親ドキュメント: `DESIGN.md` §5.10
- 本ドキュメントは DESIGN.md §5.10 の概要設計を詳細化したもの。実装はこちらを正とする。

---

## 1. 目的 / 位置づけ

`jou` セッション内でユーザーがこれまで行ってきた操作の要約 (working summary) を保持し、
LLM 呼び出し時 (completion mode / auto mode 共通) にシステムプロンプトへ含めることで、
文脈を踏まえたコマンド提案・自動実行を可能にする。

### 1.1 短期記憶と長期記憶の役割分担

近年のエージェント実装のベストプラクティス (「生の直近 + 要約された過去」のハイブリッド)
に倣い、次の役割分担とする:

| レイヤー | 実体                                                            | 役割                                               |
|----------|-----------------------------------------------------------------|----------------------------------------------------|
| 短期記憶 | `ScreenBuffer.get_text()` (直近 `history_lines` 行の生テキスト) | 現在の画面・直近の操作の正確な把握                 |
| 長期記憶 | working summary                                                 | 生画面ウィンドウからスクロールアウトした情報の保持 |

したがって working summary は「セッション全履歴の要約」ではなく、
**「生画面ウィンドウから消えゆく部分の長期記憶」**として設計する。
生画面と重複する内容を詳述しないことで、トークンの無駄と情報の二重化を避ける。

### 1.2 環境推測の重要性

`jou` の目的上、SSH 先のルーターやスイッチのベンダー CLI を操作する可能性がある。
ネットワーク装置の CLI コマンドはベンダーごとに異なる (Cisco IOS, Junos, Arista EOS, etc.) ため、
「どの装置・どの環境にいるか」という推測を summary の独立フィールドとして保持することで、
後続の LLM 呼び出しにおけるコマンド提案の精度を向上させる (§4.1 参照)。

---

## 2. 調査結果の要点 (設計根拠)

### 2.1 Responses API 側の関連仕様

- `instructions=` は `previous_response_id` チェーンに引き継がれず、毎リクエスト送信する。
  この性質は working summary と好相性で、**summary をシステムプロンプトに入れれば
  auto mode の会話チェーン中でも常に最新版を届けられる**。
- auto mode は function calling (`run_command` / `send_control` tools) ベースだが、
  instructions が毎リクエスト送信される点は変わらない。tool 結果
  (`function_call_output`) には「そのコマンドが生んだ新規出力のみ」が渡される
  (DESIGN.md §5.5) ため、**セッション全体の文脈を供給する役割は instructions 側
  (= working summary) が担う**。
- Responses API にはネイティブ Compaction 機能
  (`context_management: [{type: "compaction", compact_threshold: N}]` によるサーバー側自動圧縮、
  および `/responses/compact` エンドポイントによる明示実行) が存在する。
  ただしこれは **LLM 会話履歴の圧縮**であり、working summary が対象とする
  **ターミナル I/O 履歴の要約**とは別レイヤー。auto mode のチェーンは
  `max_auto_steps` (既定 15) のチェックポイントで区切られるため v0.1 では採用しない (§10 参照)。

### 2.2 エージェント実装のベストプラクティス

- **「生の直近 + 要約された過去」のハイブリッドが定石**: 直近のコンテキストは生で渡し、
  古い部分だけ要約に置き換える (§1.1 の役割分担の根拠)。
- **anchored iterative summarization (前回要約への差分マージ) がフルリビルドより高精度**:
  DESIGN.md §5.10 の差分更新方針は研究的にも裏付けがある。
  「差分更新の繰り返しで要約が薄くなる」懸念への対策は、要約スキーマを構造化し
  **安定的事実 (環境・目的) を明示フィールドで保持 (アンカー) する**こと (§4.1)。
- **過剰圧縮が最大の失敗モード**: 後から重要になる情報 (エラー、環境の手がかり) を
  早期に捨てない。要約プロンプトで「エラーと環境情報は保持」を明示する (§4.3)。

参考資料:

- [Conversation state | OpenAI API](https://developers.openai.com/api/docs/guides/conversation-state)
- [Compaction | OpenAI API](https://developers.openai.com/api/docs/guides/compaction)
- [Effective context engineering for AI agents | Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Efficient context management | JetBrains Research](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)

---

## 3. 設計方針

1. **役割分担**: `get_text()` (直近の生画面) = 短期記憶、working summary = 長期記憶 (§1.1)。
2. **取りこぼし抑止**: pyte のスクロールバック容量 (`scrollback_lines`) を要約トリガー
   (`summary_trigger_lines`) より十分大きく取り、「スクロールバックから消える前に
   必ず一度は要約対象になる」ことを実用上保証する (§7 の不変条件)。
   蓄積量の計測には `ScreenBuffer` の「スクロールバックへ押し出した累計行数」
   単調カウンタ (`_CountingDeque.total_appended`、DESIGN.md §5.5) を利用する。
3. **非ブロッキング**: 行数トリガーの更新はバックグラウンドの asyncio タスクで
   行い、パススルー中継 (relay ポンプ) は一切ブロックしない。
4. **要約が無意味な序盤はスキップ**: 出力が生画面ウィンドウ以内に収まっているうちは
   生画面が全履歴をカバーしており summary は冗長。遅延同期生成は
   「トリガー行数以上溜まっているのに summary がない」場合のみ行う。

---

## 4. LLM インターフェース

### 4.1 要約スキーマ (Structured Outputs)

`llm_client.py` に追加する。アンカー付き構造により、差分更新を繰り返しても
環境情報・重要事実が消えないようにする:

```python
class SummaryResult(BaseModel):
    environment: str   # 接続先環境の推測 (local shell / ssh host / vendor CLI 種別)
    activities: str    # ユーザーがこれまで行ってきた作業の要約
    notes: str         # エラー、重要な事実、未解決の問題
```

### 4.2 生成メソッド

```python
async def summarize(
    self,
    previous: SummaryResult | None,
    new_output: str,
) -> SummaryResult:
```

- `responses.parse()` によるステートレス単発呼び出し (`store=False`、
  `previous_response_id` は使わない)。
- 接続先・モデル・reasoning effort は summary 専用の設定を使用する
  (`SUMMARY_OPENAI_*` 環境変数、未設定時は `OPENAI_*` にフォールバック。§7.1)。
  `LLMClient` はメイン用とは別に summary 用の `openai.OpenAI` クライアントを保持する。
- 入力: 前回 summary (あれば) + 前回生成以降の新規出力 (差分更新、§5.2)。
- JSONL ログには `summary_request` / `summary_response` エントリを追加。

### 4.3 要約生成プロンプト (骨子、英語)

```
You are maintaining a running summary of a terminal session.
Given the previous summary (if any) and new terminal output, produce an
updated summary by MERGING new information into the previous summary.
- environment: infer what system/CLI the user is on (local shell, ssh host,
  network device vendor CLI such as Cisco IOS / Junos / Arista EOS).
  Keep previous inference unless new output contradicts it.
- Preserve: errors encountered, goals, key facts. Never drop information
  from the previous summary unless it is clearly obsolete.
- Do not describe information visible in the most recent output verbatim;
  focus on what will scroll out of view.
```

差分入力が `summary_max_input_lines` を超えて切り詰められた場合は、
プロンプトに「出力は途中省略されている」旨を明示して要約させる (§7)。

### 4.4 メイン呼び出しへの注入

`complete()` / `auto_step()` に `working_summary: str | None = None` を追加し、
非 None なら instructions (`SYSTEM_PROMPT` / `AUTO_SYSTEM_PROMPT`) 末尾に付加する:

```
## Session Context
Environment: {environment}
Activities so far: {activities}
Notes: {notes}
```

- instructions は auto mode の各ステップ (function calling の各ターン) でも
  毎回送信されるため、チェーン途中でも更新された summary を反映できる。
  ただし v0.1 では auto ループ中に summary を更新しない (§6) ので、
  1 回の auto 実行内では同一の summary が使われる。
- auto mode の tool 結果は新規出力のみ (§2.1) であるため、初回メッセージの
  生画面 + instructions 内の working summary がセッション文脈のすべてとなる。

---

## 5. WorkingSummaryManager

新モジュール `src/jou/working_summary.py`。

```python
class WorkingSummaryManager:
    def __init__(
        self,
        llm_client: SummaryGenerator,        # Protocol (テスト用に差し替え可能)
        screen_buffer: ScreenBuffer,
        trigger_lines: int = 500,
        max_input_lines: int = 2000,
    ) -> None: ...

    @property
    def summary_text(self) -> str | None:
        """システムプロンプト用に整形済みの要約。未生成なら None"""

    def check_trigger(self) -> None:
        """IORelay の pty コールバックから毎回呼ばれる。前回生成以降
        trigger_lines 行溜まっていて、更新が in-flight でなければ
        バックグラウンド更新 (asyncio.Task) を開始"""

    async def ensure_summary(self) -> None:
        """ModeExecutor.execute() 冒頭で await される。
        - 更新が in-flight → 完了を待つ (asyncio.wait)
        - summary が無く、かつ蓄積行数 >= trigger_lines → インライン生成
        - それ以外 → 何もしない"""
```

### 5.1 内部状態 (イベントループ単一スレッドのため Lock 不要)

- `_summary: SummaryResult | None` — 最新の要約
- `_last_gen_scrolled: int` — 前回生成時点の `screen_buffer.scrolled_lines_total`
- `_inflight: asyncio.Task[None] | None` — 実行中のバックグラウンド更新

### 5.2 蓄積量の計測と差分更新の入力

蓄積量は「スクロールバックへ押し出された累計行数」で測る。`ScreenBuffer` に
既存の `_CountingDeque.total_appended` を公開するプロパティ
`scrolled_lines_total` と、スクロールバック末尾 N 行を描画する
`get_scrolled_tail(n)` を追加して利用する:

```
pending    = screen_buffer.scrolled_lines_total - _last_gen_scrolled
new_output = screen_buffer.get_scrolled_tail(min(pending, max_input_lines))
```

- `pending > max_input_lines`、またはスクロールバック容量超過で行が失われた場合は、
  切り詰めが発生した旨をプロンプトに明示する (§4.3)。
- 可視画面に留まっている行 (まだスクロールアウトしていない行) は要約対象に
  **含めない**。これは §1.1 の役割分担 (summary = スクロールアウトした情報の長期記憶)
  と一致し、生成直後に陳腐化する情報を summary に混ぜないためでもある。
- **`mark()` / `get_text_since()` は使用しない**: これらは pyte の `screen.dirty`
  (単一コンシューマ前提の変更行 set) をクリアするため、auto mode の tool 結果抽出と
  競合する。working summary 側は dirty set に触れない読み取り専用 API のみ使う (§10-5)。

### 5.3 エラー処理

バックグラウンド生成の例外は握りつぶして JSONL ログに記録する
(セッションを絶対に壊さない)。失敗時は旧 summary を維持し、
`_last_gen_scrolled` は更新しない (次回トリガーで再試行される)。

---

## 6. 並行処理モデル (asyncio 単一スレッド)

すべて asyncio イベントループ上で動く。スレッド・Lock は使わない
(全体設計は DESIGN-Async.md 参照)。

- **fd コールバック**: IORelay の pty コールバックが `check_trigger()` を呼び、
  条件を満たせば `_refresh()` を `asyncio.Task` として起動する。
  HTTP 待ちは await であり、パススルー中継をブロックしない。
- **競合ケース**: 背景更新中にユーザーがクエリを確定 → `ensure_summary()` が
  in-flight タスクの完了を `asyncio.wait` で待つ (summary が古い/無い状態で
  本来の呼び出しを行うと品質が下がるため)。待ち時間は `[thinking...]` 表示で
  カバーされる。
- **Ctrl-C との関係**: `ensure_summary()` はモードタスク内で await されるため、
  ユーザー Ctrl-C (モードタスクのキャンセル) で即座に中断できる。
  行数トリガー起動の背景タスクは await 側のキャンセルに巻き込まれず完走し、
  結果は次回のクエリに活きる。インライン生成は一緒にキャンセルされ、
  次回トリガーで再試行される。
- **`_refresh()` 内のスクリーン読み取りは最初の await より前に完了させる**:
  単一スレッドのため、これで feed との競合は構造上起きない
  (旧スレッド実装に存在した deque 走査と append の潜在競合の解消)。
- **`[thinking...]` 中は pty 読み取りが止まる** (`pause_pty`) ため、
  `ensure_summary()` の実行中に `check_trigger()` が並行して新しい更新を
  始めることはない。
- **auto ループ中のトリガー**: settle 待ち中はポンプが動くため
  `check_trigger()` による背景更新は auto ループと並行して走りうる。
  背景更新は読み取り専用 API (`get_scrolled_tail` 等) しか使わないため
  auto mode の `mark()`/`get_text_since()` と干渉しない (§10-5)。
  auto ループは開始時に取得した summary を実行中ずっと使う
  (1 回の auto 実行内では同一 summary) ため、instructions が揺れることもない。

---

## 7. 設定パラメータ

ターミナル出力は 1 行あたりおおむね 10〜20 トークン。この見積もりに基づき既定値を定める。

設定手段は 2 系統に分ける:

- **動作パラメータ (行数等)**: DESIGN.md §7 の方針に従い CLI オプションで指定。
  将来的には `~/.config/jou/config.toml` に既定値を記述して CLI オプションが上書きする。
- **LLM 接続情報 (URL・APIキー・モデル等)**: 既存の `OPENAI_*` と同様に環境変数で指定 (§7.1)。

| 項目                                               | 既定値             | CLI オプション                | 根拠                                                                                                                                             |
|----------------------------------------------------|--------------------|-------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `scrollback_lines` (新設: pyte HistoryScreen 容量) | 5000               | `--scrollback-lines N`        | メモリコストほぼゼロ (数百KB)。要約差分のバースト耐性に加え、auto mode の tool 結果抽出 (`get_text_since`) が 1 コマンドの大出力で溢れにくくなる |
| `history_lines` (メイン LLM に送る生画面行数)      | 500 (200 から変更) | `--history-lines N`           | ≒5〜10k トークン。環境推測に十分な文脈を与えつつレイテンシ・コスト許容内                                                                         |
| `summary_trigger_lines`                            | 500                | `--summary-trigger-lines N`   | 背景 LLM 呼び出しの頻度を抑える。500 行 ≒ 5〜10k トークン/回。カウントは「スクロールバックへ押し出された行数」ベース (§5.2)                      |
| `summary_max_input_lines`                          | 2000               | `--summary-max-input-lines N` | バーストで pending が溜まりすぎた場合の入力上限                                                                                                  |

### 7.1 Summary 用 LLM 接続設定 (環境変数)

working summary の生成は背景で頻繁に走るため、メインとは別の LLM
(軽量モデルや別エンドポイント) を指定できるようにする。
以下の環境変数で指定し、**`SUMMARY_` 付きの変数が未設定の場合は
対応する `OPENAI_*` の値にフォールバックする**:

| 環境変数                          | フォールバック先           | 対応する `JouConfig` フィールド    |
|-----------------------------------|----------------------------|------------------------------------|
| `SUMMARY_OPENAI_BASE_URL`         | `OPENAI_BASE_URL`          | `summary_openai_base_url`          |
| `SUMMARY_OPENAI_API_KEY`          | `OPENAI_API_KEY`           | `summary_openai_api_key`           |
| `SUMMARY_OPENAI_MODEL`            | `OPENAI_MODEL`             | `summary_model`                    |
| `SUMMARY_OPENAI_REASONING_EFFORT` | `OPENAI_REASONING_EFFORT`  | `summary_openai_reasoning_effort`  |

すべて未設定ならメインの LLM と同一の接続・モデル・effort で要約が生成される
(挙動の予測しやすさ優先の既定)。`LLMClient` は summary 用の `openai.OpenAI`
クライアントをメイン用とは別に生成して保持する (§4.2)。

**`history_lines` の役割分離**: 従来 `history_lines` は「LLM に送る行数」と
「pyte スクロールバック容量」を兼ねていたが、後者を `scrollback_lines` として分離する。
`ScreenBuffer` のコンストラクタは `scrollback_lines` を容量として使い、
LLM 呼び出し側が `get_text(last_n=history_lines)` で送信量を制御する。
DESIGN.md §5.5 / §7 の「既定 200 行」の記述は本設計で更新される。

**計測の限界 (許容する近似)**: 蓄積量カウンタはスクロールを伴う出力のみを数える。
画面クリア (`clear`、alternate screen 相当の全画面再描画、ベンダー CLI のページャ等) で
消えた行はスクロールバックに押し出されず、要約対象から漏れうる。
v0.1 ではこの近似を許容する (直近の生画面が短期記憶としてカバーする範囲で足りる想定)。

**不変条件**: `summary_trigger_lines + バースト余裕 ≤ scrollback_lines`。
5000 vs 500 で十分なマージンを取る。要約 1 回の間に 4500 行以上流れるケースでは
取りこぼしが起きうるが、その場合も `summary_max_input_lines` の切り詰め明示 (§4.3) で
グレースフルに劣化する。

---

## 8. 既存コンポーネントの変更

| ファイル                    | 変更内容                                                                                                                                                                                                                                                                      |
|-----------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `screen_buffer.py`          | `scrolled_lines_total` プロパティ (既存の `_CountingDeque.total_appended` を公開)。`get_scrolled_tail(n)` (スクロールバック末尾 N 行の描画。dirty set には触れない)。`get_text(last_n: int \| None = None)` で末尾 N 行に制限。コンストラクタ引数を `scrollback_lines` に変更 |
| `llm_client.py`             | `SummaryResult` スキーマ、`SUMMARY_PROMPT`、`summarize()`。summary 用 `openai.OpenAI` クライアントを別途保持 (§7.1)。`complete()` / `auto_step()` に `working_summary` 引数を追加し instructions 末尾に付加 (§4.4)                                                            |
| `working_summary.py` (新規) | `WorkingSummaryManager` (§5)                                                                                                                                                                                                                                                  |
| `io_relay.py`               | コンストラクタに `working_summary: WorkingSummaryManager \| None = None` を追加。pty 出力を `feed()` した直後に `check_trigger()` を呼ぶ                                                                                                                                      |
| `mode_executor.py`          | `execute()` 冒頭で `ensure_summary()` を呼び (`[thinking...]` 表示中)、`summary_text` を `complete()` / `auto_step()` に渡す                                                                                                                                                  |
| `config.py`                 | §7 の動作パラメータを `JouConfig` に追加 (CLI から設定、環境変数からは読まない)。§7.1 の summary 用 LLM 接続フィールドを追加し、`from_env()` で `SUMMARY_OPENAI_*` → `OPENAI_*` のフォールバック解決を行う                                                                    |
| `cli.py`                    | §7 の CLI オプションを argparse に追加し config へ反映。`WorkingSummaryManager` の生成と結線                                                                                                                                                                                  |

---

## 9. テスト計画

- `tests/test_working_summary.py` (新規)。フェイク LLM (Protocol 実装) を使用:
  1. トリガー行数未満では生成しない
  2. トリガー超過で背景生成が走る
  3. 差分更新で previous summary が LLM に渡る
  4. in-flight 中の `ensure_summary()` が完了を待つ
  5. 生成失敗時に旧 summary を維持し、次回トリガーで再試行される
- `test_screen_buffer.py`: `scrolled_lines_total` / `get_scrolled_tail(n)` / `get_text(last_n)` を追加。`mark()`/`get_text_since()` (auto mode 用) と並行使用しても互いに影響しないことの検証
- `test_llm_client.py`: `summarize()`、`working_summary` 付き instructions の検証
- `test_config.py`: `SUMMARY_OPENAI_*` → `OPENAI_*` フォールバックの検証 (設定時 / 未設定時)
- `test_mode_executor.py`: `ensure_summary()` が呼ばれ summary が LLM に渡ることの検証

---

## 10. 設計判断の記録

1. **要約の対象範囲**: 当初の概要設計 (旧 DESIGN.md §5.10) は「セッション開始からの
   全入出力」を要約するとも読めたが、生画面と重複する部分を要約に含めると
   トークンの無駄 + 情報の二重化になるため、
   「生画面ウィンドウから消えゆく部分の長期記憶」と位置づけた (§1.1)。
2. **セッション序盤のスキップ**: 当初設計の「summary が無ければ同期生成」を無条件に行うと、
   初回クエリのレイテンシが常に LLM 2 回分になる。蓄積が `summary_trigger_lines` 未満なら
   生成しない (生画面が全履歴をカバーしているため品質低下なし)。
3. **全体再生成は実装しない**: 当初設計の検討事項にあった「定期的な全体再生成」は、
   生データが破棄済みのため原理的に不可能。代わりにスキーマのアンカー構造 (§4.1) と
   マージ指示プロンプト (§4.3) で劣化を抑える。
4. **Responses API ネイティブ Compaction は v0.1 では使わない**: 対象レイヤーが異なる (§2.1)。
   将来 `max_auto_steps` を大きく増やす場合、auto mode チェーンの圧縮手段として検討する。
5. **`mark()` / `get_text_since()` を流用しない**: pyte の `screen.dirty` は
   「クリアはコンシューマの責務」という単一コンシューマ前提の仕組みであり、
   auto mode の tool 結果抽出が既に使用している。working summary が `mark()` を呼ぶと
   auto mode のマークを壊すため、蓄積量カウンタ (`scrolled_lines_total`) と
   読み取り専用のスクロールバック参照 (`get_scrolled_tail`) のみを使う (§5.2)。
   カウンタが「可視画面に留まる行を数えない」点も、summary の対象範囲 (§1.1) と整合する。
