from expecto.response_filter import (
    FilterResult,
    MAX_SEQUENCE_LEN,
    ResponseFilter,
)


def _feed(filt: ResponseFilter, data: bytes) -> list[tuple[FilterResult, bytes]]:
    return [filt.feed(b) for b in data]


def _last(filt: ResponseFilter, data: bytes) -> tuple[FilterResult, bytes]:
    return _feed(filt, data)[-1]


class TestCsiResponses:
    def test_cpr_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[24;80R")
        assert result == FilterResult.RESPONSE
        assert seq == b"\x1b[24;80R"

    def test_decxcpr_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[?24;80;1R")
        assert result == FilterResult.RESPONSE

    def test_da1_reply_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[?62;22c")
        assert result == FilterResult.RESPONSE

    def test_da2_reply_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[>1;10;0c")
        assert result == FilterResult.RESPONSE

    def test_kitty_reply_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[?0u")
        assert result == FilterResult.RESPONSE

    def test_decrpm_reply_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[?2026;2$y")
        assert result == FilterResult.RESPONSE

    def test_xtwinops_report_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[4;600;800t")
        assert result == FilterResult.RESPONSE

    def test_dsr_ok_is_response(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[0n")
        assert result == FilterResult.RESPONSE


class TestKeyInputRejected:
    def test_arrow_key_rejected(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[A")
        assert result == FilterResult.REJECT
        assert seq == b"\x1b[A"

    def test_navigation_key_rejected(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b[1~")
        assert result == FilterResult.REJECT

    def test_kitty_keypress_rejected(self) -> None:
        """kitty-protocol key events lack the '?' prefix of protocol replies."""
        result, seq = _last(ResponseFilter(), b"\x1b[13;2u")
        assert result == FilterResult.REJECT

    def test_bare_da_request_rejected(self) -> None:
        """'ESC[c' (a DA *query*, or a stray key) has no '?'/'>' prefix."""
        result, seq = _last(ResponseFilter(), b"\x1b[c")
        assert result == FilterResult.REJECT

    def test_alt_key_rejected(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1ba")
        assert result == FilterResult.REJECT
        assert seq == b"\x1ba"

    def test_ss3_key_rejected(self) -> None:
        """F1-F4 style SS3 sequences (ESC O x) are not responses."""
        events = _feed(ResponseFilter(), b"\x1bOR")
        assert events[1] == (FilterResult.REJECT, b"\x1bO")
        assert events[2] == (FilterResult.PASS, b"")

    def test_control_byte_inside_csi_rejected(self) -> None:
        """A Ctrl-C typed mid-sequence is replayed, so it still cancels."""
        result, seq = _last(ResponseFilter(), b"\x1b[1;\x03")
        assert result == FilterResult.REJECT
        assert seq == b"\x1b[1;\x03"


class TestOscAndDcs:
    def test_osc_bel_terminated(self) -> None:
        data = b"\x1b]11;rgb:1414/1414/1414\x07"
        result, seq = _last(ResponseFilter(), data)
        assert result == FilterResult.RESPONSE
        assert seq == data

    def test_osc_st_terminated(self) -> None:
        data = b"\x1b]11;rgb:1414/1414/1414\x1b\\"
        result, seq = _last(ResponseFilter(), data)
        assert result == FilterResult.RESPONSE
        assert seq == data

    def test_dcs_st_terminated(self) -> None:
        data = b"\x1bP1+r696e646e=616263\x1b\\"
        result, seq = _last(ResponseFilter(), data)
        assert result == FilterResult.RESPONSE
        assert seq == data

    def test_osc_esc_followed_by_non_st_rejected(self) -> None:
        result, seq = _last(ResponseFilter(), b"\x1b]11;x\x1bA")
        assert result == FilterResult.REJECT


class TestChunkBoundaries:
    def test_plain_bytes_pass(self) -> None:
        filt = ResponseFilter()
        assert filt.feed(0x61) == (FilterResult.PASS, b"")
        assert filt.flush_chunk_end() == b""

    def test_lone_esc_at_chunk_end_is_esc_key(self) -> None:
        filt = ResponseFilter()
        assert filt.feed(0x1B) == (FilterResult.PENDING, b"")
        assert filt.flush_chunk_end() == b"\x1b"
        # The filter is reset afterwards.
        assert filt.feed(0x61) == (FilterResult.PASS, b"")

    def test_split_response_recognized_across_chunks(self) -> None:
        filt = ResponseFilter()
        for b in b"\x1b[?6":
            filt.feed(b)
        assert filt.flush_chunk_end() == b""  # kept pending
        result, seq = filt.feed(ord("2"))
        assert result == FilterResult.PENDING
        result, seq = filt.feed(ord("c"))
        assert result == FilterResult.RESPONSE
        assert seq == b"\x1b[?62c"

    def test_take_pending_drains_buffer(self) -> None:
        filt = ResponseFilter()
        for b in b"\x1b]11;rg":
            filt.feed(b)
        assert filt.take_pending() == b"\x1b]11;rg"
        assert filt.feed(0x61) == (FilterResult.PASS, b"")


class TestOversize:
    def test_oversized_sequence_rejected(self) -> None:
        filt = ResponseFilter()
        results = _feed(filt, b"\x1b]" + b"x" * (MAX_SEQUENCE_LEN + 1))
        rejects = [r for r in results if r[0] == FilterResult.REJECT]
        assert len(rejects) == 1
        assert len(rejects[0][1]) == MAX_SEQUENCE_LEN + 1
        # Bytes after the rejection pass through normally.
        assert results[-1] == (FilterResult.PASS, b"")
