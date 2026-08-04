import os
import pty

from jou.query_ui import QueryModeUI, _display_width, _truncate_to_width


class TestDisplayWidth:
    def test_ascii(self) -> None:
        assert _display_width("hello") == 5

    def test_cjk(self) -> None:
        assert _display_width("あいう") == 6

    def test_mixed(self) -> None:
        assert _display_width("hello世界") == 9


class TestTruncateToWidth:
    def test_no_truncation(self) -> None:
        assert _truncate_to_width("hello", 10) == "hello"

    def test_truncate_ascii(self) -> None:
        assert _truncate_to_width("hello world", 5) == "hello"

    def test_truncate_cjk(self) -> None:
        result = _truncate_to_width("あいうえお", 6)
        assert result == "あいう"

    def test_truncate_cjk_boundary(self) -> None:
        result = _truncate_to_width("あいう", 5)
        assert result == "あい"

    def test_zero_width(self) -> None:
        assert _truncate_to_width("hello", 0) == ""


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

    def test_show_completion_prompt(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show("completion")
        output = self._read_output()
        assert "? " in output
        assert "\x1b7" in output

    def test_show_auto_prompt(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show("auto")
        output = self._read_output()
        assert "# " in output

    def test_update_shows_text(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show("completion")
        self._read_output()
        ui.update("ls -la")
        output = self._read_output()
        assert "ls -la" in output
        assert "\x1b[J" in output

    def test_hide_restores_cursor(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show("completion")
        self._read_output()
        ui.hide()
        output = self._read_output()
        assert "\x1b8" in output
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

    def test_deactivate_prevents_update(self) -> None:
        ui = QueryModeUI(self.slave_fd)
        ui.show("completion")
        self._read_output()
        ui.deactivate()
        ui.update("should not appear")
        output = self._read_output()
        assert output == ""

    def test_truncation_with_cursor_col(self) -> None:
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: 30)
        ui.show("completion")
        self._read_output()

        long_text = "a" * 200
        ui.update(long_text)
        output = self._read_output()
        displayed_a_count = output.count("a")
        assert displayed_a_count < 200

    def test_cursor_col_stored_on_show(self) -> None:
        col_values: list[int] = [25]
        ui = QueryModeUI(self.slave_fd, get_cursor_col=lambda: col_values[0])
        ui.show("completion")
        assert ui._cursor_col == 25


class TestInputRouterWithQueryUI:
    def test_callbacks_called(self) -> None:
        from jou.input_router import InputRouter

        started: list[str] = []
        updated: list[str] = []

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
            on_query_start=started.append,
            on_query_update=updated.append,
        )
        router.handle(bytes([0x1F]))
        assert started == ["completion"]

        router.handle(b"ab")
        assert updated == ["a", "ab"]

        router.handle(bytes([0x7F]))
        assert updated == ["a", "ab", "a"]
