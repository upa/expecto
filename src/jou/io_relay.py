import os
import select
import sys


class IORelay:
    def __init__(self) -> None:
        self._running: bool = False

    def run(self, pty_master_fd: int, stdin_fd: int | None = None, stdout_fd: int | None = None) -> None:
        effective_stdin = stdin_fd if stdin_fd is not None else sys.stdin.fileno()
        effective_stdout = stdout_fd if stdout_fd is not None else sys.stdout.fileno()
        self._running = True

        while self._running:
            try:
                rlist, _, _ = select.select([effective_stdin, pty_master_fd], [], [])
            except (OSError, ValueError):
                break

            if effective_stdin in rlist:
                try:
                    data = os.read(effective_stdin, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(pty_master_fd, data)

            if pty_master_fd in rlist:
                try:
                    data = os.read(pty_master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(effective_stdout, data)

    def stop(self) -> None:
        self._running = False
