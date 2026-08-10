from __future__ import annotations

import os

import pytest

from expecto import fdio
from expecto.fdio import write_all


class _ShortWriter:
    """Stand-in for os.write that accepts at most `limit` bytes per call."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.written = bytearray()
        self.calls = 0

    def __call__(self, fd: int, data: memoryview | bytes) -> int:
        self.calls += 1
        chunk = bytes(data)[: self._limit]
        self.written.extend(chunk)
        return len(chunk)


class TestWriteAll:
    def test_short_writes_are_resumed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writer = _ShortWriter(limit=3)
        monkeypatch.setattr(fdio.os, "write", writer)

        write_all(1, b"0123456789")

        assert bytes(writer.written) == b"0123456789"
        assert writer.calls == 4

    def test_multibyte_split_by_a_short_write_is_still_complete(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A short write can land in the middle of a UTF-8 character; the
        # remainder must still be written.
        writer = _ShortWriter(limit=2)
        monkeypatch.setattr(fdio.os, "write", writer)

        write_all(1, "日本語".encode())

        assert bytes(writer.written).decode() == "日本語"

    def test_empty_data_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writer = _ShortWriter(limit=4)
        monkeypatch.setattr(fdio.os, "write", writer)

        write_all(1, b"")

        assert writer.calls == 0

    def test_no_progress_raises_instead_of_spinning(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(fdio.os, "write", lambda fd, data: 0)

        with pytest.raises(OSError, match="made no progress"):
            write_all(1, b"data")

    def test_writes_to_a_real_fd(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            write_all(write_fd, "こんにちは".encode())
            assert os.read(read_fd, 4096).decode() == "こんにちは"
        finally:
            os.close(read_fd)
            os.close(write_fd)
