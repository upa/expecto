# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`jou` is a transparent TTY wrapper CLI tool that sits between the terminal and user I/O. It relays input/output to a child process (default: `$SHELL`) in passthrough mode, and on a hotkey enters query mode where the user types a natural-language query. The query plus recent terminal history is sent to an LLM, which returns a command to inject into the PTY.

Key differentiator: wraps the **tty itself**, not a specific shell — works over ssh, vendor CLI (routers/switches), etc.

Two hotkeys, two modes — the hotkey pressed determines the execution mode:
- **completion mode** (`Ctrl-^`): LLM suggests one command, injected without Enter — user reviews and executes
- **auto mode** (`Ctrl-]`): LLM loops command-execute-observe until goal is met, with idle-based settle detection. LLM can also send control characters (e.g., Ctrl-C) to manage long-running commands.

## Status

Pre-implementation. `DESIGN.md` is the authoritative specification (v0.1 prototype scope). No code exists yet. Implementation is two-phase: Phase 1 = completion mode, Phase 2 = auto mode. Design should accommodate auto mode from Phase 1.

## Tech Stack

- Python 3.11+
- Target OS: macOS, Linux
- Key dependencies: `pyte` (terminal emulation), `openai` (LLM SDK with Structured Outputs)
- Heavy use of stdlib: `pty`, `termios`, `tty`, `select`, `os`, `signal`, `fcntl`
- Build/packaging: `pyproject.toml` (planned)
- Config: `~/.config/jou/config.toml`, CLI flags override
- API key: `OPENAI_API_KEY` env var

## Planned Architecture

State machine: `PASSTHROUGH` → (completion hotkey) `QUERY_COMPLETION` / (auto hotkey) `QUERY_AUTO` → `LLM_COMPLETION` or `LLM_AUTO` → `COMPLETION_INJECT` or `AUTO_LOOP` → back to `PASSTHROUGH`. Query input UI is shared; only the post-LLM dispatch differs.

Core components (each a separate module under `src/jou/`):
- `tty_controller` — raw mode, termios save/restore, SIGWINCH propagation
- `pty_manager` — pty allocation, child process fork/exec
- `io_relay` — main select loop, bidirectional byte relay
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
- Dangerous command blocklist with user confirmation before execution in auto mode
- Auto mode has max step count (default 10) and Ctrl-C interrupt
- LLM response format enforced via OpenAI Structured Outputs, not system prompt
- Design document is in Japanese; code and comments should follow whatever language convention is established
