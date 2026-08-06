from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import pytest

from expecto.dangerous import DangerousCommandMatcher
from expecto.executor import ConfirmLevel, Executor
from expecto.input_router import InputRouter
from expecto.io_relay import IORelay
from expecto.llm_client import (
    FinalResponse,
    ParsedToolCall,
    RunCommandParams,
    SendControlParams,
    StepResult,
    SummaryResult,
)
from expecto.screen_buffer import ScreenBuffer
from expecto.working_summary import WorkingSummaryManager

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputParam

HOTKEY = bytes([0x1F])
ENTER = bytes([0x0D])

MakeHarness = Callable[..., Coroutine[object, object, "Harness"]]


def _item_dict(item: object) -> dict[str, object]:
    """Narrow a ResponseInputParam item (a large TypedDict union) for key access."""
    return cast("dict[str, object]", item)


def _cmd_result(step: int, command: str) -> StepResult:
    return StepResult(
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


def _control_result(step: int, key: Literal["ctrl-c", "ctrl-d", "ctrl-z"]) -> StepResult:
    return StepResult(
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
) -> StepResult:
    return StepResult(
        tool_calls=[],
        final=FinalResponse(status=status, explanation=explanation),
        response_id=f"resp_{step}",
    )


def _risky_cmd_result(step: int, command: str, risk: str = "destructive") -> StepResult:
    return StepResult(
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
    def __init__(self, results: list[StepResult] | None = None) -> None:
        self._results = results or []
        self._call_count = 0
        self._write_output: Callable[[bytes], None] | None = None
        self.last_screen_text: str = ""
        self.last_query: str = ""
        self.last_working_summary: str | None = None
        self.step_calls: list[tuple[ResponseInputParam, str | None]] = []

    def set_output_sink(self, write: Callable[[bytes], None]) -> None:
        """Wire the fake to the harness: tool-call steps write simulated
        command output to the fake child."""
        self._write_output = write

    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
    ) -> StepResult:
        self.last_working_summary = working_summary
        self.step_calls.append((list(input_items), previous_response_id))
        result = self._results[self._call_count]
        self._call_count += 1
        if result.tool_calls and self._write_output is not None:
            self._write_output(
                f"output from step {self._call_count}\n".encode(),
            )
        return result

    def build_initial_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam:
        self.last_query = query
        self.last_screen_text = screen_text
        return [
            {"role": "user", "content": f"goal: {query}\nscreen: {screen_text}"},
        ]


@dataclass
class Harness:
    """A full session: Executor wired to a running IORelay, with a
    socketpair standing in for the pty master (expecto side / child side)
    and pipes for stdin and stdout."""

    relay: IORelay
    router: InputRouter
    run_task: asyncio.Task[None]
    expecto_sock: socket.socket
    child_sock: socket.socket
    stdin_w: int
    stdout_r: int
    pty_written: list[bytes]
    screen: ScreenBuffer
    collected: bytearray = field(default_factory=bytearray)
    exec_task: asyncio.Task[None] | None = None

    def start(self, query: str = "go") -> None:
        """Enter query mode via the hotkey and submit the query."""
        os.write(self.stdin_w, HOTKEY + query.encode() + ENTER)

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
        while self.exec_task is None:
            if loop.time() > deadline:
                raise TimeoutError("execution task never started")
            await asyncio.sleep(0.001)
        await asyncio.wait_for(
            asyncio.gather(self.exec_task, return_exceptions=True), timeout,
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
async def make_harness() -> AsyncIterator[MakeHarness]:
    harnesses: list[Harness] = []
    fds: list[int] = []

    async def _make(
        fake_llm: FakeLLMClient,
        settle_ms: int = 10,
        threshold_ms: int = 5000,
        max_steps: int = 50,
        history_lines: int = 500,
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
        working_summary: WorkingSummaryManager | None = None,
        screen: ScreenBuffer | None = None,
    ) -> Harness:
        screen = screen if screen is not None else ScreenBuffer()
        expecto_sock, child_sock = socket.socketpair()
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        os.set_blocking(stdout_r, False)
        fds.extend([stdin_r, stdin_w, stdout_r, stdout_w])
        pty_written: list[bytes] = []

        # on_submit closes over harness/relay/executor created below; all
        # exist before any stdin byte is processed.
        def on_submit(query: str) -> None:
            harness.exec_task = relay.start_execution(executor.execute(query))

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=on_submit,
            on_query_cancel=lambda: None,
        )
        relay = IORelay(input_router=router, screen_buffer=screen)
        executor = Executor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=pty_written.append,
            stdout_fd=stdout_w,
            relay=relay,
            settle_ms=settle_ms,
            threshold_ms=threshold_ms,
            max_steps=max_steps,
            history_lines=history_lines,
            working_summary=working_summary,
            confirm_level=confirm_level,
            dangerous_matcher=dangerous_matcher,
        )
        run_task = asyncio.create_task(
            relay.run(expecto_sock.fileno(), stdin_fd=stdin_r, stdout_fd=stdout_w),
        )
        await asyncio.sleep(0)
        harness = Harness(
            relay=relay,
            router=router,
            run_task=run_task,
            expecto_sock=expecto_sock,
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
        harness.expecto_sock.close()
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


class TestExecution:
    async def test_done_immediately(self, make_harness: MakeHarness) -> None:
        """LLM returns done on first step — no tool call, just done message."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "task complete")])
        harness = await make_harness(fake_llm)
        harness.start("say hello")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]  # prompt nudge + final newline
        assert b"[expecto done:" in harness.output()

    async def test_fail_status(self, make_harness: MakeHarness) -> None:
        fake_llm = FakeLLMClient(results=[_final_result(1, "fail", "cannot proceed")])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]
        assert b"[expecto failed:" in harness.output()

    async def test_two_steps(self, make_harness: MakeHarness) -> None:
        """LLM calls run_command then returns done — settle wait between steps."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "complete"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("list then finish")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"ls\n", b"\n"]
        out = harness.output()
        assert b"[expecto done:" in out
        assert b"output from step 1" in out  # relayed to the user's screen

    async def test_control_char(self, make_harness: MakeHarness) -> None:
        """LLM calls send_control with ctrl-c, then done."""
        fake_llm = FakeLLMClient(results=[
            _control_result(1, "ctrl-c"),
            _final_result(2, "done", "interrupted"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("stop it")
        await harness.wait_done()

        assert harness.pty_written[0] == b"\n"  # initial prompt nudge
        assert harness.pty_written[1] == b"\x03"
        assert harness.pty_written[-1] == b"\n"

    async def test_checkpoint_answer_n_stops(self, make_harness: MakeHarness) -> None:
        """At the checkpoint, answering 'n' stops the loop."""
        max_steps = 2
        fake_llm = FakeLLMClient(
            results=[_cmd_result(i, f"cmd{i}") for i in range(1, max_steps + 1)],
        )
        harness = await make_harness(fake_llm, max_steps=max_steps)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"n"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        out = harness.output()
        assert len(fake_llm.step_calls) == max_steps
        assert f"reached step {max_steps}".encode() in out
        assert b"continue? (y/n)" in out
        assert f"[expecto stopped at step {max_steps}]".encode() in out

    async def test_checkpoint_answer_y_continues(self, make_harness: MakeHarness) -> None:
        """Answering 'y' continues the loop past the checkpoint, keeping the
        response chain, and asks again at the next checkpoint."""
        fake_llm = FakeLLMClient(
            results=[_cmd_result(i, f"cmd{i}") for i in range(1, 5)],
        )
        harness = await make_harness(fake_llm, max_steps=2)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"y", b"n"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        assert len(fake_llm.step_calls) == 4
        # The chain is kept across the checkpoint: step 3 receives the
        # response id of step 2.
        _, prev_id_step3 = fake_llm.step_calls[2]
        assert prev_id_step3 == "resp_2"

        out = harness.output()
        assert b"reached step 2" in out
        assert b"reached step 4" in out
        assert b"[expecto stopped at step 4]" in out

    async def test_checkpoint_ctrl_c_stops(self, make_harness: MakeHarness) -> None:
        """Ctrl-C at the checkpoint prompt answers 'no' and stops the loop."""
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "cmd1")])
        harness = await make_harness(fake_llm, max_steps=1)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"\x03"]),
        )
        harness.start("keep going")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)

        assert len(fake_llm.step_calls) == 1
        assert b"[expecto stopped at step 1]" in harness.output()

    async def test_ctrl_c_interrupt_during_settle(self, make_harness: MakeHarness) -> None:
        """User Ctrl-C during the settle wait cancels the execution task."""
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "sleep 100")])
        harness = await make_harness(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("run forever")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert len(fake_llm.step_calls) == 1
        assert b"[expecto interrupted]" in harness.output()

    async def test_ctrl_c_during_llm_call(self, make_harness: MakeHarness) -> None:
        """Ctrl-C while the LLM call is in flight cancels the request
        itself; no command reaches the pty."""

        class BlockingLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.was_cancelled = False

            async def step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> StepResult:
                self.step_calls.append((list(input_items), previous_response_id))
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.was_cancelled = True
                    raise
                raise AssertionError("unreachable")

        fake_llm = BlockingLLM()
        harness = await make_harness(fake_llm)
        harness.start("run something")
        await asyncio.wait_for(fake_llm.entered.wait(), timeout=5)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]  # no command was injected
        assert fake_llm.was_cancelled  # the in-flight call was truly cancelled
        assert b"[expecto interrupted]" in harness.output()

    async def test_ctrl_c_during_summary_generation(self, make_harness: MakeHarness) -> None:
        """Ctrl-C during the pre-query working summary generation aborts
        the execution before any LLM step runs."""

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
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "x")])
        harness = await make_harness(fake_llm, working_summary=manager, screen=screen)
        harness.start("run something")
        await asyncio.wait_for(summary_llm.entered.wait(), timeout=5)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert fake_llm.step_calls == []  # the LLM step was never reached
        assert b"[expecto interrupted]" in harness.output()

    async def test_thinking_indicator(self, make_harness: MakeHarness) -> None:
        """The thinking indicator is shown while the LLM call is in flight."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "done")])
        harness = await make_harness(fake_llm)
        harness.start("test")
        await harness.wait_done()

        assert b"\x1b[90m[thinking...]\x1b[0m" in harness.output()

    async def test_multiline_explanation_uses_crlf(self, make_harness: MakeHarness) -> None:
        """Newlines in the final explanation are written as CRLF (raw mode has no ONLCR)."""
        fake_llm = FakeLLMClient(results=[
            _final_result(1, "done", "Summary:\n- item one\n- item two"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("summarize")
        await harness.wait_done()

        out = harness.output()
        assert b"Summary:\r\n- item one\r\n- item two" in out
        end_marker = out[out.index(b"[expecto done:"):]
        assert b"\r\n" in end_marker

    async def test_tool_output_chaining(self, make_harness: MakeHarness) -> None:
        """Tool results are sent back as function_call_output with previous_response_id."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_harness(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("test history")
        await harness.wait_done()

        assert len(fake_llm.step_calls) == 2

        first_items, first_prev_id = fake_llm.step_calls[0]
        assert first_prev_id is None
        assert len(first_items) == 1
        assert "goal:" in str(_item_dict(first_items[0])["content"])

        second_items, second_prev_id = fake_llm.step_calls[1]
        assert second_prev_id == "resp_1"
        assert len(second_items) == 1
        second = _item_dict(second_items[0])
        assert second["type"] == "function_call_output"
        assert second["call_id"] == "call_1"
        output = str(second["output"])
        assert "Terminal Output" in output
        assert "output from step 1" in output

    async def test_tool_output_excludes_past_output(self, make_harness: MakeHarness) -> None:
        """Tool results contain only output produced by the executed command."""
        screen = ScreenBuffer()
        screen.feed(b"previous command output\r\nuser@host:~$ ")
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "echo hi"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_harness(fake_llm, screen=screen)
        harness.start("say hi")
        await harness.wait_done()

        second_items, _ = fake_llm.step_calls[1]
        output = str(_item_dict(second_items[0])["output"])
        assert "output from step 1" in output
        assert "previous command output" not in output

    async def test_command_echo_colored_gray(self, make_harness: MakeHarness) -> None:
        """The echoed command is bracketed in gray; reset follows the first newline."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_harness(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("list files")
        await harness.wait_done()

        out = harness.output()
        # Gray starts right after the thinking indicator is cleared...
        assert b"\x1b8\x1b[J\x1b[90m" in out
        # ...and the reset comes right after the first relayed newline.
        assert b"output from step 1\n\x1b[0m" in out

    async def test_echo_color_reset_without_newline(self, make_harness: MakeHarness) -> None:
        """The gray is reset even when no newline arrives before settle."""

        class NoNewlineLLM(FakeLLMClient):
            async def step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> StepResult:
                self.step_calls.append((list(input_items), previous_response_id))
                result = self._results[self._call_count]
                self._call_count += 1
                if result.tool_calls and self._write_output is not None:
                    self._write_output(b"partial echo")
                return result

        fake_llm = NoNewlineLLM(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("list files")
        await harness.wait_done()

        assert b"partial echo\x1b[0m" in harness.output()

    async def test_invalid_tool_call_returns_error(self, make_harness: MakeHarness) -> None:
        """A tool call with invalid params sends an error output back, no pty write."""
        fake_llm = FakeLLMClient(results=[
            StepResult(
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
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        # No command was written to the pty, only the nudge and trailing newline.
        assert harness.pty_written == [b"\n", b"\n"]

        second_items, _ = fake_llm.step_calls[1]
        second = _item_dict(second_items[0])
        assert second["type"] == "function_call_output"
        assert second["call_id"] == "call_1"
        assert str(second["output"]).startswith("Error:")

    async def test_threshold_note_in_tool_output(self, make_harness: MakeHarness) -> None:
        """When output keeps flowing past the threshold, the tool output carries a note."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "long-running"),
            _final_result(2, "done", "done"),
        ])
        harness = await make_harness(fake_llm, settle_ms=500, threshold_ms=50)
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

        second_items, _ = fake_llm.step_calls[1]
        output = str(_item_dict(second_items[0])["output"])
        assert "Terminal Output" in output
        assert "[Note] Output did not settle" in output

    async def test_screen_text_limited_to_history_lines(self, make_harness: MakeHarness) -> None:
        """The screen text passed to the LLM is capped at history_lines."""
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "done")])
        harness = await make_harness(fake_llm, history_lines=5, screen=screen)
        harness.start("where am i")
        await harness.wait_done()

        lines = fake_llm.last_screen_text.split("\n")
        assert len(lines) == 5
        assert "line 0" not in fake_llm.last_screen_text


class TestDangerousCommandConfirmation:
    async def _run_gate(
        self,
        make_harness: MakeHarness,
        results: list[StepResult],
        answers: list[bytes],
        confirm_level: ConfirmLevel = "destructive",
        with_matcher: bool = False,
    ) -> tuple[FakeLLMClient, Harness]:
        fake_llm = FakeLLMClient(results=results)
        matcher = DangerousCommandMatcher.load(None)[0] if with_matcher else None
        harness = await make_harness(
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

    async def test_destructive_confirmed_y_runs(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "finished")],
            [b"y"],
        )
        assert b"reload\n" in harness.pty_written
        assert b"destructive command (test reason): reload" in harness.output()

    async def test_destructive_declined_feeds_back_to_llm(self, make_harness: MakeHarness) -> None:
        """'n' does not abort the loop: the LLM gets a declined tool result."""
        fake_llm, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "gave up")],
            [b"n"],
        )
        assert b"reload\n" not in harness.pty_written
        assert len(fake_llm.step_calls) == 2
        second_input, _ = fake_llm.step_calls[1]
        output_item = _item_dict(second_input[0])
        assert "user declined" in str(output_item["output"])

    async def test_read_only_not_confirmed(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [
                _risky_cmd_result(1, "ls -la", risk="read_only"),
                _final_result(2, "done", "finished"),
            ],
            [],
        )
        assert b"ls -la\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_mutating_not_confirmed_at_default_level(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [
                _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
                _final_result(2, "done", "finished"),
            ],
            [],
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_mutating_confirmed_at_mutating_level(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [
                _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
                _final_result(2, "done", "finished"),
            ],
            [b"y"],
            confirm_level="mutating",
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" in harness.output()

    async def test_confirm_level_none_skips_confirmation(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result(2, "done", "finished")],
            [],
            confirm_level="none",
        )
        assert b"reload\n" in harness.pty_written
        assert b"run? (y/n)" not in harness.output()

    async def test_backstop_escalates_underreported_risk(self, make_harness: MakeHarness) -> None:
        """A pattern match forces confirmation even when the LLM claims
        read_only, and the prompt shows the matched pattern."""
        _, harness = await self._run_gate(
            make_harness,
            [
                _risky_cmd_result(1, "write erase", risk="read_only"),
                _final_result(2, "done", "finished"),
            ],
            [b"n"],
            with_matcher=True,
        )
        assert b"write erase\n" not in harness.pty_written
        assert b"destructive command (matched pattern:" in harness.output()

    async def test_stdin_eof_during_confirmation_interrupts(self, make_harness: MakeHarness) -> None:
        """When the user's terminal goes away at a confirmation prompt, the
        session shutdown cancels the execution; the command never runs.
        (Replaces the old stdin_fd=-1 'non-interactive declines' test: in
        the async design a vanished stdin ends the whole session.)"""
        fake_llm = FakeLLMClient(results=[
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "gave up"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await _until(lambda: b"run? (y/n)" in harness.output())
        os.close(harness.stdin_w)
        await harness.wait_done()

        assert b"reload\n" not in harness.pty_written
        assert b"[expecto interrupted]" in harness.output()


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

    async def test_passes_summary_to_llm(self, make_harness: MakeHarness) -> None:
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = self._make_summary_manager(screen)
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "finished")])
        harness = await make_harness(fake_llm, working_summary=manager, screen=screen)
        harness.start("check bgp")
        await harness.wait_done()

        summary = fake_llm.last_working_summary
        assert summary is not None
        assert "## Session Context" in summary
        assert "Junos" in summary

    async def test_without_manager_passes_none(self, make_harness: MakeHarness) -> None:
        fake_llm = FakeLLMClient(results=[_final_result(1, "done", "finished")])
        harness = await make_harness(fake_llm)
        harness.start("where am i")
        await harness.wait_done()

        assert fake_llm.last_working_summary is None
