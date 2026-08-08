# Configuration (config.toml / ~/.expecto) Design Document

- Status: implemented
- Parent document: `DESIGN.md` (§7)
- This document is the detailed design of the files under `~/.expecto/` and of configuration loading and precedence.

---

## 1. Policy

- **`.env` holds secrets; `config.toml` holds behavior settings** — a strict separation of roles. The only environment variable expecto reads is `OPENAI_API_KEY`; every other setting is made via `config.toml` (and CLI flags).
- For settings that have defaults on the OpenAI API / SDK side (`base_url`, `reasoning_effort`), when unset expecto **omits the parameter entirely and defers to the SDK / API default**. `model` has no API-side default, so expecto carries its own default (`gpt-5.4`).
- expecto never writes to this directory (read-only; it does not auto-generate files either).

## 2. Directory Layout

```
~/.expecto/                   # or the directory given by --config-dir
├── .env                      # OPENAI_API_KEY only (secret)
├── config.toml               # behavior settings (this document)
├── AGENTS.md                 # user instructions appended to the system prompt (DESIGN.md §8)
└── dangerous_patterns.txt    # additional dangerous-command regexes (DESIGN.md §5.8)
```

- Path resolution is centralized in `config.config_dir(override)`: the `--config-dir` path when given, otherwise `~/.expecto`. Every default path (`config.toml` / `.env` / `AGENTS.md` / `dangerous_patterns.txt`) is derived from it, and the per-file flags (`-c` / `-e` / `-a` / `--dangerous-patterns-file`) still override individual files on top. A `--config-dir` path that does not exist is a startup error.
- No environment variable selects the directory. `$XDG_CONFIG_HOME` is deliberately **not** consulted: it would duplicate `--config-dir`, and §8-1's policy is that `OPENAI_API_KEY` is the only environment variable expecto reads. There is no fallback to the pre-`~/.expecto` location either — one resolution path only.
- `.env` loading: the file given by `--env PATH` → without that flag, `<config_dir>/.env` if it exists. The current directory is not searched, so an installed expecto behaves the same wherever it is launched from. An already-set environment variable always takes precedence over the file value.
- `--env` cannot be set in config.toml: the `.env` file is loaded before the config file is read.
- If `OPENAI_API_KEY` is unset (neither in the environment nor in `.env`), expecto prints an error message at startup and exits.

## 3. config.toml Schema

All keys are optional. Only the keys that are written override the defaults. For string keys, the empty string `""` is equivalent to "unset". Path keys (`log_file` / `raw_io_log_file` / `dangerous_patterns_file` / `agents_md_file`) expand `~`.

```toml
shell = "/usr/bin/fish"           # default: $SHELL (falls back to /bin/bash)
hotkey = "ctrl-/"                 # "ctrl-" + one character (§5)
confirm_level = "destructive"     # destructive | mutating | none
scrollback_lines = 5000
settle_ms = 500
threshold_ms = 30000
max_steps = 15
log_file = ""                     # JSONL log. default: disabled
raw_io_log_file = ""              # raw I/O log for debugging. default: disabled
dangerous_patterns_file = ""      # default: <config_dir>/dangerous_patterns.txt
agents_md_file = ""               # default: <config_dir>/AGENTS.md

model = "gpt-5.4"                 # expecto's own default
base_url = ""                     # if unset, SDK default (api.openai.com)
reasoning_effort = ""             # none|minimal|low|medium|high|xhigh|max
                                  # if unset, reasoning is omitted from the request

compact_thresh = 100000           # conversation-history compaction threshold (prompt_tokens)
new_output_max_chars = 20000      # max characters of new output diff between queries
```

- No tables (sections) — flat keys only.
- **API keys are not accepted**: if `api_key` is present, warn "use the `OPENAI_API_KEY` environment variable" and ignore it. Local LLMs and the like do not validate keys, so changing `base_url` alone suffices.

## 4. Precedence

```
built-in defaults < config.toml < CLI flags
```

- Implementation: `ExpectoConfig.load(config_file) -> (config, warnings)` — build an `ExpectoConfig` from the defaults, apply the TOML, and resolve `OPENAI_API_KEY` from the environment. CLI flag overrides are applied by `cli.main()` as before.
- `--config PATH` changes the location of the config file (default: `<config_dir>/config.toml`); `--config-dir PATH` moves the whole directory the defaults are resolved against (§2).

## 5. hotkey Notation

- `"ctrl-" + one character` (case and surrounding whitespace ignored): `ctrl-a` through `ctrl-z`, plus `ctrl-/` (0x1F, the default), `ctrl-]`, `ctrl-\`, `ctrl-^`, `ctrl-@`, `ctrl-_`.
- Values that collide with `ctrl-c` (execution interrupt) / `ctrl-m` (Enter) / Esc are rejected: warn and revert to the default.
- Parsing is done by `config.parse_hotkey()` (raises ValueError on invalid input).

## 6. Error Handling (warn-and-continue)

Follows the existing policy of `dangerous_patterns.txt` (invalid lines are warned about and skipped; startup continues). Warnings are printed to stderr at startup in the form `expecto: warning: <path>: <key>: ...`.

| Situation | Behavior |
|------|------|
| Directory given via `--config-dir` does not exist | **Exit with error** (so a mistyped path is noticed) |
| config.toml at the default path does not exist | Normal (all defaults) |
| File explicitly specified via `--config` does not exist | **Exit with error** (so a mistyped path is noticed) |
| `.env` at the default path does not exist | Normal (the key comes from the environment) |
| File explicitly specified via `--env` does not exist | **Exit with error** (same policy as `--config`) |
| AGENTS.md at the default path (or the `agents_md_file` path) does not exist | Normal (no user instructions are added) |
| File explicitly specified via `-a` / `--agents-md` does not exist | **Exit with error** (same policy as `--config`) |
| TOML syntax error | Ignore the entire file with a warning |
| Unknown key | Warn (typo detection), ignore |
| Invalid type or value (negative integer, value outside the enum, etc.) | Warn for that key only and keep the default (the default for `confirm_level` is the safest option, `destructive`, so the fallback errs on the safe side) |
| `api_key` key | Warn and ignore (§3) |
| `OPENAI_API_KEY` unset | **Exit with error** (pointing to where to set it) |

## 7. dangerous_patterns.txt (unchanged)

The format (one regex per line, `#` comments, case-insensitive) and the semantics — "additions to the built-in patterns, escalation to destructive only" — remain as in DESIGN.md §5.8. The only changes in this design are that the default path is now resolved via `config_dir()` (so `--config-dir` moves it) and that the path can also be specified via `dangerous_patterns_file` in config.toml.

## 8. Design-Decision Record

1. **Configuration is consolidated into config.toml**: scattering settings across environment variables / CLI hurts clarity, so no environment variables are introduced other than the secret (`OPENAI_API_KEY`). Precedence has exactly three levels (defaults < file < CLI).
2. **The blacklist is not merged into config.toml**: writing regexes through TOML escaping invites mistakes, so the one-regex-per-line format of `dangerous_patterns.txt` is kept. The mechanism is not duplicated.
3. **warn-and-continue**: expecto is a terminal wrapper; warning and running with safe defaults is better than failing to start over a config typo. This presumes the fallback (the default value) is always on the safe side.
4. **tomllib (stdlib)**: Python 3.11+ is assumed, so TOML can be read without adding a dependency. No writing is done, so tomli-w and the like are unnecessary.
