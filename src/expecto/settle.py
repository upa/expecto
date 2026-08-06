from __future__ import annotations

import asyncio
import enum


class SettleResult(enum.Enum):
    SETTLED = "settled"
    THRESHOLD = "threshold"


async def wait_settle(
    output_seen: asyncio.Event,
    settle_ms: int,
    threshold_ms: int,
) -> SettleResult:
    """Wait until terminal output settles.

    output_seen is set by the relay pump on every chunk of pty output;
    this coroutine owns clearing it (single consumer). Returns SETTLED
    once no output arrives for settle_ms. Returns THRESHOLD when output
    keeps flowing past threshold_ms — not an error: the caller reports
    "did not settle" to the LLM and lets it decide what to do."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        output_seen.clear()
        try:
            await asyncio.wait_for(output_seen.wait(), settle_ms / 1000)
        except TimeoutError:
            return SettleResult.SETTLED
        if loop.time() - start >= threshold_ms / 1000:
            return SettleResult.THRESHOLD
