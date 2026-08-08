import argparse
import asyncio
import os
import signal
import sys

from dotenv import load_dotenv

from expecto.config import ExpectoConfig, default_env_file
from expecto.dangerous import DangerousCommandMatcher
from expecto.executor import Executor
from expecto.input_router import InputRouter
from expecto.io_relay import IORelay
from expecto.llm_client import LLMClient, load_agents_md
from expecto.pty_manager import NESTING_ENV_VAR, PTYManager
from expecto.query_ui import QueryModeUI
from expecto.screen_buffer import ScreenBuffer
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


def main(argv: list[str] | None = None) -> None:
    if os.environ.get(NESTING_ENV_VAR):
        print(
            "expecto: already inside an expecto session; nested sessions are not supported. exited.",
            file=sys.stderr,
        )
        sys.exit(1)

    args = parse_args(argv)

    if args.config_dir is not None and not os.path.isdir(args.config_dir):
        print(
            f"expecto: error: config directory not found: {args.config_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        load_env(args.env, args.config_dir)
    except FileNotFoundError:
        print(f"expecto: error: env file not found: {args.env}", file=sys.stderr)
        sys.exit(1)

    try:
        config, config_warnings = ExpectoConfig.load(args.config, args.config_dir)
    except FileNotFoundError:
        print(f"expecto: error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    for warning in config_warnings:
        print(f"expecto: warning: {warning}", file=sys.stderr)

    if not config.openai_api_key:
        print(
            "expecto: error: OPENAI_API_KEY is not set. "
            f"Set it in the environment or in {default_env_file(args.config_dir)}.",
            file=sys.stderr,
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
            print(
                f"expecto: error: AGENTS.md not found: {args.agents_md}",
                file=sys.stderr,
            )
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

    llm_client = LLMClient(config, agents_md=load_agents_md(config.agents_md_file))

    dangerous_matcher, pattern_errors = DangerousCommandMatcher.load(
        config.dangerous_patterns_file or None,
    )
    for error in pattern_errors:
        print(f"expecto: warning: {error}", file=sys.stderr)

    def write_to_pty(data: bytes) -> None:
        assert pty_mgr.master_fd is not None
        os.write(pty_mgr.master_fd, data)

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
    )

    async def run_session() -> int:
        loop = asyncio.get_running_loop()
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
        return pty_mgr.wait()

    tty_ctrl.enter_raw()
    try:
        exit_code = asyncio.run(run_session())
    finally:
        pty_mgr.close()
        tty_ctrl.restore()
        if raw_io_log_fh is not None:
            raw_io_log_fh.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
