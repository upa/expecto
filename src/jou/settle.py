from __future__ import annotations

import enum
import time


class SettleResult(enum.Enum):
    SETTLED = "settled"
    THRESHOLD = "threshold"


class SettleDetector:
    def __init__(
        self,
        settle_ms: int = 500,
        threshold_ms: int = 30000,
    ) -> None:
        self._settle_s: float = settle_ms / 1000.0
        self._threshold_s: float = threshold_ms / 1000.0
        self._start_time: float = 0.0
        self._last_output_time: float = 0.0
        self._active: bool = False

    def start(self) -> None:
        now = time.monotonic()
        self._start_time = now
        self._last_output_time = now
        self._active = True

    def on_output(self) -> None:
        if self._active:
            self._last_output_time = time.monotonic()

    def check(self) -> SettleResult | None:
        if not self._active:
            return None
        now = time.monotonic()
        if now - self._last_output_time >= self._settle_s:
            self._active = False
            return SettleResult.SETTLED
        if now - self._start_time >= self._threshold_s:
            self._active = False
            return SettleResult.THRESHOLD
        return None

    @property
    def active(self) -> bool:
        return self._active

    def remaining_ms(self) -> int:
        if not self._active:
            return 0
        elapsed_since_output = time.monotonic() - self._last_output_time
        remaining = self._settle_s - elapsed_since_output
        return max(1, int(remaining * 1000))
