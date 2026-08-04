import argparse
import os
import sys

from jou.io_relay import IORelay
from jou.pty_manager import PTYManager
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
    args = parse_args(argv)

    command: str | list[str]
    if args.command:
        command = args.command
    elif args.shell:
        command = args.shell
    else:
        command = os.environ.get("SHELL", "/bin/bash")

    tty_ctrl = TTYController()
    pty_mgr = PTYManager()
    relay = IORelay()

    tty_ctrl.enter_raw()
    try:
        pty_mgr.spawn(command)
        assert pty_mgr.master_fd is not None
        rows, cols = tty_ctrl.get_window_size()
        tty_ctrl.set_pty_window_size(pty_mgr.master_fd, rows, cols)
        tty_ctrl.setup_sigwinch(pty_mgr.master_fd)

        relay.run(pty_mgr.master_fd)

        exit_code = pty_mgr.wait()
    finally:
        tty_ctrl.cleanup_sigwinch()
        pty_mgr.close()
        tty_ctrl.restore()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
