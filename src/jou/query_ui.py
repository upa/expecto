from __future__ import annotations

import os


class QueryModeUI:
    def __init__(self, stdout_fd: int) -> None:
        self._stdout_fd = stdout_fd
        self._visible: bool = False
        self._prompt: str = ""

    def show(self, mode: str) -> None:
        self._visible = True
        self._prompt = "? " if mode == "completion" else "# "
        self._write(f"\x1b7\x1b[90m{self._prompt}\x1b[0m".encode())

    def update(self, text: str) -> None:
        if not self._visible:
            return
        output = f"\x1b8\x1b[J\x1b7\x1b[90m{self._prompt}\x1b[0m{text}"
        self._write(output.encode())

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._write(b"\x1b8\x1b[J")

    def deactivate(self) -> None:
        self._visible = False

    def _write(self, data: bytes) -> None:
        os.write(self._stdout_fd, data)
