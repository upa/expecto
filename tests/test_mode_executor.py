from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Literal, cast

from jou.llm_client import (
    AutoResponse,
    AutoStepResult,
    CompletionResponse,
    ParsedToolCall,
    RunCommandParams,
    SendControlParams,
    SummaryResult,
)
from jou.mode_executor import ModeExecutor
from jou.screen_buffer import ScreenBuffer
from jou.working_summary import WorkingSummaryManager

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputParam


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


class FakeLLMClient:
    def __init__(
        self,
        response: CompletionResponse | None = None,
        auto_results: list[AutoStepResult] | None = None,
        pty_w: int = -1,
    ) -> None:
        self._response = response or CompletionResponse(command="", explanation="")
        self._auto_results = auto_results or []
        self._auto_call_count = 0
        self._pty_w = pty_w
        self.last_screen_text: str = ""
        self.last_query: str = ""
        self.last_working_summary: str | None = None
        self.auto_step_calls: list[tuple[ResponseInputParam, str | None]] = []

    def complete(
        self,
        screen_text: str,
        query: str,
        working_summary: str | None = None,
    ) -> CompletionResponse:
        self.last_screen_text = screen_text
        self.last_query = query
        self.last_working_summary = working_summary
        return self._response

    def auto_step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
    ) -> AutoStepResult:
        self.last_working_summary = working_summary
        self.auto_step_calls.append((list(input_items), previous_response_id))
        result = self._auto_results[self._auto_call_count]
        self._auto_call_count += 1
        if result.tool_calls and self._pty_w >= 0:
            os.write(
                self._pty_w,
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


class TestCompletionMode:
    def test_injects_command(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="ls -la", explanation="list all files"),
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
            )
            executor.execute("list files", "completion")

            assert pty_written == [b"ls -la"]
            assert fake_llm.last_query == "list files"
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    def test_thinking_indicator_shown(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
            )
            executor.execute("where am i", "completion")

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)

            assert b"thinking..." in stdout_output
            assert b"\x1b8" in stdout_output
        finally:
            if stdout_w >= 0:
                os.close(stdout_w)
            os.close(stdout_r)


class TestWorkingSummaryIntegration:
    def _make_summary_manager(
        self, screen: ScreenBuffer,
    ) -> WorkingSummaryManager:
        class FakeSummaryLLM:
            def summarize(
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

    def test_completion_passes_summary_to_llm(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = self._make_summary_manager(screen)
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                working_summary=manager,
            )
            executor.execute("where am i", "completion")

            summary = fake_llm.last_working_summary
            assert summary is not None
            assert "## Session Context" in summary
            assert "Junos" in summary
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    def test_completion_without_manager_passes_none(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
            )
            executor.execute("where am i", "completion")
            assert fake_llm.last_working_summary is None
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    def test_auto_passes_summary_to_llm(self) -> None:
        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "finished")])
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = self._make_summary_manager(screen)
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                get_pty_master_fd=lambda: -1,
                working_summary=manager,
            )
            executor.execute("check bgp", "auto")

            summary = fake_llm.last_working_summary
            assert summary is not None
            assert "## Session Context" in summary
        finally:
            os.close(stdout_r)
            os.close(stdout_w)

    def test_completion_screen_text_limited_to_history_lines(self) -> None:
        fake_llm = FakeLLMClient(
            response=CompletionResponse(command="pwd", explanation=""),
        )
        screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                history_lines=5,
            )
            executor.execute("where am i", "completion")

            lines = fake_llm.last_screen_text.split("\n")
            assert len(lines) == 5
            assert "line 0" not in fake_llm.last_screen_text
        finally:
            os.close(stdout_r)
            os.close(stdout_w)


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


class TestDangerousCommandConfirmation:
    def _run(
        self,
        auto_results: list[AutoStepResult],
        answers: list[bytes],
        confirm_level: str = "destructive",
        with_matcher: bool = False,
    ) -> tuple[FakeLLMClient, list[bytes], bytearray]:
        from jou.dangerous import DangerousCommandMatcher
        from jou.mode_executor import ConfirmLevel

        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        fake_llm = FakeLLMClient(auto_results=auto_results, pty_w=pty_w)
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()
        pty_written: list[bytes] = []
        collected = bytearray()
        pump = self._answer_prompts(stdout_r, stdin_w, answers, collected)
        matcher = DangerousCommandMatcher.load(None)[0] if with_matcher else None

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=50,
                confirm_level=cast("ConfirmLevel", confirm_level),
                dangerous_matcher=matcher,
            )
            executor.execute("do something", "auto")
            os.close(stdout_w)
            stdout_w = -1
            pump.join(timeout=5)
            return fake_llm, pty_written, collected
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _answer_prompts(
        self,
        stdout_r: int,
        stdin_w: int,
        answers: list[bytes],
        collected: bytearray,
    ) -> threading.Thread:
        def run() -> None:
            pending = list(answers)
            answered = 0
            while True:
                try:
                    data = os.read(stdout_r, 4096)
                except OSError:
                    return
                if not data:
                    return
                collected.extend(data)
                if pending and collected.count(b"run? (y/n)") > answered:
                    answered += 1
                    os.write(stdin_w, pending.pop(0))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_destructive_confirmed_y_runs(self) -> None:
        results = [
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "finished"),
        ]
        fake_llm, pty_written, collected = self._run(results, [b"y"])
        assert b"reload\n" in pty_written
        assert b"destructive command (test reason): reload" in collected

    def test_destructive_declined_feeds_back_to_llm(self) -> None:
        """'n' does not abort the loop: the LLM gets a declined tool result."""
        results = [
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "gave up"),
        ]
        fake_llm, pty_written, collected = self._run(results, [b"n"])
        assert b"reload\n" not in pty_written
        assert len(fake_llm.auto_step_calls) == 2
        second_input, _ = fake_llm.auto_step_calls[1]
        output_item = _item_dict(second_input[0])
        assert "user declined" in str(output_item["output"])

    def test_read_only_not_confirmed(self) -> None:
        results = [
            _risky_cmd_result(1, "ls -la", risk="read_only"),
            _final_result(2, "done", "finished"),
        ]
        _, pty_written, collected = self._run(results, [])
        assert b"ls -la\n" in pty_written
        assert b"run? (y/n)" not in collected

    def test_mutating_not_confirmed_at_default_level(self) -> None:
        results = [
            _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
            _final_result(2, "done", "finished"),
        ]
        _, pty_written, collected = self._run(results, [])
        assert b"touch /tmp/x\n" in pty_written
        assert b"run? (y/n)" not in collected

    def test_mutating_confirmed_at_mutating_level(self) -> None:
        results = [
            _risky_cmd_result(1, "touch /tmp/x", risk="mutating"),
            _final_result(2, "done", "finished"),
        ]
        _, pty_written, collected = self._run(
            results, [b"y"], confirm_level="mutating",
        )
        assert b"touch /tmp/x\n" in pty_written
        assert b"run? (y/n)" in collected

    def test_confirm_level_none_skips_confirmation(self) -> None:
        results = [
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "finished"),
        ]
        _, pty_written, collected = self._run(results, [], confirm_level="none")
        assert b"reload\n" in pty_written
        assert b"run? (y/n)" not in collected

    def test_backstop_escalates_underreported_risk(self) -> None:
        """A pattern match forces confirmation even when the LLM claims
        read_only, and the prompt shows the matched pattern."""
        results = [
            _risky_cmd_result(1, "write erase", risk="read_only"),
            _final_result(2, "done", "finished"),
        ]
        _, pty_written, collected = self._run(
            results, [b"n"], with_matcher=True,
        )
        assert b"write erase\n" not in pty_written
        assert b"destructive command (matched pattern:" in collected

    def test_non_interactive_declines_destructive(self) -> None:
        """Without stdin, a destructive command is declined, not executed."""
        pty_r, pty_w = os.pipe()
        results = [
            _risky_cmd_result(1, "reload"),
            _final_result(2, "done", "gave up"),
        ]
        fake_llm = FakeLLMClient(auto_results=results, pty_w=pty_w)
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()
        pty_written: list[bytes] = []

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=50,
            )
            executor.execute("do something", "auto")
            assert b"reload\n" not in pty_written
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


class TestAutoMode:
    def test_done_immediately(self) -> None:
        """LLM returns done on first step — no tool call, just done message."""
        auto_results = [_final_result(1, "done", "task complete")]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        pty_r, pty_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=1000,
                max_auto_steps=10,
            )
            executor.execute("say hello", "auto")

            assert pty_written == [b"\n", b"\n"]  # initial prompt nudge + final newline

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"[auto done:" in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_fail_status(self) -> None:
        """LLM returns fail — shows failed message."""
        auto_results = [_final_result(1, "fail", "cannot proceed")]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()
        pty_r, pty_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
            )
            executor.execute("do something", "auto")

            assert pty_written == [b"\n", b"\n"]  # initial prompt nudge + final newline

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"[auto failed:" in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_two_steps(self) -> None:
        """LLM calls run_command then returns done — settle wait between steps."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "ls"),
            _final_result(2, "done", "complete"),
        ]
        fake_llm = FakeLLMClient(
            auto_results=auto_results,
            pty_w=pty_w,
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=10,
            )
            executor.execute("list then finish", "auto")

            assert pty_written == [b"\n", b"ls\n", b"\n"]

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"[auto done:" in stdout_output
            assert b"output from step 1" in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_control_char(self) -> None:
        """LLM calls send_control with ctrl-c, then done."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _control_result(1, "ctrl-c"),
            _final_result(2, "done", "interrupted"),
        ]
        fake_llm = FakeLLMClient(
            auto_results=auto_results,
            pty_w=pty_w,
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
            )
            executor.execute("stop it", "auto")

            assert pty_written[0] == b"\n"  # initial prompt nudge
            assert pty_written[1] == b"\x03"
            assert pty_written[-1] == b"\n"
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_max_steps_without_stdin_stops(self) -> None:
        """At the checkpoint with no interactive stdin, the loop stops."""
        pty_r, pty_w = os.pipe()
        max_steps = 3

        auto_results = [
            _cmd_result(i, f"cmd{i}") for i in range(1, max_steps + 1)
        ]
        fake_llm = FakeLLMClient(
            auto_results=auto_results,
            pty_w=pty_w,
        )
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=max_steps,
            )
            executor.execute("keep going", "auto")

            assert len(pty_written) == max_steps + 2
            assert pty_written[0] == b"\n"  # initial prompt nudge
            assert pty_written[-1] == b"\n"

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert f"[auto stopped at step {max_steps}]".encode() in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _answer_prompts(
        self,
        stdout_r: int,
        stdin_w: int,
        answers: list[bytes],
        collected: bytearray,
    ) -> threading.Thread:
        """Drain stdout on a thread; each time the checkpoint prompt appears,
        write the next answer to stdin. (Input typed before the prompt is
        shown is consumed by the settle loop, so answers must be written
        only after the prompt is visible.)"""

        def run() -> None:
            pending = list(answers)
            answered = 0
            while True:
                try:
                    data = os.read(stdout_r, 4096)
                except OSError:
                    return
                if not data:
                    return
                collected.extend(data)
                if pending and collected.count(b"continue? (y/n)") > answered:
                    answered += 1
                    os.write(stdin_w, pending.pop(0))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_checkpoint_answer_n_stops(self) -> None:
        """At the checkpoint, answering 'n' stops the loop."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        max_steps = 2

        auto_results = [
            _cmd_result(i, f"cmd{i}") for i in range(1, max_steps + 1)
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results, pty_w=pty_w)
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()
        collected = bytearray()
        pump = self._answer_prompts(stdout_r, stdin_w, [b"n"], collected)

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=max_steps,
            )
            executor.execute("keep going", "auto")

            os.close(stdout_w)
            stdout_w = -1
            pump.join(timeout=5)

            assert len(fake_llm.auto_step_calls) == max_steps
            assert f"reached step {max_steps}".encode() in collected
            assert b"continue? (y/n)" in collected
            assert f"[auto stopped at step {max_steps}]".encode() in collected
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_checkpoint_answer_y_continues(self) -> None:
        """Answering 'y' continues the loop past the checkpoint, keeping the
        response chain, and asks again at the next checkpoint."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        max_steps = 2

        auto_results = [_cmd_result(i, f"cmd{i}") for i in range(1, 5)]
        fake_llm = FakeLLMClient(auto_results=auto_results, pty_w=pty_w)
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()
        collected = bytearray()
        pump = self._answer_prompts(stdout_r, stdin_w, [b"y", b"n"], collected)

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=max_steps,
            )
            executor.execute("keep going", "auto")

            os.close(stdout_w)
            stdout_w = -1
            pump.join(timeout=5)

            assert len(fake_llm.auto_step_calls) == 4
            # The chain is kept across the checkpoint: step 3 receives the
            # response id of step 2.
            _, prev_id_step3 = fake_llm.auto_step_calls[2]
            assert prev_id_step3 == "resp_2"

            assert b"reached step 2" in collected
            assert b"reached step 4" in collected
            assert b"[auto stopped at step 4]" in collected
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_checkpoint_ctrl_c_stops(self) -> None:
        """Ctrl-C at the checkpoint prompt stops the loop."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        max_steps = 1

        fake_llm = FakeLLMClient(
            auto_results=[_cmd_result(1, "cmd1")], pty_w=pty_w,
        )
        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()
        collected = bytearray()
        pump = self._answer_prompts(stdout_r, stdin_w, [b"\x03"], collected)

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=max_steps,
            )
            executor.execute("keep going", "auto")

            os.close(stdout_w)
            stdout_w = -1
            pump.join(timeout=5)

            assert len(fake_llm.auto_step_calls) == 1
            assert b"[auto stopped at step 1]" in collected
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_ctrl_c_interrupt(self) -> None:
        """User Ctrl-C during settle wait interrupts auto mode."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()

        class InterruptingLLMClient(FakeLLMClient):
            def auto_step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> AutoStepResult:
                os.write(stdin_w, b"\x03")
                return _cmd_result(1, "sleep 100")

        fake_llm = InterruptingLLMClient()

        screen = ScreenBuffer()
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=200,
                threshold_ms=30000,
                max_auto_steps=10,
            )
            executor.execute("run forever", "auto")

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"[auto interrupted]" in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_ctrl_c_during_llm_call(self) -> None:
        """Ctrl-C while the LLM call is in flight aborts before any command runs."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        worker_finished = threading.Event()

        class SlowLLMClient(FakeLLMClient):
            def auto_step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> AutoStepResult:
                try:
                    self.auto_step_calls.append((list(input_items), previous_response_id))
                    os.write(stdin_w, b"\x03")
                    time.sleep(0.3)
                    return _cmd_result(1, "ls")
                finally:
                    worker_finished.set()

        fake_llm = SlowLLMClient()
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
            )
            executor.execute("run something", "auto")

            # The tool call was abandoned: no command reached the pty.
            assert b"ls\n" not in pty_written

            os.close(stdout_w)
            stdout_w = -1
            out = _drain(stdout_r)
            assert b"[auto interrupted]" in out
        finally:
            # Let the abandoned daemon thread finish so it cannot outlive
            # this test (pty.fork elsewhere warns in multi-threaded processes).
            worker_finished.wait(timeout=2)
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_ctrl_c_during_summary_generation(self) -> None:
        """Ctrl-C during the pre-query working summary generation aborts auto mode."""
        pty_r, pty_w = os.pipe()
        stdin_r, stdin_w = os.pipe()
        worker_finished = threading.Event()

        class BlockingSummaryLLM:
            def summarize(
                self,
                previous: SummaryResult | None,
                new_output: str,
                truncated: bool = False,
            ) -> SummaryResult:
                try:
                    os.write(stdin_w, b"\x03")
                    time.sleep(0.3)
                    return SummaryResult(environment="e", activities="a", notes="n")
                finally:
                    worker_finished.set()

        screen = ScreenBuffer(rows=3, cols=40)
        for i in range(20):
            screen.feed(f"line {i}\r\n".encode())
        manager = WorkingSummaryManager(
            llm_client=BlockingSummaryLLM(),
            screen_buffer=screen,
            trigger_lines=5,
        )

        fake_llm = FakeLLMClient(auto_results=[_final_result(1, "done", "x")])

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=stdin_r,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                working_summary=manager,
            )
            executor.execute("run something", "auto")

            # The auto step was never reached.
            assert fake_llm.auto_step_calls == []

            os.close(stdout_w)
            stdout_w = -1
            out = _drain(stdout_r)
            assert b"[auto interrupted]" in out
        finally:
            worker_finished.wait(timeout=2)
            for fd in [stdout_r, stdout_w, pty_r, pty_w, stdin_r, stdin_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_thinking_indicator(self) -> None:
        """Auto mode shows the [auto] thinking indicator."""
        auto_results = [_final_result(1, "done", "done")]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        stdout_r, stdout_w = os.pipe()
        pty_r, pty_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                max_auto_steps=5,
            )
            executor.execute("test", "auto")

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"\x1b[90m[auto: thinking...]\x1b[0m" in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_multiline_explanation_uses_crlf(self) -> None:
        """Newlines in the final explanation are written as CRLF (raw mode has no ONLCR)."""
        auto_results = [
            _final_result(1, "done", "Summary:\n- item one\n- item two"),
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        stdout_r, stdout_w = os.pipe()
        pty_r, pty_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
            )
            executor.execute("summarize", "auto")

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert b"Summary:\r\n- item one\r\n- item two" in stdout_output
            end_marker = stdout_output[stdout_output.index(b"[auto done:"):]
            assert b"\r\n" in end_marker
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_tool_output_chaining(self) -> None:
        """Tool results are sent back as function_call_output with previous_response_id."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ]
        fake_llm = FakeLLMClient(
            auto_results=auto_results,
            pty_w=pty_w,
        )
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=10,
            )
            executor.execute("test history", "auto")

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
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_tool_output_excludes_past_output(self) -> None:
        """Tool results contain only output produced by the executed command."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "echo hi"),
            _final_result(2, "done", "done"),
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results, pty_w=pty_w)
        screen = ScreenBuffer()
        screen.feed(b"previous command output\r\nuser@host:~$ ")

        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=10,
            )
            executor.execute("say hi", "auto")

            second_items, _ = fake_llm.auto_step_calls[1]
            output = str(_item_dict(second_items[0])["output"])
            assert "output from step 1" in output
            assert "previous command output" not in output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_command_echo_colored_gray(self) -> None:
        """The echoed command is bracketed in gray; reset follows the first newline."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results, pty_w=pty_w)
        screen = ScreenBuffer()
        screen.feed(b"$ ")

        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
            )
            executor.execute("list files", "auto")

            os.close(stdout_w)
            stdout_w = -1
            out = _drain(stdout_r)
            # Gray starts right after the thinking indicator is cleared...
            assert b"\x1b8\x1b[J\x1b[90m" in out
            # ...and the reset comes right after the first relayed newline.
            assert b"output from step 1\n\x1b[0m" in out
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_echo_color_reset_without_newline(self) -> None:
        """The gray is reset even when no newline arrives before settle."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "ls"),
            _final_result(2, "done", "done"),
        ]

        class NoNewlineLLMClient(FakeLLMClient):
            def auto_step(
                self,
                input_items: ResponseInputParam,
                previous_response_id: str | None = None,
                working_summary: str | None = None,
            ) -> AutoStepResult:
                result = self._auto_results[self._auto_call_count]
                self._auto_call_count += 1
                if result.tool_calls:
                    os.write(pty_w, b"partial echo")
                return result

        fake_llm = NoNewlineLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
            )
            executor.execute("list files", "auto")

            os.close(stdout_w)
            stdout_w = -1
            out = _drain(stdout_r)
            assert b"partial echo\x1b[0m" in out
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_invalid_tool_call_returns_error(self) -> None:
        """A tool call with invalid params sends an error output back, no pty write."""
        pty_r, pty_w = os.pipe()

        auto_results = [
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
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        pty_written: list[bytes] = []
        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: pty_written.append(d),
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=10,
                threshold_ms=5000,
                max_auto_steps=10,
            )
            executor.execute("do something", "auto")

            # No command was written to the pty, only the trailing newline.
            assert pty_written == [b"\n", b"\n"]  # initial prompt nudge + final newline

            second_items, _ = fake_llm.auto_step_calls[1]
            second = _item_dict(second_items[0])
            assert second["type"] == "function_call_output"
            assert second["call_id"] == "call_1"
            assert str(second["output"]).startswith("Error:")
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_threshold_note_in_tool_output(self) -> None:
        """When output keeps flowing past the threshold, the tool output carries a note."""
        pty_r, pty_w = os.pipe()

        auto_results = [
            _cmd_result(1, "long-running"),
            _final_result(2, "done", "done"),
        ]
        fake_llm = FakeLLMClient(auto_results=auto_results)
        screen = ScreenBuffer()

        stop_writing = threading.Event()

        def keep_writing() -> None:
            while not stop_writing.is_set():
                try:
                    os.write(pty_w, b"tick\r\n")
                except OSError:
                    break
                time.sleep(0.01)

        writer = threading.Thread(target=keep_writing)
        writer.start()

        stdout_r, stdout_w = os.pipe()

        try:
            executor = ModeExecutor(
                llm_client=fake_llm,
                screen_buffer=screen,
                write_to_pty=lambda d: None,
                stdout_fd=stdout_w,
                stdin_fd=-1,
                get_pty_master_fd=lambda: pty_r,
                settle_ms=500,
                threshold_ms=50,
                max_auto_steps=10,
            )
            executor.execute("run something long", "auto")

            stop_writing.set()
            writer.join()

            second_items, _ = fake_llm.auto_step_calls[1]
            output = str(_item_dict(second_items[0])["output"])
            assert "Terminal Output" in output
            assert "[Note] Output did not settle" in output
        finally:
            stop_writing.set()
            writer.join()
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


def _drain(fd: int) -> bytes:
    output = b""
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        output += chunk
    return output
