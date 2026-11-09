from __future__ import annotations

import os
import unicodedata
from typing import TYPE_CHECKING

from expecto.fdio import write_all
from expecto.style import query_sgr

if TYPE_CHECKING:
    from collections.abc import Callable


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("F", "W") else 1
    return width


class QueryModeUI:
    """The "? " miniprompt, drawn inline at the cursor.

    The cursor can sit anywhere in the query text. Moving it right
    re-echoes the characters it passes, which leaves the terminal exactly
    as the original echo did (a pending wrap at the right margin, a wide
    character wrapped early); moving it left positions it by arithmetic
    from the prompt start, relative to the row the cursor is on."""

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
        # The terminal cursor: before character _cursor of _text, on the
        # row _row lines below the prompt's first row.
        self._cursor: int = 0
        self._row: int = 0

    def show(self) -> None:
        self._visible = True
        self._prompt = "? "
        self._start_col = self._get_cursor_col() if self._get_cursor_col else 0
        self._text = ""
        self._cursor = 0
        self._write(f"{query_sgr()}{self._prompt}\x1b[0m".encode())
        self._row = self._row_after_write(0)

    def update(self, text: str, cursor: int) -> None:
        """Show text as the query, with the cursor before character
        `cursor` of it."""
        if not self._visible:
            return
        if text == self._text:
            self._move_cursor(cursor)
            return
        old = self._text
        if (
            self._cursor == len(old)
            and cursor == len(text)
            and text.startswith(old)
        ):
            # Typing at the end: echo just the new characters.
            self._text = text
            self._move_cursor(cursor)
            return
        self._move_to_prompt_start()
        self._write(b"\x1b[J")
        self._text = text
        self._cursor = len(text)
        self._write(f"{query_sgr()}{self._prompt}{text}\x1b[0m".encode())
        self._row = self._row_after_write(len(text))
        self._move_cursor(cursor)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._move_to_prompt_start()
        self._write(b"\x1b[J")

    def finalize(self) -> None:
        """Keep the query text on screen and mark the prompt invisible.

        The cursor is moved to the end of the query first, and a trailing
        space closes it so that whatever expecto shows next — the spinner
        overlay — does not run straight on from it.
        No newline is written: the line break comes from the echo of the
        prompt nudge the executor sends before its first command, or from
        the newline it sends when the execution ends."""
        if not self._visible:
            return
        self._move_cursor(len(self._text))
        self._visible = False
        self._write(b" ")

    def _move_cursor(self, cursor: int) -> None:
        """Move the terminal cursor before character `cursor` of the
        query text."""
        if cursor > self._cursor:
            passed = self._text[self._cursor:cursor]
            self._write(f"{query_sgr()}{passed}\x1b[0m".encode())
            self._row = self._row_after_write(cursor)
        elif cursor < self._cursor:
            # A character sits at the target, so it is never in the
            # pending-wrap state the end of a full row would be.
            cols = self._get_cols()
            offset = self._offset(cursor)
            self._move_to(offset // cols, offset % cols)
        self._cursor = cursor

    def _move_to_prompt_start(self) -> None:
        self._move_to(0, self._start_col)

    def _move_to(self, row: int, col: int) -> None:
        """Move to (row, col), row counted from the prompt's first row."""
        self._write(b"\r")
        if self._row > row:
            self._write(f"\x1b[{self._row - row}A".encode())
        elif self._row < row:
            self._write(f"\x1b[{row - self._row}B".encode())
        if col > 0:
            self._write(f"\x1b[{col}C".encode())
        self._row = row

    def _offset(self, cursor: int) -> int:
        """Columns from the start of the prompt's first row to just
        before character `cursor` of the query text."""
        return (
            self._start_col
            + _display_width(self._prompt)
            + _display_width(self._text[:cursor])
        )

    def _row_after_write(self, cursor: int) -> int:
        """The row the cursor is on once the text up to `cursor` has been
        written. Writing that ends exactly at the right margin leaves the
        cursor on the same row (deferred wrap), not the next."""
        offset = self._offset(cursor)
        return (offset - 1) // self._get_cols() if offset > 0 else 0

    def _get_cols(self) -> int:
        try:
            cols = os.get_terminal_size(self._stdout_fd).columns
        except OSError:
            cols = 80
        return cols if cols > 0 else 80

    def _write(self, data: bytes) -> None:
        write_all(self._stdout_fd, data)
