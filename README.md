

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


## Architecture

`expecto` is implemented as a transparent `pty` wrapper, so it
captures all terminal I/O below it. This is what makes it
shell-agnostic. Also, `expecto` can control remote machines through
SSH. Remote machines are not only Linux machines but also anything
with a CLI, such as network devices.

