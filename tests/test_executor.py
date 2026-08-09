from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import pytest

from expecto.dangerous import DangerousCommandMatcher
from expecto.executor import ConfirmLevel, Executor
from expecto.input_router import InputRouter
from expecto.io_relay import IORelay
from expecto.llm_client import (
    LoadSkillParams,
    MCPToolParams,
    ParsedToolCall,
    RunCommandParams,
    SendKeysParams,
    StepResult,
)
from expecto.screen_buffer import ScreenBuffer
from expecto.skills import Skill

HOTKEY = bytes([0x1F])
ENTER = bytes([0x0D])

MakeHarness = Callable[..., Coroutine[object, object, "Harness"]]


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
    )


def _keys_result(step: int, keys: str) -> StepResult:
    return StepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name="send_keys",
                params=SendKeysParams(keys=keys),
            ),
        ],
        final_text=None,
    )


def _final_result(text: str) -> StepResult:
    return StepResult(tool_calls=[], final_text=text)


def _mcp_result(step: int, name: str, **arguments: object) -> StepResult:
    return StepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name=name,
                params=MCPToolParams(name=name, arguments=arguments),
            ),
        ],
        final_text=None,
    )


def _skill_result(step: int, name: str, file: str = "") -> StepResult:
    return StepResult(
        tool_calls=[
            ParsedToolCall(
                call_id=f"call_{step}",
                name="load_skill",
                params=LoadSkillParams(name=name, file=file),
            ),
        ],
        final_text=None,
    )


class FakeMCP:
    """Implements the MCPCaller protocol with a canned answer."""

    def __init__(self, output: str = "mcp result", delay: float = 0.0) -> None:
        self._output = output
        self._delay = delay
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, qualified_name: str, arguments: dict[str, object]) -> str:
        self.calls.append((qualified_name, arguments))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._output


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
    )


class FakeLLMClient:
    """Implements the LLMStepper protocol: serves canned StepResults and
    records queries and tool outputs like the real conversation-owning
    client."""

    def __init__(self, results: list[StepResult] | None = None) -> None:
        self._results = results or []
        self._write_output: Callable[[bytes], None] | None = None
        self.step_count = 0
        self.finish_calls = 0
        self.queries: list[str] = []
        self.screen_texts: list[str] = []
        self.new_outputs: list[str | None] = []
        self.tool_outputs: list[tuple[str, str]] = []
        # What the fake child will emit for the next tool call.
        self.pending_output: bytes | None = None

    @property
    def last_screen_text(self) -> str | None:
        return self.screen_texts[-1] if self.screen_texts else None

    @property
    def last_new_output(self) -> str | None:
        return self.new_outputs[-1] if self.new_outputs else None

    def set_output_sink(self, write: Callable[[bytes], None]) -> None:
        """Wire the fake to the harness (kept for tests that write to the
        child directly)."""
        self._write_output = write

    async def start_query(
        self,
        query: str,
        *,
        screen_text: str,
        new_output: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        self.queries.append(query)
        self.screen_texts.append(screen_text)
        self.new_outputs.append(new_output)
        return await self.step(on_delta=on_delta)

    def add_tool_output(self, call_id: str, output: str) -> None:
        self.tool_outputs.append((call_id, output))

    async def step(
        self, on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        result = self._results[self.step_count]
        self.step_count += 1
        if result.tool_calls:
            # Queued, not written: a real child produces its output only
            # once the command reaches the pty, which is what the harness
            # replays (see make_harness).
            self.pending_output = f"output from step {self.step_count}\n".encode()
        return result

    def finish_query(self) -> None:
        self.finish_calls += 1


class StreamingFakeLLM(FakeLLMClient):
    """Delivers each result's text through on_delta in chunks before
    returning it, like the real streaming client."""

    def __init__(self, results: list[StepResult], chunks: list[list[str]]) -> None:
        super().__init__(results)
        self._chunks = chunks

    async def step(
        self, on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        if on_delta is not None:
            for chunk in self._chunks[self.step_count]:
                on_delta(chunk)
        return await super().step(on_delta=on_delta)


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
        confirm_level: ConfirmLevel = "destructive",
        dangerous_matcher: DangerousCommandMatcher | None = None,
        screen: ScreenBuffer | None = None,
        new_output_max_chars: int = 20000,
        mcp: FakeMCP | None = None,
        skills: list[Skill] | None = None,
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

        def write_to_pty(data: bytes) -> None:
            pty_written.append(data)
            # A real child answers an injected command with its output;
            # the prompt nudge (Ctrl-U + newline) only redraws a prompt.
            if data == b"\x15\n" or fake_llm.pending_output is None:
                return
            harness.child_writes(fake_llm.pending_output)
            fake_llm.pending_output = None

        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=on_submit,
            on_query_cancel=lambda: None,
        )
        relay = IORelay(input_router=router, screen_buffer=screen)
        executor = Executor(
            llm_client=fake_llm,
            screen_buffer=screen,
            write_to_pty=write_to_pty,
            stdout_fd=stdout_w,
            relay=relay,
            settle_ms=settle_ms,
            threshold_ms=threshold_ms,
            max_steps=max_steps,
            confirm_level=confirm_level,
            dangerous_matcher=dangerous_matcher,
            new_output_max_chars=new_output_max_chars,
            mcp=mcp,
            skills=skills,
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
        fake_llm = FakeLLMClient(results=[_final_result("task complete")])
        harness = await make_harness(fake_llm)
        harness.start("say hello")
        await harness.wait_done()

        assert harness.pty_written == [b"\n"]  # no command: no prompt nudge
        assert b"[task complete]" in harness.output()

    async def test_empty_final_text(self, make_harness: MakeHarness) -> None:
        """A final response with no text still shows an end marker."""
        fake_llm = FakeLLMClient(results=[_final_result("")])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        assert harness.pty_written == [b"\n"]
        assert b"[expecto: empty response]" in harness.output()

    async def test_two_steps(self, make_harness: MakeHarness) -> None:
        """LLM calls run_command then returns done — settle wait between steps."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result("complete"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("list then finish")
        await harness.wait_done()

        assert harness.pty_written == [b"\x15\n", b"ls\n", b"\n"]
        out = harness.output()
        assert b"[complete]" in out
        assert b"output from step 1" in out  # relayed to the user's screen

    async def test_send_keys_writes_parsed_bytes(self, make_harness: MakeHarness) -> None:
        """LLM calls send_keys with key notation; the decoded bytes reach
        the pty."""
        fake_llm = FakeLLMClient(results=[
            _keys_result(1, "q<C-c><Esc><CR>"),
            _final_result("stopped it"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("stop it")
        await harness.wait_done()

        assert harness.pty_written == [b"q\x03\x1b\r", b"\n"]  # no Ctrl-U nudge

    async def test_prompt_nudge_precedes_the_first_command_only(
        self, make_harness: MakeHarness,
    ) -> None:
        """Ctrl-U + newline is sent once, right before the first injected
        command — not at query start, and not again afterwards."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _cmd_result(2, "pwd"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("look around")
        await harness.wait_done()

        assert harness.pty_written == [b"\x15\n", b"ls\n", b"pwd\n", b"\n"]

    async def test_prompt_nudge_after_a_terminal_free_tool(
        self, make_harness: MakeHarness,
    ) -> None:
        """load_skill never touches the terminal, so the command that
        follows it still gets its prompt nudge."""
        skill = Skill(
            name="greet", description="Say hello", path="/skills/greet", body="hi",
        )
        fake_llm = FakeLLMClient(results=[
            _skill_result(1, "greet"),
            _cmd_result(2, "ls"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm, skills=[skill])
        harness.start("use the skill")
        await harness.wait_done()

        assert harness.pty_written == [b"\x15\n", b"ls\n", b"\n"]

    async def test_no_prompt_nudge_once_send_keys_was_used(
        self, make_harness: MakeHarness,
    ) -> None:
        """After send_keys, the LLM is driving whatever the terminal is
        running; a later Ctrl-U would land inside it, so it is skipped."""
        fake_llm = FakeLLMClient(results=[
            _keys_result(1, "<Esc>:q<CR>"),
            _cmd_result(2, "ls"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("quit then list")
        await harness.wait_done()

        assert harness.pty_written == [b"\x1b:q\r", b"ls\n", b"\n"]

    async def test_send_keys_result_is_screen_and_cursor(
        self, make_harness: MakeHarness,
    ) -> None:
        """The send_keys tool result carries the visible screen and the
        cursor position, not a diff."""
        screen = ScreenBuffer(rows=5, cols=40)
        screen.feed(b"editor line one\r\nline two")
        fake_llm = FakeLLMClient(results=[
            _keys_result(1, "<Down>"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm, screen=screen)
        harness.start("move down")
        await harness.wait_done()

        _, output = fake_llm.tool_outputs[0]
        assert "## Terminal Screen" in output
        assert "editor line one" in output
        # "output from step 1" arrived from the fake child before settle.
        assert "[Cursor] row " in output

    async def test_send_keys_invalid_notation_reports_error(
        self, make_harness: MakeHarness,
    ) -> None:
        """An unknown key token is reported as the tool result; nothing
        is written to the pty."""
        fake_llm = FakeLLMClient(results=[
            _keys_result(1, "<NoSuchKey>"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("press it")
        await harness.wait_done()

        assert harness.pty_written == [b"\n"]  # no command: no prompt nudge
        _, output = fake_llm.tool_outputs[0]
        assert output.startswith("Error:")
        assert "NoSuchKey" in output

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
        assert fake_llm.step_count == max_steps
        assert f"reached step {max_steps}".encode() in out
        assert b"continue? (y/n)" in out
        assert f"[expecto: stopped at step {max_steps}]".encode() in out

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

        assert fake_llm.step_count == 4
        out = harness.output()
        assert b"reached step 2" in out
        assert b"reached step 4" in out
        assert b"[expecto: stopped at step 4]" in out

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

        assert fake_llm.step_count == 1
        assert b"[expecto: stopped at step 1]" in harness.output()

    async def test_max_steps_zero_never_asks(self, make_harness: MakeHarness) -> None:
        """max_steps < 1 disables the checkpoint: the loop runs to the
        final response without ever asking for continuation."""
        fake_llm = FakeLLMClient(results=[
            *[_cmd_result(i, f"cmd{i}") for i in range(1, 5)],
            _final_result("all done"),
        ])
        harness = await make_harness(fake_llm, max_steps=0)
        harness.start("keep going")
        await harness.wait_done()

        assert fake_llm.step_count == 5
        out = harness.output()
        assert b"continue? (y/n)" not in out
        assert b"[all done]" in out

    async def test_ctrl_c_interrupt_during_settle(self, make_harness: MakeHarness) -> None:
        """User Ctrl-C during the settle wait cancels the execution task."""
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "sleep 100")])
        harness = await make_harness(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("run forever")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert fake_llm.step_count == 1
        assert b"[expecto: interrupted]" in harness.output()

    async def test_ctrl_c_during_llm_call(self, make_harness: MakeHarness) -> None:
        """Ctrl-C while the LLM call is in flight cancels the request
        itself; no command reaches the pty."""

        class BlockingLLM(FakeLLMClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.was_cancelled = False

            async def step(
                self, on_delta: Callable[[str], None] | None = None,
            ) -> StepResult:
                self.step_count += 1
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

        assert harness.pty_written == [b"\n"]  # no command was injected
        assert fake_llm.was_cancelled  # the in-flight call was truly cancelled
        assert b"[expecto: interrupted]" in harness.output()

    async def test_thinking_indicator(self, make_harness: MakeHarness) -> None:
        """The thinking indicator is shown while the LLM call is in flight."""
        fake_llm = FakeLLMClient(results=[_final_result("done")])
        harness = await make_harness(fake_llm)
        harness.start("test")
        await harness.wait_done()

        assert b"\x1b[90m[thinking...]\x1b[0m" in harness.output()

    async def test_multiline_explanation_uses_crlf(self, make_harness: MakeHarness) -> None:
        """Newlines in the final explanation are written as CRLF (raw mode has no ONLCR)."""
        fake_llm = FakeLLMClient(results=[
            _final_result("Summary:\n- item one\n- item two"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("summarize")
        await harness.wait_done()

        out = harness.output()
        assert b"Summary:\r\n- item one\r\n- item two" in out
        end_marker = out[out.index(b"[Summary:"):]
        assert b"\r\n" in end_marker

    async def test_tool_output_recorded_on_conversation(self, make_harness: MakeHarness) -> None:
        """The query starts the conversation and each tool call's result is
        recorded before the follow-up step."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("test history")
        await harness.wait_done()

        assert fake_llm.step_count == 2
        assert fake_llm.queries == ["test history"]
        assert len(fake_llm.tool_outputs) == 1
        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert "Terminal Output" in output
        assert "output from step 1" in output

    async def test_tool_output_excludes_past_output(self, make_harness: MakeHarness) -> None:
        """Tool results contain only output produced by the executed command."""
        screen = ScreenBuffer()
        screen.feed(b"previous command output\r\nuser@host:~$ ")
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "echo hi"),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm, screen=screen)
        harness.start("say hi")
        await harness.wait_done()

        _, output = fake_llm.tool_outputs[0]
        assert "output from step 1" in output
        assert "previous command output" not in output

    async def test_command_echo_colored_gray(self, make_harness: MakeHarness) -> None:
        """The echoed command is bracketed in gray; reset follows the first newline."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "ls"),
            _final_result("done"),
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
                self, on_delta: Callable[[str], None] | None = None,
            ) -> StepResult:
                result = self._results[self.step_count]
                self.step_count += 1
                if result.tool_calls:
                    self.pending_output = b"partial echo"
                return result

        fake_llm = NoNewlineLLM(results=[
            _cmd_result(1, "ls"),
            _final_result("done"),
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
            ),
            _final_result("done"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await harness.wait_done()

        # No command was written to the pty, only the nudge and trailing newline.
        assert harness.pty_written == [b"\n"]

        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert output.startswith("Error:")

    async def test_threshold_note_in_tool_output(self, make_harness: MakeHarness) -> None:
        """When output keeps flowing past the threshold, the tool output carries a note."""
        fake_llm = FakeLLMClient(results=[
            _cmd_result(1, "long-running"),
            _final_result("done"),
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

        _, output = fake_llm.tool_outputs[0]
        assert "Terminal Output" in output
        assert "[Note] Output did not settle" in output

    async def test_screen_text_capped_at_new_output_max_chars(
        self, make_harness: MakeHarness,
    ) -> None:
        """The screen text passed to the LLM is tail-capped at
        new_output_max_chars, with a truncation note."""
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        fake_llm = FakeLLMClient(results=[_final_result("done")])
        harness = await make_harness(
            fake_llm, new_output_max_chars=30, screen=screen,
        )
        harness.start("where am i")
        await harness.wait_done()

        assert fake_llm.last_screen_text is not None
        assert fake_llm.last_screen_text.startswith(
            "[... earlier output truncated ...]\n",
        )
        head, _, tail = fake_llm.last_screen_text.partition("\n")
        assert len(tail) == 30
        assert tail.endswith("line 19")
        assert "line 0\n" not in fake_llm.last_screen_text

    async def test_short_screen_text_not_truncated(
        self, make_harness: MakeHarness,
    ) -> None:
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        screen.feed(b"hello\r\n$ ")
        fake_llm = FakeLLMClient(results=[_final_result("done")])
        harness = await make_harness(fake_llm, screen=screen)
        harness.start("where am i")
        await harness.wait_done()

        assert fake_llm.last_screen_text is not None
        assert "hello" in fake_llm.last_screen_text
        assert "truncated" not in fake_llm.last_screen_text


class TestStreamingFinalDisplay:
    async def test_streamed_text_shown_incrementally(self, make_harness: MakeHarness) -> None:
        """Streamed deltas erase the thinking overlay, open the gray
        gray "[" prefix, and are closed with "]" and an SGR reset."""
        fake_llm = StreamingFakeLLM(
            results=[_final_result("Hello world")],
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
            results=[_final_result("Summary:\n- one")],
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
                self, on_delta: Callable[[str], None] | None = None,
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
        assert b"partial answer\x1b[0m\r\n\x1b[90m[expecto: interrupted]" in out

    async def test_narration_before_tool_call_is_closed(self, make_harness: MakeHarness) -> None:
        """Text streamed in a response that also carries a tool call is
        closed with "]" before the command runs; the loop continues."""
        fake_llm = StreamingFakeLLM(
            results=[_cmd_result(1, "ls"), _final_result("all done")],
            chunks=[["checking"], ["all done"]],
        )
        harness = await make_harness(fake_llm)
        harness.start("list files")
        await harness.wait_done()

        out = harness.output()
        assert b"[checking]" in out
        assert b"[all done]" in out


class TestLoadSkill:
    async def test_body_recorded_and_load_shown(
        self, make_harness: MakeHarness,
    ) -> None:
        """load_skill answers with the skill's instructions, writing
        nothing to the terminal."""
        fake_llm = FakeLLMClient(results=[
            _skill_result(1, "bgp"),
            _final_result("followed the skill"),
        ])
        skill = Skill(
            name="bgp",
            description="Fix BGP",
            path="/skills/bgp",
            body="1. show bgp summary",
        )
        harness = await make_harness(fake_llm, skills=[skill])
        harness.start("fix the peer")
        await harness.wait_done()

        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert "1. show bgp summary" in output
        assert harness.pty_written == [b"\n"]
        assert b"[expecto: skill bgp]" in harness.output()

    async def test_bundled_file_is_read_locally(
        self, make_harness: MakeHarness, tmp_path: Path,
    ) -> None:
        """A bundled file is served by expecto, not fetched through the
        terminal — which may be an ssh session with no such path."""
        (tmp_path / "checklist.md").write_text("- confirm the peer address\n")
        fake_llm = FakeLLMClient(results=[
            _skill_result(1, "bgp", file="checklist.md"),
            _final_result("done"),
        ])
        skill = Skill(
            name="bgp",
            description="Fix BGP",
            path=str(tmp_path),
            body="see the checklist",
            files=("checklist.md",),
        )
        harness = await make_harness(fake_llm, skills=[skill])
        harness.start("fix the peer")
        await harness.wait_done()

        _, output = fake_llm.tool_outputs[0]
        assert "confirm the peer address" in output
        assert harness.pty_written == [b"\n"]
        assert b"[expecto: skill bgp/checklist.md]" in harness.output()

    async def test_unreadable_bundled_file_lists_what_exists(
        self, make_harness: MakeHarness, tmp_path: Path,
    ) -> None:
        fake_llm = FakeLLMClient(results=[
            _skill_result(1, "bgp", file="../secrets.txt"),
            _final_result("gave up"),
        ])
        skill = Skill(
            name="bgp",
            description="Fix BGP",
            path=str(tmp_path),
            body="b",
            files=("checklist.md",),
        )
        harness = await make_harness(fake_llm, skills=[skill])
        harness.start("fix the peer")
        await harness.wait_done()

        _, output = fake_llm.tool_outputs[0]
        assert output.startswith("Error:")
        assert "checklist.md" in output

    async def test_unknown_skill_lists_what_is_available(
        self, make_harness: MakeHarness,
    ) -> None:
        fake_llm = FakeLLMClient(results=[
            _skill_result(1, "nope"),
            _final_result("gave up"),
        ])
        skill = Skill(name="bgp", description="Fix BGP", path="/s/bgp", body="b")
        harness = await make_harness(fake_llm, skills=[skill])
        harness.start("fix the peer")
        await harness.wait_done()

        _, output = fake_llm.tool_outputs[0]
        assert output.startswith("Error: unknown skill: nope")
        assert "bgp" in output


class TestMCPToolCalls:
    async def test_result_recorded_and_call_shown(
        self, make_harness: MakeHarness,
    ) -> None:
        """An MCP call is dispatched to the server, shown on screen, and its
        result recorded — with no command written to the pty."""
        fake_llm = FakeLLMClient(results=[
            _mcp_result(1, "mcp__DOCS__search", query="bgp"),
            _final_result("found it"),
        ])
        mcp = FakeMCP(output="RFC 4271")
        harness = await make_harness(fake_llm, mcp=mcp)
        harness.start("look it up")
        await harness.wait_done()

        assert mcp.calls == [("mcp__DOCS__search", {"query": "bgp"})]
        assert fake_llm.tool_outputs == [("call_1", "RFC 4271")]
        # No command was injected: only the prompt nudge and final newline.
        assert harness.pty_written == [b"\n"]
        out = harness.output()
        assert b"[expecto: mcp DOCS.search]" in out
        assert b"[found it]" in out

    async def test_without_a_server_the_call_is_answered_with_an_error(
        self, make_harness: MakeHarness,
    ) -> None:
        fake_llm = FakeLLMClient(results=[
            _mcp_result(1, "mcp__DOCS__search"),
            _final_result("gave up"),
        ])
        harness = await make_harness(fake_llm)  # no mcp
        harness.start("look it up")
        await harness.wait_done()

        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert output.startswith("Error:")

    async def test_interrupt_during_a_call_records_the_output(
        self, make_harness: MakeHarness,
    ) -> None:
        """Ctrl-C during an MCP call still answers the tool call, so the
        conversation is never left with an unanswered one."""
        fake_llm = FakeLLMClient(results=[
            _mcp_result(1, "mcp__DOCS__search"),
            _final_result("unreachable"),
        ])
        mcp = FakeMCP(delay=5.0)
        harness = await make_harness(fake_llm, mcp=mcp)
        harness.start("look it up")
        await _until(lambda: bool(mcp.calls))
        harness.user_types(b"\x03")
        await harness.wait_done()

        assert [c for c, _ in fake_llm.tool_outputs] == ["call_1"]
        assert "interrupted" in fake_llm.tool_outputs[0][1]
        assert b"[expecto: interrupted]" in harness.output()


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
            [_risky_cmd_result(1, "reload"), _final_result("finished")],
            [b"y"],
        )
        assert b"reload\n" in harness.pty_written
        assert b"destructive command (test reason): reload" in harness.output()

    async def test_destructive_declined_feeds_back_to_llm(self, make_harness: MakeHarness) -> None:
        """'n' does not abort the loop: the LLM gets a declined tool result."""
        fake_llm, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result("gave up")],
            [b"n"],
        )
        assert b"reload\n" not in harness.pty_written
        assert fake_llm.step_count == 2
        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert "user declined" in output

    async def test_read_only_not_confirmed(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [
                _risky_cmd_result(1, "ls -la", risk="read_only"),
                _final_result("finished"),
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
                _final_result("finished"),
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
                _final_result("finished"),
            ],
            [b"y"],
            confirm_level="mutating",
        )
        assert b"touch /tmp/x\n" in harness.pty_written
        assert b"run? (y/n)" in harness.output()

    async def test_confirm_level_none_skips_confirmation(self, make_harness: MakeHarness) -> None:
        _, harness = await self._run_gate(
            make_harness,
            [_risky_cmd_result(1, "reload"), _final_result("finished")],
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
                _final_result("finished"),
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
            _final_result("gave up"),
        ])
        harness = await make_harness(fake_llm)
        harness.start("do something")
        await _until(lambda: b"run? (y/n)" in harness.output())
        os.close(harness.stdin_w)
        await harness.wait_done()

        assert b"reload\n" not in harness.pty_written
        assert b"[expecto: interrupted]" in harness.output()



class TestQueryContext:
    async def test_first_query_has_screen_no_delta(self, make_harness: MakeHarness) -> None:
        """The first query carries the full screen; there is no previous
        execution to diff against."""
        fake_llm = FakeLLMClient(results=[_final_result("one")])
        harness = await make_harness(fake_llm)
        harness.screen.feed(b"$ ")
        harness.start("first")
        await harness.wait_done()

        assert fake_llm.last_screen_text is not None
        assert "$" in fake_llm.last_screen_text
        assert fake_llm.last_new_output is None

    async def test_second_query_carries_inter_query_output(self, make_harness: MakeHarness) -> None:
        """Output produced between queries (the user's manual activity) is
        passed as new_output with the next query."""
        fake_llm = FakeLLMClient(results=[_final_result("one"), _final_result("two")])
        harness = await make_harness(fake_llm)
        harness.start("first")
        await harness.wait_done()

        harness.child_writes(b"manual command output\r\n")
        await _until(lambda: b"manual command output" in harness.output())
        harness.start("second")
        await harness.wait_done()

        assert fake_llm.queries == ["first", "second"]
        assert fake_llm.last_new_output is not None
        assert "manual command output" in fake_llm.last_new_output

    async def test_no_delta_when_nothing_happened(self, make_harness: MakeHarness) -> None:
        fake_llm = FakeLLMClient(results=[_final_result("one"), _final_result("two")])
        harness = await make_harness(fake_llm)
        harness.start("first")
        await harness.wait_done()
        harness.start("second")
        await harness.wait_done()

        assert fake_llm.last_new_output is None

    async def test_new_output_capped_tail_kept(self, make_harness: MakeHarness) -> None:
        """Inter-query output longer than new_output_max_chars keeps the
        tail and carries a truncation note."""
        fake_llm = FakeLLMClient(results=[_final_result("one"), _final_result("two")])
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


class TestConversationIntegrity:
    """Every tool call must get a recorded output, even across stops and
    interrupts, so the conversation never carries an unanswered call."""

    async def test_checkpoint_stop_keeps_outputs_recorded(self, make_harness: MakeHarness) -> None:
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "cmd1"), _final_result("done")])
        harness = await make_harness(fake_llm, max_steps=1)
        pump = asyncio.create_task(
            harness.answer_prompts(b"continue? (y/n)", [b"n"]),
        )
        harness.start("first")
        await harness.wait_done()
        await asyncio.wait_for(pump, timeout=5)
        assert b"[expecto: stopped at step 1]" in harness.output()

        assert len(fake_llm.tool_outputs) == 1
        assert fake_llm.tool_outputs[0][0] == "call_1"

    async def test_interrupted_command_records_partial_output(self, make_harness: MakeHarness) -> None:
        """Ctrl-C during a command records the output collected so far as
        the call's result."""
        fake_llm = FakeLLMClient(results=[_cmd_result(1, "sleep 100")])
        harness = await make_harness(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("run it")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()
        assert b"[expecto: interrupted]" in harness.output()

        assert len(fake_llm.tool_outputs) == 1
        call_id, output = fake_llm.tool_outputs[0]
        assert call_id == "call_1"
        assert "interrupted" in output.lower()

    async def test_finish_query_called_on_every_ending(self, make_harness: MakeHarness) -> None:
        """finish_query() fires after a normal end and after an interrupt,
        so the client can compact in the background at either ending."""
        fake_llm = FakeLLMClient(results=[
            _final_result("done"),
            _cmd_result(2, "sleep 100"),
        ])
        harness = await make_harness(fake_llm, settle_ms=200, threshold_ms=30000)
        harness.start("first")
        await harness.wait_done()
        assert fake_llm.finish_calls == 1

        harness.start("second")
        await _until(lambda: b"sleep 100\n" in harness.pty_written)
        harness.user_types(b"\x03")
        await harness.wait_done()
        assert fake_llm.finish_calls == 2
