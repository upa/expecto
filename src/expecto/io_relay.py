from __future__ import annotations

import asyncio
import os
import sys
from typing import IO, TYPE_CHECKING

from expecto.settle import SettleResult, wait_settle

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from expecto.input_router import InputRouter
    from expecto.screen_buffer import ScreenBuffer


class IORelay:
    """The single always-on relay pump plus the event hub for executions.

    Runs on the asyncio event loop: stdin and the pty master are watched
    with loop.add_reader, and their callbacks do only synchronous,
    non-blocking work. Executions run as a task started via
    start_execution(); user Ctrl-C while an execution is running cancels
    that task. Everything shares the one event-loop thread, so no locks
    are needed anywhere."""

    def __init__(
        self,
        input_router: InputRouter | None = None,
        screen_buffer: ScreenBuffer | None = None,
        log_file: IO[str] | None = None,
    ) -> None:
        self._input_router = input_router
        self._screen_buffer = screen_buffer
        self._log_file = log_file

        self._pty_fd: int = -1
        self._stdin_fd: int = -1
        self._stdout_fd: int = -1
        self._closed: asyncio.Event = asyncio.Event()
        self._output_seen: asyncio.Event = asyncio.Event()
        self._pty_reader_added: bool = False
        self._stdin_reader_added: bool = False
        self._pty_paused: bool = False
        self._sgr_reset_armed: bool = False
        self._exec_task: asyncio.Task[None] | None = None
        self._key_request: tuple[frozenset[int], asyncio.Future[int]] | None = None

        if input_router is not None:
            input_router.set_executing_input_handler(self._on_executing_input)

    async def run(
        self,
        pty_master_fd: int,
        stdin_fd: int | None = None,
        stdout_fd: int | None = None,
    ) -> None:
        self._pty_fd = pty_master_fd
        self._stdin_fd = stdin_fd if stdin_fd is not None else sys.stdin.fileno()
        self._stdout_fd = stdout_fd if stdout_fd is not None else sys.stdout.fileno()

        loop = asyncio.get_running_loop()
        loop.add_reader(self._stdin_fd, self._on_stdin)
        self._stdin_reader_added = True
        loop.add_reader(self._pty_fd, self._on_pty)
        self._pty_reader_added = True
        try:
            await self._closed.wait()
        finally:
            self._remove_readers()
            if self._exec_task is not None:
                self._exec_task.cancel()
                # Let the execution task's cleanup (finally blocks) run.
                await asyncio.gather(self._exec_task, return_exceptions=True)

    def stop(self) -> None:
        self._close()

    # --- pty output control (used by Executor) ---

    def pause_pty(self) -> None:
        """Stop reading pty output, e.g. while an overlay like
        [thinking...] is shown. Output stays in the kernel buffer
        (backpressure on the child), exactly like the pre-async behavior
        of not selecting on the pty during an LLM call."""
        if self._pty_paused or not self._pty_reader_added:
            return
        asyncio.get_running_loop().remove_reader(self._pty_fd)
        self._pty_reader_added = False
        self._pty_paused = True

    def resume_pty(self) -> None:
        if not self._pty_paused:
            return
        self._pty_paused = False
        if self._closed.is_set():
            return
        asyncio.get_running_loop().add_reader(self._pty_fd, self._on_pty)
        self._pty_reader_added = True

    async def wait_settle(self, settle_ms: int, threshold_ms: int) -> SettleResult:
        """Wait until pty output settles (see expecto.settle.wait_settle)."""
        assert not self._pty_paused, "wait_settle requires pty reading to be active"
        return await wait_settle(self._output_seen, settle_ms, threshold_ms)

    def arm_sgr_reset_at_newline(self) -> None:
        """The caller has switched the outer tty color before injecting a
        command; emit an SGR reset right after the next relayed newline
        (the echoed Enter, ending the echoed command line)."""
        self._sgr_reset_armed = True

    def flush_sgr_reset(self) -> None:
        """Emit the pending SGR reset now (no newline arrived before
        settle / interrupt / EOF). Idempotent."""
        if self._sgr_reset_armed:
            self._sgr_reset_armed = False
            if self._stdout_fd >= 0:
                os.write(self._stdout_fd, b"\x1b[0m")

    # --- input while an execution is running (used by Executor) ---

    async def wait_key(self, accept: frozenset[int]) -> int:
        """Wait for one of the accepted key bytes (y/n prompts). Other
        bytes are discarded. Include 0x03 in accept when Ctrl-C should
        answer the prompt instead of cancelling the execution task."""
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._key_request = (accept, fut)
        try:
            return await fut
        finally:
            self._key_request = None

    def _on_executing_input(self, byte: int) -> None:
        if self._key_request is not None:
            accept, fut = self._key_request
            if byte in accept and not fut.done():
                fut.set_result(byte)
            return
        if byte == 0x03 and self._exec_task is not None:
            self._exec_task.cancel()
        # Other keys typed while an execution is running are discarded.

    # --- execution task management ---

    def start_execution(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        """Run an execution as the current execution task."""
        task = asyncio.get_running_loop().create_task(coro)
        self._exec_task = task
        task.add_done_callback(self._on_execution_done)
        return task

    def _on_execution_done(self, task: asyncio.Task[None]) -> None:
        self._exec_task = None
        self.flush_sgr_reset()
        if self._key_request is not None:
            _, fut = self._key_request
            if not fut.done():
                fut.cancel()
            self._key_request = None
        if self._input_router is not None:
            self._input_router.set_passthrough()
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self._stdout_fd >= 0:
            msg = f"\r\n\x1b[90m[expecto: error: {exc!r}]\x1b[0m\r\n"
            os.write(self._stdout_fd, msg.encode())

    # --- fd callbacks ---

    def _on_stdin(self) -> None:
        try:
            data = os.read(self._stdin_fd, 4096)
        except OSError:
            self._close()
            return
        if not data:
            self._close()
            return
        if self._log_file is not None:
            self._log_file.write(f"stdin: {data!r}\n")
            self._log_file.flush()
        if self._input_router is not None:
            self._input_router.handle(data)
        else:
            os.write(self._pty_fd, data)

    def _on_pty(self) -> None:
        try:
            data = os.read(self._pty_fd, 4096)
        except OSError:
            self._close()
            return
        if not data:
            self._close()
            return
        if self._screen_buffer is not None:
            self._screen_buffer.feed(data)
        self._write_stdout(data)
        self._output_seen.set()

    def _write_stdout(self, data: bytes) -> None:
        if self._sgr_reset_armed:
            newline_at = data.find(b"\n")
            if newline_at >= 0:
                self._sgr_reset_armed = False
                os.write(self._stdout_fd, data[: newline_at + 1])
                os.write(self._stdout_fd, b"\x1b[0m")
                os.write(self._stdout_fd, data[newline_at + 1 :])
                return
        os.write(self._stdout_fd, data)

    # --- shutdown ---

    def _close(self) -> None:
        if self._closed.is_set():
            return
        self._remove_readers()
        self._closed.set()

    def _remove_readers(self) -> None:
        loop = asyncio.get_running_loop()
        if self._stdin_reader_added:
            loop.remove_reader(self._stdin_fd)
            self._stdin_reader_added = False
        if self._pty_reader_added:
            loop.remove_reader(self._pty_fd)
            self._pty_reader_added = False
