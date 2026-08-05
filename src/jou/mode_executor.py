from __future__ import annotations

import os
import select
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai.types.responses import ResponseInputParam

    from jou.screen_buffer import ScreenBuffer

from jou.llm_client import (
    AutoStepResult,
    CompletionResponse,
    ParsedToolCall,
    RunCommandParams,
)
from jou.settle import SettleDetector, SettleResult

CONTROL_CHAR_MAP: dict[str, bytes] = {
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-z": b"\x1a",
}


class LLMCompleter(Protocol):
    def complete(self, screen_text: str, query: str) -> CompletionResponse: ...

    def auto_step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
    ) -> AutoStepResult: ...

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam: ...


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
        input_items: ResponseInputParam = self._llm.build_auto_messages(query, screen_text)
        prev_response_id: str | None = None

        for step in range(1, self._max_auto_steps + 1):
            self._show_auto_thinking(step)

            result = self._llm.auto_step(
                input_items, previous_response_id=prev_response_id,
            )
            prev_response_id = result.response_id

            os.write(self._stdout_fd, b"\x1b8\x1b[J")

            if not result.tool_calls:
                final = result.final
                if final is not None and final.status == "done":
                    self._show_auto_end(f"done: {final.explanation}")
                elif final is not None:
                    self._show_auto_end(f"failed: {final.explanation}")
                else:
                    self._show_auto_end("failed: empty response")
                break

            outputs: ResponseInputParam = []
            interrupted = False
            for call in result.tool_calls:
                output_text, interrupted = self._run_tool_call(call, pty_master_fd)
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": output_text,
                })
                if interrupted:
                    break
            if interrupted:
                self._show_auto_end("interrupted")
                break
            input_items = outputs
        else:
            self._show_auto_end(
                f"max steps ({self._max_auto_steps})"
            )

        self._write_to_pty(b"\n")

    def _run_tool_call(
        self, call: ParsedToolCall, pty_master_fd: int,
    ) -> tuple[str, bool]:
        """Execute one tool call and build its output. Returns (output, interrupted)."""
        if call.params is None:
            return f"Error: {call.error}", False

        mark = self._screen.mark()

        if isinstance(call.params, RunCommandParams):
            self._write_to_pty(call.params.command.encode() + b"\n")
        else:
            ctrl_bytes = CONTROL_CHAR_MAP.get(call.params.key)
            if ctrl_bytes is None:
                return f"Error: unknown control key: {call.params.key}", False
            self._write_to_pty(ctrl_bytes)

        settle_result, interrupted = self._wait_for_settle(pty_master_fd)
        if interrupted:
            return "", True

        output_text = f"## Terminal Output\n```\n{self._screen.get_text_since(mark)}\n```"
        if settle_result == SettleResult.THRESHOLD:
            threshold_s = self._threshold_ms / 1000
            output_text += (
                f"\n[Note] Output did not settle within {threshold_s:g}s. "
                "The command may still be running."
            )
        return output_text, False

    def _show_auto_thinking(self, step: int) -> None:
        msg = f"[auto {step}/{self._max_auto_steps} thinking...]"
        os.write(self._stdout_fd, b"\x1b7")
        os.write(self._stdout_fd, f"\x1b[90m{msg}\x1b[0m".encode())

    def _show_auto_end(self, label: str) -> None:
        # The outer tty is in raw mode (no ONLCR), so bare "\n" moves down
        # without returning to column 0; normalize to "\r\n".
        text = f"[auto {label}]".replace("\r\n", "\n").replace("\n", "\r\n")
        os.write(
            self._stdout_fd,
            f"\x1b[90m{text}\x1b[0m".encode(),
        )

    def _wait_for_settle(
        self, pty_master_fd: int,
    ) -> tuple[SettleResult | None, bool]:
        """Wait until output settles. Returns (settle result, interrupted by user)."""
        detector = SettleDetector(
            settle_ms=self._settle_ms,
            threshold_ms=self._threshold_ms,
        )
        detector.start()

        watch_fds: list[int] = [pty_master_fd]
        if self._stdin_fd >= 0:
            watch_fds.append(self._stdin_fd)

        result: SettleResult | None = None
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
                    return result, True

            result = detector.check()
            if result is not None:
                break

        return result, False
