import os
import pty

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
            ui.update("ls")
            output = self._read_output()
        finally:
            set_color(DEFAULT_COLOR)
        assert "\x1b[36m? \x1b[0m" in output
        assert "\x1b[36mls\x1b[0m" in output
        assert "\x1b[90m" not in output

    def test_update_appends_incrementally(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()

        ui.update("l")
        output1 = self._read_output()
        assert "l" in output1

        ui.update("ls")
        output2 = self._read_output()
        assert "s" in output2

    def test_update_backspace_redraws(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("abc")
        self._read_output()

        ui.update("ab")
        output = self._read_output()
        assert "\x1b[J" in output
        assert "ab" in output

    def test_hide_clears_with_relative_movement(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("test")
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
        ui.update("text")
        output = self._read_output()
        assert output == ""

    def test_finalize_keeps_text_and_prevents_update(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show()
        self._read_output()
        ui.update("hello")
        self._read_output()

        ui.finalize()
        # The query line stays on screen, closed with a space so the
        # thinking overlay does not run on from it.
        assert self._read_output() == " "

        ui.update("should not appear")
        output = self._read_output()
        assert output == ""

    def test_long_text_not_truncated(self) -> None:
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 0)
        ui.show()
        self._read_output()

        long_text = "a" * 200
        ui.update(long_text)
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
        ui.update("test")
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
        ui.update(long_text)
        self._read_output()

        ui.hide()
        output = self._read_output()
        assert "\x1b[" in output
        assert "A" in output


class TestInputRouterWithQueryUI:
    def test_callbacks_called(self) -> None:
        from expecto.input_router import InputRouter

        started: list[bool] = []
        updated: list[str] = []

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q: None,
            on_query_cancel=lambda: None,
            on_query_start=lambda: started.append(True),
            on_query_update=updated.append,
        )
        router.handle(bytes([0x1F]))
        assert started == [True]

        router.handle(b"ab")
        assert updated == ["a", "ab"]

        router.handle(bytes([0x7F]))
        assert updated == ["a", "ab", "a"]
