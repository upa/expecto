import fcntl
import os
import pty
import select
import struct
import termios

import pyte

from expecto.query_ui import QueryModeUI, _display_width


class TestDisplayWidth:
    def test_ascii(self) -> None:
        assert _display_width("hello") == 5

    def test_cjk(self) -> None:
        assert _display_width("あいう") == 6

    def test_mixed(self) -> None:
        assert _display_width("hello世界") == 9


class TestQueryModeUI:
    def setup_method(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()

    def teardown_method(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _read_output(self) -> str:
        import select
        data = b""
        while True:
            rlist, _, _ = select.select([self.master_fd], [], [], 0.1)
            if not rlist:
                break
            data += os.read(self.master_fd, 4096)
        return data.decode("utf-8", errors="replace")

    def test_show_prompt(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        output = self._read_output()
        assert "? " in output

    def test_prompt_uses_configured_color(self) -> None:
        from expecto.style import DEFAULT_COLOR, set_color

        set_color("36")
        try:
            ui = QueryModeUI(self.slave_fd)
            ui.show()
            ui.update("ls", 2)
            output = self._read_output()
        finally:
            set_color(DEFAULT_COLOR)
        assert "\x1b[36m? \x1b[0m" in output
        assert "\x1b[36mls\x1b[0m" in output
        assert "\x1b[90m" not in output

    def test_prompt_uses_query_color(self) -> None:
        from expecto.style import DEFAULT_COLOR, set_color, set_query_color

        set_color("36")
        set_query_color("33")
        try:
            ui = QueryModeUI(self.slave_fd)
            ui.show()
            ui.update("ls", 2)
            output = self._read_output()
        finally:
            set_color(DEFAULT_COLOR)
            set_query_color(None)
        assert "\x1b[33m? \x1b[0m" in output
        assert "\x1b[33mls\x1b[0m" in output
        assert "\x1b[36m" not in output

    def test_update_appends_incrementally(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()

        ui.update("l", 1)
        output1 = self._read_output()
        assert "l" in output1

        ui.update("ls", 2)
        output2 = self._read_output()
        assert "s" in output2

    def test_update_backspace_redraws(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("abc", 3)
        self._read_output()

        ui.update("ab", 2)
        output = self._read_output()
        assert "\x1b[J" in output
        assert "ab" in output

    def test_hide_clears_with_relative_movement(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("test", 4)
        self._read_output()

        ui.hide()
        output = self._read_output()
        assert "\r" in output
        assert "\x1b[J" in output

    def test_hide_when_not_visible_is_noop(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.hide()
        output = self._read_output()
        assert output == ""

    def test_update_when_not_visible_is_noop(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.update("text", 4)
        output = self._read_output()
        assert output == ""

    def test_finalize_keeps_text_and_prevents_update(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("hello", 5)
        self._read_output()

        ui.finalize()
        # The query line stays on screen, closed with a space so the
        # thinking overlay does not run on from it.
        assert self._read_output() == " "

        ui.update("should not appear", 17)
        output = self._read_output()
        assert output == ""

    def test_long_text_not_truncated(self) -> None:
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 0)
        ui.show()
        self._read_output()

        long_text = "a" * 200
        ui.update(long_text, len(long_text))
        output = self._read_output()
        assert output.count("a") == 200

    def test_cursor_col_stored_on_show(self) -> None:
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 25)
        ui.show()
        assert ui._start_col == 25

    def test_hide_with_start_col(self) -> None:
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 10)
        ui.show()
        self._read_output()
        ui.update("test", 4)
        self._read_output()

        ui.hide()
        output = self._read_output()
        assert "\r" in output
        assert "\x1b[10C" in output

    def test_move_to_start_multiline(self) -> None:
        """When text wraps, hide moves cursor up correctly."""
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 0)
        ui.show()
        self._read_output()

        cols = ui._get_cols()
        long_text = "x" * (cols * 2)
        ui.update(long_text, len(long_text))
        self._read_output()

        ui.hide()
        output = self._read_output()
        assert "\x1b[" in output
        assert "A" in output


class TestQueryCursor:
    """Cursor movement inside the query line, checked by replaying what
    QueryModeUI writes through a terminal emulator (pyte)."""

    COLS = 20

    def setup_method(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 10, self.COLS, 0, 0)
        fcntl.ioctl(self.slave_fd, termios.TIOCSWINSZ, winsize)
        self.screen = pyte.Screen(self.COLS, 10)
        self.stream = pyte.Stream(self.screen)

    def teardown_method(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _drain(self) -> str:
        data = b""
        while True:
            rlist, _, _ = select.select([self.master_fd], [], [], 0.05)
            if not rlist:
                break
            data += os.read(self.master_fd, 4096)
        text = data.decode("utf-8")
        self.stream.feed(text)
        return text

    def _ui(self, start_col: int = 0) -> QueryModeUI:
        if start_col:
            self.stream.feed(" " * start_col)
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: start_col)
        ui.show()
        self._drain()
        return ui

    def _cursor(self) -> tuple[int, int]:
        return self.screen.cursor.y, self.screen.cursor.x

    def _row(self, row: int) -> str:
        return self.screen.display[row].rstrip()

    def test_left_moves_cursor_back(self) -> None:
        ui = self._ui()
        ui.update("abc", 3)
        self._drain()
        ui.update("abc", 2)
        self._drain()
        assert self._cursor() == (0, 4)  # "? ab|c"
        assert self._row(0) == "? abc"

    def test_right_reechoes_the_passed_character(self) -> None:
        ui = self._ui()
        ui.update("abc", 3)
        ui.update("abc", 0)
        self._drain()
        ui.update("abc", 1)
        output = self._drain()
        assert "a" in output
        assert self._cursor() == (0, 3)
        assert self._row(0) == "? abc"

    def test_insert_in_middle_redraws_and_keeps_cursor(self) -> None:
        ui = self._ui()
        ui.update("ac", 2)
        ui.update("ac", 1)
        self._drain()
        ui.update("abc", 2)
        output = self._drain()
        assert "\x1b[J" in output
        assert self._row(0) == "? abc"
        assert self._cursor() == (0, 4)

    def test_delete_in_middle(self) -> None:
        ui = self._ui()
        ui.update("abc", 3)
        ui.update("abc", 1)
        ui.update("bc", 0)
        self._drain()
        assert self._row(0) == "? bc"
        assert self._cursor() == (0, 2)

    def test_wide_characters_count_two_columns(self) -> None:
        ui = self._ui()
        ui.update("あいう", 3)
        ui.update("あいう", 1)
        self._drain()
        assert self._cursor() == (0, 4)  # "? あ|いう"

    def test_move_back_across_a_wrapped_line(self) -> None:
        ui = self._ui()
        text = "x" * 30  # "? " + 30 chars: two rows of 20 columns
        ui.update(text, 30)
        self._drain()
        assert self._cursor()[0] == 1
        ui.update(text, 0)
        self._drain()
        assert self._cursor() == (0, 2)
        ui.update(text, 25)  # offset 27: row 1, column 7
        self._drain()
        assert self._cursor() == (1, 7)

    def test_text_ending_at_the_right_margin(self) -> None:
        """Text that exactly fills the row leaves the cursor on that row
        (deferred wrap); moving back must not go up a row too far."""
        ui = self._ui()
        text = "y" * (self.COLS - 2)
        ui.update(text, len(text))
        ui.update(text, 0)
        self._drain()
        assert self._cursor() == (0, 2)
        assert self._row(0) == "? " + text

    def test_hide_from_middle_clears_everything(self) -> None:
        ui = self._ui(start_col=3)
        text = "z" * 25
        ui.update(text, 25)
        ui.update(text, 4)
        self._drain()
        ui.hide()
        self._drain()
        assert self._row(0) == ""
        assert self._row(1) == ""
        assert self._cursor() == (0, 3)

    def test_finalize_from_middle_appends_space_at_end(self) -> None:
        ui = self._ui()
        ui.update("hello", 5)
        ui.update("hello", 1)
        self._drain()
        ui.finalize()
        self._drain()
        assert self._row(0) == "? hello"
        assert self._cursor() == (0, 8)  # after "? hello "

    def test_typing_at_end_after_moving_back_and_forth(self) -> None:
        ui = self._ui()
        ui.update("ab", 2)
        ui.update("ab", 0)
        ui.update("ab", 2)
        self._drain()
        ui.update("abc", 3)
        output = self._drain()
        assert "\x1b[J" not in output  # still the incremental echo
        assert self._row(0) == "? abc"
        assert self._cursor() == (0, 5)


class TestInputRouterWithQueryUI:
    def test_callbacks_called(self) -> None:
        from expecto.input_router import InputRouter

        started: list[bool] = []
        updated: list[tuple[str, int]] = []

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q: None,
            on_query_cancel=lambda: None,
            on_query_start=lambda: started.append(True),
            on_query_update=lambda text, cursor: updated.append((text, cursor)),
        )
        router.handle(bytes([0x1F]))
        assert started == [True]

        router.handle(b"ab")
        assert updated == [("a", 1), ("ab", 2)]

        router.handle(bytes([0x7F]))
        assert updated == [("a", 1), ("ab", 2), ("a", 1)]

        router.handle(b"\x1b[D")
        assert updated[-1] == ("a", 0)
