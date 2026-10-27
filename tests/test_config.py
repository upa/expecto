from __future__ import annotations

from pathlib import Path

import pytest

from expecto.config import (
    DEFAULT_HOTKEY,
    DEFAULT_MODEL,
    ExpectoConfig,
    config_dir,
    default_agents_md_file,
    default_config_file,
    default_dangerous_patterns_file,
    default_env_file,
    default_skills_dir,
    parse_hotkey,
)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at an empty temp dir and clear the env vars that
    ExpectoConfig.load() reads."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SHELL", "/bin/testsh")
    return home


def _load_toml(tmp_path: Path, text: str) -> tuple[ExpectoConfig, list[str]]:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return ExpectoConfig.load(str(path))


class TestConfigDir:
    def test_defaults_to_home_expecto(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert config_dir() == str(tmp_path / ".expecto")
        assert default_config_file() == str(tmp_path / ".expecto" / "config.toml")

    def test_override_is_used_for_every_default_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert config_dir("~/elsewhere") == str(tmp_path / "elsewhere")
        assert default_config_file("/conf") == "/conf/config.toml"
        assert default_env_file("/conf") == "/conf/.env"
        assert default_agents_md_file("/conf") == "/conf/AGENTS.md"
        assert default_skills_dir("/conf") == "/conf/skills"
        assert default_dangerous_patterns_file("/conf") == "/conf/dangerous_patterns.txt"

    def test_config_dir_feeds_the_loaded_defaults(self, tmp_path: Path) -> None:
        config, warnings = ExpectoConfig.load(config_dir=str(tmp_path))
        assert warnings == []
        assert config.agents_md_file == str(tmp_path / "AGENTS.md")
        assert config.dangerous_patterns_file == str(tmp_path / "dangerous_patterns.txt")

    def test_config_dir_holds_the_default_config_file(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('model = "dir-model"\n')
        config, warnings = ExpectoConfig.load(config_dir=str(tmp_path))
        assert warnings == []
        assert config.model == "dir-model"


class TestDefaults:
    def test_no_config_file(self, isolated_home: Path) -> None:
        config, warnings = ExpectoConfig.load()
        assert warnings == []
        assert config.model == DEFAULT_MODEL
        assert config.shell == "/bin/testsh"
        assert config.openai_api_key == ""
        assert config.openai_base_url is None
        assert config.openai_reasoning_effort is None
        assert config.hotkey == DEFAULT_HOTKEY
        assert config.confirm_level == "destructive"
        assert config.scrollback_lines == 5000
        assert config.settle_ms == 500
        assert config.threshold_ms == 30000
        assert config.max_steps == 15
        assert config.log_file is None
        assert config.raw_io_log_file is None
        assert config.dangerous_patterns_file == default_dangerous_patterns_file()
        assert config.agents_md_file == default_agents_md_file()
        assert config.skills_dir == default_skills_dir()
        assert config.compact_thresh == 100000
        assert config.new_output_max_chars == 20000

    def test_api_key_from_env(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config, _ = ExpectoConfig.load()
        assert config.openai_api_key == "sk-test"

    def test_default_config_path_is_read(self, isolated_home: Path) -> None:
        config_path = isolated_home / ".expecto" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text('model = "my-model"\n')
        config, warnings = ExpectoConfig.load()
        assert warnings == []
        assert config.model == "my-model"


class TestConfigFile:
    def test_full_config_applies(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
shell = "/usr/bin/fish"
hotkey = "ctrl-]"
confirm_level = "mutating"
scrollback_lines = 1000
settle_ms = 300
threshold_ms = 10000
max_steps = 5
log_file = "~/expecto.jsonl"
raw_io_log_file = "~/raw.log"
dangerous_patterns_file = "~/patterns.txt"
agents_md_file = "~/AGENTS.md"
skills_dir = "~/skills"
model = "main-model"
base_url = "https://main.api/v1"
reasoning_effort = "high"
compact_thresh = 50000
new_output_max_chars = 5000
""")
        assert warnings == []
        assert config.shell == "/usr/bin/fish"
        assert config.hotkey == 0x1D
        assert config.confirm_level == "mutating"
        assert config.scrollback_lines == 1000
        assert config.settle_ms == 300
        assert config.threshold_ms == 10000
        assert config.max_steps == 5
        assert config.log_file == str(isolated_home / "expecto.jsonl")
        assert config.raw_io_log_file == str(isolated_home / "raw.log")
        assert config.dangerous_patterns_file == str(isolated_home / "patterns.txt")
        assert config.agents_md_file == str(isolated_home / "AGENTS.md")
        assert config.skills_dir == str(isolated_home / "skills")
        assert config.model == "main-model"
        assert config.openai_base_url == "https://main.api/v1"
        assert config.openai_reasoning_effort == "high"
        assert config.compact_thresh == 50000
        assert config.new_output_max_chars == 5000

    def test_no_mcp_section_means_no_servers(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'model = "m"\n')
        assert warnings == []
        assert config.mcp_servers == []

    def test_empty_string_means_unset(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
log_file = ""
base_url = ""
reasoning_effort = ""
""")
        assert warnings == []
        assert config.log_file is None
        assert config.openai_base_url is None
        assert config.openai_reasoning_effort is None

    def test_unknown_keys_warn(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
scrollbak_lines = 100
modle = "typo"
""")
        assert config.scrollback_lines == 5000
        assert config.model == DEFAULT_MODEL
        assert any("scrollbak_lines" in w and "unknown key" in w for w in warnings)
        assert any("modle" in w and "unknown key" in w for w in warnings)

    def test_wrong_types_warn_and_keep_defaults(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, """
scrollback_lines = "many"
settle_ms = true
max_steps = -1
shell = 3
""")
        assert config.scrollback_lines == 5000
        assert config.settle_ms == 500
        assert config.max_steps == 15
        assert config.shell == "/bin/testsh"
        assert len(warnings) == 4

    def test_max_steps_zero_disables_the_checkpoint(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, "max_steps = 0\n")
        assert warnings == []
        assert config.max_steps == 0

    def test_invalid_confirm_level_warns(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'confirm_level = "detructive"\n')
        assert config.confirm_level == "destructive"
        assert any("confirm_level" in w for w in warnings)

    def test_invalid_reasoning_effort_warns(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'reasoning_effort = "turbo"\n')
        assert config.openai_reasoning_effort is None
        assert any("reasoning_effort" in w for w in warnings)

    def test_api_key_in_file_is_rejected(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'api_key = "sk-in-file"\n')
        assert config.openai_api_key == ""
        assert any("api_key" in w and "OPENAI_API_KEY" in w for w in warnings)

    def test_invalid_toml_warns_and_uses_defaults(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, "shell = not quoted\n")
        assert config.model == DEFAULT_MODEL
        assert config.shell == "/bin/testsh"
        assert any("invalid TOML" in w for w in warnings)

    def test_explicit_missing_file_raises(self, isolated_home: Path, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ExpectoConfig.load(str(tmp_path / "nonexistent.toml"))

    def test_invalid_hotkey_warns(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, 'hotkey = "super-x"\n')
        assert config.hotkey == DEFAULT_HOTKEY
        assert any("hotkey" in w for w in warnings)

    def test_reserved_hotkey_warns(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, 'hotkey = "ctrl-c"\n')
        assert config.hotkey == DEFAULT_HOTKEY
        assert any("Ctrl-C" in w for w in warnings)

    def test_color(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, 'color = "1;36"\n')
        assert config.color == "1;36"
        assert warnings == []

    def test_invalid_color_warns(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, 'color = "cyan"\n')
        assert config.color == "90"
        assert any("color" in w for w in warnings)


    def test_query_color(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, 'query_color = "1;33"\n')
        assert config.query_color == "1;33"
        assert config.color == "90"
        assert warnings == []

    def test_query_color_defaults_to_unset(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'query_color = ""\n')
        assert config.query_color is None
        assert warnings == []

    def test_invalid_query_color_warns(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        config, warnings = _load_toml(tmp_path, 'query_color = "yellow"\n')
        assert config.query_color is None
        assert any("query_color" in w for w in warnings)


class TestMCPServers:
    def test_all_transports(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
[mcp.SERVER1]
type = "stdio"
command = "npx"
args = ["arg1", "arg2"]
env = { FOO = "bar" }

[mcp.SERVER2]
type = "sse"
url = "http://localhost:9000/sse"
token = "TOKEN"

[mcp.SERVER3]
type = "streamable-http"
url = "http://localhost:9001/mcp"
""")
        assert warnings == []
        stdio, sse, http = config.mcp_servers
        assert (stdio.name, stdio.type) == ("SERVER1", "stdio")
        assert stdio.command == "npx"
        assert stdio.args == ["arg1", "arg2"]
        assert stdio.env == {"FOO": "bar"}
        assert (sse.name, sse.type, sse.url) == (
            "SERVER2", "sse", "http://localhost:9000/sse",
        )
        assert sse.token == "TOKEN"
        assert (http.name, http.type) == ("SERVER3", "streamable-http")
        assert http.token == ""

    def test_optional_stdio_keys_default_to_empty(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, """
[mcp.S]
type = "stdio"
command = "server"
""")
        assert warnings == []
        assert config.mcp_servers[0].args == []
        assert config.mcp_servers[0].env == {}

    def test_token_expands_environment_variable(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXPECTO_TEST_TOKEN", "secret-value")
        config, warnings = _load_toml(tmp_path, """
[mcp.A]
type = "sse"
url = "http://localhost:9000/sse"
token = "$EXPECTO_TEST_TOKEN"

[mcp.B]
type = "sse"
url = "http://localhost:9000/sse"
token = "${EXPECTO_TEST_TOKEN}"
""")
        assert warnings == []
        assert [s.token for s in config.mcp_servers] == ["secret-value"] * 2

    def test_unset_token_variable_skips_the_server(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("EXPECTO_TEST_TOKEN", raising=False)
        config, warnings = _load_toml(tmp_path, """
[mcp.A]
type = "sse"
url = "http://localhost:9000/sse"
token = "$EXPECTO_TEST_TOKEN"
""")
        assert config.mcp_servers == []
        assert any("EXPECTO_TEST_TOKEN is not set" in w for w in warnings)

    def test_invalid_server_is_skipped_others_kept(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        config, warnings = _load_toml(tmp_path, """
[mcp.BAD_TYPE]
type = "carrier-pigeon"
url = "http://localhost:9000/sse"

[mcp.NO_TYPE]
command = "server"

[mcp.NO_COMMAND]
type = "stdio"

[mcp.NO_URL]
type = "sse"

[mcp.GOOD]
type = "stdio"
command = "server"
""")
        assert [s.name for s in config.mcp_servers] == ["GOOD"]
        assert any("BAD_TYPE" in w and "type" in w for w in warnings)
        assert any("NO_TYPE" in w and "type" in w for w in warnings)
        assert any("NO_COMMAND" in w and "command is required" in w for w in warnings)
        assert any("NO_URL" in w and "url is required" in w for w in warnings)

    def test_wrong_value_types_warn(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
[mcp.S]
type = "stdio"
command = "server"
args = "not-a-list"
env = { N = 1 }
""")
        assert config.mcp_servers[0].args == []
        assert config.mcp_servers[0].env == {}
        assert any("args" in w for w in warnings)
        assert any("env" in w for w in warnings)

    def test_unknown_server_key_warns(self, isolated_home: Path, tmp_path: Path) -> None:
        config, warnings = _load_toml(tmp_path, """
[mcp.S]
type = "stdio"
command = "server"
prot = "typo"
""")
        assert [s.name for s in config.mcp_servers] == ["S"]
        assert any("mcp.S" in w and "prot" in w and "unknown key" in w for w in warnings)

    def test_mcp_is_not_reported_as_an_unknown_key(
        self, isolated_home: Path, tmp_path: Path,
    ) -> None:
        _, warnings = _load_toml(tmp_path, """
[mcp.S]
type = "stdio"
command = "server"
""")
        assert not any("mcp" in w and "unknown key" in w for w in warnings)


class TestParseHotkey:
    def test_letters(self) -> None:
        assert parse_hotkey("ctrl-a") == 0x01
        assert parse_hotkey("ctrl-z") == 0x1A

    def test_special_characters(self) -> None:
        assert parse_hotkey("ctrl-/") == 0x1F
        assert parse_hotkey("ctrl-]") == 0x1D
        assert parse_hotkey("ctrl-\\") == 0x1C
        assert parse_hotkey("ctrl-^") == 0x1E
        assert parse_hotkey("ctrl-@") == 0x00
        assert parse_hotkey("ctrl-_") == 0x1F

    def test_case_and_whitespace_insensitive(self) -> None:
        assert parse_hotkey(" Ctrl-A ") == 0x01
        assert parse_hotkey("CTRL-/") == 0x1F

    def test_reserved_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="Ctrl-C"):
            parse_hotkey("ctrl-c")
        with pytest.raises(ValueError, match="Enter"):
            parse_hotkey("ctrl-m")

    def test_malformed_specs_rejected(self) -> None:
        for spec in ("", "a", "alt-a", "ctrl-", "ctrl-ab", "ctrl"):
            with pytest.raises(ValueError):
                parse_hotkey(spec)
