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
        self._start_col: int = 0
        self._text: str = ""

    def show(self, mode: str) -> None:
        self._visible = True
        self._prompt = "? " if mode == "completion" else "! "
        self._start_col = self._get_cursor_col() if self._get_cursor_col else 0
        self._text = ""
        self._write(f"\x1b[90m{self._prompt}\x1b[0m".encode())

    def update(self, text: str) -> None:
        if not self._visible:
            return
        old = self._text
        if text == old:
            return
        if len(text) > len(old) and text[: len(old)] == old:
            new_chars = text[len(old) :]
            self._write(f"\x1b[90m{new_chars}\x1b[0m".encode())
        else:
            self._move_to_prompt_start()
            self._write(b"\x1b[J")
            self._write(f"\x1b[90m{self._prompt}{text}\x1b[0m".encode())
        self._text = text

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._move_to_prompt_start()
        self._write(b"\x1b[J")

    def deactivate(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._move_to_prompt_start()
        self._write(b"\x1b[J")

    def finalize(self) -> None:
        """Keep query text visible, write newline, mark invisible."""
        if not self._visible:
            return
        self._visible = False
        self._write(b"\r\n")

    def _move_to_prompt_start(self) -> None:
        cols = self._get_cols()
        prompt_width = _display_width(self._prompt)
        text_width = _display_width(self._text)
        total = self._start_col + prompt_width + text_width

        # Account for pending wrap: when total is exactly a multiple of cols,
        # the cursor is still on the current line (deferred wrap), not the next.
        lines_down = (total - 1) // cols if total > 0 else 0

        self._write(b"\r")
        if lines_down > 0:
            self._write(f"\x1b[{lines_down}A".encode())
        if self._start_col > 0:
            self._write(f"\x1b[{self._start_col}C".encode())

    def _get_cols(self) -> int:
        try:
            cols = os.get_terminal_size(self._stdout_fd).columns
        except OSError:
            cols = 80
        return cols if cols > 0 else 80

    def _write(self, data: bytes) -> None:
        os.write(self._stdout_fd, data)
