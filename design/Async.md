# Concurrency (asyncio) Design Document

- Status: implemented
- Parent document: `DESIGN.md`
- expecto's concurrency runs on a **single-threaded** asyncio event loop.
  No threads, no locks.

---

## 1. Design Principles

1. **Make the whole system single-threaded asyncio**. No half-measure of "only
   the LLM calls are async" (that just adds bridging between two paradigms and
   makes things worse). No threads, no locks.
2. **Exactly one always-on pty → screen → stdout pump**. Waiting for settle,
   y/n, or the LLM is expressed purely as "await an event" — relay logic is
   never duplicated.
3. **User interrupt = Task cancellation**. No interrupt flags threaded through
   the code; interruption is expressed as `asyncio.CancelledError` propagation,
   with cleanup consolidated in `finally`.
4. **LLM calls use `openai.AsyncOpenAI`**. On cancellation, the in-flight
   HTTP request itself is aborted.
5. Target OS is macOS / Linux only (DESIGN.md §1.1), so we may assume
   `loop.add_reader()` on tty / pty fds.

---

## 2. Task Structure

```
asyncio event loop (single thread)
│
├─ main task: run_session()
│    startup → await relay.run() → shutdown (waitpid, termios restore are synchronous)
│
├─ fd callbacks (loop.add_reader, synchronous processing only)
│    stdin_fd → IORelay._on_stdin() → InputRouter.handle()
│    pty_fd   → IORelay._on_pty()   → feed / display / output event notification
│
├─ execution task (0 or 1. create_task on query submit)
│    Executor.execute(query)
│    tool call loop (LLM await / settle await / confirmation await)
│    user Ctrl-C = cancel() on this task
│
└─ compaction task (0 or 1. Started by LLMClient when a query ends)
     LLMClient._compact_background()  (Conversation.md §5)
```

- The execution task and the compaction task interleave naturally on the loop.
  The pump (fd callbacks) keeps running even while waiting on HTTP.
- `asyncio.TaskGroup` is not used (§6-3).

---

## 3. Components

### 3.1 IORelay (relay pump + event hub)

```python
class IORelay:
    async def run(self, pty_master_fd: int, stdin_fd: int, stdout_fd: int) -> None:
        """Register add_reader and wait until shutdown (_closed).
        In finally: remove readers and cancel the execution task."""

    # --- pty output control (used by Executor) ---
    def pause_pty(self) -> None: ...    # remove the pty add_reader (stop reading)
    def resume_pty(self) -> None: ...   # re-register
    async def wait_settle(self, settle_ms, threshold_ms) -> SettleResult: ...  # §3.3
    def arm_sgr_reset_at_newline(self) -> None: ...    # §5.2 gray echo
    def flush_sgr_reset(self) -> None: ...             # reset immediately if not yet reset

    # --- input during execution (used by Executor) ---
    async def wait_key(self, accept: frozenset[int]) -> int: ...  # §3.2

    # --- starting the execution task (used by cli's on_query_submit) ---
    def start_execution(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]: ...
```

**pty callback** (`_on_pty`, synchronous):

```
data = os.read(pty_fd, 4096)
data is empty / OSError(EIO) → set _closed and enter the shutdown sequence
screen_buffer.feed(data)
write to stdout (if SGR reset is armed, insert \x1b[0m at the first newline)
set _output_seen (asyncio.Event)               # for settle detection
```

**stdin callback** (`_on_stdin`, synchronous): records to the raw I/O log
(when `--raw-io-log-file` is given) and passes the bytes read to
`InputRouter.handle()`. Routing is determined by the InputRouter state.

**Starting the execution task**: the `on_query_submit` callback (cli) calls
`relay.start_execution(executor.execute(query))`. The done callback
(a) flushes the SGR reset and discards any pending key request, (b) returns
InputRouter to PASSTHROUGH, (c) displays exceptions other than
`CancelledError` in gray on the outer tty (`[expecto: error: ...]`).

**Semantics of `pause_pty()`**: stops reading itself (`remove_reader`).
If the pty's kernel buffer fills up, the child process's write blocks
(kernel backpressure). No in-memory pending-buffer scheme is used (§6-2).
`wait_settle()` assumes reading is enabled (calling it while paused is a
programming error, asserted).

### 3.2 InputRouter — input while EXECUTING

Bytes in the EXECUTING state are forwarded to the `on_executing_input(byte)`
callback. The implementation lives in IORelay:

```
key request (wait_key) pending → if the byte is in accept, resolve the Future;
                                  otherwise discard
no key request & byte == 0x03  → cancel() the execution task
anything else                  → discard
```

- `wait_key(accept)`: for y/n prompts. The caller passes a set that
  **includes Ctrl-C**, e.g. `frozenset({y, Y, n, N, Esc, Ctrl-C})` —
  Ctrl-C while the prompt is displayed means "n (decline/stop)", not an
  interrupt of the whole execution (see the table in §4).
- Terminal→application response sequences are not discarded but forwarded to
  the pty (ResponseFilter, DESIGN.md §5.4).

### 3.3 settle — the wait_settle coroutine

```python
class SettleResult(enum.Enum):
    SETTLED = "settled"
    THRESHOLD = "threshold"

async def wait_settle(
    output_seen: asyncio.Event,   # set on each output arrival (the waiter clears it)
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

- SETTLED after settle_ms with no output; THRESHOLD when output keeps coming
  and threshold_ms elapses.
- Ctrl-C interrupts are not this function's concern (they propagate from
  outside as cancellation of the execution task). `IORelay.wait_settle()` is
  a thin wrapper that calls this function with its own `_output_seen`.

### 3.4 Executor / LLMClient await points

- The entire execution (`Executor.execute()`) is a single asyncio task, and
  Ctrl-C can **interrupt it immediately at every await point** — waiting for
  the LLM response, waiting for settle, and waiting at confirmation prompts.
- `_thinking(awaitable)`: a shared helper that shows `[thinking...]` and
  applies `pause_pty()` around the await via try/finally. Erasing the display
  and `resume_pty()` are guaranteed on all paths, including cancellation
  (handling after streaming display has started: DESIGN.md §5.8).
- Compaction runs as a background task independent of query execution
  (Conversation.md §5).

---

## 4. Cancellation and Interrupt Semantics

| Situation | User action | Behavior |
|------|-------------|------|
| passthrough | Ctrl-C | Passed through to the pty (SIGINT to the child process) |
| during query input | Esc / Ctrl-C | Cancel → PASSTHROUGH |
| executing (waiting for LLM / settle) | Ctrl-C | **cancel() the execution task**. CancelledError propagates; in finally: restore display, SGR reset, show `[expecto: interrupted]`. The tool result for an already-sent command is recorded with the output up to that point (Conversation.md §4) |
| y/n prompt displayed | Ctrl-C / Esc / n | `wait_key` resolves → **treated as "n"** (the task survives). For a continuation confirmation this stops; for a command confirmation the decline is returned to the LLM as the tool result |
| executing | any other key | Discarded (only terminal response sequences are forwarded to the pty — DESIGN.md §5.4) |
| child process exit | — | pty EOF → cancel the execution task → shutdown sequence |

- `CancelledError` is caught inside the execution task for cleanup and then
  **re-raised** (per asyncio convention). The done callback treats
  `CancelledError` as normal completion.
- The compaction task is independent of the execution task and is not caught
  up in execution interrupts (even if Ctrl-C arrives while `start_query()` is
  waiting for it, the task runs to completion and the next query picks up the
  result).

---

## 5. Display Control

### 5.1 pty output during the `[thinking...]` overlay

To prevent interleaving between the overlay (`\x1b7` ... `\x1b8\x1b[J`) and
child process output, pty output is held in the kernel buffer via
`pause_pty()` (stop reading) while waiting for the LLM (backpressure).
Adding a write gate to the always-on pump was rejected (§6-2).

### 5.2 Gray display of command echo

Since all writes to stdout are centralized in the pump, this is implemented
as a relay flag:

- `arm_sgr_reset_at_newline()`: the Executor writes the gray SGR and calls
  this immediately before injecting the command.
- While armed, `_on_pty` inserts `\x1b[0m` right after the first `\n` in the
  data when writing, then clears the flag.
- `flush_sgr_reset()`: the guarantee for when settle, an interrupt, or EOF
  occurs before any newline arrives. Called from the execution task's finally
  and from the relay's shutdown handling (idempotent).

---

## 6. Design-Decision Record

1. **`loop.add_reader` + synchronous callbacks** adopted (rather than streams /
   `connect_read_pipe`). The fd → processing path is shortest, with no extra
   buffering layer. Everything in the callback (read / feed / write / event
   set) completes as non-blocking synchronous processing.
2. **"Stop reading" (remove_reader) during the overlay** adopted; the
   in-memory pending buffer + write gate scheme was rejected. Using
   kernel-backpressure semantics avoids introducing the design problems of
   buffer limits and flush timing.
3. **`asyncio.TaskGroup` not adopted**. Cancelling the execution task is the
   normal path, which does not fit TaskGroup's structured concurrency of
   "cancel everything when a child fails". The relay holds an explicit
   reference to the task and records exceptions in the done callback.
4. **Ctrl-C during a prompt is "n", not task cancellation**. While a key
   request is pending, all bytes go to the key request, taking priority over
   the cancellation check.
5. **settle is a free function taking an `asyncio.Event`**. Manual timekeeping
   is delegated to the event loop's timers, and it can be unit-tested
   detached from the relay.
6. **JSONL log writes stay synchronous**. Each entry is small, and an append
   to the local FS does not effectively block the loop. Switching to
   `asyncio.to_thread` would contradict the no-threads policy, so it will be
   considered only if a problem is actually observed.
