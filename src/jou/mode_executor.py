from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from jou.screen_buffer import ScreenBuffer

from jou.llm_client import CompletionResponse


class LLMCompleter(Protocol):
    def complete(self, screen_text: str, query: str) -> CompletionResponse: ...


class ModeExecutor:
    def __init__(
        self,
        llm_client: LLMCompleter,
        screen_buffer: ScreenBuffer,
        write_to_pty: Callable[[bytes], None],
        stdout_fd: int,
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._write_to_pty = write_to_pty
        self._stdout_fd = stdout_fd

    def execute(self, query: str, mode: str) -> None:
        if mode == "completion":
            self._execute_completion(query)

    def _execute_completion(self, query: str) -> None:
        os.write(self._stdout_fd, b"\x1b[90m [thinking...]\x1b[0m")

        screen_text = self._screen.get_text()
        response = self._llm.complete(screen_text, query)

        os.write(self._stdout_fd, b"\x1b8\x1b[K")
        self._write_to_pty(response.command.encode())
