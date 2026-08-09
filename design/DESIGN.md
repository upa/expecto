
# expecto Design Document

- Status: Draft v0.1 (for prototype implementation)
- Target implementation language: Python 3.11+ (prototype)
- Package manager: uv
- Target OS: macOS, Linux

---

## 1. Purpose / Concept

`expecto` is a CLI tool that sits transparently between the terminal (tty) and the user's input/output.

- Normally it behaves as a "transparent wrapper" that relays input/output to a regular shell (or any program the user launched) as-is.
- When the user presses a specific key binding (hotkey), it enters **query mode** and accepts a natural-language query.
- When the query is submitted (Enter), the recent terminal I/O history and the query are passed to an LLM, and the LLM executes commands to achieve the goal **by typing them into the tty**.
- Key property: `expecto` wraps the **tty itself**, not an individual shell, so even when the user has entered another host via `ssh`, or a vendor-specific CLI of a network device (router/switch), LLM assistance and delegated operation still reach it. It does not depend on the shell's `PS1` or readline.

### 1.1 Non-goals

- Windows support
- GUI or TUI dashboards
- Multiple panes / multiplexer-like features (reimplementing tmux-like functionality)
- Advanced authentication / secret management (e.g., GUI dialogs for password entry) — in v0.1 the user is expected to type passwords straight through
- Strict audit logging / compliance features

---

## 2. Terminology

| Term                       | Meaning                                                                                                          |
|----------------------------|------------------------------------------------------------------------------------------------------------------|
| outer tty                  | The actual terminal from which `expecto` was launched (the screen the user is looking at)                          |
| pty (master/slave)         | The pseudo-terminal that `expecto` allocates internally. A child process (shell/ssh etc.) is attached to the slave side |
| child process              | The process attached to the pty slave. Defaults to `$SHELL`, or any command specified at launch                    |
| passthrough mode           | The normal state. Bytes between the outer tty ⇄ pty master are passed through as-is                                |
| query mode                 | The overlay mode for query input, entered via the hotkey                                                           |
| the execution loop         | After query submission, the agentic loop where the LLM repeats command execution → output inspection → next command until the goal is achieved. expecto's only execution behavior |
| screen buffer / scrollback | The "recent output history" kept for passing to the LLM (text cleaned up via pyte)                                 |

---

## 3. High-Level Operation Flow

```
Launch: expecto [-- command...]
  └─ Launches $SHELL if no command is specified

[passthrough mode]
  User input → forwarded as-is to the child process
  Child process output → forwarded as-is to the outer tty
  (the history buffer is continuously recorded during this time)

  User presses the hotkey (e.g., Ctrl-/)
        │
        ▼
[query mode]
  - Draw a minimal overlay ("miniprompt" `? `) on the outer tty
  - User input is not sent to the child process; it accumulates in the query buffer
  - Enter submits / Esc returns to passthrough (cancel)
        │ Enter
        ▼
  Send "recent I/O history + query" to the LLM
        │
        ▼
  The execution loop (function calling):
    1. The LLM calls a tool (run_command / send_control)
    2. expecto writes the command + Enter (or a control character)
       to the pty master
    3. Wait until the output settles
       If the settle threshold is exceeded without settling, a note
       saying "did not settle" is attached to the result
    4. Return the screen text at that point to the LLM as the tool's
       execution result (tool message)
    5. The LLM either calls the next tool (continue) or returns a final
       response (plain text)
       (the final response is displayed on screen as characters arrive
       via streaming)
  Ends when the goal is achieved or the user interrupts with Ctrl-C.
  Every fixed number of steps (checkpoint, default 15) the user is asked
  for continuation confirmation (y/n); n ends the loop (the conversation
  history is preserved)
  → back to passthrough mode
```

---

## 4. Architecture

```
┌───────────────────────────────────────────────┐
│ outer tty (the user's real terminal)            │
└───────────────────────────────────────────────┘
        ▲ stdout                 │ stdin (raw mode)
        │                        ▼
┌───────────────────────────────────────────────┐
│                    expecto (parent process)           │
│                                                 │
│  TTYController      : raw-mode setup/restore of outer tty │
│  InputRouter         : per-byte inspection,          │
│                        hotkey detection / state transitions │
│  IORelay (asyncio pump): relays outer <-> pty master │
│  ScreenBuffer        : keeps pty output history (with pyte) │
│  QueryModeUI         : draws the miniprompt / collects input │
│  LLMClient           : OpenAI API calls (async)      │
│  Executor            : controls the execution loop   │
│  settle.wait_settle  : settle detection              │
│  MCPManager          : MCP servers (client side)     │
└───────────────────────────────────────────────┘

Concurrency runs on a single-threaded asyncio event loop (no threads or
locks). See **`Async.md`** for details.
        │ pty master fd
        ▼
   pty slave ── child process ($SHELL, ssh, etc.)
```

---

## 5. Component Design

### 5.1 TTYController

- Responsibility: save the outer tty's `termios` settings at `expecto` startup and switch to raw mode. Always restore the original settings on exit (normal exit, exception, or signal).
- Python stdlib: the `termios` and `tty` modules. Use `tty.setraw(fd)`, but save the result of `termios.tcgetattr()` for restoration.
- Ensure restoration is called via both `atexit` and signal handlers (when the process itself receives `SIGTERM` or `SIGINT`).
- `SIGWINCH` handler registration is done by cli via asyncio's `loop.add_signal_handler()` (executed on the event loop, hence safe). The handler propagates the outer tty's current size to the pty via TTYController's `get_window_size()` / `set_pty_window_size()`, and also makes the pyte screen follow via `ScreenBuffer.resize()`.

### 5.2 PTYManager

- Responsibility: allocating the pty master/slave, fork/exec of the child process, and exit detection.
- The Python stdlib `pty` module (`pty.fork()`) is the first choice. If more flexible control is needed, consider the `ptyprocess` library (its process-exit detection and resize API are easier to work with).
- The child process defaults to `os.environ.get("SHELL", "/bin/bash")`. Also allow specifying an arbitrary command, as in `expecto -- ssh myrouter`.
- When the child's exit is detected, `expecto` itself exits as well.
- The environment variable `EXPECTO_ACTIVE=1` is set for the child process (propagated to descendant processes via `execvp`). `expecto` checks this variable at startup, and if it is already set, treats it as a "nested launch inside an expecto session", prints an error message, and exits (see §10). To nest intentionally, this can be bypassed with `env -u EXPECTO_ACTIVE expecto`.

### 5.3 IORelay (always-on pump + event hub)

- Responsibility: an always-on pump that monitors the outer tty's stdin fd and the pty master fd via `loop.add_reader()` on the asyncio event loop and relays bytes bidirectionally. In addition, it serves as the event hub for the execution loop (settle waiting, key waiting, pausing pty reads, execution task management) (Async.md §3.1).
- Outline of the fd callbacks (synchronous processing only):
  ```
  stdin_fd read → InputRouter.handle(data)   # hotkey detection & per-state routing
  pty_fd read   → ScreenBuffer.feed(data)
                → write to outer stdout
                → output event notification (for settle detection)
  ```
- During query mode, writes to the pty master are stopped (user input is not passed to the child). While the `[thinking...]` overlay is displayed, reading from the pty itself is stopped via `pause_pty()` (kernel backpressure; prevents the overlay and child process output from getting entangled).
- Query submission launches the execution (`Executor.execute`) as an asyncio task; a user Ctrl-C while executing becomes a `cancel()` of this task. When the task finishes (done callback), InputRouter is returned to PASSTHROUGH.

### 5.4 InputRouter / Hotkey Detection

- Processes input as a state machine, one byte at a time. Beware of UTF-8 multibyte character splits (buffering is needed to wait until a complete character is available).
- There is a single hotkey: `Ctrl-/` (0x1f) is the default. Make it configurable so it can be easily changed if a conflict is found.
- **Forwarding of terminal response sequences** (`ResponseFilter` in `response_filter.py`): a child process may send a query to the outer terminal and hold off input processing until the response arrives (fish 4.x sends CPR `\x1b[6n`, OSC 11, and Primary DA on every prompt; bash/readline does not). Since stdin is not forwarded to the pty during QUERY / EXECUTING, the response would otherwise never reach the child process and the injected command would not be processed. Therefore, in both states, a response-recognition state machine is inserted before normal byte processing, and only sequences confirmed to be terminal→application responses are forwarded to the pty:
  - CSI responses: final character `R n y t`, or `c` with a `?`/`>` prefix, or `u` with `?` (DA and kitty keyboard protocol responses). Arrow keys (`\x1b[A`), `~`-style sequences, and kitty key input (`u` without `?`) are not treated as responses and are replayed to normal processing.
  - OSC / DCS responses (BEL / ST terminated): cannot originate from a keyboard, so they are forwarded regardless of content.
  - If only a single ESC byte is pending at the end of a chunk (one read), it is treated as the user's Esc key. Valid prefixes of 2+ bytes are held across chunks, handling responses split across reads. Pending limit is 1024 bytes.
  - As a side effect, this also prevents the ESC contained in the OSC response terminator `\x1b\` from being misread as an "n" answer to a y/n confirmation prompt (`wait_key`).
  - It is accepted that some keys indistinguishable from responses (e.g., the F3 collision with CPR) may reach the pty during execution.
- Input handling during query mode:
  - Regular characters → appended to the query buffer + reflected in the miniprompt
  - Backspace → delete one character
  - Enter → submit and proceed to the LLM call
  - Esc or Ctrl-C → cancel and return to passthrough

### 5.5 ScreenBuffer (history management)

- Uses [`pyte`](https://pypi.org/project/pyte/)'s `pyte.HistoryScreen` + `pyte.Stream` to keep the "current screen contents" with ANSI escape sequences interpreted, plus the recent scrollback lines (capacity `scrollback_lines`, default 5000), as clean text. This is what is passed to the LLM. Raw byte streams are not retained (a raw I/O log for debugging is written to a file by IORelay when `--raw-io-log-file` is specified).
- To limit token volume, the screen text sent when starting a fresh conversation is tail-capped at `new_output_max_chars` characters by the Executor (the same cap as the between-query delta; see Conversation.md).
- **New-output extraction (for tool results)**: the tool result in the execution loop carries "only the new output produced by that command" (the full history is not re-sent every time).
  - `mark()`: records the current state and returns a `ScreenMark`. What is recorded: the "cumulative number of lines pushed into scrollback (a monotonic counter unaffected by deque maxlen overflow or clear)" and the "cursor row". At the same time, pyte's `screen.dirty` (the set of changed lines; pyte only adds to it — clearing is the consumer's responsibility) is cleared.
  - `get_text_since(mark)`: returns the concatenation of "lines pushed into history since the mark (excluding pre-existing lines that were above the cursor at mark time and got pushed out by scrolling)" + "the range of visible lines changed since the mark". If scrolling occurred, the visible portion is taken from the new location of the mark-time cursor row downward (because scrolling marks all lines dirty).
  - If a single command's output exceeds the scrollback capacity, a truncation note is prepended. A redraw involving a screen clear marks all lines dirty, so the entire visible screen becomes the result (a reasonable fallback).

### 5.6 QueryModeUI

- Responsibility: display a miniprompt on the outer tty without destroying the child process's screen.
- Implementation: draws `? ` and the query string inline in gray starting at the current cursor position (the starting column is obtained from ScreenBuffer). Appending characters uses differential echo only; on deletion or rewriting, it moves back to the start of the prompt, erases with `\x1b[J`, and redraws (the return-position calculation uses display width accounting for East Asian Width, supporting line wrapping). On cancel it erases; on submit (`finalize()`) it exits leaving the query line on screen (the echo of the newline sent immediately afterward supplies the line feed).
- Entering query mode while an alternate screen (vim etc.) is in use is not supported in v0.1 (documented as a known issue; see §10 for details).

### 5.7 LLMClient

- Uses the OpenAI **Chat Completions API** (the Responses API was rejected because support in non-OpenAI compatible backends is insufficient). Each step of the execution loop uses `AsyncOpenAI`'s `client.chat.completions.stream()` (streaming); the compaction handoff summary (§5.10) uses `client.chat.completions.parse()` (Structured Outputs). The model is configurable (default: a current-generation fast model).
- Input: the entire conversation history, starting with the system prompt, is passed via `messages=` (the history is held by LLMClient; see "Conversation history" below).
- The system prompt is written in English and instructs that "output other than commands should match the language of the user's query".
- The **final response of the execution loop is plain text** (no schema enforcement). The system prompt instructs the LLM to return a summary of goal achievement, or the reason if the goal cannot be achieved, as text. There is no done/fail status field (the distinction carries no practical meaning, and plain text simplifies streaming display).
  - "Continuation" is expressed by the tool call itself. **A response containing no tool call is treated as the end of the loop.**
  - The final response text is displayed on the outer tty as a gray `[` + body + `]`, character by character as it arrives via streaming `content.delta` events (§5.8).

#### Function calling

The single step of "execute a command and receive its result" is exposed to the LLM as a **tool** via Chat Completions function calling:

| tool           | Arguments                              | Behavior                                                                                            |
|----------------|----------------------------------------|-------------------------------------------------------------------------------------------------------|
| `run_command`  | `command: string`                      | Writes the command + Enter to the pty master, waits for the output to settle, and returns the screen text |
| `send_control` | `key: "ctrl-c" \| "ctrl-d" \| "ctrl-z"` | Writes a control character to the pty master, likewise waits for settle, and returns the screen text     |
| `load_skill`   | `name: string`, `file: string`         | Returns the body of one of the user's skills, or a file bundled with it. Read by expecto itself, since skills live on the local machine while the terminal may not. Only offered when skills exist; see **`Skills.md`** |
| `mcp__<server>__<tool>` | the server's own schema | Calls a tool on one of the MCP servers configured in config.toml and returns its result text. Discovered at session start; see **`MCP.md`** |

- Tool definitions are passed as JSON Schema with `strict: true`, and arguments are validated with Pydantic models (`RunCommandParams` / `SendControlParams`). If validation fails (which normally cannot happen due to strict mode), error text is returned as the output for the corresponding `call_id`.
- `parallel_tool_calls=False` is specified to guarantee one tool call per response at the API level (if multiple are defensively returned anyway, they are executed sequentially).
- The LLM chooses between returning a tool call (continue) or a final response (plain text). **A response containing no tool call is treated as the end of the loop.** Since Structured Outputs is not used, it is structurally possible for the model to emit preamble text before a tool call (suppressed via the prompt; if it appears, it is streamed to the display as-is and the loop continues).
- Format of the tool execution result (`tool` message) — carries only the new output produced by that command, not the whole screen; see §5.5 for the extraction method:

      ## Terminal Output
      ```
      {new output since the mark (get_text_since from §5.5)}
      ```

  If the settle threshold is exceeded without settling, a note is appended at the end so the LLM can decide whether to "interrupt with Ctrl-C or keep waiting":

      [Note] Output did not settle within 30s. The command may still be running.

#### Conversation history (client-held) and prompt caching

The message history is held by `LLMClient` and continues as **a single conversation for the whole session** (details in **`Conversation.md`**):

```
[system: SYSTEM_PROMPT]                  ← fully static
[user: query 1 (full screen + Goal)]
[assistant: tool_calls=...]
[tool: execution result] ...
[assistant: final explanation]
[user: query 2 (diff of new output between queries + Goal)]
...
```

- The history is **append-only**, and the system prompt and tools definition are completely fixed. Every request is a strict extension of the previous request, so the prompt prefix cache stays effective throughout the session (except immediately after compaction).
- Follow-up queries do not re-send the full screen (past screens are already in the history). The user's manual operations between queries are passed as a diff of new output (capped by `new_output_max_chars`).
- To ensure that an assistant's `tool_calls` is always followed by the corresponding `tool` message, tool results are recorded on all paths, including interruption, checkpoint stop, and refusal (Conversation.md §4).

### 5.8 Executor

- Controls the execution loop after query submission (command execution → output inspection → next action).
- At the start (immediately after query submission), it first sends a single newline to the pty to display the shell's new prompt and reflect it on screen via a settle wait, before proceeding to the first LLM call. Since the query line has consumed the previous prompt line, without this the first command would appear to be injected into a line with no prompt.
- Implemented as a loop in which the LLM calls `run_command` / `send_control` via function calling (§5.7). Executing the tool call (writing to the pty master + waiting for settle + collecting the new output (mark / get_text_since from §5.5) and recording the tool result) is the Executor's responsibility.
- A `load_skill` call (§5.7) is handled synchronously from the skills read at startup: nothing is written to the pty and nothing is waited for; the load is shown on screen and the skill's body is recorded as the tool result. See **`Skills.md`** §4.
- A call to an MCP tool (§5.7) takes a third branch: the call is shown on the outer tty in gray and dispatched to `MCPManager`, with no command injection and no settle wait (nothing reaches the terminal). Its result text — including a server-reported failure — is recorded as the tool result. See **`MCP.md`** §5.
- **Dangerous-command confirmation gate (3 layers)**: (1) the LLM self-declares each command's risk via the required `run_command` fields `risk` (`read_only` / `mutating` / `destructive`) + `risk_reason` (the primary, environment-adaptive judgment; knowledge of vendor CLIs lives in the LLM, and session context accumulated in the conversation history supports accuracy). (2) A deterministic backstop: commands matching the built-in + user-defined (`~/.expecto/dangerous_patterns.txt`, one regex per line, `#` comments, case-insensitive) regexes are **escalated** to `destructive` regardless of the LLM's declaration (escalate-only — it never works in the direction of waiving confirmation). (3) Commands at or above `--confirm-level` (default `destructive`) require y/n confirmation from the user before execution. **A refusal is not an abort**: "the user refused" is returned to the LLM as the tool result, prompting it to take an alternative approach. If stdin is lost while waiting for confirmation (EOF), the execution is aborted as session termination (interrupted). This mechanism is a prompt-volume reduction, not a guarantee (LLM self-declaration is not robust against prompt injection or self-evaluation bias); the ultimate safety device is the structure in which "every command and its output is always visible on the user's screen" + user presence + Ctrl-C + checkpoints.
- The command echo of `run_command` is displayed in the same gray as expecto's status displays. Immediately before command injection, SGR (gray) is written to the outer tty and a reservation is placed with IORelay (`arm_sgr_reset_at_newline()`); the pump then inserts a reset right after the first newline of the echo (the echo of Enter). If settle, interruption, or EOF occurs before a newline arrives, the reset is still guaranteed via `flush_sgr_reset()` (idempotent). SGR is written only to the outer tty and never enters ScreenBuffer, so the text passed to the LLM is unaffected (if the shell itself colors its echo, that may take precedence — best effort).
- Loop processing uses the settle detection below (§5.9). Every `max_steps` steps (default 15, counted by LLM calls), the user is asked for continuation confirmation (`continue? (y/n)`) as a **checkpoint**. `max_steps = 0` disables the checkpoint entirely (the loop runs autonomously until done/fail or Ctrl-C). On y, execution continues with the conversation history preserved; on n / Esc / Ctrl-C, it ends. By making this a confirmation rather than a hard abort, the limit functions as "an opportunity to put human eyes on it" rather than "task failure" (as an interactive tool, user presence is the first safety device). While waiting for confirmation, pty output relay continues (handled by the always-on pump). If stdin is lost (EOF), the execution task is cancelled as session termination.
- The LLM can send control characters (Ctrl-C etc.) via the `send_control` tool. This lets the LLM itself decide on things like interrupting a long-running command.
- **Streaming display of the final response**: when `content.delta` starts arriving, the `[thinking...]` overlay is erased and the final response text is displayed progressively as a gray `[` followed by the arriving characters, closed with `]` + SGR reset upon completion. The overlay erasure happens exactly once at streaming start and is never rewound afterward (when a multi-line response scrolls the screen, the DECSC saved position cannot be trusted). If Ctrl-C interrupts mid-stream, the partial text already displayed is left on screen, followed by a newline and `[expecto: interrupted]`. If no delta ever arrives (empty response etc.), the text is displayed at once after completion, as before. During streaming, pty reads remain paused, so no entanglement with child process output occurs.
- The user can interrupt execution at any time with `Ctrl-C` (interrupt detection is done by InputRouter handling input with priority while executing).
- LLM calls (each step's call, and awaiting a compaction started after the previous query ended) await `AsyncOpenAI`. The entire execution is a single asyncio task, and **Ctrl-C interrupts it immediately at any await point, including while waiting for an LLM response** (task cancellation). On cancellation, the in-flight HTTP request itself is aborted (compaction, being an independent task, runs to completion), and cleanup (overlay erasure, SGR reset, trailing newline) is guaranteed in finally (Async.md §4).

### 5.9 Settle Detection (output stability detection)

- To detect the moment when "output has quieted down" after command execution, a simple idle-timer approach is adopted. The implementation is the `settle.wait_settle()` coroutine (waits for IORelay's output events via an `asyncio.Event`):
  - After the command is sent, output-arrival events from the pty master are monitored.
  - If no output arrives for a fixed period (default 300ms–800ms, configurable), the output is judged to have "settled".
  - If the threshold time (default 30 seconds, configurable) is exceeded without settling: this is not treated as a timeout error; the output up to that point is sent to the LLM as the tool's execution result. A note that "the output did not settle (the command may still be running)" is included in the result, and the LLM decides the next action (e.g., send Ctrl-C via `send_control` to interrupt the command, keep waiting, switch to another approach). Commands running longer than 30 seconds (downloads, builds, etc.) are routine, so they are not uniformly treated as timeouts.
- It does **not** depend on shell-specific prompt-marker injection (PS1 rewriting). That would not work over ssh or on router CLIs, so for generality v0.1 is built on idle detection only. A future extension of "additionally using PS1 markers on supported shells for more accurate detection" remains possible (see §11).

### 5.10 Conversation History and Compaction (session context)

The session context is carried by the client-held message history itself (all past queries, commands, and outputs remain). When the history grows beyond a threshold (`compact_thresh`, measured in prompt_tokens), after the query's execution finishes (after the explanation is displayed and control is returned to the user), LLMClient internally (almost transparently to the Executor) requests a handoff summary (`HandoffSummary`: environment / activities / notes) of the history in the background, and starts the next query as a new conversation headed by the summary + the full screen (compaction). The user's manual operations between queries are conveyed by the diff of new output attached to the next query (capped by `new_output_max_chars`).

See **`Conversation.md`** for the detailed design.

### 5.11 expecto's own messages

Everything expecto says for itself — as opposed to the child's output or the LLM's answer — is written in gray as `[expecto: ...]`, so it is distinguishable at a glance from what the terminal produced:

| message | example |
|---------|---------|
| session lifecycle | `[expecto: session started (model: gpt-5.4)]`, `[expecto: session terminated]` |
| startup status | `[expecto: connecting to 2 MCP server(s)...]` |
| startup warning / error | `[expecto: warning: <path>: <key>: ...]`, `[expecto: error: config file not found: ...]` |
| tool activity | `[expecto: mcp docs.search]`, `[expecto: skill junos-upgrade]` |
| confirmation prompt | `[expecto: destructive command (...): <cmd> — run? (y/n)]` |
| end of an execution | `[expecto: interrupted]`, `[expecto: stopped at step 15]` |
| unhandled error | `[expecto: error: ...]` (relay's done callback) |

- The LLM's final response is the one gray message **without** the prefix (`[<text>]`): it is the agent speaking, not expecto.
- Startup lines go to stderr via `cli.status()` / `warn()` / `fail()`, which drop the color and brackets when stderr is not a terminal (`expecto: warning: ...`), so a redirected log holds no escape sequences. In-session messages are written to the outer tty directly, which is a terminal by construction.

---

## 6. State Machine

| State         | Description                                        | Transition conditions                                          |
|---------------|----------------------------------------------------|----------------------------------------------------------------|
| `PASSTHROUGH` | Normal relay                                       | hotkey → `QUERY`                                               |
| `QUERY`       | Accepting query input                              | Enter → `EXECUTING` / Esc,Ctrl-C → `PASSTHROUGH`               |
| `EXECUTING`   | The execution loop (awaiting LLM response, executing commands) | Goal achieved/failed, or n/Ctrl-C at a checkpoint → `PASSTHROUGH` |

Note: the contents of `EXECUTING` (LLM calls, tool execution, confirmation prompts) are represented as an asyncio execution task, and task completion (done callback) returns the state to `PASSTHROUGH` (Async.md §3.2).

---

## 7. CLI / Configuration

```
expecto [OPTIONS] [-- COMMAND ...]

OPTIONS (implemented):
  --shell PATH               Command to launch as the child process (default: $SHELL)
  --config-dir PATH          Directory holding config.toml, .env, AGENTS.md and
                             dangerous_patterns.txt (default: ~/.expecto)
  -c, --config PATH          Path to the config file
                             (default: <config-dir>/config.toml)
  -e, --env PATH             Path to the .env file to load
                             (default: ~/.expecto/.env)
  -a, --agents-md PATH       Path to the AGENTS.md appended to the system prompt
                             (default: ~/.expecto/AGENTS.md)
  -m, --model NAME           Model to use (default: config.toml's model)
  -u, --base-url URL         OpenAI API endpoint (default: config.toml's
                             base_url, or the SDK default)
  --scrollback-lines N       pyte scrollback capacity (default: 5000)
  --confirm-level LEVEL      Minimum risk level that requires confirmation
                             (destructive|mutating|none, default: destructive)
  --dangerous-patterns-file PATH  Additional file of dangerous-command regexes
                             (default: ~/.expecto/dangerous_patterns.txt)
  -l, --log-file PATH        Destination for the JSONL log of queries and LLM requests/responses (disabled by default)
  --raw-io-log-file PATH     Destination for the raw I/O log (for debugging, disabled by default)
```

MCP servers to connect to are declared as `[mcp.<name>]` tables in config.toml; see **`MCP.md`**.

The config directory is `~/.expecto` by default and holds `config.toml`, `.env`, `AGENTS.md`, and `dangerous_patterns.txt`. `--config-dir PATH` points every one of those defaults at another directory (a path that does not exist is a startup error); the individual `-c` / `-e` / `-a` / `--dangerous-patterns-file` flags still override single files. No environment variable selects the directory — `OPENAI_API_KEY` remains the only one expecto reads.

Configuration is written in `<config-dir>/config.toml`, with precedence
**built-in defaults < config.toml < CLI flags**. The only value read from an
environment variable is `OPENAI_API_KEY` (startup error if unset); everything
else — model, base_url, reasoning effort, hotkey, compaction threshold, etc. —
is specified in config.toml. See **`Config.md`** for the schema, error
handling, and hotkey notation details.

`.env` file loading: the file given by `--env PATH` (startup error if it does not exist) → without that flag, `<config-dir>/.env` if it exists. The current directory is not searched — as an installed command, expecto must behave the same no matter where it is launched from. Environment variables that are already set always take precedence over values from the file.

Not implemented (future work): the `--hotkey` / `--max-steps` / `--settle-ms` CLI flags (all low priority now that they are configurable via config.toml).

### TODO (improvements planned for the future)

- **Secret masking**: simple masking of sensitive information such as passwords and API keys in the screen history sent to the LLM (see §9). Redact the value portion of lines containing `password`, `token`, `secret`, etc.
- **Prompt engineering**: review and refine the contents of the system prompt and user messages to improve output quality. Currently only minimal instructions.

---

## 8. Building the Context Passed to the LLM

Skeleton of the system prompt (to be tuned during implementation; the final response is plain text, so there is no JSON format instruction):

```
You are an assistant that operates the terminal the user is working in
(or an SSH destination, or a network device CLI) to achieve a goal.
- Refer to the recent terminal screen contents (attached below) to
  determine which system/shell/CLI is currently connected.
- Execute commands or control characters one step at a time until the
  goal is achieved.
  Evaluate the previous command's output and decide what to do next.
  If a command has been running for a long time, decide whether to
  interrupt it with Ctrl-C or keep waiting.
```

### Skills

Every skill under `<config_dir>/skills` contributes one line — its name and description — to a `# Skills` section of the system prompt, and the model pulls the full instructions in with `load_skill` when a goal matches. This keeps long, situational procedures out of the prompt until they are needed, which is what separates a skill from AGENTS.md (below), whose content is paid for on every request. See **`Skills.md`**.

### AGENTS.md (user instructions)

When an AGENTS.md exists (`-a` / `--agents-md`, the `agents_md_file` config key, or `<config_dir>/AGENTS.md` by default), its contents are appended verbatim to the base system prompt as a trailing `# User Instructions` section. This is where the user records site-specific knowledge the model cannot derive from the screen: hosts and device conventions, preferred commands, output style.

- The section states where the instructions come from and that they are additive: they **do not override** the tool protocol or the risk-classification and confirmation rules (§5.8). A file the user writes must not be able to talk the model out of classifying a command honestly.
- The file is read once at startup and the system prompt stays byte-identical for the whole session — including compaction requests — so the prompt-cache invariant (Conversation.md §2) holds.
- A missing file is normal (nothing is appended); a file given explicitly with `-a` that does not exist is a startup error (Config.md §6).

The user message at query start includes the following (the Goal comes last):
- First query of the session / immediately after compaction: the recent clean-text screen (§5.5) + the user's query string (immediately after compaction, the Session Summary comes first)
- Second and subsequent queries: the diff of new terminal output since the previous execution ended + the query string (past screens are already in the history and are not re-sent)
- (During the loop) each step's history accumulates in the conversation history as pairs of "the LLM's tool call" and "its execution result (tool message)" (§5.7). This lets the LLM decide the next action based on what has been tried and what happened.

---

## 9. Safety / Caveats

- **Excluding dangerous commands**: expecto is a pty wrapper, and when the execution target is an ssh destination or a network device, local OS sandboxing does not reach it, so the only available means is a pre-execution gate. This is handled by the 3-layer gate of §5.8 (LLM risk self-declaration / escalation via the deterministic backstop / user confirmation). Because the command vocabulary is not limited to UNIX, a static blocklist alone cannot cover it; the LLM carries the primary, environment-adaptive judgment, and static patterns are limited to a lower-bound guarantee for the "unmistakably catastrophic". Full sandboxing and permission control are out of scope.
- **Handling of secrets**: passwords and API keys may end up in raw logs and in content sent to the LLM. For v0.1, simple masking is considered (e.g., redacting the value portion of lines containing `password`, `token`, `secret`), but the documentation must clearly state that completeness is not guaranteed.
- **Runaway protection for the execution loop**: continuation confirmation at step-count checkpoints (default: every 15 steps; terminate in non-interactive environments) and immediate user interruption (Ctrl-C) are mandatory implementations. The conversation history is billed in full on every request (with cache discounts), so cost grows super-linearly with step count — compaction (§5.10) bounds the ceiling, but adding a token/cost budget cap (SWE-agent style) should be considered in the future.
- **Hotkey conflicts**: contention occurs when the child process side (vim, tmux, router CLI, etc.) uses the same key. Making it configurable is mandatory.

---

## 10. Known Issues / Out of Scope for v0.1 (open issues)

1. **Alternate screen support**: drawing the query mode overlay while `vim`, `less`, `top`, etc. are using the alternate screen buffer may corrupt the display on return. Future work: detect the alternate screen via pyte, and temporarily switch back to the original screen before entering query mode / restore afterward.
2. **Handling of password prompts**: content may remain in the history buffer while a password is being typed for ssh or sudo. Masking-logic accuracy needs improvement.
3. **Completion-detection accuracy**: idle detection cannot distinguish "output is just slow" (large downloads etc.) from "genuinely finished". Future considerations include shell integration such as OSC 133, or learned detection of prompt regexes.
4. **Multibyte input**: with Japanese input (IME), care is needed around per-byte processing in raw mode and the IME's composition handling (v0.1 focuses on ASCII command input; Japanese queries themselves are fine, but handling of key events during IME conversion needs verification).
5. **Nested expecto**: out of scope (not supported). Local nested launches are detected at startup via the environment variable `EXPECTO_ACTIVE` that `expecto` sets for its child process, resulting in an error exit (§5.2). This is because the outer expecto would capture the hotkey first, producing hard-to-notice misbehavior such as LLM responses carrying over the scrollback of a past session. Launching `expecto` on an ssh destination cannot be detected because the environment variable does not propagate (remains out of scope).

---

## 11. Future Extension Candidates (not implemented in v0.1)

- Improved completion-detection accuracy via shell integration (OSC 133 / PS1 markers) (only for one's own local shell).
- "Verification via recording / screenshots" features like pty-mcp / agent-tty.
- Concurrent management of multiple sessions.

---

## 12. Prototype Implementation Scope (v0.1 MVP)

- passthrough mode (raw tty relay, SIGWINCH propagation)
- Entering / cancelling query mode via the hotkey
- The execution loop (command execution → settle detection → LLM decision → repeat)
- Control-character transmission from the LLM (Ctrl-C etc.)
- Interim report to the LLM when the settle threshold is exceeded
- Continuation confirmation at step-count checkpoints, user interruption via Ctrl-C
- Dangerous-command confirmation gate (risk classification + backstop + confirmation)
- Clean-text screen capture via pyte and context provisioning to the LLM
- Launching an arbitrary command via the `--shell` option (including connecting to ssh or router CLIs)

### Not included:
- The issues and extensions listed in §10 and §11

## 13. Expected Directory Layout

```
expecto/
├── pyproject.toml
├── src/
│   └── expecto/
│       ├── __init__.py
│       ├── cli.py            # entry point, argument parsing, asyncio.run
│       ├── tty_controller.py # raw-mode setup/restore, window size get/apply
│       ├── pty_manager.py    # pty allocation, child launch
│       ├── io_relay.py       # asyncio always-on pump, execution task management
│       ├── input_router.py   # hotkey detection, state machine
│       ├── response_filter.py # terminal response sequence recognition (for forwarding while stdin is held)
│       ├── screen_buffer.py  # pyte integration, history management
│       ├── query_ui.py       # miniprompt drawing
│       ├── llm_client.py     # Chat Completions calls, conversation history keeping and compaction
│       ├── executor.py       # execution loop control
│       ├── settle.py         # settle detection (wait_settle coroutine)
│       ├── dangerous.py      # dangerous-command regex backstop
│       ├── mcp_client.py     # MCP servers: connection and tool dispatch
│       ├── skills.py         # Agent Skills: discovery and SKILL.md parsing
│       └── config.py         # configuration loading
└── tests/
```

## 14. Dependencies (expected)

- `pyte` — terminal emulation / clean-text conversion
- `openai` — LLM calls (official OpenAI Python SDK, AsyncOpenAI)
- `python-dotenv` — loading `.env` (for the API key)
- `mcp` — MCP client SDK (`ClientSession` and the stdio / SSE / streamable-HTTP transports)
- `pytest` / `pytest-asyncio` — test framework (dev dependency)
- Standard library: `asyncio`, `pty`, `termios`, `tty`, `os`, `signal`, `fcntl`, `struct`

## 15. Testing / Type-Checking Policy

- Use `pytest` as the test framework.
- **Criteria for writing test cases**: test the logic implemented for `expecto`. Do not write test cases that merely confirm the behavior of the standard library or existing libraries (e.g., verifying that argparse itself can parse options).
- Use `pytest-asyncio` for testing async components (`asyncio_mode = "auto"` is already set in `pyproject.toml`; no decorator is needed on test coroutines).
- Use `ty` for type checking (`uv run ty check`). Configuration lives in `[tool.ty]` in `pyproject.toml`.
- Add type hints to all code. Write explicit types for public interfaces and avoid using `Any`.
- Create corresponding tests under `tests/` when implementing each component. Also update tests appropriately when changing existing components.
- For tests of tty/pty operations, create a pseudo-terminal pair with `pty.openpty()`.
- Components that depend on `sys.stdin`, such as IORelay, must allow the fd to be injected externally to ensure testability.
- **End-to-end tests** (`e2e-tests/`, `make e2e-test`): run a real expecto against a real LLM inside a podman container (ubuntu 24.04). The image bundles expecto, llama.cpp's `llama-server` (OpenAI-compatible API), and a small GGUF model downloaded at build time (`MODEL_URL` build arg; default Qwen3-0.6B). pytest runs inside the container: a session fixture starts llama-server and waits for `/health`; each test drives expecto through a pty (hotkey, query, output assertions) with a per-test config.toml pointing `base_url` at the local server. The host needs only podman. Slow by nature — kept out of the regular unit-test loop.
