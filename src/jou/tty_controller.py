import atexit
import fcntl
import os
import signal
import struct
import sys
import termios
import tty


class TTYController:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._is_tty = os.isatty(self._fd)
        self._original_termios = None
        self._original_sigwinch = None
        self._pty_fd = None

    def enter_raw(self):
        if not self._is_tty:
            return
        self._original_termios = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        atexit.register(self.restore)

    def restore(self):
        if self._original_termios is not None:
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, self._original_termios)
            self._original_termios = None

    def get_window_size(self):
        if not self._is_tty:
            return 24, 80
        buf = fcntl.ioctl(self._fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols = struct.unpack("HHHH", buf)[:2]
        return rows, cols

    def set_pty_window_size(self, pty_fd, rows, cols):
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, winsize)

    def setup_sigwinch(self, pty_fd):
        self._pty_fd = pty_fd

        def handle_sigwinch(_signum, _frame):
            if self._pty_fd is not None:
                rows, cols = self.get_window_size()
                self.set_pty_window_size(self._pty_fd, rows, cols)

        self._original_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, handle_sigwinch)

    def cleanup_sigwinch(self):
        if self._original_sigwinch is not None:
            signal.signal(signal.SIGWINCH, self._original_sigwinch)
            self._original_sigwinch = None
