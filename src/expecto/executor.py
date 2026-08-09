from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from expecto.dangerous import DangerousCommandMatcher
    from expecto.io_relay import IORelay
    from expecto.screen_buffer import ScreenBuffer, ScreenMark
    from expecto.skills import Skill

from expecto.keys import parse_keys
from expecto.llm_client import (
    LoadSkillParams,
    MCPToolParams,
    ParsedToolCall,
    RiskLevel,
    RunCommandParams,
    SendKeysParams,
    StepResult,
)
from expecto.mcp_client import TOOL_NAME_PREFIX
from expecto.skills import render_skill, render_skill_file
from expecto.settle import SettleResult

T = TypeVar("T")

ConfirmLevel = Literal["destructive", "mutating", "none"]

DECLINED_TOOL_OUTPUT = (
    "Command was not executed: the user declined to run it. "
    "Take a different approach, or stop calling tools and explain why the "
    "goal cannot be achieved without it."
)

INTERRUPTED_TOOL_OUTPUT = (
    "The task was interrupted by the user before this command was executed."
)



class MCPCaller(Protocol):
    """The MCP servers, as seen by the Executor."""

    async def call(self, qualified_name: str, arguments: dict[str, object]) -> str: ...


def _mcp_label(qualified_name: str) -> str:
    """"mcp__SERVER__tool" as the "SERVER.tool" shown on screen."""
    parts = qualified_name.split("__", 2)
    if len(parts) == 3 and parts[0] == TOOL_NAME_PREFIX:
        return f"{parts[1]}.{parts[2]}"
    return qualified_name


class LLMStepper(Protocol):
    """The conversation-owning LLM client, as seen by the Executor.

    The client keeps the whole message history (queries, tool calls and
    outputs, final responses) and compacts it transparently; the Executor
    only starts queries, records tool outputs, and steps."""

    async def start_query(
        self,
        query: str,
        *,
        screen_text: str,
        new_output: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult: ...

    def add_tool_output(self, call_id: str, output: str) -> None: ...

    async def step(
        self, on_delta: Callable[[str], None] | None = None,
    ) -> StepResult: ...

    def finish_query(self) -> None: ...


class _StreamingFinal:
    """Streams the final response text to the outer tty as it arrives.

    Created per step. On the first delta it erases the [thinking...]
    overlay (the cursor was saved right before the overlay was drawn)
    and opens the gray "[" prefix; close() ends the message with "]"
    and an SGR reset."""

    def __init__(self, stdout_fd: int) -> None:
        self._stdout_fd = stdout_fd
        self.started = False

    def on_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self.started:
            self.started = True
            os.write(self._stdout_fd, b"\x1b8\x1b[J\x1b[90m[")
        # The outer tty is in raw mode (no ONLCR); normalize to "\r\n".
        text = delta.replace("\r\n", "\n").replace("\n", "\r\n")
        os.write(self._stdout_fd, text.encode())

    def close(self) -> None:
        if self.started:
            self.started = False
            os.write(self._stdout_fd, b"]\x1b[0m")


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
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
        new_output_max_chars: int = 20000,
        mcp: MCPCaller | None = None,
        skills: list[Skill] | None = None,
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._write_to_pty = write_to_pty
        self._stdout_fd = stdout_fd
        self._relay = relay
        self._settle_ms = settle_ms
        self._threshold_ms = threshold_ms
        self._max_steps = max_steps
        self._confirm_level = confirm_level
        self._dangerous_matcher = dangerous_matcher
        self._new_output_max_chars = new_output_max_chars
        self._mcp = mcp
        self._skills: dict[str, Skill] = {s.name: s for s in skills or []}

        # Where the previous execution ended: the next query sends only
        # the terminal output produced after this point.
        self._last_mark: ScreenMark | None = None

    async def _thinking(
        self, awaitable: Awaitable[T], label: bytes = b"[thinking...]",
        stream: _StreamingFinal | None = None,
    ) -> T:
        """Await with a gray thinking overlay shown and pty reading
        paused (so child output cannot corrupt the overlay); both are
        restored on every exit path, including cancellation. When a
        streaming final display has started, it already erased the
        overlay and the streamed text must not be wiped, so the erase
        is skipped."""
        self._relay.pause_pty()
        os.write(self._stdout_fd, b"\x1b7\x1b[90m" + label + b"\x1b[0m")
        try:
            return await awaitable
        finally:
            if stream is None or not stream.started:
                os.write(self._stdout_fd, b"\x1b8\x1b[J")
            self._relay.resume_pty()

    async def execute(self, query: str) -> None:
        """Run one execution (the LLM command-execute-observe loop) on the
        session-long conversation held by the LLM client. Runs as the
        relay's execution task; user Ctrl-C arrives as task cancellation."""
        stream: _StreamingFinal | None = None
        try:
            # The finalized query line took over the prompt line on the
            # outer tty; break the line there — display only. Writing
            # "\n" to the pty would execute anything the user had typed
            # but deliberately not submitted; a pending input line must
            # survive so the LLM can inspect or fix it with send_keys.
            os.write(self._stdout_fd, b"\r\n")
            await self._relay.wait_settle(self._settle_ms, self._threshold_ms)

            result: StepResult | None = None
            step = 0
            # max_steps < 1 disables the checkpoint: step (>= 1) never
            # reaches it, so the loop runs without asking.
            checkpoint = self._max_steps
            while True:
                step += 1
                stream = _StreamingFinal(self._stdout_fd)
                if result is None:
                    # First step of the query. The client picks the
                    # context: the full screen on a fresh conversation
                    # (session start / right after a compaction), the new
                    # output since the previous task otherwise.
                    coro = self._llm.start_query(
                        query,
                        screen_text=self._cap_tail(self._screen.get_text()),
                        new_output=self._new_output_since_last(),
                        on_delta=stream.on_delta,
                    )
                else:
                    coro = self._llm.step(on_delta=stream.on_delta)
                result = await self._thinking(coro, stream=stream)

                if not result.tool_calls:
                    if stream.started:
                        stream.close()
                    elif result.final_text:
                        self._show_final(result.final_text)
                    else:
                        self._show_end("(empty response)")
                    return
                # Text alongside tool calls (the model narrating before a
                # call) has already been streamed; end the open message.
                stream.close()

                index = 0
                try:
                    for index, call in enumerate(result.tool_calls):
                        await self._run_tool_call(call)
                except asyncio.CancelledError:
                    # _run_tool_call records its own call on every exit
                    # path; record the calls that never started so the
                    # conversation is not left with an unanswered call.
                    for missed in result.tool_calls[index + 1:]:
                        self._llm.add_tool_output(
                            missed.call_id, INTERRUPTED_TOOL_OUTPUT,
                        )
                    raise

                if step == checkpoint:
                    if not await self._confirm_continue(step):
                        self._show_end(f"stopped at step {step}")
                        return
                    checkpoint += self._max_steps
        except asyncio.CancelledError:
            if stream is not None and stream.started:
                # Keep the partially streamed text on screen (rewinding is
                # unreliable once it may have scrolled); just end the gray
                # and break the line before the interrupted note.
                os.write(self._stdout_fd, b"\x1b[0m\r\n")
            self._show_end("interrupted")
            raise
        finally:
            self._relay.flush_sgr_reset()
            # Line break on the outer tty only, for the same reason as at
            # the start: the pty may now hold input the LLM prepared for
            # the user to review. The fd may already be gone at EOF.
            with contextlib.suppress(OSError):
                os.write(self._stdout_fd, b"\r\n")
            # Track where this execution ended so the next query can send
            # only the terminal output produced in between.
            self._last_mark = self._screen.mark()
            # The query is over (done, stopped, or interrupted); let the
            # client compact the conversation in the background if needed.
            self._llm.finish_query()

    def _new_output_since_last(self) -> str | None:
        """Terminal output produced since the previous execution ended
        (the user's manual activity between queries), tail-capped at
        new_output_max_chars."""
        if self._last_mark is None:
            return None
        text = self._screen.get_text_since(self._last_mark)
        if not text.strip():
            return None
        return self._cap_tail(text)

    def _cap_tail(self, text: str) -> str:
        """Cap terminal context at new_output_max_chars, keeping the tail
        (the most recent output, closest to the query)."""
        if len(text) > self._new_output_max_chars:
            return (
                "[... earlier output truncated ...]\n"
                + text[len(text) - self._new_output_max_chars:]
            )
        return text

    async def _run_tool_call(self, call: ParsedToolCall) -> None:
        """Execute one tool call and record its output on the
        conversation. Every exit path — normal, declined, error, and
        cancellation — records exactly one output for the call, so the
        conversation never carries an unanswered tool call."""
        if call.params is None:
            self._llm.add_tool_output(call.call_id, f"Error: {call.error}")
            return

        if isinstance(call.params, LoadSkillParams):
            self._run_load_skill(call.call_id, call.params)
            return

        if isinstance(call.params, MCPToolParams):
            await self._run_mcp_call(call.call_id, call.params)
            return

        if isinstance(call.params, RunCommandParams):
            risk, matched = self._effective_risk(call.params)
            if self._needs_confirmation(risk):
                detail = (
                    f"matched pattern: {matched}"
                    if matched is not None
                    else call.params.risk_reason
                )
                try:
                    confirmed = await self._confirm_command(
                        call.params.command, risk, detail,
                    )
                except asyncio.CancelledError:
                    self._llm.add_tool_output(call.call_id, INTERRUPTED_TOOL_OUTPUT)
                    raise
                if not confirmed:
                    self._llm.add_tool_output(call.call_id, DECLINED_TOOL_OUTPUT)
                    return

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
            try:
                data = parse_keys(call.params.keys)
            except ValueError as exc:
                self._llm.add_tool_output(call.call_id, f"Error: {exc}")
                return

            self._write_to_pty(data)

        try:
            settle_result = await self._relay.wait_settle(
                self._settle_ms, self._threshold_ms,
            )
        except asyncio.CancelledError:
            # The keys were already sent; answer the call with whatever
            # arrived so far.
            self._llm.add_tool_output(
                call.call_id,
                self._tool_screen_output(call.params, mark)
                + "\n[Note] The task was interrupted by the user; the command "
                "may still be running.",
            )
            raise
        finally:
            self._relay.flush_sgr_reset()

        output_text = self._tool_screen_output(call.params, mark)
        if settle_result == SettleResult.THRESHOLD:
            threshold_s = self._threshold_ms / 1000
            output_text += (
                f"\n[Note] Output did not settle within {threshold_s:g}s. "
                "The command may still be running."
            )
        self._llm.add_tool_output(call.call_id, output_text)

    def _tool_screen_output(
        self, params: RunCommandParams | SendKeysParams, mark: ScreenMark,
    ) -> str:
        """The screen part of a run_command / send_keys tool result.

        run_command reports the new output the command produced;
        send_keys reports the visible screen plus the cursor, since keys
        usually drive a stateful screen (an editor, a pager, a prompt)
        where position matters more than a diff."""
        if isinstance(params, RunCommandParams):
            return f"## Terminal Output\n```\n{self._screen.get_text_since(mark)}\n```"
        row, col = self._screen.get_cursor()
        return (
            f"## Terminal Screen\n```\n{self._screen.get_visible_text()}\n```\n"
            f"[Cursor] row {row + 1}, column {col + 1}"
        )

    def _run_load_skill(self, call_id: str, params: LoadSkillParams) -> None:
        """Hand the LLM one of the user's skills, or a file bundled with
        it (design/Skills.md).

        Synchronous, and read here rather than through run_command: the
        skills live on the machine expecto runs on, which is not
        necessarily where the terminal is connected."""
        skill = self._skills.get(params.name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "(none)"
            self._llm.add_tool_output(
                call_id,
                f"Error: unknown skill: {params.name}. Available skills: {available}",
            )
            return

        label = f"{skill.name}/{params.file}" if params.file else skill.name
        self._write_gray(f"\r\n[expecto: skill {label}]\r\n")

        if not params.file:
            self._llm.add_tool_output(call_id, render_skill(skill))
            return
        try:
            output = render_skill_file(skill, params.file)
        except (OSError, ValueError) as exc:
            bundled = ", ".join(skill.files) or "(none)"
            output = (
                f"Error: cannot read {params.file!r} from skill {skill.name}: {exc}. "
                f"Files bundled with this skill: {bundled}"
            )
        self._llm.add_tool_output(call_id, output)

    async def _run_mcp_call(self, call_id: str, params: MCPToolParams) -> None:
        """Call an MCP tool and record its result (design/MCP.md §5).

        No mark / settle wait: an MCP call produces no terminal output.
        The call is shown on screen so it stays as visible as a command,
        and a failure comes back as text for the LLM rather than an
        abort."""
        if self._mcp is None:
            self._llm.add_tool_output(
                call_id, f"Error: no MCP server is available for {params.name}",
            )
            return

        self._write_gray(f"\r\n[expecto: mcp {_mcp_label(params.name)}]\r\n")
        try:
            output = await self._thinking(
                self._mcp.call(params.name, params.arguments),
            )
        except asyncio.CancelledError:
            self._llm.add_tool_output(call_id, INTERRUPTED_TOOL_OUTPUT)
            raise
        self._llm.add_tool_output(call_id, output)

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
        self._write_gray(f"[expecto {label}]")

    def _show_final(self, text: str) -> None:
        """Show a final response that did not arrive via streaming."""
        self._write_gray(f"[{text}]")

    def _write_gray(self, text: str) -> None:
        # The outer tty is in raw mode (no ONLCR), so bare "\n" moves down
        # without returning to column 0; normalize to "\r\n".
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
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
