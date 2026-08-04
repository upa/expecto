import argparse
import os
import sys

from dotenv import load_dotenv

from jou.config import JouConfig
from jou.input_router import InputRouter
from jou.io_relay import IORelay
from jou.llm_client import LLMClient
from jou.mode_executor import ModeExecutor
from jou.pty_manager import PTYManager
from jou.query_ui import QueryModeUI
from jou.screen_buffer import ScreenBuffer
from jou.tty_controller import TTYController


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
        "--log-file",
        default=None,
        help="Raw I/O log output path (for debugging)",
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

    if args.log_file:
        config.log_file = args.log_file

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
        history_lines=config.history_lines,
    )
    query_ui = QueryModeUI(stdout_fd)

    llm_client = LLMClient(config)

    def write_to_pty(data: bytes) -> None:
        assert pty_mgr.master_fd is not None
        os.write(pty_mgr.master_fd, data)

    mode_executor = ModeExecutor(
        llm_client=llm_client,
        screen_buffer=screen_buffer,
        write_to_pty=write_to_pty,
        stdout_fd=stdout_fd,
    )

    def on_query_submit(query: str, mode: str) -> None:
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

    log_fh = open(config.log_file, "a") if config.log_file else None

    relay = IORelay(input_router=input_router, screen_buffer=screen_buffer, log_file=log_fh)

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
        if log_fh is not None:
            log_fh.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
