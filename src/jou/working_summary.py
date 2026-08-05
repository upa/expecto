from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol

from jou.background import BackgroundCall

if TYPE_CHECKING:
    from jou.llm_client import SummaryResult
    from jou.screen_buffer import ScreenBuffer


class SummaryGenerator(Protocol):
    def summarize(
        self,
        previous: SummaryResult | None,
        new_output: str,
        truncated: bool = False,
    ) -> SummaryResult: ...


class WorkingSummaryManager:
    """Maintains a working summary of the session (long-term memory of
    output that has scrolled out of the visible window).

    check_trigger() runs on the main thread (IORelay); ensure_summary()
    runs on the main thread or on a ModeExecutor BackgroundCall thread —
    never concurrently with check_trigger, because the relay loop is not
    processing output while a query is being handled. Shared state is
    guarded by _lock; _inflight tracks the background update."""

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
        self._lock = threading.Lock()
        self._summary: SummaryResult | None = None
        self._last_gen_scrolled: int = 0
        self._inflight: BackgroundCall[None] | None = None

    @property
    def summary_text(self) -> str | None:
        """The current summary formatted for the system prompt, or None."""
        with self._lock:
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
        with self._lock:
            last = self._last_gen_scrolled
        return self._screen.scrolled_lines_total - last

    def check_trigger(self) -> None:
        """Start a background update if enough new lines have scrolled out
        since the last generation and no update is in flight."""
        if self._inflight is not None and not self._inflight.done:
            return
        if self._pending_lines() < self._trigger_lines:
            return
        self._inflight = BackgroundCall(self._refresh).start()

    def ensure_summary(self) -> None:
        """Synchronous path called before an LLM query. Waits for any
        in-flight update; generates one inline if there is none yet and
        enough output has accumulated."""
        inflight = self._inflight
        if inflight is not None:
            inflight.wait()
        with self._lock:
            have_summary = self._summary is not None
        if have_summary or self._pending_lines() < self._trigger_lines:
            return
        self._refresh()

    def _refresh(self) -> None:
        with self._lock:
            previous = self._summary
            last = self._last_gen_scrolled
        scrolled_now = self._screen.scrolled_lines_total
        pending = scrolled_now - last
        take = min(pending, self._max_input_lines)
        new_output = self._screen.get_scrolled_tail(take)
        try:
            result = self._llm.summarize(
                previous,
                new_output,
                truncated=pending > self._max_input_lines,
            )
        except Exception:
            # Never break the session; the old summary is kept and
            # _last_gen_scrolled stays put so the next trigger retries.
            # The LLM client logs the error to the JSONL log.
            return
        with self._lock:
            self._summary = result
            self._last_gen_scrolled = scrolled_now
