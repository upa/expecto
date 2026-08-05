# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`jou` is a transparent TTY wrapper CLI tool that sits between the terminal and user I/O. It relays input/output to a child process (default: `$SHELL`) in passthrough mode, and on a hotkey enters query mode where the user types a natural-language query. The query plus recent terminal history is sent to an LLM, which returns a command to inject into the PTY.

Key differentiator: wraps the **tty itself**, not a specific shell — works over ssh, vendor CLI (routers/switches), etc.

Two hotkeys, two modes — the hotkey pressed determines the execution mode:
- **completion mode** (`Ctrl-/`): LLM suggests one command, injected without Enter — user reviews and executes
- **auto mode** (`Ctrl-]`): LLM loops command-execute-observe until goal is met, with idle-based settle detection. LLM can also send control characters (e.g., Ctrl-C) to manage long-running commands.

## Status

Phase 1 (completion mode) complete. Phase 2 (auto mode) core loop complete (LLMClient with Responses API function calling — `run_command` / `send_control` tools, ModeExecutor tool-call loop, idle-timer settle detection). Working Summary implemented (`working_summary.py`, see `DESIGN-WorkingSummary.md`). Concurrency is a single-threaded asyncio event loop — one always-on relay pump, mode executions as cancellable tasks, no threads/locks (see `DESIGN-Async.md`). `DESIGN.md` is the authoritative specification.

### TODO

- **Secret masking**: LLM に送るスクリーン履歴からパスワード・APIキー等を簡易マスキング
- **Prompt engineering**: completion mode / auto mode のシステムプロンプト内容の精査・改善（現状は最低限の指示のみ）

## Tech Stack

- Python 3.11+
- Target OS: macOS, Linux
- Key dependencies: `pyte` (terminal emulation), `openai` (LLM SDK, AsyncOpenAI with Structured Outputs)
- Heavy use of stdlib: `asyncio`, `pty`, `termios`, `tty`, `os`, `signal`, `fcntl`
- Build/packaging: `pyproject.toml` (planned)
- Config: `~/.config/jou/config.toml`, CLI flags override
- API key: `OPENAI_API_KEY` env var

## Planned Architecture

State machine: `PASSTHROUGH` → (completion hotkey) `QUERY_COMPLETION` / (auto hotkey) `QUERY_AUTO` → `LLM_COMPLETION` or `LLM_AUTO` → `COMPLETION_INJECT` or `AUTO_LOOP` → back to `PASSTHROUGH`. Query input UI is shared; only the post-LLM dispatch differs.

Core components (each a separate module under `src/jou/`):
- `tty_controller` — raw mode, termios save/restore, SIGWINCH propagation
- `pty_manager` — pty allocation, child process fork/exec
- `io_relay` — always-on asyncio relay pump (loop.add_reader), bidirectional byte relay, mode-task management
- `input_router` — per-byte state machine, dual hotkey detection, UTF-8 buffering
- `screen_buffer` — pyte-based screen capture + scrollback ring buffer for LLM context
- `query_ui` — ANSI-based miniprompt overlay on bottom line
- `llm_client` — OpenAI API with Structured Outputs (response_format)
- `mode_executor` — completion/auto mode logic
- `settle` — idle-timer output settle detection (no PS1 dependency)
- `config` — TOML config + CLI arg merging
- `cli` — entry point, argparse

## Build & Test

- Package manager: `uv`
- Install: `uv sync`
- Run tests: `uv run pytest tests/ -v`
- Run single test: `uv run pytest tests/test_foo.py::TestClass::test_name -v`
- Type check: `uv run ty check`
- Type check single path: `uv run ty check src/<path>`
- Tests live in `tests/` and mirror `src/jou/` modules. Create or update tests alongside every code change.

## Code Style

- All code must use type hints. Prefer explicit types over type inference for public interfaces.
- Avoid using `Any` wherever possible.
- Run `uv run ty check` to verify types before completing a change.

## Design Constraints

- No shell-specific dependencies (PS1, readline) — must work over ssh/vendor CLI
- Settle detection is idle-timer only (no prompt markers). When settle threshold is exceeded without settling, current output is sent to LLM for decision (not treated as timeout error)
- Dangerous command gate in auto mode (3 layers): LLM self-classifies each command's risk (`read_only`/`mutating`/`destructive` in the run_command schema); a deterministic regex backstop (builtin + `~/.config/jou/dangerous_patterns.txt`) escalates matches to destructive (escalate-only); commands at/above `--confirm-level` (default: destructive) require y/n confirmation. Decline feeds back to the LLM as a tool result, not an abort
- Auto mode asks user confirmation (continue? y/n) every `max_auto_steps` steps (default 15, checkpoint interval, keeps the response chain on continue) and supports Ctrl-C interrupt
- LLM response format enforced via OpenAI Structured Outputs, not system prompt
- Design document is in Japanese; code and comments should follow whatever language convention is established
