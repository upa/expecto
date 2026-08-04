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

    def test_show_draws_prompt(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.show("completion")
        output = self._read_output()
        assert "jou(comp)>" in output

    def test_show_auto_mode(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.show("auto")
        output = self._read_output()
        assert "jou(auto)>" in output

    def test_update_shows_text(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.show("completion")
        self._read_output()  # consume initial draw
        ui.update("ls -la")
        output = self._read_output()
        assert "ls -la" in output

    def test_hide_clears_overlay(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.show("completion")
        self._read_output()
        ui.hide()
        output = self._read_output()
        # Should contain clear line escape (\x1b[2K)
        assert "\x1b[2K" in output

    def test_hide_when_not_visible_is_noop(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.hide()  # should not raise
        output = self._read_output()
        assert output == ""

    def test_update_when_not_visible_is_noop(self) -> None:
        ui = QueryModeUI(self.slave_fd, lambda: (24, 80))
        ui.update("text")
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
        # Enter completion query mode
        router.handle(bytes([0x1E]))
        assert started == ["completion"]

        # Type characters
        router.handle(b"ab")
        assert updated == ["a", "ab"]

        # Backspace
        router.handle(bytes([0x7F]))
        assert updated == ["a", "ab", "a"]
