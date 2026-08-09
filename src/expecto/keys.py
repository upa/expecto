from __future__ import annotations

# What each named key sends: the byte sequence a terminal emits for that
# key in normal (non-application) mode. Names are matched
# case-insensitively; <lt> is a literal "<".
_NAMED_KEYS: dict[str, bytes] = {
    "esc": b"\x1b",
    "cr": b"\r",
    "enter": b"\r",
    "tab": b"\t",
    "bs": b"\x7f",
    "space": b" ",
    "lt": b"<",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pgup": b"\x1b[5~",
    "pgdn": b"\x1b[6~",
    "del": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

# <C-x> for the non-letter control characters.
_CTRL_SPECIALS: dict[str, int] = {
    "@": 0x00,
    "[": 0x1B,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "?": 0x7F,
}


def parse_keys(spec: str) -> bytes:
    """Turn a key spec into the bytes a terminal would send.

    Plain characters are typed as-is (UTF-8); angle-bracket tokens name
    special keys: <CR>, <Esc>, <Tab>, <Up>, <F5>, control characters as
    <C-x>, and <lt> for a literal "<". This is the key notation vim
    popularized, but the keys themselves are terminal keys — the spec
    can drive any program reading from the tty.

    Raises ValueError (naming the offending token) for an unknown key or
    an unclosed "<"."""
    out = bytearray()
    index = 0
    while index < len(spec):
        char = spec[index]
        if char != "<":
            out.extend(char.encode())
            index += 1
            continue
        end = spec.find(">", index + 1)
        if end < 0:
            raise ValueError(
                f"unclosed '<' at position {index}; "
                "use <lt> to type a literal '<'",
            )
        token = spec[index + 1:end]
        out.extend(_key_bytes(token))
        index = end + 1
    return bytes(out)


def _key_bytes(token: str) -> bytes:
    lowered = token.lower()
    if lowered in _NAMED_KEYS:
        return _NAMED_KEYS[lowered]
    if lowered.startswith("c-") and len(token) == 3:
        char = lowered[2]
        if "a" <= char <= "z":
            return bytes([ord(char) - 0x60])
        if char in _CTRL_SPECIALS:
            return bytes([_CTRL_SPECIALS[char]])
    raise ValueError(
        f"unknown key <{token}>; known keys: <C-x>, "
        + ", ".join(f"<{name}>" for name in _NAMED_KEYS),
    )
