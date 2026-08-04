from __future__ import annotations

import os
import select
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai.types.responses import EasyInputMessageParam

    from jou.screen_buffer import ScreenBuffer

from jou.llm_client import AutoResponse, CompletionResponse
from jou.settle import SettleDetector

CONTROL_CHAR_MAP: dict[str, bytes] = {
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-z": b"\x1a",
}


class LLMCompleter(Protocol):
    def complete(self, screen_text: str, query: str) -> CompletionResponse: ...

    def auto_step(
        self, messages: list[EasyInputMessageParam]
    ) -> AutoResponse: ...

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
        history: list[EasyInputMessageParam],
    ) -> list[EasyInputMessageParam]: ...


class ModeExecutor:
    def __init__(
        self,
        llm_client: LLMCompleter,
        screen_buffer: ScreenBuffer,
        write_to_pty: Callable[[bytes], None],
        stdout_fd: int,
        stdin_fd: int = -1,
        get_pty_master_fd: Callable[[], int] | None = None,
        settle_ms: int = 500,
        threshold_ms: int = 30000,
        max_auto_steps: int = 10,
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._write_to_pty = write_to_pty
        self._stdout_fd = stdout_fd
        self._stdin_fd = stdin_fd
        self._get_pty_master_fd = get_pty_master_fd
        self._settle_ms = settle_ms
        self._threshold_ms = threshold_ms
        self._max_auto_steps = max_auto_steps

    def execute(self, query: str, mode: str) -> None:
        if mode == "completion":
            self._execute_completion(query)
        elif mode == "auto":
            self._execute_auto(query)

    def _execute_completion(self, query: str) -> None:
        os.write(self._stdout_fd, b"\x1b7\x1b[90m[thinking...]\x1b[0m")

        screen_text = self._screen.get_text()
        response = self._llm.complete(screen_text, query)

        os.write(self._stdout_fd, b"\x1b8\x1b[J")
        self._write_to_pty(response.command.encode())

    def _execute_auto(self, query: str) -> None:
        assert self._get_pty_master_fd is not None
        pty_master_fd = self._get_pty_master_fd()

        screen_text = self._screen.get_text()
        history: list[EasyInputMessageParam] = []

        for step in range(1, self._max_auto_steps + 1):
            self._show_auto_thinking(step)

            messages = self._llm.build_auto_messages(query, screen_text, history)
            response = self._llm.auto_step(messages)

            os.write(self._stdout_fd, b"\x1b8\x1b[J")

            if response.status == "done":
                self._show_auto_end(f"done: {response.explanation}")
                break
            if response.status == "fail":
                self._show_auto_end(f"failed: {response.explanation}")
                break

            if response.action is not None:
                if response.action.type == "command":
                    self._write_to_pty(
                        response.action.value.encode() + b"\n"
                    )
                elif response.action.type == "control":
                    ctrl_bytes = CONTROL_CHAR_MAP.get(response.action.value)
                    if ctrl_bytes:
                        self._write_to_pty(ctrl_bytes)

            interrupted = self._wait_for_settle(pty_master_fd)
            if interrupted:
                self._show_auto_end("interrupted")
                break

            new_screen_text = self._screen.get_text()
            history.append({
                "role": "assistant",
                "content": response.model_dump_json(),
            })
            history.append({
                "role": "user",
                "content": (
                    f"## コマンド実行結果\n```\n{new_screen_text}\n```"
                ),
            })
        else:
            self._show_auto_end(
                f"max steps ({self._max_auto_steps})"
            )

        self._write_to_pty(b"\n")

    def _show_auto_thinking(self, step: int) -> None:
        msg = f"[auto {step}/{self._max_auto_steps} thinking...]"
        os.write(self._stdout_fd, b"\x1b7")
        os.write(self._stdout_fd, f"\x1b[90m{msg}\x1b[0m".encode())

    def _show_auto_end(self, label: str) -> None:
        os.write(
            self._stdout_fd,
            f"\x1b[90m[auto {label}]\x1b[0m".encode(),
        )

    def _wait_for_settle(self, pty_master_fd: int) -> bool:
        detector = SettleDetector(
            settle_ms=self._settle_ms,
            threshold_ms=self._threshold_ms,
        )
        detector.start()

        watch_fds: list[int] = [pty_master_fd]
        if self._stdin_fd >= 0:
            watch_fds.append(self._stdin_fd)

        while detector.active:
            timeout_s = detector.remaining_ms() / 1000.0
            try:
                rlist, _, _ = select.select(watch_fds, [], [], timeout_s)
            except (OSError, ValueError):
                break

            if pty_master_fd in rlist:
                try:
                    data = os.read(pty_master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._screen.feed(data)
                os.write(self._stdout_fd, data)
                detector.on_output()

            if self._stdin_fd in rlist:
                try:
                    data = os.read(self._stdin_fd, 4096)
                except OSError:
                    break
                if 0x03 in data:
                    return True

            result = detector.check()
            if result is not None:
                break

        return False
