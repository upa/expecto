from __future__ import annotations

import os
import select
import sys
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from jou.input_router import InputRouter
    from jou.screen_buffer import ScreenBuffer
    from jou.working_summary import WorkingSummaryManager


class IORelay:
    def __init__(
        self,
        input_router: InputRouter | None = None,
        screen_buffer: ScreenBuffer | None = None,
        log_file: IO[str] | None = None,
        working_summary: WorkingSummaryManager | None = None,
    ) -> None:
        self._running: bool = False
        self._input_router = input_router
        self._screen_buffer = screen_buffer
        self._log_file = log_file
        self._working_summary = working_summary

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
                if self._log_file is not None:
                    self._log_file.write(f"stdin: {data!r}\n")
                    self._log_file.flush()
                if self._input_router is not None:
                    self._input_router.handle(data)
                else:
                    os.write(pty_master_fd, data)

            if pty_master_fd in rlist:
                try:
                    data = os.read(pty_master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                if self._screen_buffer is not None:
                    self._screen_buffer.feed(data)
                    if self._working_summary is not None:
                        self._working_summary.check_trigger()
                os.write(effective_stdout, data)

    def stop(self) -> None:
        self._running = False
