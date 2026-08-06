
# expecto

Summon an AI agent into your terminal. 

`expecto` wraps the tty, not your shell. It silently follows
everything on your terminal, and when you ask the AI agent, the agent
runs commands on your behalf until the goal is done.



## How to use

* Install

```console
git clone https://github.com/upa/expecto
cd expecto && uv tool install .
```

* Set API key:

```
export OPENAI_API_KEY="[API KEY HERE]"
```

`expecto` loads `~/.config/expecto/.env` by default.


* Run

```console
expecto
```

Press `Ctrl-/` (the default hotkey) to query your AI agent.



## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic.  Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also
anything with a CLI, such as network devices.

