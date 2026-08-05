import os
import pty
import termios

from jou.tty_controller import TTYController


class TestTTYControllerWithPTY:
    def setup_method(self):
        self.master_fd, self.slave_fd = pty.openpty()

    def teardown_method(self):
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_enter_raw_and_restore(self):
        ctrl = TTYController.__new__(TTYController)
        ctrl._fd = self.slave_fd
        ctrl._is_tty = True
        ctrl._original_termios = None

        original = termios.tcgetattr(self.slave_fd)
        ctrl.enter_raw()

        raw_attrs = termios.tcgetattr(self.slave_fd)
        assert raw_attrs != original

        ctrl.restore()
        restored = termios.tcgetattr(self.slave_fd)
        assert restored == original

    def test_window_size(self):
        ctrl = TTYController.__new__(TTYController)
        ctrl._fd = self.slave_fd
        ctrl._is_tty = True
        ctrl._original_termios = None

        ctrl.set_pty_window_size(self.slave_fd, 40, 120)
        rows, cols = ctrl.get_window_size()
        assert rows == 40
        assert cols == 120


class TestTTYControllerNonTTY:
    def test_enter_raw_noop_on_non_tty(self):
        r, w = os.pipe()
        try:
            ctrl = TTYController.__new__(TTYController)
            ctrl._fd = r
            ctrl._is_tty = False
            ctrl._original_termios = None

            ctrl.enter_raw()
            assert ctrl._original_termios is None
        finally:
            os.close(r)
            os.close(w)

    def test_get_window_size_fallback(self):
        r, w = os.pipe()
        try:
            ctrl = TTYController.__new__(TTYController)
            ctrl._fd = r
            ctrl._is_tty = False
            ctrl._original_termios = None

            rows, cols = ctrl.get_window_size()
            assert rows == 24
            assert cols == 80
        finally:
            os.close(r)
            os.close(w)
