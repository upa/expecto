# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`expecto` is a transparent TTY wrapper CLI tool that sits between the terminal and user I/O. It relays input/output to a child process (default: `$SHELL`) in passthrough mode, and on a hotkey (`Ctrl-/`) enters query mode where the user types a natural-language query (goal). The query plus recent terminal history is sent to an LLM, which loops command-execute-observe against the terminal until the goal is met, with idle-based settle detection. The LLM can also send control characters (e.g., Ctrl-C) to manage long-running commands.

Key differentiator: wraps the **tty itself**, not a specific shell — works over ssh, vendor CLI (routers/switches), etc.

## Status

Core loop complete (LLMClient with Chat Completions function calling — `run_command` / `send_control` tools, Executor tool-call loop, idle-timer settle detection). The final response is plain text streamed to the terminal as it arrives (`chat.completions.stream()`, shown gray in `[...]`). A former one-shot "completion mode" has been removed; the command-execute-observe loop is expecto's sole behavior. The LLMClient owns the message history, which spans the whole session (append-only for prompt-cache hits) and is compacted transparently into a handoff summary past a token threshold (see `design/Conversation.md`). Concurrency is a single-threaded asyncio event loop — one always-on relay pump, executions as cancellable tasks, no threads/locks (see `design/Async.md`). `design/DESIGN.md` is the authoritative specification.

### TODO

- **Secret masking**: LLM に送るスクリーン履歴からパスワード・APIキー等を簡易マスキング
- **Prompt engineering**: システムプロンプト内容の精査・改善（現状は最低限の指示のみ）

## Tech Stack

- Python 3.11+
- Target OS: macOS, Linux
- Key dependencies: `pyte` (terminal emulation), `openai` (LLM SDK, AsyncOpenAI with Structured Outputs), `mcp` (MCP client SDK)
- Heavy use of stdlib: `asyncio`, `pty`, `termios`, `tty`, `os`, `signal`, `fcntl`
- Build/packaging: `pyproject.toml` (hatchling, `expecto` script entry point)
- Config: `~/.expecto/config.toml` (TOML, warn-and-continue; see `design/Config.md`), overridden by CLI flags. `OPENAI_API_KEY` is the only env var (required — startup error when missing; `.env` from cwd, falling back to `~/.expecto/.env`)

## Architecture

State machine (InputRouter): `PASSTHROUGH` → (hotkey) `QUERY` → (Enter) `EXECUTING` → back to `PASSTHROUGH` when the execution task finishes. Everything after query submit runs as a cancellable asyncio execution task — user Ctrl-C while executing cancels that task.

Core components (each a separate module under `src/expecto/`):
- `tty_controller` — raw mode, termios save/restore, window-size helpers (SIGWINCH itself is registered in `cli` via `loop.add_signal_handler`)
- `pty_manager` — pty allocation, child process fork/exec
- `io_relay` — always-on asyncio relay pump (loop.add_reader), bidirectional byte relay, execution-task management, settle/key-wait event hub
- `input_router` — per-byte state machine, hotkey detection, UTF-8 buffering, terminal-response forwarding
- `response_filter` — recognizes terminal→application response sequences (CPR/DA/OSC/DCS replies) in stdin so they reach the child while stdin is withheld in query/executing states (fish 4.x blocks on them at every prompt)
- `screen_buffer` — pyte-based screen capture + scrollback ring buffer for LLM context
- `query_ui` — gray inline miniprompt (`? `) drawn at the cursor position
- `llm_client` — Chat Completions via AsyncOpenAI; owns the session-long message history and its compaction
- `executor` — the command-execute-observe loop (async, cancellation-based interrupts)
- `settle` — idle-timer output settle detection (`wait_settle` coroutine, no PS1 dependency)
- `dangerous` — regex backstop for unmistakably catastrophic commands
- `skills` — Agent Skills from `<config_dir>/skills`: SKILL.md discovery/parsing; the `load_skill` tool serves the body (see `design/Skills.md`)
- `mcp_client` — MCP servers from `[mcp.*]`: connect at startup, expose their tools to the LLM as `mcp__<server>__<tool>`, dispatch calls (see `design/MCP.md`)
- `config` — config.toml loading (defaults < file < CLI flags), hotkey parsing, startup warnings (see `design/Config.md`)
- `cli` — entry point, argparse, asyncio.run

## Build & Test

- Package manager: `uv`
- Install: `uv sync`
- Run tests: `uv run pytest tests/ -v` (or `make test`)
- Run single test: `uv run pytest tests/test_foo.py::TestClass::test_name -v`
- Type check: `uv run ty check`
- Type check single path: `uv run ty check src/<path>`
- Tests live in `tests/` and mirror `src/expecto/` modules. Create or update tests alongside every code change.
- End-to-end tests: `make e2e-test` — builds a podman image (`e2e-tests/Dockerfile`: ubuntu 24.04 + expecto + llama.cpp `llama-server` + a small GGUF model) and runs `pytest e2e-tests` inside the container, driving a real expecto through a pty against the local LLM. Slow; not part of the regular test loop.

## Code Style

- All code must use type hints. Prefer explicit types over type inference for public interfaces.
- Avoid using `Any` wherever possible.
- Run `uv run ty check` to verify types before completing a change.

## Design Constraints

- No shell-specific dependencies (PS1, readline) — must work over ssh/vendor CLI
- Settle detection is idle-timer only (no prompt markers). When settle threshold is exceeded without settling, current output is sent to LLM for decision (not treated as timeout error)
- Dangerous command gate (3 layers): LLM self-classifies each command's risk (`read_only`/`mutating`/`destructive` in the run_command schema); a deterministic regex backstop (builtin + `~/.expecto/dangerous_patterns.txt`) escalates matches to destructive (escalate-only); commands at/above `--confirm-level` (default: destructive) require y/n confirmation. Decline feeds back to the LLM as a tool result, not an abort
- The execution loop asks user confirmation (continue? y/n) every `max_steps` steps (default 15, checkpoint interval, keeps the response chain on continue; 0 disables the checkpoint) and supports Ctrl-C interrupt
- Tool arguments are enforced via strict JSON Schema (+ Pydantic validation); the loop's final response is plain text (no Structured Outputs) so it can be streamed to the screen. Structured Outputs is still used for the compaction handoff summary
- Design documents, code, and comments are in English
