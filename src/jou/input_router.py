from __future__ import annotations

import enum
from collections.abc import Callable


def _utf8_expected_len(first_byte: int) -> int:
    if first_byte < 0xE0:
        return 2
    if first_byte < 0xF0:
        return 3
    return 4


class State(enum.Enum):
    PASSTHROUGH = "passthrough"
    QUERY_COMPLETION = "query_completion"
    QUERY_AUTO = "query_auto"


class InputRouter:
    def __init__(
        self,
        write_to_pty: Callable[[bytes], None],
        on_query_submit: Callable[[str, str], None],
        on_query_cancel: Callable[[], None],
        on_query_start: Callable[[str], None] | None = None,
        on_query_update: Callable[[str], None] | None = None,
        completion_hotkey: int = 0x1F,
        auto_hotkey: int = 0x1D,
    ) -> None:
        self._write_to_pty = write_to_pty
        self._on_query_submit = on_query_submit
        self._on_query_cancel = on_query_cancel
        self._on_query_start = on_query_start
        self._on_query_update = on_query_update
        self._completion_hotkey = completion_hotkey
        self._auto_hotkey = auto_hotkey
        self._state: State = State.PASSTHROUGH
        self._query_buf: list[str] = []
        self._utf8_pending: bytearray = bytearray()

    @property
    def state(self) -> State:
        return self._state

    @property
    def query_text(self) -> str:
        return "".join(self._query_buf)

    def handle(self, data: bytes) -> None:
        for byte in data:
            self._handle_byte(byte)

    def _handle_byte(self, byte: int) -> None:
        if self._state == State.PASSTHROUGH:
            self._handle_passthrough(byte)
        else:
            self._handle_query(byte)

    def _handle_passthrough(self, byte: int) -> None:
        if byte == self._completion_hotkey:
            self._state = State.QUERY_COMPLETION
            self._query_buf.clear()
            if self._on_query_start is not None:
                self._on_query_start("completion")
        elif byte == self._auto_hotkey:
            self._state = State.QUERY_AUTO
            self._query_buf.clear()
            if self._on_query_start is not None:
                self._on_query_start("auto")
        else:
            self._write_to_pty(bytes([byte]))

    def _handle_query(self, byte: int) -> None:
        if self._utf8_pending:
            if 0x80 <= byte <= 0xBF:
                self._utf8_pending.append(byte)
                expected = _utf8_expected_len(self._utf8_pending[0])
                if len(self._utf8_pending) >= expected:
                    try:
                        char = bytes(self._utf8_pending).decode("utf-8")
                        self._query_buf.append(char)
                        if self._on_query_update is not None:
                            self._on_query_update(self.query_text)
                    except UnicodeDecodeError:
                        pass
                    self._utf8_pending.clear()
            else:
                self._utf8_pending.clear()
                self._handle_query(byte)
            return

        if byte == 0x0D:  # Enter
            mode = "completion" if self._state == State.QUERY_COMPLETION else "auto"
            query = self.query_text
            self._state = State.PASSTHROUGH
            self._on_query_submit(query, mode)
        elif byte in (0x1B, 0x03):  # Esc, Ctrl-C
            self._state = State.PASSTHROUGH
            self._query_buf.clear()
            self._utf8_pending.clear()
            self._on_query_cancel()
        elif byte == 0x7F:  # Backspace
            if self._query_buf:
                self._query_buf.pop()
                if self._on_query_update is not None:
                    self._on_query_update(self.query_text)
        elif byte >= 0xC0:  # Start of multi-byte UTF-8
            self._utf8_pending.append(byte)
        elif byte >= 0x20:  # printable ASCII
            self._query_buf.append(chr(byte))
            if self._on_query_update is not None:
                self._on_query_update(self.query_text)
