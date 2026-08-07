# 並行処理 (asyncio) 設計ドキュメント

- ステータス: 実装済み
- 親ドキュメント: `DESIGN.md`
- expecto の並行処理は asyncio **単一スレッド**のイベントループで行う。
  スレッド・Lock は使わない。

---

## 1. 設計方針

1. **全体を asyncio 単一スレッドにする**。「LLM 呼び出しだけ async」の折衷はしない
   (2 つのパラダイムの橋渡しが増えるだけで悪化する)。スレッド・Lock は使わない。
2. **pty → screen → stdout のポンプは常時 1 本**。settle 待ち・y/n 待ち・LLM 待ちは
   「イベントを await する」だけにし、中継処理を重複実装しない。
3. **ユーザーの中断 = Task キャンセル**。中断フラグの引き回しはせず
   `asyncio.CancelledError` の伝播で表現し、後始末は `finally` に集約する。
4. **LLM 呼び出しは `openai.AsyncOpenAI`**。キャンセル時は in-flight の
   HTTP リクエスト自体が中断される。
5. 対象 OS は macOS / Linux のみ (DESIGN.md §1.1) なので、tty / pty fd への
   `loop.add_reader()` を前提としてよい。

---

## 2. タスク構成

```
asyncio イベントループ (単一スレッド)
│
├─ メインタスク: run_session()
│    起動処理 → relay.run() を await → 終了処理 (waitpid, termios 復元は同期)
│
├─ fd コールバック (loop.add_reader, 同期処理のみ)
│    stdin_fd → IORelay._on_stdin() → InputRouter.handle()
│    pty_fd   → IORelay._on_pty()   → feed / 表示 / 出力イベント通知
│
├─ 実行タスク (0 or 1 個。クエリ確定時に create_task)
│    Executor.execute(query)
│    tool call ループ (LLM await / settle await / 確認 await)
│    ユーザー Ctrl-C = このタスクの cancel()
│
└─ compaction タスク (0 or 1 個。query 終了時に LLMClient が起動)
     LLMClient._compact_background()  (Conversation.md §5)
```

- 実行タスクと compaction タスクはループ上で自然にインターリーブする。
  HTTP 待ち中もポンプ (fd コールバック) は動き続ける。
- `asyncio.TaskGroup` は採用しない (§6-3)。

---

## 3. コンポーネント

### 3.1 IORelay (中継ポンプ + イベントハブ)

```python
class IORelay:
    async def run(self, pty_master_fd: int, stdin_fd: int, stdout_fd: int) -> None:
        """add_reader を登録し、終了 (_closed) まで待つ。
        finally で reader 解除・実行タスクの cancel。"""

    # --- pty 出力の制御 (Executor が使う) ---
    def pause_pty(self) -> None: ...    # pty の add_reader を解除 (読み取り停止)
    def resume_pty(self) -> None: ...   # 再登録
    async def wait_settle(self, settle_ms, threshold_ms) -> SettleResult: ...  # §3.3
    def arm_sgr_reset_at_newline(self) -> None: ...    # §5.2 グレーエコー
    def flush_sgr_reset(self) -> None: ...             # 未リセットなら即リセット

    # --- 実行中の入力 (Executor が使う) ---
    async def wait_key(self, accept: frozenset[int]) -> int: ...  # §3.2

    # --- 実行タスクの起動 (cli の on_query_submit が使う) ---
    def start_execution(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]: ...
```

**pty コールバック** (`_on_pty`、同期):

```
data = os.read(pty_fd, 4096)
data が空 / OSError(EIO) → _closed をセットして終了シーケンスへ
screen_buffer.feed(data)
stdout へ書き込み (SGR リセット armed なら最初の改行で \x1b[0m を挿入)
_output_seen (asyncio.Event) をセット          # settle 検知用
```

**stdin コールバック** (`_on_stdin`、同期): raw I/O ログ (`--raw-io-log-file`
指定時) に記録し、読んだバイト列を `InputRouter.handle()` に渡す。
ルーティング先は InputRouter の状態が決める。

**実行タスクの起動**: `on_query_submit` コールバック (cli) が
`relay.start_execution(executor.execute(query))` を呼ぶ。done callback で
(a) SGR リセットの flush と pending なキー要求の破棄、(b) InputRouter を
PASSTHROUGH に戻す、(c) `CancelledError` 以外の例外を outer tty にグレーで
表示する (`[expecto: error: ...]`)。

**`pause_pty()` の意味論**: 読み取り自体を止める (`remove_reader`)。
pty のカーネルバッファが溢れれば子プロセスの write がブロックする
(カーネル背圧)。メモリ上の保留バッファ方式は採らない (§6-2)。
`wait_settle()` は読み取りが有効であることを前提とする (pause 中の呼び出しは
プログラミングエラーとして assert する)。

### 3.2 InputRouter — EXECUTING 中の入力

EXECUTING 状態のバイトはコールバック `on_executing_input(byte)` に転送する。
実装は IORelay 側:

```
キー要求 (wait_key) が pending → accept に含まれるバイトなら Future を解決、
                                  含まれなければ破棄
キー要求なし & byte == 0x03    → 実行タスクを cancel()
それ以外                       → 破棄
```

- `wait_key(accept)`: y/n プロンプト用。呼び出し側は
  `frozenset({y, Y, n, N, Esc, Ctrl-C})` のように **Ctrl-C を含めて**渡す —
  プロンプト表示中の Ctrl-C は「n (拒否/停止)」であり、実行全体の中断ではない
  (§4 の表を参照)。
- 端末→アプリケーション応答シーケンスは破棄せず pty へ転送する
  (ResponseFilter、DESIGN.md §5.4)。

### 3.3 settle — wait_settle コルーチン

```python
class SettleResult(enum.Enum):
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

- settle_ms の無出力で SETTLED、出力が続いたまま threshold_ms 経過で THRESHOLD。
- Ctrl-C 中断はこの関数の関心事ではない (実行タスクのキャンセルとして外から
  伝播する)。`IORelay.wait_settle()` は自身の `_output_seen` を渡して
  この関数を呼ぶ薄いラッパ。

### 3.4 Executor / LLMClient の await 点

- 実行全体 (`Executor.execute()`) が 1 つの asyncio タスクであり、LLM 応答待ち・
  settle 待ち・確認プロンプト待ちを含む**あらゆる await 点で Ctrl-C により
  即座に中断**できる。
- `_thinking(awaitable)`: `[thinking...]` 表示 + `pause_pty()` を try/finally で
  かけて await する共通ヘルパ。キャンセルを含む全ての経路で表示消去と
  `resume_pty()` が保証される (streaming 表示開始後の扱いは DESIGN.md §5.8)。
- compaction はクエリ実行とは独立の背景タスクで走る (Conversation.md §5)。

---

## 4. キャンセル・割り込みセマンティクス

| 状況 | ユーザー操作 | 動作 |
|------|-------------|------|
| passthrough | Ctrl-C | pty へ素通し (子プロセスへの SIGINT) |
| query 入力中 | Esc / Ctrl-C | キャンセル → PASSTHROUGH |
| 実行中 (LLM 待ち / settle 待ち) | Ctrl-C | **実行タスクを cancel()**。CancelledError が伝播し、finally で表示復元・SGR リセット・`[expecto interrupted]` 表示。送信済みコマンドの tool 結果はその時点までの出力で記録される (Conversation.md §4) |
| y/n プロンプト表示中 | Ctrl-C / Esc / n | `wait_key` が解決 → **「n」として扱う** (タスクは生存)。継続確認なら停止、コマンド確認なら拒否を tool 結果で LLM へ返す |
| 実行中 | その他のキー | 破棄 (端末応答シーケンスのみ pty へ転送 — DESIGN.md §5.4) |
| 子プロセス終了 | — | pty EOF → 実行タスクを cancel → 終了シーケンス |

- `CancelledError` は実行タスク内で捕捉して後始末したうえで **re-raise** する
  (asyncio の規約に従う)。done callback は `CancelledError` を正常終了として扱う。
- compaction タスクは実行タスクから独立しており、実行の中断に巻き込まれない
  (`start_query()` の待ち合わせ中に Ctrl-C されてもタスクは完走し、次の query が
  結果を拾う)。

---

## 5. 表示制御

### 5.1 `[thinking...]` オーバーレイ中の pty 出力

オーバーレイ (`\x1b7` ... `\x1b8\x1b[J`) と子プロセス出力の混線を防ぐため、
LLM 待ち中は `pause_pty()` (読み取り停止) で pty 出力をカーネルバッファに
滞留させる (背圧)。常時ポンプに書き込みゲートを付ける案は採らない (§6-2)。

### 5.2 コマンドエコーのグレー表示

stdout への書き込みはポンプに一元化されているため、relay のフラグとして実装する:

- `arm_sgr_reset_at_newline()`: Executor がグレー SGR を書き、コマンドを
  注入する直前に呼ぶ。
- `_on_pty` は armed 中、data 内の最初の `\n` の直後に `\x1b[0m` を挿入して書き、
  フラグを解除する。
- `flush_sgr_reset()`: 改行が来ないまま settle・中断・EOF となった場合の保証。
  実行タスクの finally と relay の終了処理から呼ぶ (冪等)。

---

## 6. 設計判断の記録

1. **`loop.add_reader` + 同期コールバック** を採用 (streams /
   `connect_read_pipe` ではなく)。fd → 処理の経路が最短で、余分なバッファ層が
   ない。コールバック内の処理 (read / feed / write / イベントセット) はすべて
   非ブロッキングの同期処理で完結する。
2. **オーバーレイ中は「読み取り停止」(remove_reader)** を採用し、メモリ上の
   保留バッファ + 書き込みゲート方式は不採用。カーネル背圧のセマンティクスを
   使うことで、バッファ上限・flush タイミングという設計問題を持ち込まない。
3. **`asyncio.TaskGroup` は不採用**。実行タスクのキャンセルは正常系であり、
   「子の失敗で全体をキャンセルする」TaskGroup の構造化並行性と噛み合わない。
   タスクは relay が明示的に参照を保持し、done callback で例外を記録する。
4. **プロンプト中の Ctrl-C は「n」であってタスクキャンセルではない**。
   キー要求 pending 中はすべてのバイトをキー要求に回し、キャンセル判定より
   優先する。
5. **settle は `asyncio.Event` を引数に取る自由関数**。時刻の手管理はイベント
   ループのタイマーに任せ、relay から切り離して単体テストできる。
6. **JSONL ログの write は同期のまま**。1 エントリは小さく、ローカル FS への
   append でループを実質ブロックしない。`asyncio.to_thread` 化はスレッド全廃の
   方針に反するため、問題が観測されたときに検討する。
