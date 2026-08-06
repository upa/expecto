from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from expecto.llm_client import SummaryResult
    from expecto.screen_buffer import ScreenBuffer


class SummaryGenerator(Protocol):
    async def summarize(
        self,
        previous: SummaryResult | None,
        new_output: str,
        truncated: bool = False,
    ) -> SummaryResult: ...


class WorkingSummaryManager:
    """Maintains a working summary of the session (long-term memory of
    output that has scrolled out of the visible window).

    Runs entirely on the event loop: check_trigger() is called from the
    relay's pty callback and starts a background asyncio.Task;
    ensure_summary() is awaited by Executor before an LLM query.
    pty reading is paused while ensure_summary() runs (the thinking
    overlay is up), so check_trigger() cannot start a concurrent update."""

    def __init__(
        self,
        llm_client: SummaryGenerator,
        screen_buffer: ScreenBuffer,
        trigger_lines: int = 500,
        max_input_lines: int = 2000,
    ) -> None:
        self._llm = llm_client
        self._screen = screen_buffer
        self._trigger_lines = trigger_lines
        self._max_input_lines = max_input_lines
        self._summary: SummaryResult | None = None
        self._last_gen_scrolled: int = 0
        self._inflight: asyncio.Task[None] | None = None

    @property
    def summary_text(self) -> str | None:
        """The current summary formatted for the system prompt, or None."""
        summary = self._summary
        if summary is None:
            return None
        return (
            "## Session Context\n"
            f"Environment: {summary.environment}\n"
            f"Activities so far: {summary.activities}\n"
            f"Notes: {summary.notes}"
        )

    def _pending_lines(self) -> int:
        return self._screen.scrolled_lines_total - self._last_gen_scrolled

    def check_trigger(self) -> None:
        """Start a background update if enough new lines have scrolled out
        since the last generation and no update is in flight."""
        if self._inflight is not None and not self._inflight.done():
            return
        if self._pending_lines() < self._trigger_lines:
            return
        self._inflight = asyncio.get_running_loop().create_task(self._refresh())

    async def ensure_summary(self) -> None:
        """Bring the summary up to date before an LLM query. Waits for any
        in-flight update; generates one inline if there is none yet and
        enough output has accumulated."""
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            # asyncio.wait (unlike a bare await) does not re-raise into us
            # when the update task itself was cancelled at shutdown.
            await asyncio.wait([inflight])
        if self._summary is not None or self._pending_lines() < self._trigger_lines:
            return
        await self._refresh()

    async def _refresh(self) -> None:
        previous = self._summary
        # Read everything from the screen before the first await: the
        # buffer only advances in event-loop callbacks, never mid-await
        # of this coroutine's own step.
        scrolled_now = self._screen.scrolled_lines_total
        pending = scrolled_now - self._last_gen_scrolled
        take = min(pending, self._max_input_lines)
        new_output = self._screen.get_scrolled_tail(take)
        try:
            result = await self._llm.summarize(
                previous,
                new_output,
                truncated=pending > self._max_input_lines,
            )
        except Exception:
            # Never break the session; the old summary is kept and
            # _last_gen_scrolled stays put so the next trigger retries.
            # The LLM client logs the error to the JSONL log.
            return
        self._summary = result
        self._last_gen_scrolled = scrolled_now
