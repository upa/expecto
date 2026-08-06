from __future__ import annotations

import asyncio

from expecto.llm_client import SummaryResult
from expecto.screen_buffer import ScreenBuffer
from expecto.working_summary import WorkingSummaryManager


class FakeSummaryLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[SummaryResult | None, str, bool]] = []
        self.fail: bool = False
        self.block: asyncio.Event | None = None
        self.result: SummaryResult = SummaryResult(
            environment="local shell (bash)",
            activities="listing files",
            notes="no errors",
        )

    async def summarize(
        self,
        previous: SummaryResult | None,
        new_output: str,
        truncated: bool = False,
    ) -> SummaryResult:
        if self.block is not None:
            await asyncio.wait_for(self.block.wait(), timeout=5)
        self.calls.append((previous, new_output, truncated))
        if self.fail:
            raise RuntimeError("summary LLM failed")
        return self.result


def _make_manager(
    trigger_lines: int = 5,
    max_input_lines: int = 100,
) -> tuple[WorkingSummaryManager, FakeSummaryLLM, ScreenBuffer]:
    llm = FakeSummaryLLM()
    screen = ScreenBuffer(rows=3, cols=40, scrollback_lines=50)
    manager = WorkingSummaryManager(
        llm_client=llm,
        screen_buffer=screen,
        trigger_lines=trigger_lines,
        max_input_lines=max_input_lines,
    )
    return manager, llm, screen


def _feed_lines(screen: ScreenBuffer, n: int, prefix: str = "line") -> None:
    for i in range(n):
        screen.feed(f"{prefix} {i}\r\n".encode())


class TestCheckTrigger:
    async def test_below_trigger_does_nothing(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=100)
        _feed_lines(screen, 10)
        manager.check_trigger()
        await manager.ensure_summary()  # would wait for any in-flight update
        assert llm.calls == []
        assert manager.summary_text is None

    async def test_above_trigger_generates_in_background(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20)
        manager.check_trigger()
        await manager.ensure_summary()  # wait for the background update
        assert len(llm.calls) == 1
        text = manager.summary_text
        assert text is not None
        assert "## Session Context" in text
        assert "Environment: local shell (bash)" in text
        assert "Activities so far: listing files" in text
        assert "Notes: no errors" in text

    async def test_first_generation_has_no_previous(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20)
        manager.check_trigger()
        await manager.ensure_summary()
        previous, new_output, truncated = llm.calls[0]
        assert previous is None
        assert "line 0" in new_output
        assert truncated is False

    async def test_incremental_update_passes_previous(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20, prefix="first")
        manager.check_trigger()
        await manager.ensure_summary()
        _feed_lines(screen, 20, prefix="second")
        manager.check_trigger()
        await manager.ensure_summary()
        assert len(llm.calls) == 2
        previous, new_output, _ = llm.calls[1]
        assert previous == llm.result
        assert "second" in new_output
        # Lines already summarized in the first pass are excluded. (Lines
        # that were still visible at that time scroll out later and are
        # legitimately part of the second delta.)
        assert "first 0" not in new_output
        assert "first 17" not in new_output

    async def test_no_retrigger_while_in_flight(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        llm.block = asyncio.Event()
        _feed_lines(screen, 20)
        manager.check_trigger()
        await asyncio.sleep(0)  # let the background task start
        manager.check_trigger()  # in flight: must not start a second update
        llm.block.set()
        await manager.ensure_summary()
        assert len(llm.calls) == 1

    async def test_truncated_when_pending_exceeds_max_input(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5, max_input_lines=10)
        _feed_lines(screen, 30)
        manager.check_trigger()
        await manager.ensure_summary()
        _, new_output, truncated = llm.calls[0]
        assert truncated is True
        assert len(new_output.split("\n")) <= 10


class TestEnsureSummary:
    async def test_generates_inline_when_missing(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20)
        await manager.ensure_summary()
        assert len(llm.calls) == 1
        assert manager.summary_text is not None

    async def test_skips_when_below_trigger(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=100)
        _feed_lines(screen, 10)
        await manager.ensure_summary()
        assert llm.calls == []

    async def test_waits_for_in_flight_update(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        llm.block = asyncio.Event()
        _feed_lines(screen, 20)
        manager.check_trigger()
        asyncio.get_running_loop().call_later(0.02, llm.block.set)
        await manager.ensure_summary()  # must wait for the background update
        assert len(llm.calls) == 1
        assert manager.summary_text is not None

    async def test_does_not_regenerate_when_summary_exists(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20)
        await manager.ensure_summary()
        await manager.ensure_summary()
        assert len(llm.calls) == 1

    async def test_cancellation_leaves_background_update_running(self) -> None:
        """Cancelling the caller of ensure_summary() (mode task Ctrl-C)
        does not cancel the background update task; its result lands."""
        manager, llm, screen = _make_manager(trigger_lines=5)
        llm.block = asyncio.Event()
        _feed_lines(screen, 20)
        manager.check_trigger()
        waiter = asyncio.create_task(manager.ensure_summary())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert waiter.cancelled()

        llm.block.set()
        await manager.ensure_summary()
        assert len(llm.calls) == 1
        assert manager.summary_text is not None


class TestErrorHandling:
    async def test_failure_keeps_old_summary_and_retries(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        _feed_lines(screen, 20)
        manager.check_trigger()
        await manager.ensure_summary()
        assert manager.summary_text is not None

        llm.fail = True
        _feed_lines(screen, 20)
        manager.check_trigger()
        await manager.ensure_summary()
        assert len(llm.calls) == 2
        assert manager.summary_text is not None  # old summary kept

        # _last_gen_scrolled was not advanced: the next trigger retries
        # without needing more output.
        llm.fail = False
        manager.check_trigger()
        await manager.ensure_summary()
        assert len(llm.calls) == 3

    async def test_failure_without_summary_leaves_none(self) -> None:
        manager, llm, screen = _make_manager(trigger_lines=5)
        llm.fail = True
        _feed_lines(screen, 20)
        manager.check_trigger()
        await manager.ensure_summary()
        assert manager.summary_text is None
