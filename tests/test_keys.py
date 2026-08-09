from __future__ import annotations

import pytest

from expecto.keys import parse_keys


class TestPlainText:
    def test_ascii_passes_through(self) -> None:
        assert parse_keys("ls -la") == b"ls -la"

    def test_utf8_text(self) -> None:
        assert parse_keys("日本語") == "日本語".encode()

    def test_empty(self) -> None:
        assert parse_keys("") == b""


class TestNamedKeys:
    def test_enter_and_cr_are_carriage_return(self) -> None:
        assert parse_keys("<CR>") == b"\r"
        assert parse_keys("<Enter>") == b"\r"

    def test_esc(self) -> None:
        assert parse_keys("<Esc>") == b"\x1b"

    def test_tab_bs_space(self) -> None:
        assert parse_keys("<Tab><BS><Space>") == b"\t\x7f "

    def test_arrows(self) -> None:
        assert parse_keys("<Up><Down><Right><Left>") == b"\x1b[A\x1b[B\x1b[C\x1b[D"

    def test_navigation(self) -> None:
        assert parse_keys("<Home><End><PgUp><PgDn><Del>") == (
            b"\x1b[H\x1b[F\x1b[5~\x1b[6~\x1b[3~"
        )

    def test_function_keys(self) -> None:
        assert parse_keys("<F1>") == b"\x1bOP"
        assert parse_keys("<F5>") == b"\x1b[15~"
        assert parse_keys("<F12>") == b"\x1b[24~"

    def test_names_are_case_insensitive(self) -> None:
        assert parse_keys("<esc>") == parse_keys("<ESC>") == parse_keys("<Esc>")


class TestControlKeys:
    def test_ctrl_letters(self) -> None:
        assert parse_keys("<C-c>") == b"\x03"
        assert parse_keys("<C-d>") == b"\x04"
        assert parse_keys("<C-z>") == b"\x1a"
        assert parse_keys("<C-a>") == b"\x01"

    def test_ctrl_case_insensitive(self) -> None:
        assert parse_keys("<C-C>") == parse_keys("<c-c>") == b"\x03"

    def test_ctrl_specials(self) -> None:
        assert parse_keys("<C-[>") == b"\x1b"
        assert parse_keys("<C-\\>") == b"\x1c"
        assert parse_keys("<C-]>") == b"\x1d"
        assert parse_keys("<C-@>") == b"\x00"
        assert parse_keys("<C-?>") == b"\x7f"


class TestMixedSpecs:
    def test_editor_style_sequence(self) -> None:
        assert parse_keys("iHello<Esc>:wq<CR>") == b"iHello\x1b:wq\r"

    def test_prompt_answer(self) -> None:
        assert parse_keys("y<CR>") == b"y\r"

    def test_literal_angle_bracket(self) -> None:
        assert parse_keys("a<lt>b") == b"a<b"


class TestErrors:
    def test_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="unknown key <Banana>"):
            parse_keys("<Banana>")

    def test_unclosed_bracket(self) -> None:
        with pytest.raises(ValueError, match="unclosed '<'"):
            parse_keys("abc<esc")

    def test_unknown_ctrl_combo(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            parse_keys("<C-42>")
