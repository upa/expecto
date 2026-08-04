from jou.input_router import InputRouter, State


class TestPassthrough:
    def test_normal_bytes_forwarded(self) -> None:
        forwarded: list[bytes] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(b"hello")
        assert b"".join(forwarded) == b"hello"
        assert router.state == State.PASSTHROUGH

    def test_completion_hotkey_enters_query_mode(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        assert router.state == State.QUERY_COMPLETION

    def test_auto_hotkey_enters_query_mode(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1D]))
        assert router.state == State.QUERY_AUTO

    def test_hotkey_not_forwarded(self) -> None:
        forwarded: list[bytes] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(b"ab" + bytes([0x1F]) + b"cd")
        # "ab" forwarded, hotkey consumed, "cd" goes to query buffer
        assert b"".join(forwarded) == b"ab"
        assert router.query_text == "cd"

    def test_custom_hotkeys(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
            completion_hotkey=0x01,
            auto_hotkey=0x02,
        )
        router.handle(bytes([0x01]))
        assert router.state == State.QUERY_COMPLETION
        router._state = State.PASSTHROUGH
        router.handle(bytes([0x02]))
        assert router.state == State.QUERY_AUTO


class TestQueryMode:
    def test_typing_accumulates_query(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"ls -la")
        assert router.query_text == "ls -la"

    def test_backspace_deletes_char(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"abc")
        router.handle(bytes([0x7F]))
        assert router.query_text == "ab"

    def test_backspace_on_empty_buffer(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(bytes([0x7F]))
        assert router.query_text == ""
        assert router.state == State.QUERY_COMPLETION

    def test_enter_submits_completion(self) -> None:
        submitted: list[tuple[str, str]] = []
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: submitted.append((q, m)),
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"df -h")
        router.handle(bytes([0x0D]))
        assert submitted == [("df -h", "completion")]
        assert router.state == State.PASSTHROUGH

    def test_enter_submits_auto(self) -> None:
        submitted: list[tuple[str, str]] = []
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: submitted.append((q, m)),
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1D]))
        router.handle(b"deploy")
        router.handle(bytes([0x0D]))
        assert submitted == [("deploy", "auto")]
        assert router.state == State.PASSTHROUGH

    def test_esc_cancels(self) -> None:
        cancelled: list[bool] = []
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: cancelled.append(True),
        )
        router.handle(bytes([0x1F]))
        router.handle(b"partial")
        router.handle(bytes([0x1B]))
        assert cancelled == [True]
        assert router.state == State.PASSTHROUGH
        assert router.query_text == ""

    def test_ctrl_c_cancels(self) -> None:
        cancelled: list[bool] = []
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: cancelled.append(True),
        )
        router.handle(bytes([0x1D]))
        router.handle(b"partial")
        router.handle(bytes([0x03]))
        assert cancelled == [True]
        assert router.state == State.PASSTHROUGH

    def test_control_chars_ignored_in_query(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"a")
        router.handle(bytes([0x01]))  # Ctrl-A
        router.handle(b"b")
        assert router.query_text == "ab"

    def test_query_not_forwarded_to_pty(self) -> None:
        forwarded: list[bytes] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"query text")
        router.handle(bytes([0x0D]))
        assert forwarded == []


    def test_utf8_multibyte_japanese(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle("あいう".encode("utf-8"))
        assert router.query_text == "あいう"

    def test_utf8_split_across_reads(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        # "あ" is E3 81 82 in UTF-8, split across two reads
        router.handle(bytes([0xE3, 0x81]))
        assert router.query_text == ""
        router.handle(bytes([0x82]))
        assert router.query_text == "あ"

    def test_utf8_mixed_with_ascii(self) -> None:
        submitted: list[tuple[str, str]] = []
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: submitted.append((q, m)),
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle(b"ls ")
        router.handle("日本語".encode("utf-8"))
        router.handle(bytes([0x0D]))
        assert submitted == [("ls 日本語", "completion")]

    def test_utf8_backspace_deletes_whole_char(self) -> None:
        router = InputRouter(
            write_to_pty=lambda d: None,
            on_query_submit=lambda q, m: None,
            on_query_cancel=lambda: None,
        )
        router.handle(bytes([0x1F]))
        router.handle("あい".encode("utf-8"))
        router.handle(bytes([0x7F]))
        assert router.query_text == "あ"


class TestRoundTrip:
    def test_passthrough_query_passthrough(self) -> None:
        forwarded: list[bytes] = []
        submitted: list[tuple[str, str]] = []
        router = InputRouter(
            write_to_pty=forwarded.append,
            on_query_submit=lambda q, m: submitted.append((q, m)),
            on_query_cancel=lambda: None,
        )
        router.handle(b"before")
        router.handle(bytes([0x1F]))
        router.handle(b"query")
        router.handle(bytes([0x0D]))
        router.handle(b"after")

        assert b"".join(forwarded) == b"beforeafter"
        assert submitted == [("query", "completion")]
        assert router.state == State.PASSTHROUGH
