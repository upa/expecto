from __future__ import annotations

import os
import select
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai.types.responses import ResponseInputParam

    from jou.dangerous import DangerousCommandMatcher
    from jou.screen_buffer import ScreenBuffer
    from jou.working_summary import WorkingSummaryManager

from jou.background import BackgroundCall
from jou.llm_client import (
    AutoStepResult,
    CompletionResponse,
    ParsedToolCall,
    RiskLevel,
    RunCommandParams,
)
from jou.settle import SettleDetector, SettleResult

T = TypeVar("T")

ConfirmLevel = Literal["destructive", "mutating", "none"]

DECLINED_TOOL_OUTPUT = (
    "Command was not executed: the user declined to run it. "
    "Take a different approach, or finish with status \"fail\" if the goal "
    "cannot be achieved without it."
)

CONTROL_CHAR_MAP: dict[str, bytes] = {
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-z": b"\x1a",
}


class LLMCompleter(Protocol):
    def complete(
        self,
        screen_text: str,
        query: str,
        working_summary: str | None = None,
    ) -> CompletionResponse: ...

    def auto_step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
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
        max_auto_steps: int = 15,
        history_lines: int = 500,
        working_summary: WorkingSummaryManager | None = None,
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
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
        self._history_lines = history_lines
        self._working_summary = working_summary
        self._confirm_level = confirm_level
        self._dangerous_matcher = dangerous_matcher

    def execute(self, query: str, mode: str) -> None:
        if mode == "completion":
            self._execute_completion(query)
        elif mode == "auto":
            self._execute_auto(query)

    def _ensure_working_summary(self) -> str | None:
        """Bring the working summary up to date and return its prompt text."""
        if self._working_summary is None:
            return None
        self._working_summary.ensure_summary()
        return self._working_summary.summary_text

    def _execute_completion(self, query: str) -> None:
        os.write(self._stdout_fd, b"\x1b7\x1b[90m[thinking...]\x1b[0m")

        working_summary = self._ensure_working_summary()
        screen_text = self._screen.get_text(last_n=self._history_lines)
        response = self._llm.complete(screen_text, query, working_summary=working_summary)

        os.write(self._stdout_fd, b"\x1b8\x1b[J")
        self._write_to_pty(response.command.encode())

    def _execute_auto(self, query: str) -> None:
        assert self._get_pty_master_fd is not None
        pty_master_fd = self._get_pty_master_fd()

        # The finalized query line took over the prompt line on the outer
        # tty; nudge the shell with a newline so a fresh prompt is shown
        # (and captured) before the first command is injected.
        self._write_to_pty(b"\n")
        _, interrupted = self._wait_for_settle(pty_master_fd)
        if interrupted:
            self._show_auto_end("interrupted")
            return

        os.write(self._stdout_fd, b"\x1b7\x1b[90m[thinking...]\x1b[0m")
        summary_call = BackgroundCall(self._ensure_working_summary).start()
        if self._wait_for_llm(summary_call):
            os.write(self._stdout_fd, b"\x1b8\x1b[J")
            self._show_auto_end("interrupted")
            return
        working_summary = summary_call.result()
        os.write(self._stdout_fd, b"\x1b8\x1b[J")

        screen_text = self._screen.get_text(last_n=self._history_lines)
        input_items: ResponseInputParam = self._llm.build_auto_messages(query, screen_text)
        prev_response_id: str | None = None

        step = 0
        checkpoint = self._max_auto_steps
        while True:
            step += 1
            self._show_auto_thinking()

            step_call = BackgroundCall(partial(
                self._llm.auto_step,
                input_items,
                previous_response_id=prev_response_id,
                working_summary=working_summary,
            )).start()
            if self._wait_for_llm(step_call):
                os.write(self._stdout_fd, b"\x1b8\x1b[J")
                self._show_auto_end("interrupted")
                break
            result = step_call.result()
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

            if step == checkpoint:
                if not self._confirm_continue(pty_master_fd, step):
                    self._show_auto_end(f"stopped at step {step}")
                    break
                checkpoint += self._max_auto_steps

        self._write_to_pty(b"\n")

    def _effective_risk(self, params: RunCommandParams) -> tuple[RiskLevel, str | None]:
        """The command's risk after the deterministic backstop.

        A pattern match escalates to destructive regardless of the LLM's
        own classification; it never de-escalates."""
        matched: str | None = None
        if self._dangerous_matcher is not None:
            matched = self._dangerous_matcher.match(params.command)
        if matched is not None:
            return "destructive", matched
        return params.risk, None

    def _needs_confirmation(self, risk: RiskLevel) -> bool:
        if self._confirm_level == "none":
            return False
        if self._confirm_level == "mutating":
            return risk in ("mutating", "destructive")
        return risk == "destructive"

    def _run_tool_call(
        self, call: ParsedToolCall, pty_master_fd: int,
    ) -> tuple[str, bool]:
        """Execute one tool call and build its output. Returns (output, interrupted)."""
        if call.params is None:
            return f"Error: {call.error}", False

        if isinstance(call.params, RunCommandParams):
            risk, matched = self._effective_risk(call.params)
            if self._needs_confirmation(risk):
                detail = (
                    f"matched pattern: {matched}"
                    if matched is not None
                    else call.params.risk_reason
                )
                if not self._confirm_command(
                    pty_master_fd, call.params.command, risk, detail,
                ):
                    return DECLINED_TOOL_OUTPUT, False

        mark = self._screen.mark()

        reset_color_at_newline = False
        if isinstance(call.params, RunCommandParams):
            # Show the echoed command in gray, like jou's own status
            # messages; the reset is emitted once the echoed Enter (first
            # relayed newline) arrives.
            os.write(self._stdout_fd, b"\x1b[90m")
            reset_color_at_newline = True
            self._write_to_pty(call.params.command.encode() + b"\n")
        else:
            ctrl_bytes = CONTROL_CHAR_MAP.get(call.params.key)
            if ctrl_bytes is None:
                return f"Error: unknown control key: {call.params.key}", False
            self._write_to_pty(ctrl_bytes)

        settle_result, interrupted = self._wait_for_settle(
            pty_master_fd, reset_color_at_newline=reset_color_at_newline,
        )
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

    def _wait_for_llm(self, call: BackgroundCall[T]) -> bool:
        """Wait for a background LLM call while watching stdin for Ctrl-C.

        Returns True when the user interrupted; the call is then abandoned
        (its daemon thread finishes on its own and the result is dropped).
        Other keys typed while waiting are discarded, matching
        _wait_for_settle."""
        if self._stdin_fd < 0:
            call.wait()
            return False

        while not call.done:
            try:
                rlist, _, _ = select.select([self._stdin_fd], [], [], 0.1)
            except (OSError, ValueError):
                call.wait()
                return False
            if rlist:
                try:
                    data = os.read(self._stdin_fd, 4096)
                except OSError:
                    call.wait()
                    return False
                if 0x03 in data:
                    return True
        return False

    def _show_auto_thinking(self) -> None:
        os.write(self._stdout_fd, b"\x1b7")
        os.write(self._stdout_fd, b"\x1b[90m[auto: thinking...]\x1b[0m")

    def _confirm_continue(self, pty_master_fd: int, step: int) -> bool:
        """Ask the user whether to continue past the step checkpoint."""
        return self._prompt_yes_no(
            pty_master_fd, f"[auto] reached step {step} — continue?",
        )

    def _confirm_command(
        self, pty_master_fd: int, command: str, risk: str, detail: str,
    ) -> bool:
        """Ask the user whether to run a command that hit the confirm level."""
        return self._prompt_yes_no(
            pty_master_fd,
            f"[auto] {risk} command ({detail}): {command} — run?",
        )

    def _prompt_yes_no(self, pty_master_fd: int, message: str) -> bool:
        """Show a y/n prompt and wait for the answer.

        Keeps relaying pty output while waiting so the screen stays live.
        Returns False when no interactive stdin is available."""
        if self._stdin_fd < 0:
            return False

        os.write(
            self._stdout_fd,
            f"\r\n\x1b[90m{message} (y/n) \x1b[0m".encode(),
        )

        watch_fds: list[int] = [self._stdin_fd]
        if pty_master_fd >= 0:
            watch_fds.append(pty_master_fd)

        while True:
            try:
                rlist, _, _ = select.select(watch_fds, [], [])
            except (OSError, ValueError):
                return False

            if pty_master_fd in rlist:
                try:
                    data = os.read(pty_master_fd, 4096)
                except OSError:
                    return False
                if not data:
                    return False
                self._screen.feed(data)
                os.write(self._stdout_fd, data)

            if self._stdin_fd in rlist:
                try:
                    data = os.read(self._stdin_fd, 4096)
                except OSError:
                    return False
                for byte in data:
                    if byte in (0x79, 0x59):  # y, Y
                        os.write(self._stdout_fd, b"y\r\n")
                        return True
                    if byte in (0x6E, 0x4E, 0x1B, 0x03):  # n, N, Esc, Ctrl-C
                        os.write(self._stdout_fd, b"n\r\n")
                        return False

    def _show_auto_end(self, label: str) -> None:
        # The outer tty is in raw mode (no ONLCR), so bare "\n" moves down
        # without returning to column 0; normalize to "\r\n".
        text = f"[auto {label}]".replace("\r\n", "\n").replace("\n", "\r\n")
        os.write(
            self._stdout_fd,
            f"\x1b[90m{text}\x1b[0m".encode(),
        )

    def _wait_for_settle(
        self, pty_master_fd: int, reset_color_at_newline: bool = False,
    ) -> tuple[SettleResult | None, bool]:
        """Wait until output settles. Returns (settle result, interrupted by user).

        With reset_color_at_newline, the caller has switched the outer tty
        color before injecting a command; an SGR reset is emitted right after
        the first relayed newline (the echoed Enter, ending the echoed
        command line). The reset is guaranteed on every exit path."""
        detector = SettleDetector(
            settle_ms=self._settle_ms,
            threshold_ms=self._threshold_ms,
        )
        detector.start()

        watch_fds: list[int] = [pty_master_fd]
        if self._stdin_fd >= 0:
            watch_fds.append(self._stdin_fd)

        pending_reset = reset_color_at_newline
        result: SettleResult | None = None
        try:
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
                    newline_at = data.find(b"\n") if pending_reset else -1
                    if newline_at >= 0:
                        os.write(self._stdout_fd, data[: newline_at + 1])
                        os.write(self._stdout_fd, b"\x1b[0m")
                        os.write(self._stdout_fd, data[newline_at + 1:])
                        pending_reset = False
                    else:
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
        finally:
            if pending_reset:
                os.write(self._stdout_fd, b"\x1b[0m")
