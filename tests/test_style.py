from __future__ import annotations

from collections.abc import Iterator

import pytest

from expecto import style
from expecto.style import DEFAULT_COLOR, parse_color, set_color, sgr


@pytest.fixture(autouse=True)
def restore_default_color() -> Iterator[None]:
    yield
    set_color(DEFAULT_COLOR)


class TestParseColor:
    @pytest.mark.parametrize("value", ["36", "1;90", "38;5;245", "0"])
    def test_accepts_sgr_parameter_lists(self, value: str) -> None:
        assert parse_color(value) == value

    @pytest.mark.parametrize("value", ["", "cyan", "36m", "\x1b[36m", "1;", ";1", "3 6"])
    def test_rejects_anything_else(self, value: str) -> None:
        with pytest.raises(ValueError):
            parse_color(value)


class TestSetColor:
    def test_default_is_gray(self) -> None:
        assert sgr() == "\x1b[90m"

    def test_set_color_changes_sgr(self) -> None:
        set_color("1;36")
        assert sgr() == "\x1b[1;36m"
        assert style.sgr() == "\x1b[1;36m"  # read at call time, not import time

    def test_invalid_leaves_sgr_untouched(self) -> None:
        with pytest.raises(ValueError):
            set_color("bogus")
        assert sgr() == "\x1b[90m"
