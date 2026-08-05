from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import pytest

from jou.dangerous import DangerousCommandMatcher
from jou.input_router import InputRouter
from jou.io_relay import IORelay
from jou.llm_client import (
    AutoResponse,
    AutoStepResult,
    CompletionResponse,
    ParsedToolCall,
    RunCommandParams,
    SendControlParams,
    SummaryResult,
)
from jou.mode_executor import ConfirmLevel, ModeExecutor
from jou.screen_buffer import ScreenBuffer
from jou.working_summary import WorkingSummaryManager

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputParam

AUTO_HOTKEY = bytes([0x1D])
ENTER = bytes([0x0D])

MakeAuto = Callable[..., Coroutine[object, object, "AutoHarness"]]


def _item_dict(item: object) -> dict[str, object]:
    """Narrow a ResponseInputParam item (a large TypedDict union) for key access."""
    return cast("dict[str, object]", item)


def _cmd_result(step: int, command: str) -> AutoStepResult:
    return AutoStepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name="run_command",
                params=RunCommandParams(command=command),
            ),
        ],
        final=None,
        response_id=f"resp_{step}",
    )


def _control_result(step: int, key: Literal["ctrl-c", "ctrl-d", "ctrl-z"]) -> AutoStepResult:
    return AutoStepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name="send_control",
                params=SendControlParams(key=key),
            ),
        ],
        final=None,
        response_id=f"resp_{step}",
    )


def _final_result(
    step: int, status: Literal["done", "fail"], explanation: str,
) -> AutoStepResult:
    return AutoStepResult(
        tool_calls=[],
        final=AutoResponse(status=status, explanation=explanation),
        response_id=f"resp_{step}",
    )


def _risky_cmd_result(step: int, command: str, risk: str = "destructive") -> AutoStepResult:
    return AutoStepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name="run_command",
                params=RunCommandParams(
                    command=command,
                    risk=cast('Literal["read_only", "mutating", "destructive"]', risk),
                    risk_reason="test reason",
                ),
            ),
        ],
        final=None,
        response_id=f"resp_{step}",
    )


class FakeLLMClient:
    def __init__(
        self,
        response: CompletionResponse | None = None,
        auto_results: list[AutoStepResult] | None = None,
    ) -> None:
        self._response = response or CompletionResponse(command="", explanation="")
        self._auto_results = auto_results or []
        self._auto_call_count = 0
        self._write_output: Callable[[bytes], None] | None = None
        self.block: asyncio.Event | None = None
        self.last_screen_text: str = ""
        self.last_query: str = ""
        self.last_working_summary: str | None = None
        self.auto_step_calls: list[tuple[ResponseInputParam, str | None]] = []

    def set_output_sink(self, write: Callable[[bytes], None]) -> None:
        """Wire the fake to the harness: tool-call steps write simulated
        command output to the fake child."""
        self._write_output = write

    async def complete(
        self,
        screen_text: str,
        query: str,
        working_summary: str | None = None,
    ) -> CompletionResponse:
        if self.block is not None:
            await self.block.wait()
        self.last_screen_text = screen_text
        self.last_query = query
        self.last_working_summary = working_summary
        return self._response

    async def auto_step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
    ) -> AutoStepResult:
        self.last_working_summary = working_summary
        self.auto_step_calls.append((list(input_items), previous_response_id))
        result = self._auto_results[self._auto_call_count]
        self._auto_call_count += 1
        if result.tool_calls and self._write_output is not None:
            self._write_output(
                f"output from step {self._auto_call_count}\n".encode(),
            )
        return result

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam:
        return [
            {"role": "user", "content": f"goal: {query}\nscreen: {screen_text}"},
        ]


@dataclass
class AutoHarness:
    """A full auto-mode session: ModeExecutor wired to a running IORelay,
    with a socketpair standing in for the pty master (jou side / child
    side) and pipes for stdin and stdout."""

    relay: IORelay
    router: InputRouter
    run_task: asyncio.Task[None]
    jou_sock: socket.socket
    child_sock: socket.socket
    stdin_w: int
    stdout_r: int
    pty_written: list[bytes]
    screen: ScreenBuffer
    collected: bytearray = field(default_factory=bytearray)
    mode_task: asyncio.Task[None] | None = None

    def start(self, query: str = "go") -> None:
        """Enter auto mode via the hotkey and submit the query."""
        os.write(self.stdin_w, AUTO_HOTKEY + query.encode() + ENTER)

    def user_types(self, data: bytes) -> None:
        os.write(self.stdin_w, data)

    def child_writes(self, data: bytes) -> None:
        self.child_sock.sendall(data)

    def output(self) -> bytes:
        while True:
            try:
                chunk = os.read(self.stdout_r, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.collected.extend(chunk)
        return bytes(self.collected)

    async def wait_done(self, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.mode_task is None:
            if loop.time() > deadline:
                raise TimeoutError("mode task never started")
            await asyncio.sleep(0.001)
        await asyncio.wait_for(
            asyncio.gather(self.mode_task, return_exceptions=True), timeout,
        )

    async def answer_prompts(self, marker: bytes, answers: list[bytes]) -> None:
        """Each time marker appears (again) on stdout, type the next answer."""
        answered = 0
        pending = list(answers)
        while pending:
            if self.output().count(marker) > answered:
                answered += 1
                self.user_types(pending.pop(0))
            await asyncio.sleep(0.005)


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.005)


@pytest.fixture
async def make_auto() -> AsyncIterator[MakeAuto]:
    harnesses: list[AutoHarness] = []
    fds: list[int] = []

    async def _make(
        fake_llm: FakeLLMClient,
        settle_ms: int = 10,
        threshold_ms: int = 5000,
        max_auto_steps: int = 50,
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
        working_summary: WorkingSummaryManager | None = None,
        screen: ScreenBuffer | None = None,
    ) -> AutoHarness:
        screen = screen if screen is not None else ScreenBuffer()
        jou_sock, child_sock = socket.socketpair()
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        os.set_blocking(stdout_r, False)
        fds.extend([stdin_r, stdin_w, stdout_r, stdout_w])
        pty_written: list[bytes] = []

        # on_submit closes over harness/relay/executor created below; all
        # exist before any stdin byte is processed.
        def on_submit(query: str, mode: str) -> None:
            harness.mode_task = relay.start_mode(executor.execute(query, mode))

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=on_submit,
            on_query_cancel=lambda: None,
        )
        relay = IORelay(input_router=router, screen_buffer=screen)
        executor = ModeExecutor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=pty_written.append,
            stdout_fd=stdout_w,
            relay=relay,
            settle_ms=settle_ms,
            threshold_ms=threshold_ms,
            max_auto_steps=max_auto_steps,
            working_summary=working_summary,
            confirm_level=confirm_level,
            dangerous_matcher=dangerous_matcher,
        )
        run_task = asyncio.create_task(
            relay.run(jou_sock.fileno(), stdin_fd=stdin_r, stdout_fd=stdout_w),
        )
        await asyncio.sleep(0)
        harness = AutoHarness(
            relay=relay,
            router=router,
            run_task=run_task,
            jou_sock=jou_sock,
            child_sock=child_sock,
            stdin_w=stdin_w,
            stdout_r=stdout_r,
            pty_written=pty_written,
            screen=screen,
        )
        fake_llm.set_output_sink(harness.child_writes)
        harnesses.append(harness)
        return harness

    yield _make

    for harness in harnesses:
        harness.relay.stop()
        await asyncio.gather(harness.run_task, return_exceptions=True)
        harness.child_sock.close()
        harness.jou_sock.close()
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def _make_executor(
    fake_llm: FakeLLMClient,
    screen: ScreenBuffer,
    stdout_w: int,
    pty_written: list[bytes] | None = None,
    working_summary: WorkingSummaryManager | None = None,
    history_lines: int = 500,
) -> ModeExecutor:
    sink: list[bytes] = [] if pty_written is None else pty_written
    return ModeExecutor(
        llm_client=fake_llm,
        screen_buffer=screen,
        write_to_pty=sink.append,
        stdout_fd=stdout_w,
        relay=IORelay(),  # bare relay: pause/resume are no-ops off the loop readers
        history_lines=history_lines,
        working_summary=working_summary,
    )


def _drain(fd: int) -> bytes:
    output = b""
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        output += chunk
    return output


class TestCompletionMode:
    async def test_injects_command(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="ls -la", explanation="list all files"),
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(fake_llm, screen, stdout_w, pty_written)
            await executor.execute("list files", "completion")

            assert pty_written == [b"ls -la"]
            assert fake_llm.last_query == "list files"
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    async def test_thinking_indicator_shown(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(fake_llm, screen, stdout_w)
            await executor.execute("where am i", "completion")

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)

            assert b"thinking..." in stdout_output
            assert b"\x1b8" in stdout_output
        finally:
            if stdout_w >= 0:
                os.close(stdout_w)
            os.close(stdout_r)

    async def test_cancel_during_llm_call_restores_display(self) -> None:
        """Cancellation (user Ctrl-C) while the LLM call is in flight:
        no command is injected and the thinking overlay is cleared."""
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="ls", explanation=""),
        )
        fake_llm.block = asyncio.Event()  # never set: the call hangs
        screen = ScreenBuffer()
        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(fake_llm, screen, stdout_w, pty_written)
            task = asyncio.create_task(executor.execute("list files", "completion"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert pty_written == []
            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"thinking..." in stdout_output
            assert b"\x1b8\x1b[J" in stdout_output  # overlay cleared in finally
        finally:
            if stdout_w >= 0:
                os.close(stdout_w)
            os.close(stdout_r)


class TestAutoMode:
    async def test_done_immediately(self, make_auto: MakeAuto) -> None:
        """LLM returns done on first step — no tool call, just done message."""
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "task complete")])
        harness = await make_auto(fake_llm)
        harness.start("say hello")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]  # prompt nudge + final newline
        assert b"[auto done:" in harness.output()

    async def test_fail_status(self, make_auto: MakeAuto) -> None:
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "fail", "cannot proceed")])
        harness = await make_auto(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]
        assert b"[auto failed:" in harness.output()

    async def test_two_steps(self, make_auto: MakeAuto) -> None:
        """LLM calls run_command then returns done — settle wait between steps."""
        fake_llm = FakeLLMClient(auto_results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "complete"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("list then finish")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"ls\n", b"\n"]
        out = harness.output()
        assert b"[auto done:" in out
        assert b"output from step 1" in out  # relayed to the user's screen

    async def test_control_char(self, make_auto: MakeAuto) -> None:
        """LLM calls send_control with ctrl-c, then done."""
        fake_llm = FakeLLMClient(auto_results=[
            _control_result(1, "ctrl-c"),
            _final_result(2, "done", "interrupted"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("stop it")
        await harness.wait_done()

        assert harness.pty_written[0] == b"\n"  # initial prompt nudge
        assert harness.pty_written[1] == b"\x03"
        assert harness.pty_written[-1] == b"\n"

    async def test_checkpoint_answer_n_stops(self, make_auto: MakeAuto) -> None:
        """At the checkpoint, answering 'n' stops the loop."""
        max_steps = 2
        fake_llm = FakeLLMClient(
            auto_results=[_cmd_result(i, f"cmd{i}") for i in range(1, max_steps + 1)],
        )
        harness = await make_auto(fake_llm, max_auto_steps=max_steps)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"n"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        out = harness.output()
        assert len(fake_llm.auto_step_calls) == max_steps
        assert f"reached step {max_steps}".encode() in out
        assert b"continue? (y/n)" in out
        assert f"[auto stopped at step {max_steps}]".encode() in out

    async def test_checkpoint_answer_y_continues(self, make_auto: MakeAuto) -> None:
        """Answering 'y' continues the loop past the checkpoint, keeping the
        response chain, and asks again at the next checkpoint."""
        fake_llm = FakeLLMClient(
            auto_results=[_cmd_result(i, f"cmd{i}") for i in range(1, 5)],
        )
        harness = await make_auto(fake_llm, max_auto_steps=2)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"y", b"n"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        assert len(fake_llm.auto_step_calls) == 4
        # The chain is kept across the checkpoint: step 3 receives the
        # response id of step 2.
        _, prev_id_step3 = fake_llm.auto_step_calls[2]
        assert prev_id_step3 == "resp_2"

        out = harness.output()
        assert b"reached step 2" in out
        assert b"reached step 4" in out
        assert b"[auto stopped at step 4]" in out

    async def test_checkpoint_ctrl_c_stops(self, make_auto: MakeAuto) -> None:
        """Ctrl-C at the checkpoint prompt answers 'no' and stops the loop."""
        fake_llm = FakeLLMClient(auto_results=[_cmd_result(1, "cmd1")])
        harness = await make_auto(fake_llm, max_auto_steps=1)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"\x03"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        assert len(fake_llm.auto_step_calls) == 1
        assert b"[auto stopped at step 1]" in harness.output()

    async def test_ctrl_c_interrupt_during_settle(self, make_auto: MakeAuto) -> None:
        """User Ctrl-C during the settle wait cancels the mode task."""
        fake_llm = FakeLLMClient(auto_results=[_cmd_result(1, "sleep 100")])
        harness = await make_auto(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("run forever")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert len(fake_llm.auto_step_calls) == 1
        assert b"[auto interrupted]" in harness.output()

    async def test_ctrl_c_during_llm_call(self, make_auto: MakeAuto) -> None:
        """Ctrl-C while the LLM call is in flight cancels the request
        itself; no command reaches the pty."""

        class BlockingAutoLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.was_cancelled = False

            async def auto_step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> AutoStepResult:
                self.auto_step_calls.append((list(input_items), previous_response_id))
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.was_cancelled = True
                    raise
                raise AssertionError("unreachable")

        fake_llm = BlockingAutoLLM()
        harness = await make_auto(fake_llm)
        harness.start("run something")
        await asyncio.wait_for(fake_llm.entered.wait(), timeout=5)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]  # no command was injected
        assert fake_llm.was_cancelled  # the in-flight call was truly cancelled
        assert b"[auto interrupted]" in harness.output()

    async def test_ctrl_c_during_summary_generation(self, make_auto: MakeAuto) -> None:
        """Ctrl-C during the pre-query working summary generation aborts
        auto mode before any LLM step runs."""

        class BlockingSummaryLLM:
            def __init__(self) -> None:
                self.entered = asyncio.Event()

            async def summarize(
                self,
                previous: SummaryResult | None,
                new_output: str,
                truncated: bool = False,
            ) -> SummaryResult:
                self.entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        screen = ScreenBuffer(rows=3, cols=40)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        summary_llm = BlockingSummaryLLM()
        manager = WorkingSummaryManager(
            llm_client=summary_llm,
            screen_buffer=screen,
            trigger_lines=5,
        )
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "x")])
        harness = await make_auto(fake_llm, working_summary=manager, screen=screen)
        harness.start("run something")
        await asyncio.wait_for(summary_llm.entered.wait(), timeout=5)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert fake_llm.auto_step_calls == []  # the auto step was never reached
        assert b"[auto interrupted]" in harness.output()

    async def test_thinking_indicator(self, make_auto: MakeAuto) -> None:
        """Auto mode shows the [auto] thinking indicator."""
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "done")])
        harness = await make_auto(fake_llm)
        harness.start("test")
        await harness.wait_done()

        assert b"\x1b[90m[auto: thinking...]\x1b[0m" in harness.output()

    async def test_multiline_explanation_uses_crlf(self, make_auto: MakeAuto) -> None:
        """Newlines in the final explanation are written as CRLF (raw mode has no ONLCR)."""
        fake_llm = FakeLLMClient(auto_results=[
            _final_result(1, "done", "Summary:\n- item one\n- item two"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("summarize")
        await harness.wait_done()

        out = harness.output()
        assert b"Summary:\r\n- item one\r\n- item two" in out
        end_marker = out[out.index(b"[auto done:"):]
        assert b"\r\n" in end_marker

    async def test_tool_output_chaining(self, make_auto: MakeAuto) -> None:
        """Tool results are sent back as function_call_output with previous_response_id."""
        fake_llm = FakeLLMClient(auto_results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("test history")
        await harness.wait_done()

        assert len(fake_llm.auto_step_calls) == 2

        first_items, first_prev_id = fake_llm.auto_step_calls[0]
        assert first_prev_id is None
        assert len(first_items) == 1
        assert "goal:" in str(_item_dict(first_items[0])["content"])

        second_items, second_prev_id = fake_llm.auto_step_calls[1]
        assert second_prev_id == "resp_1"
        assert len(second_items) == 1
        second = _item_dict(second_items[0])
        assert second["type"] == "function_call_output"
        assert second["call_id"] == "call_1"
        output = str(second["output"])
        assert "Terminal Output" in output
        assert "output from step 1" in output

    async def test_tool_output_excludes_past_output(self, make_auto: MakeAuto) -> None:
        """Tool results contain only output produced by the executed command."""
        screen = ScreenBuffer()
        screen.feed(b"previous command output\r\nuser@host:~$ ")
        fake_llm = FakeLLMClient(auto_results=[
            _cmd_result(1, "echo hi"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm, screen=screen)
        harness.start("say hi")
        await harness.wait_done()

        second_items, _ = fake_llm.auto_step_calls[1]
        output = str(_item_dict(second_items[0])["output"])
        assert "output from step 1" in output
        assert "previous command output" not in output

    async def test_command_echo_colored_gray(self, make_auto: MakeAuto) -> None:
        """The echoed command is bracketed in gray; reset follows the first newline."""
        fake_llm = FakeLLMClient(auto_results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("list files")
        await harness.wait_done()

        out = harness.output()
        # Gray starts right after the thinking indicator is cleared...
        assert b"\x1b8\x1b[J\x1b[90m" in out
        # ...and the reset comes right after the first relayed newline.
        assert b"output from step 1\n\x1b[0m" in out

    async def test_echo_color_reset_without_newline(self, make_auto: MakeAuto) -> None:
        """The gray is reset even when no newline arrives before settle."""

        class NoNewlineAutoLLM(FakeLLMClient):
            async def auto_step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> AutoStepResult:
                self.auto_step_calls.append((list(input_items), previous_response_id))
                result = self._auto_results[self._auto_call_count]
                self._auto_call_count += 1
                if result.tool_calls and self._write_output is not None:
                    self._write_output(b"partial echo")
                return result

        fake_llm = NoNewlineAutoLLM(auto_results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("list files")
        await harness.wait_done()

        assert b"partial echo\x1b[0m" in harness.output()

    async def test_invalid_tool_call_returns_error(self, make_auto: MakeAuto) -> None:
        """A tool call with invalid params sends an error output back, no pty write."""
        fake_llm = FakeLLMClient(auto_results=[
            AutoStepResult(
                tool_calls=[
                    ParsedToolCall(
                        call_id="call_1",
                        name="run_command",
                        params=None,
                        error="invalid arguments: boom",
                    ),
                ],
                final=None,
                response_id="resp_1",
            ),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        # No command was written to the pty, only the nudge and trailing newline.
        assert harness.pty_written == [b"\n", b"\n"]

        second_items, _ = fake_llm.auto_step_calls[1]
        second = _item_dict(second_items[0])
        assert second["type"] == "function_call_output"
        assert second["call_id"] == "call_1"
        assert str(second["output"]).startswith("Error:")

    async def test_threshold_note_in_tool_output(self, make_auto: MakeAuto) -> None:
        """When output keeps flowing past the threshold, the tool output carries a note."""
        fake_llm = FakeLLMClient(auto_results=[
            _cmd_result(1, "long-running"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_auto(fake_llm, settle_ms=500, threshold_ms=50)
        stop = asyncio.Event()

        async def keep_writing() -> None:
            while not stop.is_set():
                harness.child_writes(b"tick\r\n")
                await asyncio.sleep(0.005)

        writer = asyncio.create_task(keep_writing())
        try:
            harness.start("run something long")
            await harness.wait_done(timeout=10)
        finally:
            stop.set()
            await writer

        second_items, _ = fake_llm.auto_step_calls[1]
        output = str(_item_dict(second_items[0])["output"])
        assert "Terminal Output" in output
        assert "[Note] Output did not settle" in output


class TestDangerousCommandConfirmation:
    async def _run_gate(
        self,
        make_auto: MakeAuto,
        auto_results: list[AutoStepResult],
        answers: list[bytes],
        confirm_level: ConfirmLevel = "destructive",
        with_matcher: bool = False,
    ) -> tuple[FakeLLMClient, AutoHarness]:
        fake_llm = FakeLLMClient(auto_results=auto_results)
        matcher = DangerousCommandMatcher.load(None)[0] if with_matcher else None
        harness = await make_auto(
            fake_llm, confirm_level=confirm_level, dangerous_matcher=matcher,
        )
        pump = (
            asyncio.create_task(harness.answer_prompts(b"run? (y/n)", list(answers)))
            if answers
            else None
        )
        harness.start("do something")
        await harness.wait_done()
        if pump is not None:
            await asyncio.wait_for(pump, timeout=5)
        return fake_llm, harness

    async def test_destructive_confirmed_y_runs(self, make_auto: MakeAuto) -> None:
        _, harness = await self._run_gate(
            make_auto,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "finished")],
            [b"y"],
        )
        assert b"reload\n" in harness.pty_written
        assert b"destructive command (test reason): reload" in harness.output()

    async def test_destructive_declined_feeds_back_to_llm(self, make_auto: MakeAuto) -> None:
        """'n' does not abort the loop: the LLM gets a declined tool result."""
        fake_llm, harness = await self._run_gate(
            make_auto,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "gave up")],
            [b"n"],
        )
        assert b"reload\n" not in harness.pty_written
        assert len(fake_llm.auto_step_calls) == 2
        second_input, _ = fake_llm.auto_step_calls[1]
        output_item = _item_dict(second_input[0])
        assert "user declined" in str(output_item["output"])

    async def test_read_only_not_confirmed(self, make_auto: MakeAuto) -> None:
        _, harness = await self._run_gate(
            make_auto,
            [
                _risky_cmd_result(1, "ls -la", risk="read_only"),
                _final_result(2, "done", "finished"),
            ],
            [],
        )
        assert b"ls -la\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_mutating_not_confirmed_at_default_level(self, make_auto: MakeAuto) -> None:
        _, harness = await self._run_gate(
            make_auto,
            [
                _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
                _final_result(2, "done", "finished"),
            ],
            [],
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_mutating_confirmed_at_mutating_level(self, make_auto: MakeAuto) -> None:
        _, harness = await self._run_gate(
            make_auto,
            [
                _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
                _final_result(2, "done", "finished"),
            ],
            [b"y"],
            confirm_level="mutating",
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" in harness.output()

    async def test_confirm_level_none_skips_confirmation(self, make_auto: MakeAuto) -> None:
        _, harness = await self._run_gate(
            make_auto,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "finished")],
            [],
            confirm_level="none",
        )
        assert b"reload\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_backstop_escalates_underreported_risk(self, make_auto: MakeAuto) -> None:
        """A pattern match forces confirmation even when the LLM claims
        read_only, and the prompt shows the matched pattern."""
        _, harness = await self._run_gate(
            make_auto,
            [
                _risky_cmd_result(1, "write erase", risk="read_only"),
                _final_result(2, "done", "finished"),
            ],
            [b"n"],
            with_matcher=True,
        )
        assert b"write erase\n" not in harness.pty_written
        assert b"destructive command (matched pattern:" in harness.output()

    async def test_stdin_eof_during_confirmation_interrupts(self, make_auto: MakeAuto) -> None:
        """When the user's terminal goes away at a confirmation prompt, the
        session shutdown cancels auto mode; the command never runs.
        (Replaces the old stdin_fd=-1 'non-interactive declines' test: in
        the async design a vanished stdin ends the whole session.)"""
        fake_llm = FakeLLMClient(auto_results=[
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "gave up"),
        ])
        harness = await make_auto(fake_llm)
        harness.start("do something")
        await _until(lambda: b"run? (y/n)" in harness.output())
        os.close(harness.stdin_w)
        await harness.wait_done()

        assert b"reload\n" not in harness.pty_written
        assert b"[auto interrupted]" in harness.output()


class TestWorkingSummaryIntegration:
    def _make_summary_manager(
        self, screen: ScreenBuffer,
    ) -> WorkingSummaryManager:
        class FakeSummaryLLM:
            async def summarize(
                self,
                previous: SummaryResult | None,
                new_output: str,
                truncated: bool = False,
            ) -> SummaryResult:
                return SummaryResult(
                    environment="ssh to router1 (Junos)",
                    activities="checking BGP",
                    notes="none",
                )

        return WorkingSummaryManager(
            llm_client=FakeSummaryLLM(),
            screen_buffer=screen,
            trigger_lines=5,
        )

    async def test_completion_passes_summary_to_llm(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = self._make_summary_manager(screen)
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(
                fake_llm, screen, stdout_w, working_summary=manager,
            )
            await executor.execute("where am i", "completion")

            summary = fake_llm.last_working_summary
            assert summary is not None
            assert "## Session Context" in summary
            assert "Junos" in summary
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    async def test_completion_without_manager_passes_none(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(fake_llm, screen, stdout_w)
            await executor.execute("where am i", "completion")
            assert fake_llm.last_working_summary is None
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    async def test_auto_passes_summary_to_llm(self, make_auto: MakeAuto) -> None:
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = self._make_summary_manager(screen)
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "finished")])
        harness = await make_auto(fake_llm, working_summary=manager, screen=screen)
        harness.start("check bgp")
        await harness.wait_done()

        summary = fake_llm.last_working_summary
        assert summary is not None
        assert "## Session Context" in summary

    async def test_completion_screen_text_limited_to_history_lines(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        stdout_r, stdout_w = os.pipe()

        try:
            executor = _make_executor(fake_llm, screen, stdout_w, history_lines=5)
            await executor.execute("where am i", "completion")

            lines = fake_llm.last_screen_text.split("\n")
            assert len(lines) == 5
            assert "line 0" not in fake_llm.last_screen_text
        finally:
            os.close(stdout_r)
            os.close(stdout_w)
