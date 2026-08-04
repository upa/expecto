from __future__ import annotations

import os
from collections.abc import Callable


class QueryModeUI:
    def __init__(self, stdout_fd: int, get_window_size: Callable[[], tuple[int, int]]) -> None:
        self._stdout_fd = stdout_fd
        self._get_window_size = get_window_size
        self._visible: bool = False
        self._mode: str = ""

    def show(self, mode: str) -> None:
        self._mode = mode
        self._visible = True
        self._draw("")

    def update(self, text: str) -> None:
        if self._visible:
            self._draw(text)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        rows, cols = self._get_window_size()
        output = (
            f"\x1b7"
            f"\x1b[{rows};1H"
            f"\x1b[2K"
            f"\x1b8"
        )
        self._write(output.encode())

    def _draw(self, text: str) -> None:
        rows, cols = self._get_window_size()
        label = "comp" if self._mode == "completion" else "auto"
        prompt = f"jou({label})> "
        max_text = cols - len(prompt) - 1
        display_text = text[-max_text:] if len(text) > max_text else text
        line = prompt + display_text
        output = (
            f"\x1b7"
            f"\x1b[{rows};1H"
            f"\x1b[2K"
            f"\x1b[7m{line}\x1b[0m"
            f"\x1b8"
        )
        self._write(output.encode())

    def _write(self, data: bytes) -> None:
        os.write(self._stdout_fd, data)
