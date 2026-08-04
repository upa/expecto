from __future__ import annotations

import os
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("F", "W") else 1
    return width


def _truncate_to_width(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    width = 0
    for i, ch in enumerate(text):
        eaw = unicodedata.east_asian_width(ch)
        char_width = 2 if eaw in ("F", "W") else 1
        if width + char_width > max_width:
            return text[:i]
        width += char_width
    return text


class QueryModeUI:
    def __init__(
        self,
        stdout_fd: int,
        get_cursor_col: Callable[[], int] | None = None,
    ) -> None:
        self._stdout_fd = stdout_fd
        self._get_cursor_col = get_cursor_col
        self._visible: bool = False
        self._prompt: str = ""
        self._cursor_col: int = 0

    def show(self, mode: str) -> None:
        self._visible = True
        self._prompt = "? " if mode == "completion" else "# "
        self._cursor_col = self._get_cursor_col() if self._get_cursor_col else 0
        self._write(f"\x1b7\x1b[90m{self._prompt}\x1b[0m".encode())

    def update(self, text: str) -> None:
        if not self._visible:
            return
        max_width = self._available_width()
        display_text = _truncate_to_width(text, max_width)
        output = f"\x1b8\x1b[J\x1b7\x1b[90m{self._prompt}\x1b[0m{display_text}"
        self._write(output.encode())

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._write(b"\x1b8\x1b[J")

    def deactivate(self) -> None:
        self._visible = False

    def _available_width(self) -> int:
        try:
            cols = os.get_terminal_size(self._stdout_fd).columns
        except OSError:
            cols = 80
        if cols <= 0:
            cols = 80
        prompt_width = _display_width(self._prompt)
        return max(1, cols - self._cursor_col - prompt_width - 1)

    def _write(self, data: bytes) -> None:
        os.write(self._stdout_fd, data)
