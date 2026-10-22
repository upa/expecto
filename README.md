

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
overrides the model. For example:

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

`expecto` keeps its files in `~/.expecto/`. `--config-dir PATH` points
all of them at another directory.

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
color = "90"                    # SGR color of expecto's own text, e.g. "36" (cyan), "1;90" (bold gray)
confirm_level = "destructive"   # risk level that asks y/n: destructive/mutating/none
max_steps = 15                  # steps between "continue?" checkpoints (0 = never ask)
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
skills_dir = ""                 # skills (default: ~/.expecto/skills)

# MCP
mcp_connect_timeout_ms = 10000  # per-server connection timeout
```


## Skills

A skill is a procedure you write once for a recurring kind of task. Put
each one in `~/.expecto/skills/<name>/SKILL.md`, for example:

```markdown
---
name: junos-upgrade
description: Upgrade Junos on our edge routers. Use when asked to upgrade or reboot rtr1-rtr4.
---

# Junos upgrade

1. `request system snapshot` before anything else.
2. ...
```

Only the `description` goes into the agent's system prompt; when a
goal matches it, the agent calls `load_skill` and reads the rest; a
long procedure costs you nothing until it is relevant. Write the
description to say both what the skill covers and when to use it.



## MCP servers

Declare MCP servers as `[mcp.<name>]` tables in `config.toml`, and
`expecto` connects to them at startup and offers their tools to the agent
alongside the terminal:

```toml
[mcp.docs]
type = "stdio"
command = "npx"
args = ["-y", "@example/docs-mcp"]

[mcp.inventory]
type = "streamable-http"
url = "http://localhost:9001/mcp"
token = "$INVENTORY_TOKEN"       # "$NAME" reads the environment variable
```

`type` is `stdio`, `sse`, or `streamable-http`. Their tools appear to the
agent as `mcp__<server>__<tool>`, and every call is printed on your
screen as it happens. A server that fails to connect is reported and
skipped — expecto still starts.


