import argparse
import asyncio
import os
import signal
import sys

from dotenv import find_dotenv, load_dotenv

from expecto.config import DEFAULT_ENV_FILE, ExpectoConfig
from expecto.dangerous import DangerousCommandMatcher
from expecto.executor import Executor
from expecto.input_router import InputRouter
from expecto.io_relay import IORelay
from expecto.llm_client import LLMClient
from expecto.pty_manager import NESTING_ENV_VAR, PTYManager
from expecto.query_ui import QueryModeUI
from expecto.screen_buffer import ScreenBuffer
from expecto.tty_controller import TTYController
from expecto.working_summary import WorkingSummaryManager


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
        "--raw-io-log-file",
        default=None,
        help="Raw I/O log output path (for debugging)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Log file for queries and LLM requests/responses (JSONL format)",
    )
    parser.add_argument(
        "--history-lines",
        type=int,
        default=None,
        help="Number of screen history lines sent to the LLM (default: 500)",
    )
    parser.add_argument(
        "--scrollback-lines",
        type=int,
        default=None,
        help="Scrollback buffer capacity in lines (default: 5000)",
    )
    parser.add_argument(
        "--summary-trigger-lines",
        type=int,
        default=None,
        help="Scrolled lines that trigger a working summary update (default: 500)",
    )
    parser.add_argument(
        "--summary-max-input-lines",
        type=int,
        default=None,
        help="Maximum lines fed into one working summary update (default: 2000)",
    )
    parser.add_argument(
        "--confirm-level",
        choices=["destructive", "mutating", "none"],
        default=None,
        help="Minimum command risk that requires user confirmation "
        "(default: destructive)",
    )
    parser.add_argument(
        "--dangerous-patterns-file",
        default=None,
        help="File with additional dangerous command regex patterns "
        "(default: ~/.config/expecto/dangerous_patterns.txt)",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to run (after --)",
    )
    return parser.parse_args(argv)


def load_env() -> None:
    """Load .env from the current directory (searching upwards, dotenv's
    convention); fall back to ~/.config/expecto/.env. Existing environment
    variables always take precedence over file values."""
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
        return
    load_dotenv(os.path.expanduser(DEFAULT_ENV_FILE))


def main(argv: list[str] | None = None) -> None:
    if os.environ.get(NESTING_ENV_VAR):
        print(
            "expecto: already inside an expecto session; nested sessions are not supported. exited.",
            file=sys.stderr,
        )
        sys.exit(1)

    load_env()

    args = parse_args(argv)
    config = ExpectoConfig.from_env()

    if args.raw_io_log_file:
        config.raw_io_log_file = args.raw_io_log_file
    if args.log_file:
        config.log_file = args.log_file
    if args.history_lines is not None:
        config.history_lines = args.history_lines
    if args.scrollback_lines is not None:
        config.scrollback_lines = args.scrollback_lines
    if args.summary_trigger_lines is not None:
        config.summary_trigger_lines = args.summary_trigger_lines
    if args.summary_max_input_lines is not None:
        config.summary_max_input_lines = args.summary_max_input_lines
    if args.confirm_level is not None:
        config.confirm_level = args.confirm_level
    if args.dangerous_patterns_file is not None:
        config.dangerous_patterns_file = args.dangerous_patterns_file

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

    llm_client = LLMClient(config)

    dangerous_matcher, pattern_errors = DangerousCommandMatcher.load(
        config.dangerous_patterns_file or None,
    )
    for error in pattern_errors:
        print(f"expecto: warning: {error}", file=sys.stderr)

    working_summary = WorkingSummaryManager(
        llm_client=llm_client,
        screen_buffer=screen_buffer,
        trigger_lines=config.summary_trigger_lines,
        max_input_lines=config.summary_max_input_lines,
    )

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
        working_summary=working_summary,
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
        history_lines=config.history_lines,
        working_summary=working_summary,
        confirm_level=config.confirm_level,
        dangerous_matcher=dangerous_matcher,
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
