from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pyte
import pyte.modes
from pyte.screens import Char, StaticDefaultDict


class _CountingDeque(deque[StaticDefaultDict[int, Char]]):
    """Deque that counts total appends. The count is monotonic: it survives
    maxlen evictions and clear(), so it serves as an absolute line counter
    for lines pushed into scrollback."""

    total_appended: int = 0

    def append(self, x: StaticDefaultDict[int, Char]) -> None:
        super().append(x)
        self.total_appended += 1


@dataclass(frozen=True)
class ScreenMark:
    """A point-in-time marker of the screen state, for extracting new output."""

    scrolled_total: int
    cursor_row: int


class ScreenBuffer:
    def __init__(self, rows: int = 24, cols: int = 80, scrollback_lines: int = 5000) -> None:
        self._screen: pyte.HistoryScreen = pyte.HistoryScreen(cols, rows, history=scrollback_lines)
        self._history_top: _CountingDeque = _CountingDeque(maxlen=scrollback_lines)
        self._screen.history = self._screen.history._replace(top=self._history_top)
        self._stream: pyte.Stream = pyte.Stream(self._screen)
        self._screen.set_mode(pyte.modes.LNM)

    def feed(self, data: bytes) -> None:
        try:
            self._stream.feed(data.decode("utf-8", errors="replace"))
        except TypeError:
            self._screen.reset()

    def _render_history_line(self, line: StaticDefaultDict[int, Char]) -> str:
        return "".join(line[col].data for col in sorted(line)).rstrip()

    def _render_screen_row(self, row: int) -> str:
        line = self._screen.buffer[row]
        return "".join(line[col].data for col in range(self._screen.columns)).rstrip()

    def get_text(self, last_n: int | None = None) -> str:
        history_lines: list[str] = []
        for line in self._screen.history.top:
            history_lines.append(self._render_history_line(line))

        screen_lines: list[str] = []
        for row in range(self._screen.lines):
            screen_lines.append(self._render_screen_row(row))

        while screen_lines and screen_lines[-1] == "":
            screen_lines.pop()

        all_lines = history_lines + screen_lines
        if last_n is not None and len(all_lines) > last_n:
            all_lines = all_lines[len(all_lines) - last_n:]
        return "\n".join(all_lines)

    def mark(self) -> ScreenMark:
        """Record the current screen state and reset change tracking.

        Subsequent output can be extracted with get_text_since()."""
        self._screen.dirty.clear()
        return ScreenMark(
            scrolled_total=self._history_top.total_appended,
            cursor_row=self._screen.cursor.y,
        )

    def get_text_since(self, mark: ScreenMark) -> str:
        """Return only the output produced after mark() was taken.

        Combines lines scrolled into history since the mark (excluding
        pre-mark rows that merely scrolled off the screen) with the visible
        rows modified since the mark."""
        scrolled = self._history_top.total_appended - mark.scrolled_total

        # The first lines scrolled off are the pre-mark rows that were above
        # the cursor; skip them. The cursor row itself (command echo) is kept.
        skip = min(scrolled, mark.cursor_row)
        wanted = scrolled - skip
        available = len(self._history_top)
        take = min(wanted, available)
        truncated = wanted > available
        history_lines = [
            self._render_history_line(line)
            for line in list(self._history_top)[available - take:]
        ]

        dirty = self._screen.dirty
        screen_lines: list[str] = []
        if dirty:
            if scrolled > 0:
                # Scrolling marks every row dirty; start where the mark-time
                # cursor row has moved up to, so stale rows above it are skipped.
                start = max(0, mark.cursor_row - scrolled)
            else:
                start = min(dirty)
            for row in range(start, max(dirty) + 1):
                screen_lines.append(self._render_screen_row(row))

        while screen_lines and screen_lines[-1] == "":
            screen_lines.pop()

        all_lines = history_lines + screen_lines
        if truncated:
            all_lines.insert(0, "[... earlier output truncated: exceeded scrollback capacity ...]")
        return "\n".join(all_lines)

    def get_cursor_col(self) -> int:
        return self._screen.cursor.x

    def resize(self, rows: int, cols: int) -> None:
        self._screen.resize(rows, cols)
