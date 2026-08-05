from __future__ import annotations

import threading

import pytest

from jou.background import BackgroundCall


class TestBackgroundCall:
    def test_result(self) -> None:
        call = BackgroundCall(lambda: 42).start()
        assert call.result() == 42
        assert call.done

    def test_none_result(self) -> None:
        call = BackgroundCall(lambda: None).start()
        assert call.result() is None

    def test_exception_reraised_in_result(self) -> None:
        def boom() -> int:
            raise RuntimeError("boom")

        call = BackgroundCall(boom).start()
        assert call.wait(timeout=1)
        with pytest.raises(RuntimeError, match="boom"):
            call.result()

    def test_done_and_wait_track_completion(self) -> None:
        gate = threading.Event()
        call = BackgroundCall(gate.wait).start()
        assert not call.done
        assert call.wait(timeout=0.01) is False
        gate.set()
        assert call.wait(timeout=1) is True
        assert call.done
