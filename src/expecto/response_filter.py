from __future__ import annotations

import enum

# Safety cap for a pending sequence; anything longer is not a plausible
# terminal response and is handed back to normal input handling.
MAX_SEQUENCE_LEN = 1024

# CSI finals that only terminals emit (never keyboards): CPR (R), DSR
# replies (n), DECRPM ($y), XTWINOPS reports (t).
_CSI_RESPONSE_FINALS = frozenset(b"Rnyt")


class FilterResult(enum.Enum):
    PASS = "pass"          # byte is not part of an escape sequence
    PENDING = "pending"    # byte absorbed, sequence not yet decided
    RESPONSE = "response"  # a complete terminal response: forward to pty
    REJECT = "reject"      # not a response: replay bytes as normal input


class _State(enum.Enum):
    IDLE = "idle"
    ESC = "esc"
    CSI = "csi"
    OSC = "osc"
    OSC_ESC = "osc_esc"
    DCS = "dcs"
    DCS_ESC = "dcs_esc"


class ResponseFilter:
    """Recognizes terminal→application response sequences in the stdin
    byte stream.

    While a query or an execution is active, expecto withholds stdin from
    the pty — but the outer terminal's replies to queries issued by the
    child (fish 4.x requests CPR / DA / OSC 11 / kitty keyboard protocol
    state at every prompt and defers input handling until they arrive)
    must still reach the child. This filter picks those replies out of
    the stream so InputRouter can forward them, while ordinary keys keep
    their state-dependent handling.

    Recognized as responses:
    - CSI replies: ``ESC [`` params, final in {R n y t}, or final ``c`` /
      ``u`` with a ``?``/``>`` prefix (DA and kitty replies). Key-input
      CSI sequences (arrows ``ESC[A``, ``ESC[1~``, kitty keypresses
      ``ESC[13u`` without ``?``) are rejected.
    - OSC replies: ``ESC ]`` … BEL or ST — keyboards never produce OSC.
    - DCS replies: ``ESC P`` … ST (XTGETTCAP / XTVERSION replies).

    A lone ESC at the end of a read chunk is treated as the Esc key
    (flush_chunk_end); a longer valid prefix is kept pending so replies
    split across reads are still recognized.
    """

    def __init__(self) -> None:
        self._state: _State = _State.IDLE
        self._buf: bytearray = bytearray()

    def feed(self, byte: int) -> tuple[FilterResult, bytes]:
        """Process one stdin byte. Returns the verdict and, for RESPONSE
        and REJECT, the buffered sequence the verdict applies to."""
        if self._state == _State.IDLE:
            if byte == 0x1B:
                self._buf.append(byte)
                self._state = _State.ESC
                return (FilterResult.PENDING, b"")
            return (FilterResult.PASS, b"")

        if len(self._buf) >= MAX_SEQUENCE_LEN:
            return self._reject(byte)

        if self._state == _State.ESC:
            if byte == 0x5B:  # [
                self._state = _State.CSI
            elif byte == 0x5D:  # ]
                self._state = _State.OSC
            elif byte == 0x50:  # P
                self._state = _State.DCS
            else:
                return self._reject(byte)
            self._buf.append(byte)
            return (FilterResult.PENDING, b"")

        if self._state == _State.CSI:
            if 0x20 <= byte <= 0x3F:  # parameter / intermediate bytes
                self._buf.append(byte)
                return (FilterResult.PENDING, b"")
            if 0x40 <= byte <= 0x7E:  # final byte
                self._buf.append(byte)
                seq = bytes(self._buf)
                self._reset()
                if self._is_csi_response(seq):
                    return (FilterResult.RESPONSE, seq)
                return (FilterResult.REJECT, seq)
            return self._reject(byte)

        if self._state == _State.OSC:
            if byte == 0x07:  # BEL terminator
                self._buf.append(byte)
                seq = bytes(self._buf)
                self._reset()
                return (FilterResult.RESPONSE, seq)
            if byte == 0x1B:
                self._buf.append(byte)
                self._state = _State.OSC_ESC
                return (FilterResult.PENDING, b"")
            if byte >= 0x20:
                self._buf.append(byte)
                return (FilterResult.PENDING, b"")
            return self._reject(byte)

        if self._state == _State.OSC_ESC or self._state == _State.DCS_ESC:
            if byte == 0x5C:  # backslash: ST
                self._buf.append(byte)
                seq = bytes(self._buf)
                self._reset()
                return (FilterResult.RESPONSE, seq)
            return self._reject(byte)

        # _State.DCS: payload may contain anything but ESC (which starts ST)
        if byte == 0x1B:
            self._buf.append(byte)
            self._state = _State.DCS_ESC
            return (FilterResult.PENDING, b"")
        self._buf.append(byte)
        return (FilterResult.PENDING, b"")

    def flush_chunk_end(self) -> bytes:
        """Called at the end of a stdin read chunk. A lone pending ESC is
        the Esc key, not the start of a response — return it for replay.
        A longer prefix stays pending (a reply split across reads)."""
        if self._state == _State.ESC and len(self._buf) == 1:
            self._reset()
            return b"\x1b"
        return b""

    def take_pending(self) -> bytes:
        """Drain the pending buffer (used when returning to passthrough:
        the remainder of a split reply then flows to the pty directly)."""
        seq = bytes(self._buf)
        self._reset()
        return seq

    def _reject(self, byte: int) -> tuple[FilterResult, bytes]:
        self._buf.append(byte)
        seq = bytes(self._buf)
        self._reset()
        return (FilterResult.REJECT, seq)

    def _reset(self) -> None:
        self._state = _State.IDLE
        self._buf.clear()

    @staticmethod
    def _is_csi_response(seq: bytes) -> bool:
        final = seq[-1]
        if final in _CSI_RESPONSE_FINALS:
            return True
        if final == 0x63 and seq[2:3] in (b"?", b">"):  # c: DA1 / DA2 reply
            return True
        if final == 0x75 and seq[2:3] == b"?":  # u: kitty protocol reply
            return True
        return False
