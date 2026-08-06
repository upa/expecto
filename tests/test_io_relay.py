from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass

import pytest

from expecto.input_router import InputRouter, State
from expecto.io_relay import IORelay
from expecto.screen_buffer import ScreenBuffer

HOTKEY = bytes([0x1F])
ENTER = bytes([0x0D])

MakeSession = Callable[..., Coroutine[object, object, "Session"]]


@dataclass
class Session:
    """A relay wired to fake fds: a socketpair stands in for the pty
    master (expecto side / child side), pipes for stdin and stdout."""

    relay: IORelay
    run_task: asyncio.Task[None]
    child_sock: socket.socket
    expecto_sock: socket.socket
    stdin_w: int
    stdout_r: int

    def child_writes(self, data: bytes) -> None:
        self.child_sock.sendall(data)

    def user_types(self, data: bytes) -> None:
        os.write(self.stdin_w, data)

    def screen_output(self) -> bytes:
        out = b""
        while True:
            try:
                chunk = os.read(self.stdout_r, 4096)
            except BlockingIOError:
                return out
            if not chunk:
                return out
            out += chunk


@pytest.fixture
async def make_session() -> AsyncIterator[MakeSession]:
    sessions: list[Session] = []
    fds_to_close: list[int] = []

    async def _make(
        input_router: InputRouter | None = None,
        screen_buffer: ScreenBuffer | None = None,
    ) -> Session:
        expecto_sock, child_sock = socket.socketpair()
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        os.set_blocking(stdout_r, False)
        fds_to_close.extend([stdin_r, stdin_w, stdout_r, stdout_w])

        relay = IORelay(input_router=input_router, screen_buffer=screen_buffer)
        run_task = asyncio.create_task(
            relay.run(expecto_sock.fileno(), stdin_fd=stdin_r, stdout_fd=stdout_w),
        )
        await asyncio.sleep(0)
        session = Session(
            relay=relay,
            run_task=run_task,
            child_sock=child_sock,
            expecto_sock=expecto_sock,
            stdin_w=stdin_w,
            stdout_r=stdout_r,
        )
        sessions.append(session)
        return session

    yield _make

    for session in sessions:
        session.relay.stop()
        await asyncio.gather(session.run_task, return_exceptions=True)
        session.child_sock.close()
        session.expecto_sock.close()
    for fd in fds_to_close:
        try:
            os.close(fd)
        except OSError:
            pass


async def _settle_pump() -> None:
    """Give the event loop a few turns so reader callbacks run."""
    for _ in range(5):
        await asyncio.sleep(0.01)


class TestRelayPump:
    async def test_pty_output_relayed_to_stdout_and_screen(self, make_session: MakeSession) -> None:
        screen = ScreenBuffer()
        session = await make_session(screen_buffer=screen)
        session.child_writes(b"child output")
        await _settle_pump()
        assert b"child output" in session.screen_output()
        assert "child output" in screen.get_text()

    async def test_stdin_routed_through_router(self, make_session: MakeSession) -> None:
        forwarded: list[bytes] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=lambda q: None,
            on_query_cancel=lambda: None,
        )
        session = await make_session(input_router=router)
        session.user_types(b"ls")
        await _settle_pump()
        assert b"".join(forwarded) == b"ls"

    async def test_pty_eof_ends_run(self, make_session: MakeSession) -> None:
        session = await make_session()
        session.child_sock.close()
        await asyncio.wait_for(session.run_task, timeout=2)

    async def test_pause_and_resume_pty(self, make_session: MakeSession) -> None:
        session = await make_session()
        session.relay.pause_pty()
        session.child_writes(b"while paused")
        await _settle_pump()
        assert session.screen_output() == b""
        session.relay.resume_pty()
        await _settle_pump()
        assert b"while paused" in session.screen_output()

    async def test_sgr_reset_inserted_after_first_newline(self, make_session: MakeSession) -> None:
        session = await make_session()
        session.relay.arm_sgr_reset_at_newline()
        session.child_writes(b"echo line\nnext")
        await _settle_pump()
        assert b"echo line\n\x1b[0mnext" in session.screen_output()

    async def test_flush_sgr_reset_when_no_newline(self, make_session: MakeSession) -> None:
        session = await make_session()
        session.relay.arm_sgr_reset_at_newline()
        session.child_writes(b"partial echo")
        await _settle_pump()
        session.relay.flush_sgr_reset()
        out = session.screen_output()
        assert out.endswith(b"partial echo\x1b[0m")

    async def test_wait_settle_settles_after_output_stops(self, make_session: MakeSession) -> None:
        session = await make_session()
        session.child_writes(b"some output")
        result = await session.relay.wait_settle(settle_ms=30, threshold_ms=5000)
        assert result.name == "SETTLED"


def _make_executing_router(
    on_submit: Callable[[str], None],
) -> InputRouter:
    return InputRouter(
        write_to_pty=lambda d: None,
        on_query_submit=on_submit,
        on_query_cancel=lambda: None,
    )


class TestExecutionTask:
    async def test_ctrl_c_cancels_execution_task(self, make_session: MakeSession) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fake_execution() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def on_submit(query: str) -> None:
            session.relay.start_execution(fake_execution())

        router = _make_executing_router(on_submit)
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + b"go" + ENTER)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert router.state == State.EXECUTING

        session.user_types(b"\x03")
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        await _settle_pump()
        assert router.state == State.PASSTHROUGH

    async def test_other_keys_discarded_while_executing(self, make_session: MakeSession) -> None:
        finish = asyncio.Event()

        async def fake_execution() -> None:
            await finish.wait()

        def on_submit(query: str) -> None:
            session.relay.start_execution(fake_execution())

        forwarded: list[bytes] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=on_submit,
            on_query_cancel=lambda: None,
        )
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + ENTER)
        await _settle_pump()
        session.user_types(b"abc")
        await _settle_pump()
        assert forwarded == []  # discarded, not forwarded to the pty
        assert router.state == State.EXECUTING

        finish.set()
        await _settle_pump()
        assert router.state == State.PASSTHROUGH
        session.user_types(b"d")
        await _settle_pump()
        assert forwarded == [b"d"]

    async def test_wait_key_resolves_on_accepted_byte(self, make_session: MakeSession) -> None:
        answer: list[int] = []

        async def fake_execution() -> None:
            key = await session.relay.wait_key(frozenset({0x79, 0x6E}))
            answer.append(key)

        def on_submit(query: str) -> None:
            session.relay.start_execution(fake_execution())

        router = _make_executing_router(on_submit)
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + ENTER)
        await _settle_pump()
        session.user_types(b"x")  # not accepted: discarded
        await _settle_pump()
        assert answer == []
        session.user_types(b"y")
        await _settle_pump()
        assert answer == [0x79]

    async def test_ctrl_c_answers_prompt_instead_of_cancelling(self, make_session: MakeSession) -> None:
        outcome: list[str] = []

        async def fake_execution() -> None:
            try:
                key = await session.relay.wait_key(frozenset({0x79, 0x03}))
                outcome.append("answered" if key == 0x03 else "other")
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        def on_submit(query: str) -> None:
            session.relay.start_execution(fake_execution())

        router = _make_executing_router(on_submit)
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + ENTER)
        await _settle_pump()
        session.user_types(b"\x03")
        await _settle_pump()
        assert outcome == ["answered"]

    async def test_execution_error_reported_and_state_restored(self, make_session: MakeSession) -> None:
        async def failing_execution() -> None:
            raise RuntimeError("boom")

        def on_submit(query: str) -> None:
            session.relay.start_execution(failing_execution())

        router = _make_executing_router(on_submit)
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + ENTER)
        await _settle_pump()
        assert router.state == State.PASSTHROUGH
        out = session.screen_output()
        assert b"[expecto: error:" in out
        assert b"boom" in out

    async def test_run_cancels_execution_task_on_eof(self, make_session: MakeSession) -> None:
        cancelled = asyncio.Event()

        async def fake_execution() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def on_submit(query: str) -> None:
            session.relay.start_execution(fake_execution())

        router = _make_executing_router(on_submit)
        session = await make_session(input_router=router)

        session.user_types(HOTKEY + ENTER)
        await _settle_pump()
        session.child_sock.close()
        await asyncio.wait_for(session.run_task, timeout=2)
        assert cancelled.is_set()
