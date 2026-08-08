# MCP Client Design Document

- Status: implemented
- Parent document: `DESIGN.md` (§5.7, §7)
- This document is the detailed design of expecto acting as an **MCP
  client**: connecting to the MCP servers listed in config.toml and
  exposing their tools to the LLM alongside `run_command` /
  `send_control`.

---

## 1. Purpose

expecto drives a terminal, but a goal often needs information the
terminal cannot provide — an issue tracker, a documentation index, an
inventory database. MCP servers are how the user plugs those in. The
servers are configured once, connected at session start, and their tools
join the same function-calling loop the terminal tools already use, so
the LLM chooses between "run a command" and "call an MCP tool" with one
mechanism.

## 2. Configuration

Servers are declared as `[mcp.<name>]` tables in config.toml. The name is
the user's label and appears in tool names (§4).

```toml
[mcp.SERVER1]
type = "stdio"
command = "npx"
args = ["arg1", "arg2"]
env = { FOO = "bar" }             # optional

[mcp.SERVER2]
type = "sse"
url = "http://localhost:9000/sse"
token = "TOKEN"                   # optional → Authorization: Bearer TOKEN

[mcp.SERVER3]
type = "streamable-http"
url = "http://localhost:9001/mcp"
token = "$MY_TOKEN"               # environment variable reference
```

| key | transports | meaning |
|-----|-----------|---------|
| `type` | all | `stdio` / `sse` / `streamable-http` (required) |
| `command` | stdio | executable to launch (required) |
| `args` | stdio | argument list |
| `env` | stdio | extra environment variables for the child |
| `url` | sse, streamable-http | server endpoint (required) |
| `token` | sse, streamable-http | bearer token |

- `[mcp.*]` is the one exception to Config.md §3's "flat keys only" rule;
  a nested table is the natural shape for a list of servers, and the
  alternative (a flat key per server per field) would be worse.
- **`token` may reference an environment variable**: a value that is
  exactly `$NAME` or `${NAME}` is replaced by that variable's value. This
  keeps the secret in `.env` rather than config.toml, per Config.md §1's
  separation of roles. A value in any other shape is used literally.
- **Validation is warn-and-continue, per server**: an entry with an
  unknown `type`, a missing required key, a wrongly typed value, or a
  token whose environment variable is unset is reported to stderr and
  **that server alone is dropped**. Unknown keys inside a server table are
  warned about (typo detection) and ignored. A bad entry never prevents
  expecto from starting.
- Parsing produces `config.mcp_servers: list[MCPServerConfig]` in the
  order written. `mcp_connect_timeout_ms` (top-level, default 10000)
  bounds each server's connection.

## 3. Connection lifecycle

`MCPManager` (`mcp_client.py`) owns this. It is created by `cli.main()`
only when servers are configured, and `run_session()` drives it.

- Servers are connected (for `stdio`, launched) **at session start,
  synchronously, before the child process is spawned and before the tty
  goes into raw mode**. The tool list must be fixed before the first LLM
  request (§4), connecting up front keeps the startup path linear, and
  warnings can still be printed as ordinary lines. The cost is that a
  slow server delays the shell appearing; a status line is printed so
  the wait is not mistaken for a hang.
- Each connection opens its transport, constructs a `ClientSession`, and
  runs `initialize()` then `list_tools()`, all under
  `mcp_connect_timeout_ms` (default 10s). **A failure at any step
  produces a warning on stderr and drops that server**; the session
  continues with whatever connected.
- The sessions stay open for the whole expecto session and are held on a
  `contextlib.AsyncExitStack`. The stack is entered and closed from
  `run_session()` — the same task — because the SDK's transports are
  anyio cancel scopes and cannot be exited from a different task.
- Everything runs on the single asyncio event loop; no threads are added
  (Async.md §1).
- **A stdio server's stderr goes to `/dev/null`.** The SDK's default of
  `sys.stderr` would write straight onto the user's screen, which is in
  raw mode. (Routing it to a debug log instead is future work; the JSONL
  log is not a candidate, since free-form text would break its format.)
- **A stdio child does not inherit expecto's environment.** It gets the
  SDK's minimal default environment (`PATH`, `HOME`, `SHELL`, …) plus the
  server's own `env` table, so `OPENAI_API_KEY` and the rest of expecto's
  environment are not handed to third-party servers.

## 4. Exposing the tools to the LLM

- **Naming**: `mcp__<server>__<tool>`. This cannot collide with
  `run_command` / `send_control` or across servers, and the origin of a
  call is readable in the JSONL log.
- **Schema**: the server's `inputSchema` is passed through as the
  function `parameters`, **without `strict`**. Arbitrary MCP schemas do
  not satisfy the strict-mode requirements (every property required,
  `additionalProperties: false`); expecto's own two tools keep
  `strict: true`.
- The description is prefixed with `[MCP server <name>]` so the model can
  tell where a tool comes from even when the server's own description is
  terse (or missing, in which case a generated one is used).
- **Prompt-cache invariant**: the tools definition must be identical on
  every request of a session (Conversation.md §2). MCP tools are
  discovered once at startup and never change afterwards, so the
  invariant holds — this is why connection precedes the first request.
  `LLMClient.set_mcp_tools()` is the one-shot hand-off: it is called
  after connecting and before the relay starts, and asserts that the
  conversation has not begun, since it also rewrites the system message.
- When at least one MCP tool exists, a paragraph is appended to the
  system prompt telling the model that `mcp__`-prefixed tools come from
  servers the user configured. Like the AGENTS.md section, it is fixed
  for the session.
- Each tool definition costs prompt tokens on every request (discounted
  by the cache). A server exposing many tools is the user's decision to
  make.

## 5. Calling a tool

`Executor._run_tool_call` gains a third branch beside `run_command` /
`send_control`:

```
show "[expecto: mcp <server>.<tool>]" on the outer tty in gray
await the MCP call (inside _thinking(), so the pty stays paused)
record the result text as the tool output
```

The arguments are handed to the server as the model produced them
(decoded from JSON, not validated against the schema): the server
validates its own input, and a rejection comes back as tool-output text.

- No `mark()` and no `wait_settle()`: an MCP call produces no terminal
  output, so there is nothing to settle.
- **Result conversion**: the text parts of `CallToolResult.content` are
  joined; non-text parts become a placeholder such as
  `[non-text content omitted: image/png]`. An `isError` result is
  prefixed with `Error: ` and returned as the tool output — like a
  declined command, a failure is information for the LLM, not an abort.
- **No confirmation gate.** The risk classification and the regex
  backstop cover terminal commands; MCP tools are not commands, and the
  user chose to configure the server. The safety property expecto does
  keep is visibility: every call is printed on the user's screen.
- A hung call is an ordinary await point, so Ctrl-C cancels the execution
  task. That path records a tool output for the call as well, keeping the
  conversation consistent (Conversation.md §4).

## 6. Design-Decision Record

1. **Connect synchronously at startup** rather than in the background.
   The tools definition has to be frozen before the first request, and a
   background connection would mean either a wait on the first query or a
   changing tool list. Startup latency is the accepted cost.
2. **Namespace tool names** instead of trusting servers not to collide.
   Two servers exposing `search` is unremarkable, and the LLM must be able
   to address both.
3. **Non-strict schemas for MCP tools.** Rejecting servers whose schemas
   are not strict-compatible would rule out most of them; the arguments
   are already validated by the server itself.
4. **A failed server is dropped, never fatal.** expecto is a terminal
   wrapper — an unreachable side service must not stop the user from
   getting a shell.

## 7. Future work

- Reconnecting a server that dies mid-session (currently its tool calls
  just fail until expecto is restarted).
- MCP resources and prompts (only tools are used today).
- Per-server tool filtering (`allow` / `deny` lists) when a server's tool
  count becomes a prompt-size problem.
