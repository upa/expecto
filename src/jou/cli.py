import argparse
import os
import sys

from dotenv import load_dotenv

from jou.config import JouConfig
from jou.dangerous import DangerousCommandMatcher
from jou.input_router import InputRouter
from jou.io_relay import IORelay
from jou.llm_client import LLMClient
from jou.mode_executor import ModeExecutor
from jou.pty_manager import PTYManager
from jou.query_ui import QueryModeUI
from jou.screen_buffer import ScreenBuffer
from jou.tty_controller import TTYController
from jou.working_summary import WorkingSummaryManager


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jou",
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
        help="Auto mode: minimum command risk that requires user confirmation "
        "(default: destructive)",
    )
    parser.add_argument(
        "--dangerous-patterns-file",
        default=None,
        help="File with additional dangerous command regex patterns "
        "(default: ~/.config/jou/dangerous_patterns.txt)",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to run (after --)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()

    args = parse_args(argv)
    config = JouConfig.from_env()

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
        print(f"jou: warning: {error}", file=sys.stderr)

    working_summary = WorkingSummaryManager(
        llm_client=llm_client,
        screen_buffer=screen_buffer,
        trigger_lines=config.summary_trigger_lines,
        max_input_lines=config.summary_max_input_lines,
    )

    def write_to_pty(data: bytes) -> None:
        assert pty_mgr.master_fd is not None
        os.write(pty_mgr.master_fd, data)

    def get_pty_master_fd() -> int:
        assert pty_mgr.master_fd is not None
        return pty_mgr.master_fd

    mode_executor = ModeExecutor(
        llm_client=llm_client,
        screen_buffer=screen_buffer,
        write_to_pty=write_to_pty,
        stdout_fd=stdout_fd,
        stdin_fd=stdin_fd,
        get_pty_master_fd=get_pty_master_fd,
        settle_ms=config.settle_ms,
        threshold_ms=config.threshold_ms,
        max_auto_steps=config.max_auto_steps,
        history_lines=config.history_lines,
        working_summary=working_summary,
        confirm_level=config.confirm_level,
        dangerous_matcher=dangerous_matcher,
    )

    def on_query_submit(query: str, mode: str) -> None:
        if mode == "auto":
            query_ui.finalize()
        else:
            query_ui.deactivate()
        mode_executor.execute(query, mode)

    input_router = InputRouter(
        write_to_pty=write_to_pty,
        on_query_submit=on_query_submit,
        on_query_cancel=query_ui.hide,
        on_query_start=query_ui.show,
        on_query_update=query_ui.update,
        completion_hotkey=config.completion_hotkey,
        auto_hotkey=config.auto_hotkey,
    )

    raw_io_log_fh = open(config.raw_io_log_file, "a") if config.raw_io_log_file else None

    relay = IORelay(
        input_router=input_router,
        screen_buffer=screen_buffer,
        log_file=raw_io_log_fh,
        working_summary=working_summary,
    )

    tty_ctrl.enter_raw()
    try:
        pty_mgr.spawn(command)
        assert pty_mgr.master_fd is not None
        rows, cols = tty_ctrl.get_window_size()
        tty_ctrl.set_pty_window_size(pty_mgr.master_fd, rows, cols)
        screen_buffer.resize(rows, cols)
        tty_ctrl.setup_sigwinch(pty_mgr.master_fd)

        relay.run(pty_mgr.master_fd, stdin_fd=stdin_fd, stdout_fd=stdout_fd)

        exit_code = pty_mgr.wait()
    finally:
        tty_ctrl.cleanup_sigwinch()
        pty_mgr.close()
        tty_ctrl.restore()
        if raw_io_log_fh is not None:
            raw_io_log_fh.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
