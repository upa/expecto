

# expecto 👻 — Summon an AI agent into your terminal

An AI agent summoned by `expecto` silently follows everything on your
terminal, and when you ask the AI agent, it runs commands on your
behalf until the goal is done.


https://github.com/user-attachments/assets/cb0954d9-463a-47a1-9a19-5c9f1441a789



## How to use

* Install

```console
uv tool install git+https://github.com/upa/expecto
```

* Set an API key and run

```console
export OPENAI_API_KEY="[API KEY HERE]"

# Execute the `expecto` command
expecto
```

Press `Ctrl-/` (the default hotkey) to query your AI agent.

`-u` / `--base-url` overrides the endpoint, and `-m` / `--model`
overrides the model, for a single run. For example:

```console
OPENAI_API_KEY=ollama expecto -u http://localhost:11434/v1 -m gpt-oss:20b
```

See [Configuration](#configuration) for all available options.


## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic. Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also anything
with a CLI, such as network devices.

`expecto` keeps a single LLM conversation for the whole pty session:
the context window lasts from launching `expecto` until you exit.
When you send a query, the terminal output produced since the previous
query is sent along with it (the amount is capped, configurable via
`new_output_max_chars`), so the agent also knows what you did by hand
in between. When the conversation grows past a threshold
(`compact_thresh`), it is compacted: summarized so the session can
keep going without exhausting the context window.



## Configuration

`expecto` keeps its files in `~/.expecto/` — `config.toml`, `.env`,
`AGENTS.md`, and `dangerous_patterns.txt`. `--config-dir PATH` points all
of them at another directory; `-c` / `-e` / `-a` /
`--dangerous-patterns-file` override single files.

`expecto` reads `~/.expecto/config.toml` (or a file given by
`--config`). All keys are optional; command-line flags override the
file. `OPENAI_API_KEY` is intentionally *not* read from this file —
set it in the environment or in `~/.expecto/.env` (or the file
given by `--env`).

```toml
# LLM connection
model = "gpt-5.4"               # model name
base_url = ""                   # API endpoint (default: OpenAI)
reasoning_effort = ""           # none/minimal/low/medium/high/xhigh/max

# Behavior
shell = ""                      # child command (default: $SHELL)
hotkey = "ctrl-/"               # hotkey to enter query mode, "ctrl-<char>"
confirm_level = "destructive"   # risk level that asks y/n: destructive/mutating/none
max_steps = 15                  # steps between "continue?" checkpoints
settle_ms = 500                 # idle time to regard command output as settled
threshold_ms = 30000            # report unsettled output to the LLM after this long

# Context for the LLM
scrollback_lines = 5000         # scrollback buffer capacity
compact_thresh = 100000         # prompt tokens that trigger conversation compaction
new_output_max_chars = 20000    # max chars of between-query output sent to the LLM

# Files
log_file = ""                   # JSONL log of queries and LLM requests/responses
raw_io_log_file = ""            # raw terminal I/O log (for debugging)
dangerous_patterns_file = ""    # extra dangerous-command regex file
agents_md_file = ""             # AGENTS.md (default: ~/.expecto/AGENTS.md)
```


