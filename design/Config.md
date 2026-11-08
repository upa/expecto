# Configuration (config.toml / ~/.expecto) Design Document

- Status: implemented
- Parent document: `DESIGN.md` (§7)
- This document is the detailed design of the files under `~/.expecto/` and of configuration loading and precedence.

---

## 1. Policy

- **`.env` holds secrets; `config.toml` holds behavior settings** — a strict separation of roles. The only setting expecto takes from an environment variable is `OPENAI_API_KEY`; every other setting is made via `config.toml` (and CLI flags). The environment variables involved are listed in §1.1.
- For settings that have defaults on the OpenAI API / SDK side (`base_url`, `reasoning_effort`), when unset expecto **omits the parameter entirely and defers to the SDK / API default**. `model` has no API-side default, so expecto carries its own default (`gpt-5.4`).
- expecto never writes to this directory (read-only; it does not auto-generate files either).

### 1.1 Environment Variables

Read by expecto itself:

| Variable | Use |
|------|------|
| `OPENAI_API_KEY` | API key; required (startup error when missing). Passed to the SDK explicitly. Rejected in config.toml (§3) |
| `SHELL` | Default child command when neither `--shell` nor `shell` is given (falls back to `/bin/bash`) |
| `EXPECTO_ACTIVE` | Set to `1` in the child's environment; an expecto started with it set refuses to run (no nested sessions) |
| `$NAME` / `${NAME}` | An MCP server's `token` written this way is resolved from the environment (MCP.md §2) |

Read implicitly by the OpenAI SDK — expecto never references these, but they take effect because the SDK falls back to them for parameters expecto leaves unset:

| Variable | Use |
|------|------|
| `OPENAI_BASE_URL` | Endpoint when neither `-u`/`--base-url` nor `base_url` is set (expecto passes `base_url=None`). Precedence: `-u` > `base_url` > `OPENAI_BASE_URL` > SDK default (api.openai.com) |
| `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_CUSTOM_HEADERS`, ... | Applied as the SDK defines them |

Values in `.env` are loaded into the process environment, so they reach the SDK as well (e.g. `OPENAI_BASE_URL` written in `.env`).

## 2. Directory Layout

```
~/.expecto/                   # or the directory given by --config-dir
├── .env                      # environment variables, normally OPENAI_API_KEY (secret; §1.1)
├── config.toml               # behavior settings (this document)
├── AGENTS.md                 # user instructions appended to the system prompt (DESIGN.md §8)
├── skills/                   # Agent Skills, one directory each (Skills.md)
└── dangerous_patterns.txt    # additional dangerous-command regexes (DESIGN.md §5.8)
```

- Path resolution is centralized in `config.config_dir(override)`: the `--config-dir` path when given, otherwise `~/.expecto`. Every default path (`config.toml` / `.env` / `AGENTS.md` / `skills/` / `dangerous_patterns.txt`) is derived from it, and the per-file flags (`-c` / `-e` / `-a` / `--dangerous-patterns-file`) still override individual files on top. A `--config-dir` path that does not exist is a startup error.
- No environment variable selects the directory. `$XDG_CONFIG_HOME` is deliberately **not** consulted: it would duplicate `--config-dir`, and §8-1's policy is that `OPENAI_API_KEY` is the only setting taken from an environment variable. There is no fallback to the pre-`~/.expecto` location either — one resolution path only.
- `.env` loading: the file given by `--env PATH` → without that flag, `<config_dir>/.env` if it exists. The current directory is not searched, so an installed expecto behaves the same wherever it is launched from. An already-set environment variable always takes precedence over the file value.
- `--env` cannot be set in config.toml: the `.env` file is loaded before the config file is read.
- If `OPENAI_API_KEY` is unset (neither in the environment nor in `.env`), expecto prints an error message at startup and exits.

## 3. config.toml Schema

All keys are optional. Only the keys that are written override the defaults. For string keys, the empty string `""` is equivalent to "unset". Path keys (`log_file` / `raw_io_log_file` / `dangerous_patterns_file` / `agents_md_file` / `skills_dir`) expand `~`.

```toml
shell = "/usr/bin/fish"           # default: $SHELL (falls back to /bin/bash)
hotkey = "ctrl-/"                 # "ctrl-" + one character (§5)
color = "90"                      # SGR parameters for expecto's own text (DESIGN.md §5.11)
query_color = ""                  # SGR parameters for the query prompt and its input. default: same as color
confirm_level = "destructive"     # destructive | mutating | none
scrollback_lines = 5000
settle_ms = 500
threshold_ms = 30000
llm_input_char_interval = -1     # ms between typed keys of run_command / send_keys (DESIGN.md §5.8)
                                 # 0: one key at a time, no wait; negative: all at once
log_file = ""                     # JSONL log. default: disabled
raw_io_log_file = ""              # raw I/O log for debugging. default: disabled
dangerous_patterns_file = ""      # default: <config_dir>/dangerous_patterns.txt
agents_md_file = ""               # default: <config_dir>/AGENTS.md
skills_dir = ""                   # default: <config_dir>/skills

model = "gpt-5.4"                 # expecto's own default
base_url = ""                     # if unset, SDK default (api.openai.com)
reasoning_effort = ""             # none|minimal|low|medium|high|xhigh|max
                                  # if unset, reasoning is omitted from the request

model_context_window = 128000     # the model's context window (tokens). default: 128000 (§8-6)
compact_thresh_percent = 75       # compact the conversation at this % of model_context_window (1-100)
new_output_max_chars = 20000      # max characters of new output diff between queries
tool_output_max_percent = 4       # max size of one tool result, as % of model_context_window (1-100; §8-7)

mcp_connect_timeout_ms = 10000    # per-server MCP connection timeout

[mcp.SERVER1]                     # MCP servers to connect to (MCP.md §2)
type = "stdio"                    # stdio | sse | streamable-http
command = "npx"
args = ["arg1", "arg2"]
```

- Flat keys only, with one exception: the `[mcp.<name>]` tables that declare MCP servers (**`MCP.md` §2**). A list of servers is naturally nested, and flattening it would be worse to read and to write.
- **API keys are not accepted**: if `api_key` is present, warn "use the `OPENAI_API_KEY` environment variable" and ignore it. Local LLMs and the like do not validate keys, so changing `base_url` alone suffices.

## 4. Precedence

```
built-in defaults < config.toml < CLI flags
```

- Implementation: `ExpectoConfig.load(config_file, config_dir) -> (config, warnings)` — build an `ExpectoConfig` from the defaults, apply the TOML, and resolve `OPENAI_API_KEY` from the environment. CLI flag overrides are applied by `cli.main()` as before.
- `--config PATH` changes the location of the config file (default: `<config_dir>/config.toml`); `--config-dir PATH` moves the whole directory the defaults are resolved against (§2).

## 5. hotkey Notation

- `"ctrl-" + one character` (case and surrounding whitespace ignored): `ctrl-a` through `ctrl-z`, plus `ctrl-/` (0x1F, the default), `ctrl-]`, `ctrl-\`, `ctrl-^`, `ctrl-@`, `ctrl-_`.
- Values that collide with `ctrl-c` (execution interrupt) / `ctrl-m` (Enter) / Esc are rejected: warn and revert to the default.
- Parsing is done by `config.parse_hotkey()` (raises ValueError on invalid input).

## 6. Error Handling (warn-and-continue)

Follows the existing policy of `dangerous_patterns.txt` (invalid lines are warned about and skipped; startup continues). Warnings are printed to stderr at startup in the form `[expecto: warning: <path>: <key>: ...]` (the plain `expecto: warning: ...` form when stderr is not a terminal — see DESIGN.md §5.11).

| Situation | Behavior |
|------|------|
| Directory given via `--config-dir` does not exist | **Exit with error** (so a mistyped path is noticed) |
| config.toml at the default path does not exist | Normal (all defaults) |
| File explicitly specified via `--config` does not exist | **Exit with error** (so a mistyped path is noticed) |
| `.env` at the default path does not exist | Normal (the key comes from the environment) |
| File explicitly specified via `--env` does not exist | **Exit with error** (same policy as `--config`) |
| AGENTS.md at the default path (or the `agents_md_file` path) does not exist | Normal (no user instructions are added) |
| The skills directory does not exist | Normal (no skills are offered) |
| A skill directory is unusable (no SKILL.md, no frontmatter, no description) | Warn and skip **that skill only** (Skills.md §2) |
| File explicitly specified via `-a` / `--agents-md` does not exist | **Exit with error** (same policy as `--config`) |
| TOML syntax error | Ignore the entire file with a warning |
| Unknown key | Warn (typo detection), ignore |
| Invalid type or value (negative integer, value outside the enum, etc.) | Warn for that key only and keep the default (the default for `confirm_level` is the safest option, `destructive`, so the fallback errs on the safe side) |
| `api_key` key | Warn and ignore (§3) |
| Invalid `[mcp.*]` entry (unknown type, missing required key, unset token variable) | Warn and drop **that server only**; the rest of the session is unaffected (MCP.md §2) |
| `OPENAI_API_KEY` unset | **Exit with error** (pointing to where to set it) |

## 7. dangerous_patterns.txt (unchanged)

The format (one regex per line, `#` comments, case-insensitive) and the semantics — "additions to the built-in patterns, escalation to destructive only" — remain as in DESIGN.md §5.8. The only changes in this design are that the default path is now resolved via `config_dir()` (so `--config-dir` moves it) and that the path can also be specified via `dangerous_patterns_file` in config.toml.

## 8. Design-Decision Record

1. **Configuration is consolidated into config.toml**: scattering settings across environment variables / CLI hurts clarity, so no environment variables are introduced as settings other than the secret (`OPENAI_API_KEY`). Variables the OpenAI SDK reads on its own (§1.1) are not expecto settings and are left to the SDK. Precedence has exactly three levels (defaults < file < CLI).
2. **The blacklist is not merged into config.toml**: writing regexes through TOML escaping invites mistakes, so the one-regex-per-line format of `dangerous_patterns.txt` is kept. The mechanism is not duplicated.
3. **warn-and-continue**: expecto is a terminal wrapper; warning and running with safe defaults is better than failing to start over a config typo. This presumes the fallback (the default value) is always on the safe side.
4. **tomllib (stdlib)**: Python 3.11+ is assumed, so TOML can be read without adding a dependency. No writing is done, so tomli-w and the like are unnecessary.
5. **No step limit (`max_steps` removed)**: the execution loop used to ask `continue? (y/n)` every `max_steps` steps (default 15). It was removed rather than defaulted to off: long-running tasks are a normal use of an agent, a step count is a poor proxy for runaway behavior, and the user watching the screen with Ctrl-C at hand already covers the case. A leftover `max_steps` in config.toml is warned about as an unknown key.
6. **The context window is configured, not discovered**: OpenAI-compatible APIs have no common way to report a model's context window — OpenAI's `/v1/models` does not carry it, and the backends that do each use their own endpoint and field (llama.cpp `/props`, vLLM `max_model_len`, ollama `/api/ps` only once the model is loaded, LM Studio `/api/v1/models`). Probing them in turn at startup would issue requests that are bound to fail on every other backend at every session start, so expecto does not ask. `model_context_window` is set by the user; when it is not, `DEFAULT_CONTEXT_WINDOW` (128000, the most common window among current models) is assumed. The session start line shows the value and its origin (`ctxlen: 128000 by default` / `by config`). `compact_thresh_percent` replaced the former token-count `compact_thresh` so the threshold follows the window; its default 75 leaves headroom for the tool output a single query adds before the end-of-query check and for the compaction request itself, and 75% of the default window (96000) keeps the former 100000-token behavior.
7. **Tool results are capped at a share of the context window**: a `run_command` result used to be bounded only by `scrollback_lines` (up to about 400k characters at 80 columns), so one `cat` of a large file could overflow the window in the middle of a query, before the end-of-query compaction check ever ran. Each `run_command` / `send_keys` / MCP result is now cut to `tool_output_max_percent` of `model_context_window`, converted at `CHARS_PER_TOKEN` (4) characters per token, keeping the beginning and the end (DESIGN.md §5.7). The default 4% matches what other agents use (Codex CLI: 10,000 tokens of a 272k window, Claude Code: about 30,000 characters of 200k, both about 3.7%), and gives 20,480 characters at the default window, in line with `new_output_max_chars`.
