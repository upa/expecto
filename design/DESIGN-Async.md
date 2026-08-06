# 非同期化 (asyncio) 設計ドキュメント

- ステータス: 実装済み (§11 の全ステップ完了)
- 親ドキュメント: `DESIGN.md`
- 本ドキュメントは expecto の並行処理基盤を select + thread から asyncio 単一スレッドへ
  移行するための詳細設計。実装はこちらを正とする。
- 実装時の設計との差分: 旧「非対話環境 (stdin なし) では destructive を自動拒否」は、
  常時ポンプ設計では stdin 喪失 (EOF) がセッション終了 = モードタスクのキャンセル
  になるため、「確認待ちのまま stdin が消えたら interrupted」に置き換えた (§5 と
  整合)。
- **本ドキュメント実装後の変更**: completion mode は削除され、実行ループ (旧 auto
  mode) が expecto の唯一の振る舞いになった (DESIGN.md §12)。本文中の completion /
  auto mode への言及と API 名は移行当時の記録である。現在の名前:
  `ModeExecutor` → `Executor` (`mode_executor.py` → `executor.py`)、
  `start_mode()` → `start_execution()` (「モードタスク」→「実行タスク」)、
  `auto_step()` → `step()`、`build_auto_messages()` → `build_initial_messages()`、
  `AutoResponse` → `FinalResponse`、`AutoStepResult` → `StepResult`、
  `AUTO_TOOLS` → `TOOLS`、`max_auto_steps` → `max_steps`、
  `LLMCompleter` → `LLMStepper`。ホットキーは `Ctrl-/` の 1 つに統合され、
  InputRouter の状態は `PASSTHROUGH / QUERY / EXECUTING` の 3 つ。
  エラー表示は `[expecto: error: ...]`、終了表示は `[expecto done: ...]` 等。

---

## 1. 目的 / 背景

現行実装は「1 つの select ループ」ではなく、実質 4 つの select ループの入れ子である:

1. `IORelay.run()` — 通常時の中継ループ
2. `ModeExecutor._wait_for_settle()` — settle 待ち中の pty 中継 + Ctrl-C 監視の再実装
3. `ModeExecutor._prompt_yes_no()` — y/n 待ち中の pty 中継の再実装
4. `ModeExecutor._wait_for_llm()` — LLM 待ち中の stdin 0.1 秒ポーリング

これに加えて以下の構造的な問題がある:

- **スレッド機構**: `BackgroundCall` (daemon スレッド + Event) と
  `WorkingSummaryManager` の Lock、および「`check_trigger` と `ensure_summary` は
  relay が止まっているので並行実行されない」という暗黙の実行順序への依存。
- **潜在的データ競合**: 背景スレッドの `_refresh()` が `get_scrolled_tail()` で
  スクロールバック deque を走査する間も、メインスレッドは `screen_buffer.feed()` で
  同じ deque に append し続ける (GIL 頼みで無保証)。
- **中断フラグの引き回し**: `interrupted: bool` が
  `_wait_for_settle` → `_run_tool_call` → auto ループの 3 階層をタプルで伝播する。
- **不完全なキャンセル**: 放棄された LLM スレッドは HTTP を完走して結果ごと捨てる。
- **非対称性**: auto mode の LLM 待ちは Ctrl-C で中断できるが、
  completion mode (`_execute_completion`) は同期ブロッキングで中断不能。

asyncio 単一スレッドへの移行により、これらを構造上発生しない形にする。

---

## 2. 設計方針

1. **全体を asyncio 単一スレッドにする**。「LLM 呼び出しだけ async」の折衷はしない
   (2 つのパラダイムの橋渡しが増えるだけで悪化する)。スレッド・Lock は全廃する。
2. **pty → screen → stdout のポンプは常時 1 本**。settle 待ち・y/n 待ち・LLM 待ちは
   「イベントを await する」だけにし、中継処理の重複実装をなくす。
3. **ユーザーの中断 = Task キャンセル**。`interrupted` フラグの引き回しを
   `asyncio.CancelledError` の伝播に置き換え、後始末は `finally` に集約する。
4. **LLM 呼び出しは `openai.AsyncOpenAI`**。キャンセル時は HTTP リクエスト自体が
   中断される (現行の「スレッド放棄」より意味的に正しい)。
5. **判定ロジックは無変更**。`screen_buffer` / `query_ui` / `dangerous` / `config` /
   InputRouter のバイト状態機械 / LLM スキーマ・プロンプトには手を入れない。
   変更は I/O オーケストレーション層に閉じる。
6. 対象 OS は macOS / Linux のみ (DESIGN.md §1.1) なので、tty / pty fd への
   `loop.add_reader()` を前提としてよい。

---

## 3. タスク構成

```
asyncio イベントループ (単一スレッド)
│
├─ メインタスク: async_main()
│    起動処理 → relay.run() を await → 終了処理 (waitpid, termios 復元は同期)
│
├─ fd コールバック (loop.add_reader, 同期処理のみ)
│    stdin_fd → IORelay._on_stdin() → InputRouter.handle()
│    pty_fd   → IORelay._on_pty()   → feed / 表示 / output 通知 / check_trigger
│
├─ モードタスク (0 or 1 個。クエリ確定時に create_task)
│    ModeExecutor.execute(query, mode)
│    - completion: ensure_summary → LLM → 注入
│    - auto: tool call ループ (LLM await / settle await / 確認 await)
│    - ユーザー Ctrl-C = このタスクの cancel()
│
└─ summary タスク (0 or 1 個。check_trigger が起動)
     WorkingSummaryManager._refresh()
```

- モードタスクと summary タスクはループ上で自然にインターリーブする。
  HTTP 待ち中もポンプ (fd コールバック) は動き続ける。
- `asyncio.TaskGroup` は採用しない (§12-3)。

---

## 4. コンポーネント設計

### 4.1 IORelay (中継ポンプ + イベントハブ)

モジュール名・クラス名は維持し、内部を全面書き換えする。

```python
class IORelay:
    async def run(self, pty_master_fd: int, stdin_fd: int, stdout_fd: int) -> None:
        """add_reader を登録し、終了 (_closed) まで待つ。
        finally で reader 解除・モード/summary タスクの cancel。"""

    # --- pty 出力の制御 (ModeExecutor が使う) ---
    def pause_pty(self) -> None: ...    # pty の add_reader を解除 (読み取り停止)
    def resume_pty(self) -> None: ...   # 再登録
    async def wait_settle(self) -> SettleResult: ...   # §4.3 の関数に委譲
    def arm_sgr_reset_at_newline(self) -> None: ...    # §6.2 グレーエコー
    def flush_sgr_reset(self) -> None: ...             # 未リセットなら即リセット

    # --- 実行中モードへの入力 (ModeExecutor が使う) ---
    async def wait_key(self, accept: frozenset[int]) -> int: ...  # §4.2

    # --- モードタスクの起動 (cli の on_query_submit が使う) ---
    def start_mode(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]: ...
```

**pty コールバック** (`_on_pty`、同期):

```
data = os.read(pty_fd, 4096)
data が空 / OSError(EIO) → _closed をセットして終了シーケンスへ
screen_buffer.feed(data)
stdout へ書き込み (SGR リセット armed なら最初の改行で \x1b[0m を挿入)
_output_seen (asyncio.Event) をセット          # settle 検知用
working_summary.check_trigger()
raw I/O ログ
```

**stdin コールバック** (`_on_stdin`、同期): 読んだバイト列を
`InputRouter.handle()` に渡すのみ。ルーティング先は InputRouter の状態が決める。

**モードタスクの起動**: `on_query_submit` コールバック (cli) が
`relay.start_mode(executor.execute(query, mode))` を呼ぶ。done callback で
(a) SGR リセットの flush と pending なキー要求の破棄、(b) InputRouter を
PASSTHROUGH に戻す、(c) `CancelledError` 以外の例外を outer tty にグレーで
表示する (`[expecto: mode error: ...]`)。

**`pause_pty()` の意味論**: 読み取り自体を止める (`remove_reader`)。
pty のカーネルバッファが溢れれば子プロセスの write がブロックする — これは
現行実装で LLM 待ち中に pty を読まないことと**同一の背圧挙動**である。
メモリ上の保留バッファ方式は採らない (§12-2)。
`wait_settle()` は読み取りが有効であることを前提とする (pause 中の呼び出しは
プログラミングエラーとして assert する)。

### 4.2 InputRouter — EXECUTING 状態の追加

既存の状態機械 (PASSTHROUGH / QUERY_COMPLETION / QUERY_AUTO) に
`State.EXECUTING` を追加する。

- **遷移**: Enter でクエリ確定 → `on_query_submit` 呼び出し後、PASSTHROUGH ではなく
  EXECUTING に遷移する。モードタスク完了 (done callback) で PASSTHROUGH に戻す
  (`set_passthrough()` を追加)。
- **EXECUTING 中のバイト処理**: 新コールバック `on_executing_input(byte)` に転送する。
  実装は IORelay 側:

```
キー要求 (wait_key) が pending → accept に含まれるバイトなら Future を解決、
                                  含まれなければ破棄
キー要求なし & byte == 0x03    → モードタスクを cancel()
それ以外                       → 破棄
```

- `wait_key(accept)`: y/n プロンプト用。呼び出し側は
  `frozenset({y, Y, n, N, Esc, Ctrl-C})` のように **Ctrl-C を含めて**渡す —
  プロンプト表示中の Ctrl-C は現行どおり「n (拒否/停止)」であり、
  auto mode 全体の中断ではない (§5 の表を参照)。
- UTF-8 バッファリング等、既存のクエリ入力処理は無変更。

### 4.3 settle — SettleDetector をコルーチンに置換

`SettleDetector` クラス (時刻管理 + `remaining_ms()` の手計算) を削除し、
`settle.py` を以下の関数 + 既存 enum に置き換える:

```python
class SettleResult(enum.Enum):   # 既存のまま
    SETTLED = "settled"
    THRESHOLD = "threshold"

async def wait_settle(
    output_seen: asyncio.Event,   # 出力到着ごとにセットされる (待つ側が clear)
    settle_ms: int,
    threshold_ms: int,
) -> SettleResult:
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        output_seen.clear()
        try:
            await asyncio.wait_for(output_seen.wait(), settle_ms / 1000)
        except TimeoutError:
            return SettleResult.SETTLED
        if loop.time() - start >= threshold_ms / 1000:
            return SettleResult.THRESHOLD
```

- 意味論は現行と同じ: settle_ms の無出力で SETTLED、出力が続いたまま
  threshold_ms 経過で THRESHOLD。
- Ctrl-C 中断はこの関数の関心事ではなくなる (モードタスクのキャンセルとして
  外から伝播する)。`IORelay.wait_settle()` は自身の `_output_seen` を渡して
  この関数を呼ぶ薄いラッパとする。

### 4.4 ModeExecutor — async 化と大幅な縮小

`execute()` / `_execute_completion()` / `_execute_auto()` / `_run_tool_call()` /
`_prompt_yes_no()` / `_confirm_*()` を `async def` にする。
**削除されるもの**: `_wait_for_llm()` (キャンセルで代替)、`_wait_for_settle()`
(relay の `wait_settle()` で代替)、`_prompt_yes_no()` 内の pty 中継ループ
(ポンプが止まらないので不要)、`BackgroundCall` の利用すべて、
`(output, interrupted)` タプルの引き回し。

**completion mode**:

```python
async def _execute_completion(self, query: str) -> None:
    response = await self._thinking(self._completion_request(query))
    self._write_to_pty(response.command.encode())

async def _completion_request(self, query: str) -> CompletionResponse:
    working_summary = await self._ensure_working_summary()
    screen_text = self._screen.get_text(last_n=self._history_lines)
    return await self._llm.complete(screen_text, query, working_summary=working_summary)
```

キャンセル時 (thinking 中の Ctrl-C) は finally で表示と pty 読み取りが復元され、
注入は行われない。**completion mode も中断可能になる** (現行との差分、§7)。

**auto mode** (骨子):

```python
async def _execute_auto(self, query: str) -> None:
    try:
        self._write_to_pty(b"\n")
        await self._relay.wait_settle()
        working_summary = await self._thinking(self._ensure_working_summary())
        input_items = self._llm.build_auto_messages(query, screen_text)
        prev_id = None
        step, checkpoint = 0, self._max_auto_steps
        while True:
            step += 1
            result = await self._thinking(
                self._llm.auto_step(input_items, prev_id, working_summary))
            prev_id = result.response_id
            if not result.tool_calls:
                self._show_auto_end(...)   # done / failed
                return
            input_items = [await self._run_tool_call(c) for c in result.tool_calls]
            if step == checkpoint:
                if not await self._confirm_continue(step):
                    self._show_auto_end(f"stopped at step {step}")
                    return
                checkpoint += self._max_auto_steps
    except asyncio.CancelledError:
        self._show_auto_end("interrupted")
        raise
    finally:
        self._relay.flush_sgr_reset()
        self._write_to_pty(b"\n")
```

- `_thinking(awaitable)`: `[thinking...]` 表示 + `pause_pty()` を try/finally で
  かけて await する共通ヘルパ (completion / auto / summary 待ちで共用)。
- `_run_tool_call()`: 確認ゲート (3 層)・`mark()` / `get_text_since()`・
  settle 注記の組み立ては**無変更**。グレーエコーは
  `relay.arm_sgr_reset_at_newline()` を呼んでからコマンドを注入する形になる。
- y/n プロンプト:

```python
async def _prompt_yes_no(self, message: str) -> bool:
    os.write(stdout, ...)
    key = await self._relay.wait_key(frozenset({0x79, 0x59, 0x6E, 0x4E, 0x1B, 0x03}))
    ...
```

  待機中の pty 中継はポンプが担うため、現行の 40 行の select ループは消える。

### 4.5 LLMClient — AsyncOpenAI 化

- `openai.OpenAI` → `openai.AsyncOpenAI` (メイン・summary 用の 2 クライアントとも)。
- `complete()` / `auto_step()` / `summarize()` を `async def` にし、
  `await client.responses.parse(...)` に変更。
- スキーマ、tool 定義、プロンプト、パース処理、JSONL ログは**無変更**。
  ログの同期 write は十分小さくループ上で許容する (§12-6)。
- `LLMCompleter` / `SummaryGenerator` Protocol のメソッドも async に更新。

### 4.6 WorkingSummaryManager — Lock 廃止、Task 化

```python
class WorkingSummaryManager:
    _inflight: asyncio.Task[None] | None      # BackgroundCall → Task
    # threading.Lock は削除 (単一スレッドなので不要)

    def check_trigger(self) -> None:
        """pty コールバックから呼ばれる (同期)。閾値超過かつ in-flight なしなら
        asyncio.create_task(self._refresh()) を起動"""

    async def ensure_summary(self) -> None:
        """in-flight があれば await、なければ必要時に同 (イン) ライン生成"""

    async def _refresh(self) -> None:
        """スクリーン状態の読み取り (同期・await 前に完了) → await summarize()
        → 結果反映。例外は握りつぶして JSONL ログへ (現行どおり)"""
```

- `_refresh()` は最初の await より前に `scrolled_lines_total` /
  `get_scrolled_tail()` の読み取りを終える。単一スレッドなので
  **feed との競合は構造上起きない** (現行の潜在競合の解消)。
- `ensure_summary()` を await 中にモードタスクがキャンセルされた場合:
  `check_trigger` 起動の背景 Task は await している側のキャンセルでは
  巻き込まれない (Task は独立に完走する)。インライン生成 (自コルーチン内での
  `_refresh()` 直接 await) は一緒にキャンセルされる — 現行の「放棄」と同等で、
  次回トリガーで再試行される。
- docstring の「実行順序により並行しない」という説明は不要になる。

### 4.7 cli / 起動・終了シーケンス

```python
def main() -> None:                # 同期のまま
    ネスト検査 / load_dotenv / parse_args / config
    tty_ctrl.enter_raw()
    try:
        exit_code = asyncio.run(async_main(...))
    finally:
        tty_ctrl.restore()
    sys.exit(exit_code)

async def async_main(...) -> int:
    各コンポーネント生成・結線 (現行 main と同じ構造)
    pty_mgr.spawn(command); ウィンドウサイズ設定
    loop.add_signal_handler(signal.SIGWINCH, on_winch)
    await relay.run(...)
    return pty_mgr.wait()          # EOF 検知後の waitpid は同期で可
```

- **SIGWINCH**: `signal.signal` + ハンドラ内 ioctl から
  `loop.add_signal_handler` に変更 (イベントループ上で実行され安全)。
  併せて `screen_buffer.resize()` も呼ぶ (現行はリサイズ時に pty へ伝播するのみで
  pyte 側が追従していなかった。§7-5)。
- **終了**: pty EOF → `_closed` セット → `relay.run()` の finally で
  reader 解除・モード/summary タスク cancel → waitpid → termios 復元。
  `TTYController` の atexit / raw 化ロジックは無変更。
- `background.py` と `tests/test_background.py` は削除する。

---

## 5. キャンセル・割り込みセマンティクス

| 状況 | ユーザー操作 | 動作 |
|------|-------------|------|
| passthrough | Ctrl-C | 現行どおり pty へ素通し (子プロセスへの SIGINT) |
| query 入力中 | Esc / Ctrl-C | 現行どおりキャンセル → PASSTHROUGH |
| モード実行中 (LLM 待ち / settle 待ち) | Ctrl-C | **モードタスクを cancel()**。CancelledError が伝播し、finally で表示復元・SGR リセット。auto は `[auto interrupted]` 表示 |
| y/n プロンプト表示中 | Ctrl-C / Esc / n | `wait_key` が解決 → **「n」として扱う** (タスクは生存)。継続確認なら停止、コマンド確認なら拒否を tool result で LLM へ返す (現行どおり) |
| モード実行中 | その他のキー | 破棄 (現行の auto mode と同じ。completion にも適用 — §7-2) |
| 子プロセス終了 | — | pty EOF → モード/summary タスクを cancel → 終了シーケンス |

- `CancelledError` はモードタスク内で捕捉して後始末したうえで **re-raise** する
  (asyncio の規約に従う)。done callback は `CancelledError` を正常終了として扱う。

---

## 6. 表示制御

### 6.1 `[thinking...]` オーバーレイ中の pty 出力

現行は「LLM 待ち中は pty を読まない」ことで、オーバーレイ (`\x1b7` ... `\x1b8\x1b[J`)
と子プロセス出力の混線を防いでいる (カーネルバッファに滞留し、後でまとめて流れる)。
async 版でも `pause_pty()` (読み取り停止) で**同じ意味論を維持**する。
常時ポンプに書き込みゲートを付ける案は採らない (§12-2)。

### 6.2 auto mode コマンドエコーのグレー表示

現行の「注入直前に SGR (グレー) を書き、エコーの最初の改行でリセット」は、
stdout への書き込みがポンプに一元化されるため、relay のフラグとして実装する:

- `arm_sgr_reset_at_newline()`: ModeExecutor がグレー SGR を書き、コマンドを
  注入する直前に呼ぶ。
- `_on_pty` は armed 中、data 内の最初の `\n` の直後に `\x1b[0m` を挿入して書き、
  フラグを解除する。
- `flush_sgr_reset()`: 改行が来ないまま settle・中断・EOF となった場合の保証。
  モードタスクの finally と relay の終了処理から呼ぶ (冪等)。

---

## 7. 現行実装からの挙動変更点 (意図的なもの)

1. **completion mode の thinking 中に Ctrl-C で中断可能になる** (改善。
   現行は同期ブロッキングで中断不能)。
2. **モード実行中に打鍵された Ctrl-C 以外のキーは破棄される**。auto mode は現行
   どおり。completion mode は現行「relay 停止中にカーネルに滞留し、完了後 pty へ
   流れていた」が、破棄に変わる (thinking 中の打鍵が注入コマンドと混ざる事故を
   防ぐ方向の変更)。
3. **ユーザー中断時に LLM への HTTP リクエストが実際にキャンセルされる**
   (現行はスレッド放棄で完走)。
4. **checkpoint / 確認プロンプト待機中も表示が生き続ける**のは同じだが、
   実装が「専用中継ループ」から「常時ポンプ」に変わる。
5. **SIGWINCH 時に `screen_buffer.resize()` も行う** (現行の欠落の修正)。

これ以外のユーザー可視挙動 (ホットキー、query UI、settle 意味論、危険コマンド
ゲート、checkpoint、グレーエコー、summary の更新タイミング) は維持する。

---

## 8. 依存関係・設定の変更

- 追加 (dev): `pytest-asyncio` (asyncio モードのテスト用)
- 削除: なし (openai SDK は同一パッケージで async 対応。runtime 依存は不変)
- config: 変更なし (settle_ms / threshold_ms 等の意味も不変)

---

## 9. 既存コンポーネントの変更一覧

| ファイル | 変更 |
|----------|------|
| `io_relay.py` | 全面書き換え: add_reader ポンプ、pause/resume、wait_settle、wait_key、SGR arm、モードタスク管理、終了処理 |
| `input_router.py` | `State.EXECUTING` と `on_executing_input` / `set_passthrough()` の追加。既存のバイト処理は不変 |
| `settle.py` | `SettleDetector` を削除し `wait_settle()` コルーチンに置換。`SettleResult` は維持 |
| `mode_executor.py` | async 化。`_wait_for_llm` / `_wait_for_settle` / `_prompt_yes_no` の中継ループを削除し relay の API に委譲。ゲート・mark/get_text_since・出力組み立ては不変 |
| `llm_client.py` | `AsyncOpenAI` 化、3 メソッドを async に。スキーマ・プロンプト・ログは不変 |
| `working_summary.py` | Lock 削除、`BackgroundCall` → `asyncio.Task`、`ensure_summary` / `_refresh` を async に |
| `cli.py` | `asyncio.run` 化、SIGWINCH を `add_signal_handler` に移行、終了シーケンス整理 |
| `background.py` | **削除** |
| `tty_controller.py` | `setup_sigwinch` / `cleanup_sigwinch` を削除 (登録は cli の `add_signal_handler` へ移行)。raw 化・復元・ウィンドウサイズ取得/反映は不変 |
| `screen_buffer.py` / `query_ui.py` / `dangerous.py` / `config.py` | 変更なし |

---

## 10. テスト計画

- `pytest-asyncio` を導入 (`asyncio_mode = "auto"` を pyproject に設定)。
- `test_background.py`: 削除。
- `test_settle.py`: `wait_settle()` のテストに書き換え。`asyncio.Event` を直接
  セットして SETTLED / THRESHOLD / 意味論 (出力継続中は settle しない) を検証。
  実時間 sleep は使わず、短い settle_ms で決定的に。
- `test_io_relay.py`: `pty.openpty()` + イベントループで、中継・EXECUTING
  ルーティング・wait_key・pause/resume・SGR arm・EOF 終了を検証。
- `test_mode_executor.py`: async フェイク LLM (`asyncio.Event` で応答タイミングを
  制御) に置き換え。キャンセル系 (thinking 中 / settle 中の Ctrl-C、finally の
  表示復元) が現行のスレッド + timeout ベースより決定的に書けるようになる。
  ゲート・checkpoint・出力組み立てのテスト内容 (アサーション) は維持する。
- `test_working_summary.py`: フェイクを async 化。トリガー/in-flight/失敗時
  リトライのシナリオは維持。
- `test_llm_client.py`: モックを AsyncOpenAI に合わせて更新。検証内容は維持。

---

## 11. 実装ステップ

CLAUDE.md の原則に従い、各ステップでレビューを挟む。相互依存が強いため、
ステップ間でブランチが過渡状態になる点は許容する (下記に明記):

- **Step 1: 基盤 + completion 経路 + summary**
  `cli` / `io_relay` / `input_router` / `settle` / `llm_client` /
  `working_summary` / `mode_executor` (completion のみ) を async 化。
  `background.py` 削除。auto mode は一時的に `[auto mode: migration in
  progress]` を表示して何もしない (このステップの間のみ)。
  テスト: mode_executor の auto 系以外をすべて green にする。
- **Step 2: auto 経路**
  auto ループ・tool call 実行・確認ゲート・グレーエコー・checkpoint を async 化。
  `test_mode_executor.py` の auto 系を復活させ全体 green。
- **Step 3: 後片付け**
  DESIGN.md 本文の該当節 (§5.3, §5.8, §5.9, §5.10 のスレッド記述) を更新、
  本ドキュメントのステータスを「実装済み」に変更、残った参照の掃除。

---

## 12. 設計判断の記録

1. **`loop.add_reader` + 同期コールバック** を採用 (streams /
   `connect_read_pipe` ではなく)。fd → 処理の経路が最短で、余分なバッファ層が
   ない。コールバック内の処理 (read / feed / write / イベントセット) はすべて
   非ブロッキングの同期処理で完結する。
2. **オーバーレイ中は「読み取り停止」(remove_reader)** を採用し、メモリ上の
   保留バッファ + 書き込みゲート方式は不採用。現行実装の「LLM 待ち中は pty を
   読まない」と同一のカーネル背圧セマンティクスを保て、バッファ上限・flush
   タイミングという新しい設計問題を持ち込まない。
3. **`asyncio.TaskGroup` は不採用**。モードタスクのキャンセルは正常系であり、
   「子の失敗で全体をキャンセルする」TaskGroup の構造化並行性と噛み合わない。
   タスクは relay が明示的に参照を保持し、done callback で例外を記録する。
4. **プロンプト中の Ctrl-C は「n」であってタスクキャンセルではない** (現行踏襲)。
   キー要求 pending 中はすべてのバイトをキー要求に回し、キャンセル判定より
   優先する。
5. **`SettleDetector` クラスは削除し関数化**。時刻の手管理 (`remaining_ms` の
   `max(1, ...)` 等) はイベントループのタイマーに任せる。`asyncio.Event` を
   引数に取る自由関数にすることで relay から切り離して単体テストできる。
6. **JSONL ログの write は同期のまま**。1 エントリは小さく、ローカル FS への
   append でループを実質ブロックしない。`asyncio.to_thread` 化はスレッド全廃の
   方針に反するため、問題が観測されたときに検討する。
7. **モジュール名・クラス名は維持** (`io_relay.IORelay` 等)。責務の配置は
   変わらないため、リネームによる差分の膨張を避ける。
