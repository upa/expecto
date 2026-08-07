

# expecto 👻 — Summon an AI agent into your terminal

An AI agent summoned by `expecto` silently follows everything on your
terminal, and when you ask the AI agent, it runs commands on your
behalf until the goal is done.



## How to use

* Install

```console
git clone https://github.com/upa/expecto
cd expecto && uv tool install .
```

* Set API key and run

```console
export OPENAI_API_KEY="[API KEY HERE]"

# Execute the `expecto` command
expecto
```

Press `Ctrl-/` (the default hotkey) to query your AI agent.

Instead of exporting it, you can write the key to
`~/.config/expecto/.env`, which `expecto` loads at startup (use
`--env PATH` for a different file):

```
OPENAI_API_KEY=[API KEY HERE]
```

A key already set in the environment always wins over the file.


To change the model or the OpenAI API endpoint, put the following in
`~/.config/expecto/config.toml`

```toml
model = "gpt-5.6"
base_url = "[YOUR API ENDPOINT URL]"
```

See [Configuration](#configuration) for all available options.


## Configuration

`expecto` reads `~/.config/expecto/config.toml` (or a file given by
`--config`). All keys are optional; command-line flags override the
file. `OPENAI_API_KEY` is intentionally *not* read from this file —
set it in the environment or in `~/.config/expecto/.env` (or the file
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
history_lines = 500             # recent screen lines sent to the LLM
scrollback_lines = 5000         # scrollback buffer capacity
compact_thresh = 100000         # prompt tokens that trigger conversation compaction
new_output_max_chars = 20000    # max chars of between-query output sent to the LLM

# Files
log_file = ""                   # JSONL log of queries and LLM requests/responses
raw_io_log_file = ""            # raw terminal I/O log (for debugging)
dangerous_patterns_file = ""    # extra dangerous-command regex file
agents_md_file = ""             # AGENTS.md with your own instructions
```


## AGENTS.md

If `~/.config/expecto/AGENTS.md` exists (or a file given by `-a` /
`--agents-md`), its contents are appended to the system prompt. Use it for
what the agent cannot see on the screen — your hosts and their quirks,
device conventions, commands you prefer:

```markdown
- rtr1 and rtr2 are Junos devices; rtr3 runs Cisco IOS.
- Never commit a candidate config without showing me `show | compare` first.
- Answer in Japanese.
```

These instructions are additive: they cannot override the risk
classification and confirmation rules.


## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic. Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also anything
with a CLI, such as network devices.

