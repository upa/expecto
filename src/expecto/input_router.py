from __future__ import annotations

import enum
from collections.abc import Callable

from expecto.response_filter import FilterResult, ResponseFilter


def _utf8_expected_len(first_byte: int) -> int:
    if first_byte < 0xE0:
        return 2
    if first_byte < 0xF0:
        return 3
    return 4


class State(enum.Enum):
    PASSTHROUGH = "passthrough"
    QUERY = "query"
    EXECUTING = "executing"


class InputRouter:
    def __init__(
        self,
        write_to_pty: Callable[[bytes], None],
        on_query_submit: Callable[[str], None],
        on_query_cancel: Callable[[], None],
        on_query_start: Callable[[], None] | None = None,
        on_query_update: Callable[[str], None] | None = None,
        hotkey: int = 0x1F,
    ) -> None:
        self._write_to_pty = write_to_pty
        self._on_query_submit = on_query_submit
        self._on_query_cancel = on_query_cancel
        self._on_query_start = on_query_start
        self._on_query_update = on_query_update
        self._hotkey = hotkey
        self._state: State = State.PASSTHROUGH
        self._query_buf: list[str] = []
        self._utf8_pending: bytearray = bytearray()
        self._on_executing_input: Callable[[int], None] | None = None
        self._response_filter: ResponseFilter = ResponseFilter()

    @property
    def state(self) -> State:
        return self._state

    @property
    def query_text(self) -> str:
        return "".join(self._query_buf)

    def set_executing_input_handler(self, handler: Callable[[int], None]) -> None:
        """Set the receiver for bytes typed while an execution is running
        (wired by IORelay: key requests, Ctrl-C cancellation)."""
        self._on_executing_input = handler

    def set_passthrough(self) -> None:
        """Return to passthrough (called when an execution finishes)."""
        self._state = State.PASSTHROUGH
        self._query_buf.clear()
        self._utf8_pending.clear()
        # The remainder of a split terminal reply flows to the pty
        # directly from here on; forward what was already buffered.
        pending = self._response_filter.take_pending()
        if pending:
            self._write_to_pty(pending)

    def handle(self, data: bytes) -> None:
        for byte in data:
            self._handle_byte(byte)
        if self._state in (State.QUERY, State.EXECUTING):
            # A lone ESC at the chunk end is the Esc key, not the start
            # of a terminal reply.
            for byte in self._response_filter.flush_chunk_end():
                self._dispatch_byte(byte)

    def _handle_byte(self, byte: int) -> None:
        if self._state in (State.QUERY, State.EXECUTING):
            # Terminal replies to queries issued by the child (fish 4.x
            # requests CPR / DA / OSC 11 at every prompt and defers input
            # handling until they arrive) must reach the pty even while
            # stdin is otherwise withheld from it.
            result, seq = self._response_filter.feed(byte)
            if result == FilterResult.RESPONSE:
                self._write_to_pty(seq)
                return
            if result == FilterResult.REJECT:
                for buffered in seq:
                    self._dispatch_byte(buffered)
                return
            if result == FilterResult.PENDING:
                return
        self._dispatch_byte(byte)

    def _dispatch_byte(self, byte: int) -> None:
        if self._state == State.PASSTHROUGH:
            self._handle_passthrough(byte)
        elif self._state == State.EXECUTING:
            if self._on_executing_input is not None:
                self._on_executing_input(byte)
        else:
            self._handle_query(byte)

    def _handle_passthrough(self, byte: int) -> None:
        if byte == self._hotkey:
            self._state = State.QUERY
            self._query_buf.clear()
            if self._on_query_start is not None:
                self._on_query_start()
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
            query = self.query_text
            # The submit callback starts the execution task; input stays
            # routed to the executing handler until the task's done
            # callback calls set_passthrough().
            self._state = State.EXECUTING
            self._on_query_submit(query)
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
