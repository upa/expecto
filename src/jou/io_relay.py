import os
import select
import sys


class IORelay:
    def __init__(self):
        self._running = False

    def run(self, pty_master_fd, stdin_fd=None, stdout_fd=None):
        if stdin_fd is None:
            stdin_fd = sys.stdin.fileno()
        if stdout_fd is None:
            stdout_fd = sys.stdout.fileno()
        self._running = True

        while self._running:
            try:
                rlist, _, _ = select.select([stdin_fd, pty_master_fd], [], [])
            except (OSError, ValueError):
                break

            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, 4096)
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
                os.write(stdout_fd, data)

    def stop(self):
        self._running = False
