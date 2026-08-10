from __future__ import annotations

import enum
import re
from collections.abc import Callable

from expecto.response_filter import FilterResult, ResponseFilter

# A program that turns on xterm's modifyOtherKeys (vim sends CSI > 4 ; 2 m
# at startup and CSI > 4 ; m on exit) or the kitty keyboard protocol makes
# the terminal stop sending legacy control bytes: Ctrl-/ arrives as
# "CSI 27 ; 5 ; 47 ~" or "CSI 47 ; 5 u" instead of 0x1F. Both forms are
# recognized here so the hotkey keeps working while such a program runs.
_MODIFIED_KEY = re.compile(
    rb"\x1b\["
    rb"(?:27;(?P<xmod>\d+)(?::\d+)*;(?P<xcode>\d+)~"          # modifyOtherKeys
    rb"|(?P<ucode>\d+)(?::\d+)*;(?P<umod>\d+)(?::\d+)*(?:;[\d:]*)?u)"  # CSI u
)

# Modifier bits, once the encoding's +1 bias is removed:
# 1 = Shift, 2 = Alt, 4 = Ctrl, 8 = Super.
_MOD_SHIFT = 0x01
_MOD_CTRL = 0x04

# Which key a terminal names for the control bytes that are not Ctrl+letter.
# Several keys share one byte (Ctrl-^ and Ctrl-6 both give 0x1E), and the
# legacy encoding conflates them too, so every spelling is accepted.
_CTRL_BASE_CODES: dict[int, tuple[int, ...]] = {
    0x00: (0x40, 0x20),        # @ Space
    0x1B: (0x5B,),             # [
    0x1C: (0x5C,),             # backslash
    0x1D: (0x5D,),             # ]
    0x1E: (0x5E, 0x36),        # ^ 6
    0x1F: (0x5F, 0x2F, 0x37),  # _ / 7
}


def _base_codes(control_byte: int) -> tuple[int, ...]:
    """The key codes a terminal reports for the key that produces this
    legacy control byte when Ctrl is held. modifyOtherKeys names the
    unmodified key, so the hotkey has to be recognized by that key."""
    if 0x01 <= control_byte <= 0x1A:
        lower = control_byte + 0x60
        return (lower, lower - 0x20)  # a-z, and A-Z when Shift is held too
    return _CTRL_BASE_CODES.get(control_byte, ())


def _has_ctrl(modifier: int) -> bool:
    """Whether the reported modifier is Ctrl, possibly with Shift.

    Shift is allowed because which of "/" and "_" needs it depends on the
    keyboard layout; Alt and Super are not, since they make a different
    key that the legacy encoding would not have produced either."""
    bits = modifier - 1
    if bits < 0:
        return False
    return bool(bits & _MOD_CTRL) and not bits & ~(_MOD_CTRL | _MOD_SHIFT)


def rewrite_modified_keys(data: bytes, wanted: frozenset[int]) -> bytes:
    """Turn CSI-encoded Ctrl-key sequences back into their legacy control
    byte, for the bytes in `wanted` only. Everything else — including
    encoded keys expecto does not claim — is returned byte for byte, so
    what reaches the child is unchanged.

    Sequences split across two reads are not recognized: one key press
    arrives as one read, and buffering the tail across chunks would delay
    a bare Esc, which the program on the terminal needs immediately."""
    if not wanted or b"\x1b[" not in data:
        return data

    def replace(match: re.Match[bytes]) -> bytes:
        if match.group("xcode") is not None:
            code, modifier = int(match.group("xcode")), int(match.group("xmod"))
        else:
            code, modifier = int(match.group("ucode")), int(match.group("umod"))
        if _has_ctrl(modifier):
            for control_byte in wanted:
                if code in _base_codes(control_byte):
                    return bytes([control_byte])
        return match.group(0)

    return _MODIFIED_KEY.sub(replace, data)


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
        # Decided once per chunk, from the state the chunk arrives in;
        # one key press is one chunk, so this does not straddle a
        # transition in practice.
        data = rewrite_modified_keys(data, self._claimed_keys())
        for byte in data:
            self._handle_byte(byte)
        if self._state in (State.QUERY, State.EXECUTING):
            # A lone ESC at the chunk end is the Esc key, not the start
            # of a terminal reply.
            for byte in self._response_filter.flush_chunk_end():
                self._dispatch_byte(byte)

    def _claimed_keys(self) -> frozenset[int]:
        """The keys whose CSI-encoded form is decoded back to a control
        byte (see rewrite_modified_keys).

        The hotkey always: expecto swallows it in every state, so the
        child never sees it either way. Ctrl-C only outside passthrough,
        where stdin is withheld from the child anyway — decoding it in
        passthrough would hand the child the legacy byte it asked the
        terminal not to send, throwing away the distinction it turned
        modifyOtherKeys on to get."""
        if self._state == State.PASSTHROUGH:
            return frozenset({self._hotkey})
        return frozenset({self._hotkey, 0x03})

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
