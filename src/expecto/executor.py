from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from openai.types.responses import ResponseInputParam

    from expecto.dangerous import DangerousCommandMatcher
    from expecto.io_relay import IORelay
    from expecto.screen_buffer import ScreenBuffer
    from expecto.working_summary import WorkingSummaryManager

from expecto.llm_client import (
    ParsedToolCall,
    RiskLevel,
    RunCommandParams,
    StepResult,
)
from expecto.settle import SettleResult

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


class LLMStepper(Protocol):
    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
    ) -> StepResult: ...

    def build_initial_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam: ...


class Executor:
    def __init__(
        self,
        llm_client: LLMStepper,
        screen_buffer: ScreenBuffer,
        write_to_pty: Callable[[bytes], None],
        stdout_fd: int,
        relay: IORelay,
        settle_ms: int = 500,
        threshold_ms: int = 30000,
        max_steps: int = 15,
        history_lines: int = 500,
        working_summary: WorkingSummaryManager | None = None,
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._write_to_pty = write_to_pty
        self._stdout_fd = stdout_fd
        self._relay = relay
        self._settle_ms = settle_ms
        self._threshold_ms = threshold_ms
        self._max_steps = max_steps
        self._history_lines = history_lines
        self._working_summary = working_summary
        self._confirm_level = confirm_level
        self._dangerous_matcher = dangerous_matcher

    async def _thinking(
        self, awaitable: Awaitable[T], label: bytes = b"[thinking...]",
    ) -> T:
        """Await with a gray thinking overlay shown and pty reading
        paused (so child output cannot corrupt the overlay); both are
        restored on every exit path, including cancellation."""
        self._relay.pause_pty()
        os.write(self._stdout_fd, b"\x1b7\x1b[90m" + label + b"\x1b[0m")
        try:
            return await awaitable
        finally:
            os.write(self._stdout_fd, b"\x1b8\x1b[J")
            self._relay.resume_pty()

    async def _ensure_working_summary(self) -> str | None:
        """Bring the working summary up to date and return its prompt text."""
        if self._working_summary is None:
            return None
        await self._working_summary.ensure_summary()
        return self._working_summary.summary_text

    async def execute(self, query: str) -> None:
        """Run one execution (the LLM command-execute-observe loop).
        Runs as the relay's execution task; user Ctrl-C arrives as task
        cancellation."""
        try:
            # The finalized query line took over the prompt line on the
            # outer tty; nudge the shell with a newline so a fresh prompt
            # is shown (and captured) before the first command is injected.
            self._write_to_pty(b"\n")
            await self._relay.wait_settle(self._settle_ms, self._threshold_ms)

            working_summary = await self._thinking(self._ensure_working_summary())
            screen_text = self._screen.get_text(last_n=self._history_lines)
            input_items: ResponseInputParam = self._llm.build_initial_messages(
                query, screen_text,
            )
            prev_response_id: str | None = None

            step = 0
            checkpoint = self._max_steps
            while True:
                step += 1
                result = await self._thinking(
                    self._llm.step(
                        input_items,
                        previous_response_id=prev_response_id,
                        working_summary=working_summary,
                    ),
                )
                prev_response_id = result.response_id

                if not result.tool_calls:
                    final = result.final
                    if final is not None and final.status == "done":
                        self._show_end(f"done: {final.explanation}")
                    elif final is not None:
                        self._show_end(f"failed: {final.explanation}")
                    else:
                        self._show_end("failed: empty response")
                    return

                outputs: ResponseInputParam = []
                for call in result.tool_calls:
                    output_text = await self._run_tool_call(call)
                    outputs.append({
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": output_text,
                    })
                input_items = outputs

                if step == checkpoint:
                    if not await self._confirm_continue(step):
                        self._show_end(f"stopped at step {step}")
                        return
                    checkpoint += self._max_steps
        except asyncio.CancelledError:
            self._show_end("interrupted")
            raise
        finally:
            self._relay.flush_sgr_reset()
            # The pty (or the whole session) may already be gone when we
            # get here through cancellation at EOF.
            with contextlib.suppress(OSError):
                self._write_to_pty(b"\n")

    async def _run_tool_call(self, call: ParsedToolCall) -> str:
        """Execute one tool call and build its function_call_output text."""
        if call.params is None:
            return f"Error: {call.error}"

        if isinstance(call.params, RunCommandParams):
            risk, matched = self._effective_risk(call.params)
            if self._needs_confirmation(risk):
                detail = (
                    f"matched pattern: {matched}"
                    if matched is not None
                    else call.params.risk_reason
                )
                if not await self._confirm_command(call.params.command, risk, detail):
                    return DECLINED_TOOL_OUTPUT

        mark = self._screen.mark()

        if isinstance(call.params, RunCommandParams):
            # Show the echoed command in gray, like expecto's own status
            # messages; the relay pump resets right after the echoed Enter
            # (first relayed newline), or flush_sgr_reset() below when no
            # newline arrives before settle / interrupt.
            os.write(self._stdout_fd, b"\x1b[90m")
            self._relay.arm_sgr_reset_at_newline()
            self._write_to_pty(call.params.command.encode() + b"\n")
        else:
            ctrl_bytes = CONTROL_CHAR_MAP.get(call.params.key)
            if ctrl_bytes is None:
                return f"Error: unknown control key: {call.params.key}"
            self._write_to_pty(ctrl_bytes)

        try:
            settle_result = await self._relay.wait_settle(
                self._settle_ms, self._threshold_ms,
            )
        finally:
            self._relay.flush_sgr_reset()

        output_text = f"## Terminal Output\n```\n{self._screen.get_text_since(mark)}\n```"
        if settle_result == SettleResult.THRESHOLD:
            threshold_s = self._threshold_ms / 1000
            output_text += (
                f"\n[Note] Output did not settle within {threshold_s:g}s. "
                "The command may still be running."
            )
        return output_text

    async def _confirm_continue(self, step: int) -> bool:
        """Ask the user whether to continue past the step checkpoint."""
        return await self._prompt_yes_no(
            f"[expecto] reached step {step} — continue?",
        )

    async def _confirm_command(self, command: str, risk: str, detail: str) -> bool:
        """Ask the user whether to run a command that hit the confirm level."""
        return await self._prompt_yes_no(
            f"[expecto] {risk} command ({detail}): {command} — run?",
        )

    async def _prompt_yes_no(self, message: str) -> bool:
        """Show a y/n prompt and wait for the answer. The relay pump keeps
        relaying pty output meanwhile, so the screen stays live. Ctrl-C and
        Esc answer "no" here (they do not cancel the execution task); a
        session that goes away entirely arrives as cancellation instead."""
        os.write(
            self._stdout_fd,
            f"\r\n\x1b[90m{message} (y/n) \x1b[0m".encode(),
        )
        key = await self._relay.wait_key(
            frozenset({0x79, 0x59, 0x6E, 0x4E, 0x1B, 0x03}),  # y Y n N Esc Ctrl-C
        )
        if key in (0x79, 0x59):
            os.write(self._stdout_fd, b"y\r\n")
            return True
        os.write(self._stdout_fd, b"n\r\n")
        return False

    def _show_end(self, label: str) -> None:
        # The outer tty is in raw mode (no ONLCR), so bare "\n" moves down
        # without returning to column 0; normalize to "\r\n".
        text = f"[expecto {label}]".replace("\r\n", "\n").replace("\n", "\r\n")
        os.write(
            self._stdout_fd,
            f"\x1b[90m{text}\x1b[0m".encode(),
        )

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
