import os

from jou.llm_client import CompletionResponse
from jou.mode_executor import ModeExecutor
from jou.screen_buffer import ScreenBuffer


class FakeLLMClient:
    def __init__(self, response: CompletionResponse) -> None:
        self._response = response
        self.last_screen_text: str = ""
        self.last_query: str = ""

    def complete(self, screen_text: str, query: str) -> CompletionResponse:
        self.last_screen_text = screen_text
        self.last_query = query
        return self._response


class TestModeExecutor:
    def test_completion_confirmed(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="ls -la", explanation="list all files")
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
            get_window_size=lambda: (24, 80),
        )

        os.write(stdin_w, b"\r")

        executor.execute("list files", "completion")

        assert pty_written == [b"ls -la"]
        assert fake_llm.last_query == "list files"

        os.close(stdin_r)
        os.close(stdin_w)
        os.close(stdout_r)
        os.close(stdout_w)

    def test_completion_cancelled(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="rm -rf /", explanation="danger")
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
            get_window_size=lambda: (24, 80),
        )

        os.write(stdin_w, b"\x1b")

        executor.execute("delete everything", "completion")

        assert pty_written == []

        os.close(stdin_r)
        os.close(stdin_w)
        os.close(stdout_r)
        os.close(stdout_w)

    def test_completion_cancelled_ctrl_c(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="echo hi", explanation="greet")
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
            get_window_size=lambda: (24, 80),
        )

        os.write(stdin_w, b"\x03")

        executor.execute("say hi", "completion")

        assert pty_written == []

        os.close(stdin_r)
        os.close(stdin_w)
        os.close(stdout_r)
        os.close(stdout_w)

    def test_auto_mode_noop(self) -> None:
        """Auto mode is not implemented in Phase 1."""
        fake_llm = FakeLLMClient(
            CompletionResponse(command="ls", explanation="test")
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
            get_window_size=lambda: (24, 80),
        )

        executor.execute("do something", "auto")

        assert pty_written == []
        assert fake_llm.last_query == ""

        os.close(stdin_r)
        os.close(stdin_w)
        os.close(stdout_r)
        os.close(stdout_w)

    def test_overlay_output_written(self) -> None:
        """Verify ANSI escape sequences are written to stdout."""
        fake_llm = FakeLLMClient(
            CompletionResponse(command="pwd", explanation="print directory")
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
            get_window_size=lambda: (24, 80),
        )

        os.write(stdin_w, b"\r")

        executor.execute("where am i", "completion")

        os.close(stdout_w)
        stdout_output = b""
        while True:
            chunk = os.read(stdout_r, 4096)
            if not chunk:
                break
            stdout_output += chunk

        assert b"> pwd" in stdout_output
        assert b"print directory" in stdout_output

        os.close(stdin_r)
        os.close(stdin_w)
        os.close(stdout_r)
