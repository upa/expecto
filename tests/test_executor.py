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
    ChainLostError,
    HandoffSummary,
    ParsedToolCall,
    RunCommandParams,
    SendControlParams,
    StepResult,
)
from expecto.screen_buffer import ScreenBuffer

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
        final_text=None,
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
        final_text=None,
        response_id=f"resp_{step}",
    )


def _final_result(step: int, text: str) -> StepResult:
    return StepResult(
        tool_calls=[],
        final_text=text,
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
        final_text=None,
        response_id=f"resp_{step}",
    )


class FakeLLMClient:
    def __init__(self, results: list[StepResult] | None = None) -> None:
        self._results = results or []
        self._call_count = 0
        self._write_output: Callable[[bytes], None] | None = None
        self.last_screen_text: str | None = None
        self.last_new_output: str | None = None
        self.last_summary: HandoffSummary | None = None
        self.last_query: str = ""
        self.step_calls: list[tuple[ResponseInputParam, str | None]] = []
        self.compact_calls: list[tuple[str, list[object] | None]] = []
        self.compact_result = HandoffSummary(
            environment="test env", activities="test acts", notes="test notes",
        )
        self.compact_error: Exception | None = None

    def set_output_sink(self, write: Callable[[bytes], None]) -> None:
        """Wire the fake to the harness: tool-call steps write simulated
        command output to the fake child."""
        self._write_output = write

    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        self.step_calls.append((list(input_items), previous_response_id))
        result = self._results[self._call_count]
        self._call_count += 1
        if result.tool_calls and self._write_output is not None:
            self._write_output(
                f"output from step {self._call_count}\n".encode(),
            )
        return result

    def build_query_message(
        self,
        query: str,
        *,
        screen_text: str | None = None,
        new_output: str | None = None,
        summary: HandoffSummary | None = None,
    ) -> ResponseInputParam:
        self.last_query = query
        self.last_screen_text = screen_text
        self.last_new_output = new_output
        self.last_summary = summary
        parts = [f"goal: {query}"]
        if screen_text is not None:
            parts.append(f"screen: {screen_text}")
        if new_output is not None:
            parts.append(f"new: {new_output}")
        if summary is not None:
            parts.append(f"summary: {summary.notes}")
        return [{"role": "user", "content": "\n".join(parts)}]

    async def compact(
        self,
        previous_response_id: str,
        pending_items: ResponseInputParam | None = None,
    ) -> HandoffSummary:
        self.compact_calls.append(
            (previous_response_id, list(pending_items) if pending_items else None),
        )
        if self.compact_error is not None:
            raise self.compact_error
        return self.compact_result


class StreamingFakeLLM(FakeLLMClient):
    """Delivers each result's text through on_delta in chunks before
    returning it, like the real streaming client."""

    def __init__(self, results: list[StepResult], chunks: list[list[str]]) -> None:
        super().__init__(results)
        self._chunks = chunks

    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        if on_delta is not None:
            for chunk in self._chunks[self._call_count]:
                on_delta(chunk)
        return await super().step(
            input_items, previous_response_id, on_delta,
        )


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
        """Enter query mode via the hotkey and submit the query. Resets
        exec_task so wait_done() waits for this query's execution."""
        self.exec_task = None
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
        screen: ScreenBuffer | None = None,
        chain_compact_thresh: int = 100000,
        new_output_max_chars: int = 20000,
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
            confirm_level=confirm_level,
            dangerous_matcher=dangerous_matcher,
            chain_compact_thresh=chain_compact_thresh,
            new_output_max_chars=new_output_max_chars,
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
        fake_llm = FakeLLMClient(results=[_final_result(1, "task complete")])
        harness = await make_harness(fake_llm)
        harness.start("say hello")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]  # prompt nudge + final newline
        assert b"[task complete]" in harness.output()

    async def test_empty_final_text(self, make_harness: MakeHarness) -> None:
        """A final response with no text still shows an end marker."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "")])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"\n"]
        assert b"[expecto (empty response)]" in harness.output()

    async def test_two_steps(self, make_harness: MakeHarness) -> None:
        """LLM calls run_command then returns done — settle wait between steps."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "complete"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("list then finish")
        await harness.wait_done()

        assert harness.pty_written == [b"\n", b"ls\n", b"\n"]
        out = harness.output()
        assert b"[complete]" in out
        assert b"output from step 1" in out  # relayed to the user's screen

    async def test_control_char(self, make_harness: MakeHarness) -> None:
        """LLM calls send_control with ctrl-c, then done."""
        fake_llm = FakeLLMClient(results=[
            _control_result(1, "ctrl-c"),
            _final_result(2, "stopped it"),
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
                on_delta: Callable[[str], None] | None = None,
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

    async def test_thinking_indicator(self, make_harness: MakeHarness) -> None:
        """The thinking indicator is shown while the LLM call is in flight."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "done")])
        harness = await make_harness(fake_llm)
        harness.start("test")
        await harness.wait_done()

        assert b"\x1b[90m[thinking...]\x1b[0m" in harness.output()

    async def test_multiline_explanation_uses_crlf(self, make_harness: MakeHarness) -> None:
        """Newlines in the final explanation are written as CRLF (raw mode has no ONLCR)."""
        fake_llm = FakeLLMClient(results=[
            _final_result(1, "Summary:\n- item one\n- item two"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("summarize")
        await harness.wait_done()

        out = harness.output()
        assert b"Summary:\r\n- item one\r\n- item two" in out
        end_marker = out[out.index(b"[Summary:"):]
        assert b"\r\n" in end_marker

    async def test_tool_output_chaining(self, make_harness: MakeHarness) -> None:
        """Tool results are sent back as function_call_output with previous_response_id."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done"),
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
            _final_result(2, "done"),
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
            _final_result(2, "done"),
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
                on_delta: Callable[[str], None] | None = None,
            ) -> StepResult:
                self.step_calls.append((list(input_items), previous_response_id))
                result = self._results[self._call_count]
                self._call_count += 1
                if result.tool_calls and self._write_output is not None:
                    self._write_output(b"partial echo")
                return result

        fake_llm = NoNewlineLLM(results=[
            _cmd_result(1, "ls"),
            _final_result(2, "done"),
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
                final_text=None,
                response_id="resp_1",
            ),
            _final_result(2, "done"),
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
            _final_result(2, "done"),
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
        fake_llm = FakeLLMClient(results=[_final_result(1, "done")])
        harness = await make_harness(fake_llm, history_lines=5, screen=screen)
        harness.start("where am i")
        await harness.wait_done()

        assert fake_llm.last_screen_text is not None
        lines = fake_llm.last_screen_text.split("\n")
        assert len(lines) == 5
        assert "line 0" not in fake_llm.last_screen_text


class TestStreamingFinalDisplay:
    async def test_streamed_text_shown_incrementally(self, make_harness: MakeHarness) -> None:
        """Streamed deltas erase the thinking overlay, open the gray
        gray "[" prefix, and are closed with "]" and an SGR reset."""
        fake_llm = StreamingFakeLLM(
            results=[_final_result(1, "Hello world")],
            chunks=[["Hello ", "world"]],
        )
        harness = await make_harness(fake_llm)
        harness.start("greet")
        await harness.wait_done()

        out = harness.output()
        assert b"\x1b8\x1b[J\x1b[90m[Hello " in out
        assert b"[Hello world]\x1b[0m" in out
        # The streamed text is not repeated by the non-streaming fallback.
        assert out.count(b"Hello world") == 1

    async def test_streamed_newlines_use_crlf(self, make_harness: MakeHarness) -> None:
        """Newlines in streamed deltas are normalized to CRLF (raw mode)."""
        fake_llm = StreamingFakeLLM(
            results=[_final_result(1, "Summary:\n- one")],
            chunks=[["Summary:\n", "- one"]],
        )
        harness = await make_harness(fake_llm)
        harness.start("summarize")
        await harness.wait_done()

        assert b"[Summary:\r\n- one]" in harness.output()

    async def test_interrupt_mid_stream_keeps_partial_text(self, make_harness: MakeHarness) -> None:
        """Ctrl-C during streaming keeps the partial text on screen, closes
        the gray, and shows the interrupted note on the next line."""

        class BlockingStreamLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.streamed = asyncio.Event()

            async def step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                on_delta: Callable[[str], None] | None = None,
            ) -> StepResult:
                assert on_delta is not None
                on_delta("partial answer")
                self.streamed.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        fake_llm = BlockingStreamLLM()
        harness = await make_harness(fake_llm)
        harness.start("explain")
        await asyncio.wait_for(fake_llm.streamed.wait(), timeout=5)
        harness.user_types(b"\x03")
        await harness.wait_done()

        out = harness.output()
        assert b"[partial answer" in out
        assert b"partial answer\x1b[0m\r\n\x1b[90m[expecto interrupted]" in out

    async def test_narration_before_tool_call_is_closed(self, make_harness: MakeHarness) -> None:
        """Text streamed in a response that also carries a tool call is
        closed with "]" before the command runs; the loop continues."""
        fake_llm = StreamingFakeLLM(
            results=[_cmd_result(1, "ls"), _final_result(2, "all done")],
            chunks=[["checking"], ["all done"]],
        )
        harness = await make_harness(fake_llm)
        harness.start("list files")
        await harness.wait_done()

        out = harness.output()
        assert b"[checking]" in out
        assert b"[all done]" in out


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
            [_risky_cmd_result(1, "reload"), _final_result(2, "finished")],
            [b"y"],
        )
        assert b"reload\n" in harness.pty_written
        assert b"destructive command (test reason): reload" in harness.output()

    async def test_destructive_declined_feeds_back_to_llm(self, make_harness: MakeHarness) -> None:
        """'n' does not abort the loop: the LLM gets a declined tool result."""
        fake_llm, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result(2, "gave up")],
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
                _final_result(2, "finished"),
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
                _final_result(2, "finished"),
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
                _final_result(2, "finished"),
            ],
            [b"y"],
            confirm_level="mutating",
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" in harness.output()

    async def test_confirm_level_none_skips_confirmation(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result(2, "finished")],
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
                _final_result(2, "finished"),
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
            _final_result(2, "gave up"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await _until(lambda: b"run? (y/n)" in harness.output())
        os.close(harness.stdin_w)
        await harness.wait_done()

        assert b"reload\n" not in harness.pty_written
        assert b"[expecto interrupted]" in harness.output()



class TestSessionChain:
    async def test_chain_continues_across_queries(self, make_harness: MakeHarness) -> None:
        """The response chain spans queries: the second query starts from
        the first query's last response id."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "one"), _final_result(2, "two")])
        harness = await make_harness(fake_llm)
        harness.start("first")
        await harness.wait_done()
        harness.start("second")
        await harness.wait_done()

        assert len(fake_llm.step_calls) == 2
        assert fake_llm.step_calls[0][1] is None
        assert fake_llm.step_calls[1][1] == "resp_1"

    async def test_second_query_sends_delta_not_screen(self, make_harness: MakeHarness) -> None:
        """A continuing chain gets only the output produced between the
        queries, not the full screen (which the chain already has)."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "one"), _final_result(2, "two")])
        harness = await make_harness(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("first")
        await harness.wait_done()
        assert fake_llm.last_screen_text is not None
        assert fake_llm.last_new_output is None

        harness.child_writes(b"manual command output\r\n")
        await _until(lambda: b"manual command output" in harness.output())
        harness.start("second")
        await harness.wait_done()
        assert fake_llm.last_screen_text is None
        assert fake_llm.last_new_output is not None
        assert "manual command output" in fake_llm.last_new_output

    async def test_no_delta_section_when_nothing_happened(self, make_harness: MakeHarness) -> None:
        fake_llm = FakeLLMClient(results=[_final_result(1, "one"), _final_result(2, "two")])
        harness = await make_harness(fake_llm)
        harness.start("first")
        await harness.wait_done()
        harness.start("second")
        await harness.wait_done()

        assert fake_llm.last_screen_text is None
        assert fake_llm.last_new_output is None

    async def test_new_output_capped_tail_kept(self, make_harness: MakeHarness) -> None:
        """Inter-query output longer than new_output_max_chars keeps the
        tail and carries a truncation note."""
        fake_llm = FakeLLMClient(results=[_final_result(1, "one"), _final_result(2, "two")])
        harness = await make_harness(fake_llm, new_output_max_chars=60)
        harness.start("first")
        await harness.wait_done()

        for i in range(20):
            harness.child_writes(f"line-{i:02d}\r\n".encode())
        await _until(lambda: b"line-19" in harness.output())
        harness.start("second")
        await harness.wait_done()

        new_output = fake_llm.last_new_output
        assert new_output is not None
        assert new_output.startswith("[... earlier output truncated ...]\n")
        assert "line-19" in new_output
        body = new_output.removeprefix("[... earlier output truncated ...]\n")
        assert len(body) <= 60

    async def test_pending_outputs_after_checkpoint_stop(self, make_harness: MakeHarness) -> None:
        """Stopping at a checkpoint leaves the last tool output undelivered;
        the next query delivers it before the new goal, keeping the chain
        free of unanswered function calls."""
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "cmd1"), _final_result(2, "done")])
        harness = await make_harness(fake_llm, max_steps=1)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"n"]),
        )
        harness.start("first")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)
        assert b"[expecto stopped at step 1]" in harness.output()

        harness.start("second")
        await harness.wait_done()
        items, prev_id = fake_llm.step_calls[1]
        assert prev_id == "resp_1"
        first = _item_dict(items[0])
        assert first["type"] == "function_call_output"
        assert first["call_id"] == "call_1"
        assert "goal: second" in str(_item_dict(items[1])["content"])

    async def test_interrupted_command_answered_on_next_query(self, make_harness: MakeHarness) -> None:
        """Ctrl-C during a command leaves a dangling function_call; the next
        query answers it with the output collected up to the interrupt."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "sleep 100"),
            _final_result(2, "done"),
        ])
        harness = await make_harness(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("run it")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()
        assert b"[expecto interrupted]" in harness.output()

        harness.start("second")
        await harness.wait_done()
        items, prev_id = fake_llm.step_calls[1]
        assert prev_id == "resp_1"
        first = _item_dict(items[0])
        assert first["type"] == "function_call_output"
        assert first["call_id"] == "call_1"
        assert "interrupted" in str(first["output"]).lower()

    async def test_compaction_starts_a_new_chain(self, make_harness: MakeHarness) -> None:
        """Past the token threshold, the next query compacts the chain and
        restarts fresh: summary + full screen, no previous id."""
        fake_llm = FakeLLMClient(results=[
            StepResult(
                tool_calls=[], final_text="one", response_id="resp_1",
                input_tokens=500,
            ),
            _final_result(2, "two"),
        ])
        harness = await make_harness(fake_llm, chain_compact_thresh=100)
        harness.start("first")
        await harness.wait_done()
        harness.start("second")
        await harness.wait_done()

        assert fake_llm.compact_calls == [("resp_1", None)]
        assert fake_llm.step_calls[1][1] is None
        assert fake_llm.last_summary is fake_llm.compact_result
        assert fake_llm.last_screen_text is not None
        assert b"[compacting...]" in harness.output()

    async def test_compaction_failure_keeps_the_chain(self, make_harness: MakeHarness) -> None:
        """When the compaction call fails, the old chain is kept and the
        query proceeds on it (compaction is retried at the next query)."""
        fake_llm = FakeLLMClient(results=[
            StepResult(
                tool_calls=[], final_text="one", response_id="resp_1",
                input_tokens=500,
            ),
            _final_result(2, "two"),
        ])
        fake_llm.compact_error = RuntimeError("api down")
        harness = await make_harness(fake_llm, chain_compact_thresh=100)
        harness.start("first")
        await harness.wait_done()
        harness.start("second")
        await harness.wait_done()

        assert len(fake_llm.compact_calls) == 1
        assert fake_llm.step_calls[1][1] == "resp_1"  # old chain kept
        assert fake_llm.last_summary is None

    async def test_chain_lost_restarts_fresh(self, make_harness: MakeHarness) -> None:
        """A ChainLostError from the server restarts the query on a fresh
        chain with the full screen."""

        class ChainLosingLLM(FakeLLMClient):
            def __init__(self, results: list[StepResult]) -> None:
                super().__init__(results)
                self.fail_next = False

            async def step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                on_delta: Callable[[str], None] | None = None,
            ) -> StepResult:
                if self.fail_next:
                    self.fail_next = False
                    self.step_calls.append((list(input_items), previous_response_id))
                    raise ChainLostError("previous response not found")
                return await super().step(input_items, previous_response_id, on_delta)

        fake_llm = ChainLosingLLM([_final_result(1, "one"), _final_result(2, "two")])
        harness = await make_harness(fake_llm)
        harness.start("first")
        await harness.wait_done()
        fake_llm.fail_next = True
        harness.start("second")
        await harness.wait_done()

        assert len(fake_llm.step_calls) == 3
        assert fake_llm.step_calls[1][1] == "resp_1"  # failed attempt
        assert fake_llm.step_calls[2][1] is None  # fresh restart
        assert fake_llm.last_screen_text is not None
        assert b"[two]" in harness.output()
