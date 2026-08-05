from __future__ import annotations

import os
from typing import TYPE_CHECKING

from jou.llm_client import AutoAction, AutoResponse, CompletionResponse
from jou.mode_executor import ModeExecutor
from jou.screen_buffer import ScreenBuffer

if TYPE_CHECKING:
    from openai.types.responses import EasyInputMessageParam


class FakeLLMClient:
    def __init__(
        self,
        response: CompletionResponse | None = None,
        auto_responses: list[AutoResponse] | None = None,
        pty_w: int = -1,
    ) -> None:
        self._response = response or CompletionResponse(command="", explanation="")
        self._auto_responses = auto_responses or []
        self._auto_call_count = 0
        self._pty_w = pty_w
        self.last_screen_text: str = ""
        self.last_query: str = ""
        self.auto_step_calls: list[tuple[list[EasyInputMessageParam], str | None]] = []

    def complete(self, screen_text: str, query: str) -> CompletionResponse:
        self.last_screen_text = screen_text
        self.last_query = query
        return self._response

    def auto_step(
        self,
        messages: list[EasyInputMessageParam],
        previous_response_id: str | None = None,
    ) -> tuple[AutoResponse, str]:
        self.auto_step_calls.append((list(messages), previous_response_id))
        resp = self._auto_responses[self._auto_call_count]
        self._auto_call_count += 1
        if resp.status == "continue" and self._pty_w >= 0:
            os.write(
                self._pty_w,
                f"output from step {self._auto_call_count}\n".encode(),
            )
        response_id = f"resp_{self._auto_call_count}"
        return resp, response_id

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
    ) -> list[EasyInputMessageParam]:
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
            stdout_output = b""
            while True:
                chunk = os.read(stdout_r, 4096)
                if not chunk:
                    break
                stdout_output += chunk

            assert b"thinking..." in stdout_output
            assert b"\x1b8" in stdout_output
        finally:
            if stdout_w >= 0:
                os.close(stdout_w)
            os.close(stdout_r)


class TestAutoMode:
    def test_done_immediately(self) -> None:
        """LLM returns done on first step — no command, just done message."""
        auto_responses = [
            AutoResponse(
                status="done",
                explanation="task complete",
            ),
        ]
        fake_llm = FakeLLMClient(auto_responses=auto_responses)
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
        auto_responses = [
            AutoResponse(
                status="fail",
                explanation="cannot proceed",
            ),
        ]
        fake_llm = FakeLLMClient(auto_responses=auto_responses)
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
        """LLM returns continue then done — settle wait between steps."""
        pty_r, pty_w = os.pipe()

        auto_responses = [
            AutoResponse(
                action=AutoAction(type="command", value="ls"),
                status="continue",
                explanation="listing files",
            ),
            AutoResponse(
                status="done",
                explanation="complete",
            ),
        ]
        fake_llm = FakeLLMClient(
            auto_responses=auto_responses,
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
        """LLM returns control action with continue, then done."""
        pty_r, pty_w = os.pipe()

        auto_responses = [
            AutoResponse(
                action=AutoAction(type="control", value="ctrl-c"),
                status="continue",
                explanation="interrupting the command",
            ),
            AutoResponse(
                status="done",
                explanation="interrupted",
            ),
        ]
        fake_llm = FakeLLMClient(
            auto_responses=auto_responses,
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

        auto_responses = [
            AutoResponse(
                action=AutoAction(type="command", value=f"cmd{i}"),
                status="continue",
                explanation=f"step {i}",
            )
            for i in range(1, max_steps + 1)
        ]
        fake_llm = FakeLLMClient(
            auto_responses=auto_responses,
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

        auto_responses = [
            AutoResponse(
                action=AutoAction(type="command", value="sleep 100"),
                status="continue",
                explanation="waiting",
            ),
        ]

        def fake_auto_step(
            messages: list[EasyInputMessageParam],
            previous_response_id: str | None = None,
        ) -> tuple[AutoResponse, str]:
            os.write(stdin_w, b"\x03")
            return auto_responses[0], "resp_fake"

        fake_llm = FakeLLMClient(auto_responses=auto_responses)
        fake_llm.auto_step = fake_auto_step  # type: ignore[assignment]

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
        auto_responses = [
            AutoResponse(
                status="done",
                explanation="done",
            ),
        ]
        fake_llm = FakeLLMClient(auto_responses=auto_responses)
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

    def test_previous_response_id_chaining(self) -> None:
        """Auto mode chains calls via previous_response_id, sending only new output."""
        pty_r, pty_w = os.pipe()

        auto_responses = [
            AutoResponse(
                action=AutoAction(type="command", value="ls"),
                status="continue",
                explanation="listing",
            ),
            AutoResponse(
                status="done",
                explanation="done",
            ),
        ]

        fake_llm = FakeLLMClient(
            auto_responses=auto_responses,
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

            first_msgs, first_prev_id = fake_llm.auto_step_calls[0]
            assert first_prev_id is None
            assert len(first_msgs) == 1
            assert "goal:" in str(first_msgs[0]["content"])

            second_msgs, second_prev_id = fake_llm.auto_step_calls[1]
            assert second_prev_id == "resp_1"
            assert len(second_msgs) == 1
            assert second_msgs[0]["role"] == "user"
            assert "Command Output" in str(second_msgs[0]["content"])
        finally:
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
