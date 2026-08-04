import os
from unittest.mock import MagicMock

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
    def test_completion_injects_command(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="ls -la", explanation="list all files")
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdout_fd=stdout_w,
        )

        executor.execute("list files", "completion")

        assert pty_written == [b"ls -la"]
        assert fake_llm.last_query == "list files"

        os.close(stdout_r)
        os.close(stdout_w)

    def test_thinking_indicator_shown(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="pwd", explanation="")
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: None,
            stdout_fd=stdout_w,
        )

        executor.execute("where am i", "completion")

        os.close(stdout_w)
        stdout_output = b""
        while True:
            chunk = os.read(stdout_r, 4096)
            if not chunk:
                break
            stdout_output += chunk

        assert b"thinking..." in stdout_output
        assert b"\x1b8" in stdout_output

        os.close(stdout_r)

    def test_auto_mode_noop(self) -> None:
        fake_llm = FakeLLMClient(
            CompletionResponse(command="ls", explanation="test")
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=lambda d: pty_written.append(d),
            stdout_fd=stdout_w,
        )

        executor.execute("do something", "auto")

        assert pty_written == []
        assert fake_llm.last_query == ""

        os.close(stdout_r)
        os.close(stdout_w)
