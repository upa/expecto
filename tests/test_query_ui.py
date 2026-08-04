import os
import pty

from jou.query_ui import QueryModeUI


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
