from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Literal, cast

from expecto.style import DEFAULT_COLOR, parse_color

ReasoningEffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

ConfirmLevel = Literal["destructive", "mutating", "none"]

MCPServerType = Literal["stdio", "sse", "streamable-http"]

REASONING_EFFORT_LEVELS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

CONFIRM_LEVELS: tuple[str, ...] = ("destructive", "mutating", "none")

MCP_SERVER_TYPES: tuple[str, ...] = ("stdio", "sse", "streamable-http")

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_HOTKEY = 0x1F  # Ctrl-/


DEFAULT_CONFIG_DIR = "~/.expecto"


def config_dir(override: str | None = None) -> str:
    """The expecto config directory: the --config-dir path when given,
    otherwise ~/.expecto."""
    return os.path.expanduser(override or DEFAULT_CONFIG_DIR)


def default_config_file(base: str | None = None) -> str:
    return os.path.join(config_dir(base), "config.toml")


def default_env_file(base: str | None = None) -> str:
    return os.path.join(config_dir(base), ".env")


def default_dangerous_patterns_file(base: str | None = None) -> str:
    return os.path.join(config_dir(base), "dangerous_patterns.txt")


def default_agents_md_file(base: str | None = None) -> str:
    return os.path.join(config_dir(base), "AGENTS.md")


def default_skills_dir(base: str | None = None) -> str:
    return os.path.join(config_dir(base), "skills")


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

    def take_int(self, key: str, minimum: int = 1) -> int | None:
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            expected = (
                "a positive integer"
                if minimum == 1
                else f"an integer >= {minimum}"
            )
            self._warn(
                key, f"expected {expected}, got {value!r}; using the default",
            )
            return None
        return value

    def take_str_list(self, key: str) -> list[str] | None:
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in cast("list[object]", value)
        ):
            self._warn(key, f"expected a list of strings, got {value!r}; ignored")
            return None
        return cast("list[str]", value)

    def take_str_dict(self, key: str) -> dict[str, str] | None:
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if not isinstance(value, dict) or not all(
            isinstance(item, str) for item in cast("dict[str, object]", value).values()
        ):
            self._warn(key, f"expected a table of strings, got {value!r}; ignored")
            return None
        return cast("dict[str, str]", value)

    def take_table(self, key: str) -> dict[str, object] | None:
        """Consume a table-valued key (the only one is [mcp.*])."""
        if key not in self._data:
            return None
        value = self._data.pop(key)
        if not isinstance(value, dict):
            self._warn(key, f"expected a table, got {value!r}; ignored")
            return None
        return cast("dict[str, object]", value)

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


_ENV_REF = re.compile(r"\A\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\Z")


@dataclass
class MCPServerConfig:
    """One MCP server from a [mcp.<name>] table (see design/MCP.md)."""

    name: str
    type: MCPServerType
    command: str = ""                                        # stdio
    args: list[str] = field(default_factory=list)            # stdio
    env: dict[str, str] = field(default_factory=dict)        # stdio
    url: str = ""                                            # sse / streamable-http
    token: str = ""                                          # sse / streamable-http


def _resolve_token(value: str) -> str:
    """Resolve a token that may reference an environment variable.

    A value that is exactly "$NAME" or "${NAME}" is replaced by that
    variable, keeping the secret out of config.toml (Config.md §1).
    Raises KeyError when the variable is unset; any other value is
    returned as written."""
    match = _ENV_REF.match(value)
    if match is None:
        return value
    return os.environ[match.group(1)]


def _parse_mcp_server(
    name: str,
    raw: object,
    path: str,
    warnings: list[str],
) -> MCPServerConfig | None:
    """Build one server config from its table. A server whose entry is
    invalid is warned about and skipped (warn-and-continue): expecto keeps
    starting, just without that server."""
    prefix = f"{path}: mcp.{name}"
    if not isinstance(raw, dict):
        warnings.append(f"{prefix}: expected a table, got {raw!r}; server ignored")
        return None

    table = _Table(cast("dict[str, object]", raw), prefix, warnings)
    type_ = table.take_choice("type", MCP_SERVER_TYPES)
    if type_ is None:
        warnings.append(
            f"{prefix}: type must be one of {', '.join(MCP_SERVER_TYPES)}; "
            "server ignored",
        )
        return None

    server = MCPServerConfig(name=name, type=cast("MCPServerType", type_))
    if type_ == "stdio":
        command = table.take_str("command")
        if command is None:
            warnings.append(f"{prefix}: command is required for stdio; server ignored")
            return None
        server.command = command
        server.args = table.take_str_list("args") or []
        server.env = table.take_str_dict("env") or {}
    else:
        url = table.take_str("url")
        if url is None:
            warnings.append(f"{prefix}: url is required for {type_}; server ignored")
            return None
        server.url = url
        if (token := table.take_str("token")) is not None:
            try:
                server.token = _resolve_token(token)
            except KeyError as exc:
                warnings.append(
                    f"{prefix}: token: environment variable {exc.args[0]} is not set; "
                    "server ignored",
                )
                return None

    table.warn_unknown()
    return server


def _parse_mcp_servers(
    data: dict[str, object], path: str, warnings: list[str],
) -> list[MCPServerConfig]:
    servers: list[MCPServerConfig] = []
    for name, raw in data.items():
        server = _parse_mcp_server(name, raw, path, warnings)
        if server is not None:
            servers.append(server)
    return servers


@dataclass
class ExpectoConfig:
    shell: str = "/bin/bash"
    model: str = DEFAULT_MODEL
    openai_api_key: str = ""
    openai_base_url: str | None = None
    openai_reasoning_effort: ReasoningEffortLevel | None = None
    settle_ms: int = 500
    threshold_ms: int = 30000
    max_steps: int = 15
    hotkey: int = DEFAULT_HOTKEY
    color: str = DEFAULT_COLOR
    raw_io_log_file: str | None = None
    log_file: str | None = None
    scrollback_lines: int = 5000
    compact_thresh: int = 100000
    new_output_max_chars: int = 20000
    confirm_level: ConfirmLevel = "destructive"
    dangerous_patterns_file: str = ""
    agents_md_file: str = ""
    skills_dir: str = ""
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    mcp_connect_timeout_ms: int = 10000

    @classmethod
    def load(
        cls,
        config_file: str | None = None,
        config_dir: str | None = None,
    ) -> tuple[ExpectoConfig, list[str]]:
        """Build the effective config: built-in defaults overridden by the
        TOML config file. The API key comes from the environment
        (OPENAI_API_KEY, the only setting that does); CLI flags are applied
        on top by the caller. Returns (config, warnings).

        config_dir (--config-dir) is the directory the default file paths
        are resolved against. An explicitly given config_file that does not
        exist raises FileNotFoundError; the default path is optional."""
        config = cls(
            shell=os.environ.get("SHELL", "/bin/bash"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            dangerous_patterns_file=default_dangerous_patterns_file(config_dir),
            agents_md_file=default_agents_md_file(config_dir),
            skills_dir=default_skills_dir(config_dir),
        )
        warnings: list[str] = []
        path = (
            config_file if config_file is not None else default_config_file(config_dir)
        )
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
    if (color := table.take_str("color")) is not None:
        try:
            config.color = parse_color(color)
        except ValueError as exc:
            warnings.append(f"{path}: color: {exc}; using the default")
    if (confirm := table.take_choice("confirm_level", CONFIRM_LEVELS)) is not None:
        config.confirm_level = cast("ConfirmLevel", confirm)
    if (v := table.take_int("scrollback_lines")) is not None:
        config.scrollback_lines = v
    if (v := table.take_int("settle_ms")) is not None:
        config.settle_ms = v
    if (v := table.take_int("threshold_ms")) is not None:
        config.threshold_ms = v
    # max_steps = 0 disables the continuation checkpoint (autonomous run).
    if (v := table.take_int("max_steps", minimum=0)) is not None:
        config.max_steps = v
    if (log_file := table.take_str("log_file")) is not None:
        config.log_file = os.path.expanduser(log_file)
    if (raw_log := table.take_str("raw_io_log_file")) is not None:
        config.raw_io_log_file = os.path.expanduser(raw_log)
    if (patterns := table.take_str("dangerous_patterns_file")) is not None:
        config.dangerous_patterns_file = os.path.expanduser(patterns)
    if (agents_md := table.take_str("agents_md_file")) is not None:
        config.agents_md_file = os.path.expanduser(agents_md)
    if (skills := table.take_str("skills_dir")) is not None:
        config.skills_dir = os.path.expanduser(skills)

    if (model := table.take_str("model")) is not None:
        config.model = model
    if (base_url := table.take_str("base_url")) is not None:
        config.openai_base_url = base_url
    if (effort := table.take_choice("reasoning_effort", REASONING_EFFORT_LEVELS)) is not None:
        config.openai_reasoning_effort = cast("ReasoningEffortLevel", effort)
    if (v := table.take_int("compact_thresh")) is not None:
        config.compact_thresh = v
    if (v := table.take_int("new_output_max_chars")) is not None:
        config.new_output_max_chars = v

    if (v := table.take_int("mcp_connect_timeout_ms")) is not None:
        config.mcp_connect_timeout_ms = v
    if (mcp := table.take_table("mcp")) is not None:
        config.mcp_servers = _parse_mcp_servers(mcp, path, warnings)

    table.warn_unknown()
