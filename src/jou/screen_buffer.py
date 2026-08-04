from __future__ import annotations

import pyte
import pyte.modes


class ScreenBuffer:
    def __init__(self, rows: int = 24, cols: int = 80, history_lines: int = 200) -> None:
        self._screen: pyte.HistoryScreen = pyte.HistoryScreen(cols, rows, history=history_lines)
        self._stream: pyte.Stream = pyte.Stream(self._screen)
        self._screen.set_mode(pyte.modes.LNM)

    def feed(self, data: bytes) -> None:
        try:
            self._stream.feed(data.decode("utf-8", errors="replace"))
        except TypeError:
            self._screen.reset()

    def get_text(self) -> str:
        history_lines: list[str] = []
        for line in self._screen.history.top:
            text = "".join(line[col].data for col in sorted(line)).rstrip()
            history_lines.append(text)

        screen_lines: list[str] = []
        for row in range(self._screen.lines):
            line = self._screen.buffer[row]
            text = "".join(line[col].data for col in range(self._screen.columns)).rstrip()
            screen_lines.append(text)

        while screen_lines and screen_lines[-1] == "":
            screen_lines.pop()

        all_lines = history_lines + screen_lines
        return "\n".join(all_lines)

    def get_cursor_col(self) -> int:
        return self._screen.cursor.x

    def resize(self, rows: int, cols: int) -> None:
        self._screen.resize(rows, cols)
