import asyncio

from jou.settle import SettleResult, wait_settle


class TestWaitSettle:
    async def test_settles_when_no_output(self) -> None:
        event = asyncio.Event()
        result = await wait_settle(event, settle_ms=10, threshold_ms=1000)
        assert result == SettleResult.SETTLED

    async def test_output_resets_idle_timer(self) -> None:
        event = asyncio.Event()

        async def produce_bursts() -> None:
            for _ in range(3):
                await asyncio.sleep(0.01)
                event.set()

        producer = asyncio.create_task(produce_bursts())
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await wait_settle(event, settle_ms=30, threshold_ms=5000)
        await producer
        assert result == SettleResult.SETTLED
        # Settled only after the last burst (~30ms) plus the idle window,
        # not after the initial 10ms of silence.
        assert loop.time() - start >= 0.05

    async def test_threshold_when_output_keeps_flowing(self) -> None:
        event = asyncio.Event()
        stop = False

        async def keep_producing() -> None:
            while not stop:
                event.set()
                await asyncio.sleep(0.005)

        producer = asyncio.create_task(keep_producing())
        result = await wait_settle(event, settle_ms=100, threshold_ms=30)
        stop = True
        await producer
        assert result == SettleResult.THRESHOLD

    async def test_output_from_before_the_call_does_not_count(self) -> None:
        # The coroutine owns clearing the event: a leftover set() from
        # before the command must not defer settling.
        event = asyncio.Event()
        event.set()
        result = await wait_settle(event, settle_ms=10, threshold_ms=50)
        assert result == SettleResult.SETTLED
