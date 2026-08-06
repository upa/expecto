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


class TestGetTextLastN:
    def test_returns_only_tail_lines(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_text(last_n=3)
        lines = text.split("\n")
        assert len(lines) == 3
        assert "line 0" not in text

    def test_no_limit_when_none(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        assert buf.get_text(last_n=None) == buf.get_text()

    def test_limit_larger_than_content(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"only line")
        assert buf.get_text(last_n=100) == "only line"


class TestScrolledLines:
    def test_counter_starts_at_zero(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        assert buf.scrolled_lines_total == 0

    def test_counter_counts_scrolled_lines(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        # 10 newlines on a 3-row screen: rows scrolled out = 10 - (3 - 1).
        assert buf.scrolled_lines_total == 8

    def test_counter_monotonic_past_capacity(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=5)
        for i in range(30):
            buf.feed(f"line {i}\r\n".encode())
        assert buf.scrolled_lines_total == 28

    def test_lines_on_screen_not_counted(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"line1\r\nline2\r\n")
        assert buf.scrolled_lines_total == 0


class TestGetScrolledTail:
    def test_returns_last_n_scrolled_lines(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_scrolled_tail(3)
        lines = text.split("\n")
        assert len(lines) == 3
        assert lines[-1] == "line 7"

    def test_visible_screen_not_included(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_scrolled_tail(100)
        assert "line 9" not in text  # still on the visible screen

    def test_zero_or_negative_returns_empty(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        assert buf.get_scrolled_tail(0) == ""
        assert buf.get_scrolled_tail(-1) == ""

    def test_truncation_note_when_lines_evicted(self) -> None:
        buf = ScreenBuffer(rows=3, cols=40, scrollback_lines=5)
        for i in range(30):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_scrolled_tail(20)
        assert "truncated" in text
        assert "line 27" in text

    def test_does_not_clear_dirty_state(self) -> None:
        """get_scrolled_tail must not interfere with mark()/get_text_since()."""
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(b"$ ")
        mark = buf.mark()
        buf.feed(b"cmd\r\nresult\r\n$ ")
        buf.get_scrolled_tail(10)
        buf.scrolled_lines_total  # noqa: B018 - read-only access
        text = buf.get_text_since(mark)
        assert "result" in text


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
