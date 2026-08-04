import time

from jou.settle import SettleDetector, SettleResult


class TestSettleDetector:
    def test_not_active_before_start(self) -> None:
        detector = SettleDetector()
        assert not detector.active
        assert detector.check() is None

    def test_active_after_start(self) -> None:
        detector = SettleDetector()
        detector.start()
        assert detector.active

    def test_settle_after_idle(self) -> None:
        detector = SettleDetector(settle_ms=50)
        detector.start()
        assert detector.check() is None
        time.sleep(0.06)
        result = detector.check()
        assert result == SettleResult.SETTLED
        assert not detector.active

    def test_output_resets_idle_timer(self) -> None:
        detector = SettleDetector(settle_ms=50)
        detector.start()
        time.sleep(0.03)
        detector.on_output()
        time.sleep(0.03)
        assert detector.check() is None
        time.sleep(0.03)
        result = detector.check()
        assert result == SettleResult.SETTLED

    def test_threshold_exceeded(self) -> None:
        detector = SettleDetector(settle_ms=500, threshold_ms=50)
        detector.start()
        detector.on_output()
        time.sleep(0.06)
        result = detector.check()
        assert result == SettleResult.THRESHOLD
        assert not detector.active

    def test_settle_before_threshold(self) -> None:
        detector = SettleDetector(settle_ms=30, threshold_ms=200)
        detector.start()
        time.sleep(0.04)
        result = detector.check()
        assert result == SettleResult.SETTLED

    def test_remaining_ms(self) -> None:
        detector = SettleDetector(settle_ms=100)
        detector.start()
        remaining = detector.remaining_ms()
        assert 50 <= remaining <= 100

    def test_remaining_ms_when_inactive(self) -> None:
        detector = SettleDetector()
        assert detector.remaining_ms() == 0

    def test_on_output_when_inactive_is_noop(self) -> None:
        detector = SettleDetector()
        detector.on_output()
        assert not detector.active

    def test_restart_resets_state(self) -> None:
        detector = SettleDetector(settle_ms=50)
        detector.start()
        time.sleep(0.06)
        assert detector.check() == SettleResult.SETTLED
        detector.start()
        assert detector.active
        assert detector.check() is None
