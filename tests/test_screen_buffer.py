from expecto.screen_buffer import ScreenBuffer


class TestScreenBuffer:
    def test_feed_and_get_text(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"hello world")
        text = buf.get_text()
        assert "hello world" in text

    def test_multiline_output(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"line1\r\nline2\r\nline3")
        text = buf.get_text()
        assert "line1" in text
        assert "line2" in text
        assert "line3" in text

    def test_ansi_escape_stripped(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"\x1b[31mred text\x1b[0m")
        text = buf.get_text()
        assert "red text" in text
        assert "\x1b" not in text

    def test_trailing_blank_lines_stripped(self) -> None:
        buf = ScreenBuffer(rows=10, cols=80)
        buf.feed(b"only line")
        text = buf.get_text()
        lines = text.split("\n")
        assert lines[-1] != ""

    def test_multibyte_text(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed("こんにちは world".encode())
        assert "こんにちは world" in buf.get_text()

    def test_multibyte_split_across_feeds(self) -> None:
        # pty output arrives in os.read() chunks, so a character can be cut
        # in half at a chunk boundary; decoding spans feeds so it survives.
        buf = ScreenBuffer(rows=24, cols=80)
        data = "あいうえお".encode()
        buf.feed(data[:4])  # cuts the second character in the middle
        buf.feed(data[4:])
        assert "あいうえお" in buf.get_text()
        assert "�" not in buf.get_text()

    def test_invalid_bytes_become_replacement_chars(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"a\xffb")
        assert "a�b" in buf.get_text()

    def test_resize(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.resize(40, 120)
        buf.feed(b"after resize")
        text = buf.get_text()
        assert "after resize" in text

    def test_scrollback_preserved(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_text()
        assert "line 0" in text
        assert "line 9" in text

    def test_empty_buffer(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        text = buf.get_text()
        assert text == ""

    def test_get_cursor_col(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"user@host:~$ ")
        assert buf.get_cursor_col() == 13

    def test_get_cursor_col_after_newline(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"line1\r\nuser@host:~$ ")
        assert buf.get_cursor_col() == 13


class TestGetTextSince:
    def test_only_new_output(self) -> None:
        """Output fed before mark() is excluded; the echo and output after are included."""
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"old command output\r\nuser@host:~$ ")
        mark = buf.mark()
        buf.feed(b"ls\r\nfile1  file2\r\nuser@host:~$ ")
        text = buf.get_text_since(mark)
        assert "old command output" not in text
        assert "user@host:~$ ls" in text
        assert "file1  file2" in text

    def test_no_change_returns_empty(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"some output\r\nuser@host:~$ ")
        mark = buf.mark()
        assert buf.get_text_since(mark) == ""

    def test_scrolling_output_excludes_old_screen(self) -> None:
        """When new output scrolls the screen, pre-mark rows pushed into
        history are not included, while all new output is."""
        buf = ScreenBuffer(rows=5, cols=40, scrollback_lines=100)
        for i in range(4):
            buf.feed(f"old {i}\r\n".encode())
        buf.feed(b"$ ")
        mark = buf.mark()
        buf.feed(b"seq 20\r\n")
        for i in range(20):
            buf.feed(f"new {i}\r\n".encode())
        buf.feed(b"$ ")
        text = buf.get_text_since(mark)
        assert "old 0" not in text
        assert "old 3" not in text
        assert "$ seq 20" in text
        for i in range(20):
            assert f"new {i}" in text

    def test_history_overflow_adds_truncation_note(self) -> None:
        """Output larger than the scrollback capacity carries a truncation note."""
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=5)
        buf.feed(b"$ ")
        mark = buf.mark()
        for i in range(30):
            buf.feed(f"flood {i}\r\n".encode())
        text = buf.get_text_since(mark)
        assert "truncated" in text
        assert "flood 29" in text

    def test_carriage_return_rewrite(self) -> None:
        """A line rewritten in place (progress bar) appears once, final state only."""
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"$ ")
        mark = buf.mark()
        buf.feed(b"download\r\n")
        buf.feed(b"progress 10%\rprogress 50%\rprogress 100%")
        text = buf.get_text_since(mark)
        assert "progress 100%" in text
        assert "progress 10%" not in text
        assert "progress 50%" not in text

    def test_clear_screen_includes_redrawn_content(self) -> None:
        """After a clear-screen redraw, the new content is included."""
        buf = ScreenBuffer(rows=10, cols=40)
        buf.feed(b"old stuff\r\n$ ")
        mark = buf.mark()
        buf.feed(b"\x1b[2J\x1b[Hfresh content")
        text = buf.get_text_since(mark)
        assert "fresh content" in text
        assert "old stuff" not in text

    def test_two_sequential_marks(self) -> None:
        """Each mark captures only its own command's output."""
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"$ ")
        mark1 = buf.mark()
        buf.feed(b"cmd1\r\nresult1\r\n$ ")
        text1 = buf.get_text_since(mark1)
        mark2 = buf.mark()
        buf.feed(b"cmd2\r\nresult2\r\n$ ")
        text2 = buf.get_text_since(mark2)
        assert "result1" in text1
        assert "result2" not in text1
        assert "result2" in text2
        assert "result1" not in text2
