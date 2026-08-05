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

            assert pty_written == [b"\n"]

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

            assert pty_written == [b"\n"]

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

            assert pty_written == [b"ls\n", b"\n"]

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

            assert pty_written[0] == b"\x03"
            assert pty_written[-1] == b"\n"
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_max_steps(self) -> None:
        """Loop stops at max_auto_steps and shows max steps message."""
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

            assert len(pty_written) == max_steps + 1
            assert pty_written[-1] == b"\n"

            os.close(stdout_w)
            stdout_w = -1
            stdout_output = _drain(stdout_r)
            assert f"[auto max steps ({max_steps})]".encode() in stdout_output
        finally:
            for fd in [stdout_r, stdout_w, pty_r, pty_w]:
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
                settle_ms=5000,
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

    def test_thinking_indicator(self) -> None:
        """Auto mode shows step-numbered thinking indicator."""
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
            assert b"[auto 1/5 thinking...]" in stdout_output
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
            assert pty_written == [b"\n"]

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
