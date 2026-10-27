import argparse
import asyncio
import os
import signal
import sys

from dotenv import load_dotenv

from expecto.config import ExpectoConfig, default_env_file
from expecto.dangerous import DangerousCommandMatcher
from expecto.executor import Executor
from expecto.fdio import write_all
from expecto.input_router import InputRouter
from expecto.io_relay import IORelay
from expecto.llm_client import LLMClient, load_agents_md
from expecto.mcp_client import MCPManager
from expecto.pty_manager import NESTING_ENV_VAR, PTYManager
from expecto.query_ui import QueryModeUI
from expecto.screen_buffer import ScreenBuffer
from expecto.skills import load_skills
from expecto.style import set_color, set_query_color, sgr
from expecto.tty_controller import TTYController


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="expecto",
        description="Transparent TTY wrapper with LLM-powered command assistance",
    )
    parser.add_argument(
        "--shell",
        default=None,
        help="Child process command (default: $SHELL)",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help="Directory holding config.toml, .env, AGENTS.md and "
        "dangerous_patterns.txt (default: ~/.expecto)",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Config file path (default: <config-dir>/config.toml)",
    )
    parser.add_argument(
        "-e",
        "--env",
        default=None,
        help="Env file to load (default: <config-dir>/.env)",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Model to use (default: the config file's model)",
    )
    parser.add_argument(
        "-u",
        "--base-url",
        default=None,
        help="OpenAI API endpoint URL (default: the config file's base_url, "
        "or the SDK default)",
    )
    parser.add_argument(
        "--raw-io-log-file",
        default=None,
        help="Raw I/O log output path (for debugging)",
    )
    parser.add_argument(
        "-l",
        "--log-file",
        default=None,
        help="Log file for queries and LLM requests/responses (JSONL format)",
    )
    parser.add_argument(
        "--scrollback-lines",
        type=int,
        default=None,
        help="Scrollback buffer capacity in lines (default: 5000)",
    )
    parser.add_argument(
        "--confirm-level",
        choices=["destructive", "mutating", "none"],
        default=None,
        help="Minimum command risk that requires user confirmation "
        "(default: destructive)",
    )
    parser.add_argument(
        "-a",
        "--agents-md",
        default=None,
        help="AGENTS.md with user instructions for the system prompt "
        "(default: <config-dir>/AGENTS.md)",
    )
    parser.add_argument(
        "--dangerous-patterns-file",
        default=None,
        help="File with additional dangerous command regex patterns "
        "(default: <config-dir>/dangerous_patterns.txt)",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to run (after --)",
    )
    return parser.parse_args(argv)


def load_env(env_file: str | None = None, config_dir: str | None = None) -> None:
    """Load the env file given with --env, or <config-dir>/.env when it
    exists. Existing environment variables always take precedence over file
    values.

    An explicitly given env_file that does not exist raises
    FileNotFoundError; the default path is optional."""
    if env_file is not None:
        if not os.path.exists(env_file):
            raise FileNotFoundError(env_file)
        load_dotenv(env_file)
        return
    load_dotenv(default_env_file(config_dir))


def status(message: str) -> None:
    """Write one of expecto's own lines to the terminal, in the same gray
    bracketed form as its in-session messages. Redirected output gets the
    plain form, so no escape sequences land in a log file."""
    if sys.stderr.isatty():
        print(f"{sgr()}[expecto: {message}]\x1b[0m", file=sys.stderr)
    else:
        print(f"expecto: {message}", file=sys.stderr)


def warn(message: str) -> None:
    status(f"warning: {message}")


def fail(message: str) -> None:
    """Report a startup problem. The caller exits."""
    status(f"error: {message}")


def main(argv: list[str] | None = None) -> None:
    if os.environ.get(NESTING_ENV_VAR):
        fail(
            "already inside an expecto session; "
            "nested sessions are not supported",
        )
        sys.exit(1)

    args = parse_args(argv)

    if args.config_dir is not None and not os.path.isdir(args.config_dir):
        fail(f"config directory not found: {args.config_dir}")
        sys.exit(1)

    try:
        load_env(args.env, args.config_dir)
    except FileNotFoundError:
        fail(f"env file not found: {args.env}")
        sys.exit(1)

    try:
        config, config_warnings = ExpectoConfig.load(args.config, args.config_dir)
    except FileNotFoundError:
        fail(f"config file not found: {args.config}")
        sys.exit(1)
    set_color(config.color)
    set_query_color(config.query_color)
    for warning in config_warnings:
        warn(warning)

    if not config.openai_api_key:
        fail(
            "OPENAI_API_KEY is not set. Set it in the environment or in "
            f"{default_env_file(args.config_dir)}",
        )
        sys.exit(1)

    if args.model is not None:
        config.model = args.model
    if args.base_url is not None:
        config.openai_base_url = args.base_url
    if args.raw_io_log_file:
        config.raw_io_log_file = args.raw_io_log_file
    if args.log_file:
        config.log_file = args.log_file
    if args.scrollback_lines is not None:
        config.scrollback_lines = args.scrollback_lines
    if args.confirm_level is not None:
        config.confirm_level = args.confirm_level
    if args.dangerous_patterns_file is not None:
        config.dangerous_patterns_file = args.dangerous_patterns_file
    if args.agents_md is not None:
        if not os.path.exists(args.agents_md):
            fail(f"AGENTS.md not found: {args.agents_md}")
            sys.exit(1)
        config.agents_md_file = args.agents_md

    command: str | list[str]
    if args.command:
        command = args.command
    elif args.shell:
        command = args.shell
    else:
        command = config.shell

    tty_ctrl = TTYController()
    pty_mgr = PTYManager()

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    screen_buffer = ScreenBuffer(
        scrollback_lines=config.scrollback_lines,
    )
    query_ui = QueryModeUI(stdout_fd, get_cursor_col=screen_buffer.get_cursor_col)

    skills, skill_warnings = load_skills(config.skills_dir)
    for warning in skill_warnings:
        warn(warning)

    llm_client = LLMClient(
        config,
        agents_md=load_agents_md(config.agents_md_file),
        skills=skills,
    )

    dangerous_matcher, pattern_errors = DangerousCommandMatcher.load(
        config.dangerous_patterns_file or None,
    )
    for error in pattern_errors:
        warn(error)

    def write_to_pty(data: bytes) -> None:
        assert pty_mgr.master_fd is not None
        write_all(pty_mgr.master_fd, data)

    # on_query_submit closes over relay and executor, which are
    # created below; both exist before any input can arrive.
    def on_query_submit(query: str) -> None:
        query_ui.finalize()
        relay.start_execution(executor.execute(query))

    input_router = InputRouter(
        write_to_pty=write_to_pty,
        on_query_submit=on_query_submit,
        on_query_cancel=query_ui.hide,
        on_query_start=query_ui.show,
        on_query_update=query_ui.update,
        hotkey=config.hotkey,
    )

    raw_io_log_fh = open(config.raw_io_log_file, "a") if config.raw_io_log_file else None

    mcp_manager = (
        MCPManager(connect_timeout_ms=config.mcp_connect_timeout_ms)
        if config.mcp_servers
        else None
    )

    relay = IORelay(
        input_router=input_router,
        screen_buffer=screen_buffer,
        log_file=raw_io_log_fh,
    )

    executor = Executor(
        llm_client=llm_client,
        screen_buffer=screen_buffer,
        write_to_pty=write_to_pty,
        stdout_fd=stdout_fd,
        relay=relay,
        settle_ms=config.settle_ms,
        threshold_ms=config.threshold_ms,
        max_steps=config.max_steps,
        confirm_level=config.confirm_level,
        dangerous_matcher=dangerous_matcher,
        new_output_max_chars=config.new_output_max_chars,
        mcp=mcp_manager,
        skills=skills,
    )

    async def run_session() -> int:
        loop = asyncio.get_running_loop()
        status(f"session started (model: {config.model})")
        # MCP servers are connected before the child starts: their tools
        # must be known before the first LLM request (design/MCP.md §3),
        # and warnings still print with the tty in its normal mode.
        if mcp_manager is not None:
            status(f"connecting to {len(config.mcp_servers)} MCP server(s)...")
            for warning in await mcp_manager.connect(config.mcp_servers):
                warn(warning)
            llm_client.set_mcp_tools(mcp_manager.tool_defs())

        tty_ctrl.enter_raw()
        pty_mgr.spawn(command)
        assert pty_mgr.master_fd is not None
        master_fd = pty_mgr.master_fd
        rows, cols = tty_ctrl.get_window_size()
        tty_ctrl.set_pty_window_size(master_fd, rows, cols)
        screen_buffer.resize(rows, cols)

        def on_sigwinch() -> None:
            rows, cols = tty_ctrl.get_window_size()
            tty_ctrl.set_pty_window_size(master_fd, rows, cols)
            screen_buffer.resize(rows, cols)

        loop.add_signal_handler(signal.SIGWINCH, on_sigwinch)
        try:
            await relay.run(master_fd, stdin_fd=stdin_fd, stdout_fd=stdout_fd)
        finally:
            loop.remove_signal_handler(signal.SIGWINCH)
            # Same task that opened them, as the transports require.
            if mcp_manager is not None:
                await mcp_manager.aclose()
        return pty_mgr.wait()

    try:
        exit_code = asyncio.run(run_session())
    finally:
        pty_mgr.close()
        # Restore first: the message is written after the tty is back in
        # its normal mode, so it needs no raw-mode line handling.
        tty_ctrl.restore()
        status("session terminated")
        if raw_io_log_fh is not None:
            raw_io_log_fh.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
