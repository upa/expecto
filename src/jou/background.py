from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


class BackgroundCall(Generic[T]):
    """A one-shot function call running on a daemon thread.

    The single way jou launches background LLM calls (working summary
    updates, interruptible auto-mode calls): completion is signaled via
    an event, and the result or exception is captured for the caller.
    An abandoned call simply finishes on its own; its result is dropped
    with the object."""

    def __init__(self, fn: Callable[[], T]) -> None:
        self._fn = fn
        self._done = threading.Event()
        self._result: T | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> BackgroundCall[T]:
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in result()
            self._error = exc
        finally:
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for completion. Returns True if the call has finished."""
        return self._done.wait(timeout)

    def result(self) -> T:
        """Wait for completion and return the result, re-raising any
        exception the call raised."""
        self._done.wait()
        if self._error is not None:
            raise self._error
        return cast("T", self._result)
