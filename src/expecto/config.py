from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import Literal, cast

ReasoningEffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

ConfirmLevel = Literal["destructive", "mutating", "none"]

REASONING_EFFORT_LEVELS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

CONFIRM_LEVELS: tuple[str, ...] = ("destructive", "mutating", "none")

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_HOTKEY = 0x1F  # Ctrl-/


def config_dir() -> str:
    """The expecto config directory: $XDG_CONFIG_HOME/expecto, falling
    back to ~/.config/expecto."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "expecto")


def default_config_file() -> str:
    return os.path.join(config_dir(), "config.toml")


def default_env_file() -> str:
    return os.path.join(config_dir(), ".env")


def default_dangerous_patterns_file() -> str:
    return os.path.join(config_dir(), "dangerous_patterns.txt")


_HOTKEY_SPECIAL_CODES: dict[str, int] = {
    "@": 0x00,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "/": 0x1F,
}

_RESERVED_HOTKEYS: dict[int, str] = {
    0x03: "Ctrl-C",
    0x0D: "Enter",
    0x1B: "Esc",
}


def parse_hotkey(spec: str) -> int:
    """Parse a "ctrl-<char>" hotkey spec into its control byte.

    Raises ValueError for malformed specs and for keys that collide with
    expecto's own controls (Ctrl-C / Enter / Esc)."""
    text = spec.strip().lower()
    prefix, _, char = text.partition("-")
    if prefix != "ctrl" or len(char) != 1:
        raise ValueError(f'invalid hotkey {spec!r}: expected "ctrl-<char>"')
    if char in _HOTKEY_SPECIAL_CODES:
        code = _HOTKEY_SPECIAL_CODES[char]
    elif "a" <= char <= "z":
        code = ord(char) - 0x60
    else:
        raise ValueError(f"invalid hotkey {spec!r}: unsupported key {char!r}")
    reserved = _RESERVED_HOTKEYS.get(code)
    if reserved is not None:
        raise ValueError(f"invalid hotkey {spec!r}: conflicts with {reserved}")
    return code


class _Table:
    """A TOML table consumed key by key; leftovers are unknown keys.

    Bad values are never fatal: they produce a warning and the built-in
    default stays in effect (same policy as dangerous_patterns.txt)."""

    def __init__(
        self,
        data: dict[str, object],
        path: str,
        warnings: list[str],
    ) -> None:
        self._data = dict(data)
        self._path = path
        self._warnings = warnings

    def _warn(self, key: str, message: str) -> None:
        self._warnings.append(f"{self._path}: {key}: {message}")

    def take_str(self, key: str) -> str | None:
        """Consume a string key. None when absent, empty ("" means unset),
        or not a string (warned)."""
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if not isinstance(value, str):
            self._warn(key, f"expected a string, got {value!r}; using the default")
            return None
        return value or None

    def take_int(self, key: str) -> int | None:
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self._warn(
                key, f"expected a positive integer, got {value!r}; using the default",
            )
            return None
        return value

    def take_choice(self, key: str, choices: tuple[str, ...]) -> str | None:
        value = self.take_str(key)
        if value is None:
            return None
        if value not in choices:
            self._warn(
                key,
                f"must be one of {', '.join(choices)}, got {value!r}; "
                "using the default",
            )
            return None
        return value

    def reject_api_key(self) -> None:
        if "api_key" in self._data:
            self._data.pop("api_key")
            self._warn(
                "api_key",
                "API keys are not read from the config file; "
                "set OPENAI_API_KEY in the environment (ignored)",
            )

    def warn_unknown(self) -> None:
        for key in self._data:
            self._warn(key, "unknown key (ignored)")


@dataclass
class ExpectoConfig:
    shell: str = "/bin/bash"
    model: str = DEFAULT_MODEL
    openai_api_key: str = ""
    openai_base_url: str | None = None
    openai_reasoning_effort: ReasoningEffortLevel | None = None
    history_lines: int = 500
    settle_ms: int = 500
    threshold_ms: int = 30000
    max_steps: int = 15
    hotkey: int = DEFAULT_HOTKEY
    raw_io_log_file: str | None = None
    log_file: str | None = None
    scrollback_lines: int = 5000
    chain_compact_thresh: int = 100000
    new_output_max_chars: int = 20000
    confirm_level: ConfirmLevel = "destructive"
    dangerous_patterns_file: str = ""

    @classmethod
    def load(cls, config_file: str | None = None) -> tuple[ExpectoConfig, list[str]]:
        """Build the effective config: built-in defaults overridden by the
        TOML config file. The API key comes from the environment
        (OPENAI_API_KEY, the only setting that does); CLI flags are applied
        on top by the caller. Returns (config, warnings).

        An explicitly given config_file that does not exist raises
        FileNotFoundError; the default path is optional."""
        config = cls(
            shell=os.environ.get("SHELL", "/bin/bash"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            dangerous_patterns_file=default_dangerous_patterns_file(),
        )
        warnings: list[str] = []
        path = config_file if config_file is not None else default_config_file()
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            if config_file is not None:
                raise
            return config, warnings
        except tomllib.TOMLDecodeError as exc:
            warnings.append(f"{path}: invalid TOML, file ignored: {exc}")
            return config, warnings

        _apply_toml(config, data, path, warnings)
        return config, warnings


def _apply_toml(
    config: ExpectoConfig,
    data: dict[str, object],
    path: str,
    warnings: list[str],
) -> None:
    table = _Table(data, path, warnings)
    table.reject_api_key()

    if (shell := table.take_str("shell")) is not None:
        config.shell = shell
    if (hotkey := table.take_str("hotkey")) is not None:
        try:
            config.hotkey = parse_hotkey(hotkey)
        except ValueError as exc:
            warnings.append(f"{path}: hotkey: {exc}; using the default")
    if (confirm := table.take_choice("confirm_level", CONFIRM_LEVELS)) is not None:
        config.confirm_level = cast("ConfirmLevel", confirm)
    if (v := table.take_int("history_lines")) is not None:
        config.history_lines = v
    if (v := table.take_int("scrollback_lines")) is not None:
        config.scrollback_lines = v
    if (v := table.take_int("settle_ms")) is not None:
        config.settle_ms = v
    if (v := table.take_int("threshold_ms")) is not None:
        config.threshold_ms = v
    if (v := table.take_int("max_steps")) is not None:
        config.max_steps = v
    if (log_file := table.take_str("log_file")) is not None:
        config.log_file = os.path.expanduser(log_file)
    if (raw_log := table.take_str("raw_io_log_file")) is not None:
        config.raw_io_log_file = os.path.expanduser(raw_log)
    if (patterns := table.take_str("dangerous_patterns_file")) is not None:
        config.dangerous_patterns_file = os.path.expanduser(patterns)

    if (model := table.take_str("model")) is not None:
        config.model = model
    if (base_url := table.take_str("base_url")) is not None:
        config.openai_base_url = base_url
    if (effort := table.take_choice("reasoning_effort", REASONING_EFFORT_LEVELS)) is not None:
        config.openai_reasoning_effort = cast("ReasoningEffortLevel", effort)
    if (v := table.take_int("chain_compact_thresh")) is not None:
        config.chain_compact_thresh = v
    if (v := table.take_int("new_output_max_chars")) is not None:
        config.new_output_max_chars = v

    table.warn_unknown()
