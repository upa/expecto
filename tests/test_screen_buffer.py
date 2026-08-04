from jou.screen_buffer import ScreenBuffer


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
        buf = ScreenBuffer(rows=3, cols=40, history_lines=100)
        for i in range(10):
            buf.feed(f"line {i}\r\n".encode())
        text = buf.get_text()
        assert "line 0" in text
        assert "line 9" in text

    def test_empty_buffer(self) -> None:
        buf = ScreenBuffer(rows=24, cols=80)
        text = buf.get_text()
        assert text == ""
