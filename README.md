
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

`OPENAI_API_KEY` is the only environment variable `expecto` reads. Set it
in your environment:

```console
export OPENAI_API_KEY="[API KEY HERE]"
```

or write it to `~/.config/expecto/.env`, which `expecto` loads at startup:

```
OPENAI_API_KEY=[API KEY HERE]
```

Use `--env PATH` to load a different env file. Values already set in the
environment always win over the file.


* Run

```console
expecto
```

Press `Ctrl-/` (the default hotkey) to query your AI agent.

Everything else — model, `base_url`, hotkey, confirmation level, and so on
— is configured in `~/.config/expecto/config.toml`:

```toml
model = "gpt-5.4"
hotkey = "ctrl-/"
confirm_level = "destructive"
```



## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic.  Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also
anything with a CLI, such as network devices.

