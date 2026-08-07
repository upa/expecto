

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
set it in the environment or in `~/.config/expecto/.env`.

```toml
# LLM connection
model = "gpt-5.4"               # model name
base_url = ""                   # API endpoint (default: OpenAI)
reasoning_effort = ""           # none/minimal/low/medium/high/xhigh/max
summary_model = ""              # model for the working summary (default: same as model)

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
summary_trigger_lines = 500     # scrolled-out lines that trigger a summary update
summary_max_input_lines = 2000  # max lines fed into one summary update

# Files
log_file = ""                   # JSONL log of queries and LLM requests/responses
raw_io_log_file = ""            # raw terminal I/O log (for debugging)
dangerous_patterns_file = ""    # extra dangerous-command regex file
```


## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic. Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also anything
with a CLI, such as network devices.

