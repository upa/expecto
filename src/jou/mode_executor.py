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
        stdin_fd: int,
        stdout_fd: int,
        get_window_size: Callable[[], tuple[int, int]],
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._write_to_pty = write_to_pty
        self._stdin_fd = stdin_fd
        self._stdout_fd = stdout_fd
        self._get_window_size = get_window_size

    def execute(self, query: str, mode: str) -> None:
        if mode == "completion":
            self._execute_completion(query)

    def _execute_completion(self, query: str) -> None:
        self._show_status("thinking...")

        screen_text = self._screen.get_text()
        response = self._llm.complete(screen_text, query)

        self._show_result(response.command, response.explanation)

        confirmed = self._wait_confirmation()

        self._clear_overlay()

        if confirmed:
            self._write_to_pty(response.command.encode())

    def _show_status(self, text: str) -> None:
        rows, _cols = self._get_window_size()
        line = f"jou: {text}"
        output = (
            f"\x1b7"
            f"\x1b[{rows};1H"
            f"\x1b[2K"
            f"\x1b[7m{line}\x1b[0m"
            f"\x1b8"
        )
        os.write(self._stdout_fd, output.encode())

    def _show_result(self, command: str, explanation: str) -> None:
        rows, cols = self._get_window_size()
        cmd_line = f"> {command}"[:cols]
        exp_line = f"  {explanation}"[:cols]
        hint = " [Enter/Esc]"
        exp_with_hint = (exp_line + hint)[:cols]
        output = (
            f"\x1b7"
            f"\x1b[{rows - 1};1H"
            f"\x1b[2K"
            f"\x1b[7m{cmd_line}\x1b[0m"
            f"\x1b[{rows};1H"
            f"\x1b[2K"
            f"\x1b[2m{exp_with_hint}\x1b[0m"
            f"\x1b8"
        )
        os.write(self._stdout_fd, output.encode())

    def _wait_confirmation(self) -> bool:
        while True:
            try:
                data = os.read(self._stdin_fd, 1)
            except OSError:
                return False
            if not data:
                return False
            byte = data[0]
            if byte == 0x0D:
                return True
            if byte in (0x1B, 0x03):
                return False

    def _clear_overlay(self) -> None:
        rows, _cols = self._get_window_size()
        output = (
            f"\x1b7"
            f"\x1b[{rows - 1};1H"
            f"\x1b[2K"
            f"\x1b[{rows};1H"
            f"\x1b[2K"
            f"\x1b8"
        )
        os.write(self._stdout_fd, output.encode())
