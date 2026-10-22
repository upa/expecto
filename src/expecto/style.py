from __future__ import annotations

import re

# The one color in which expecto writes for itself — the query prompt,
# [expecto: ...] messages, the echoed command, and the LLM's answer —
# so it is distinguishable at a glance from what the terminal produced
# (DESIGN.md §5.11). Set once at startup from config.toml's `color`;
# the value is an SGR parameter list ("90", "1;36", "38;5;245", ...).
DEFAULT_COLOR = "90"  # bright black: gray on most terminals

_SGR_PARAMS = re.compile(r"\d+(;\d+)*")
_sgr: str = f"\x1b[{DEFAULT_COLOR}m"


def parse_color(value: str) -> str:
    """Validate an SGR parameter list such as "36" or "1;90".

    Raises ValueError on anything else, so an unparseable value cannot
    reach the terminal as a broken escape sequence."""
    if not _SGR_PARAMS.fullmatch(value):
        raise ValueError(
            f"expected SGR parameters like \"36\" or \"1;90\", got {value!r}"
        )
    return value


def set_color(params: str) -> None:
    global _sgr
    _sgr = f"\x1b[{parse_color(params)}m"


def sgr() -> str:
    """The escape sequence that switches the outer tty to expecto's color."""
    return _sgr
