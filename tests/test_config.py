from __future__ import annotations

from pathlib import Path

import pytest

from expecto.config import (
    DEFAULT_HOTKEY,
    DEFAULT_MODEL,
    ExpectoConfig,
    config_dir,
    default_config_file,
    default_agents_md_file,
    default_dangerous_patterns_file,
    parse_hotkey,
)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at an empty temp dir and clear the env vars that
    ExpectoConfig.load() reads."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SHELL", "/bin/testsh")
    return home


def _load_toml(tmp_path: Path, text: str) -> tuple[ExpectoConfig, list[str]]:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return ExpectoConfig.load(str(path))


class TestConfigDir:
    def test_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/conf")
        assert config_dir() == "/xdg/conf/expecto"
        assert default_config_file() == "/xdg/conf/expecto/config.toml"

    def test_falls_back_to_home_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert config_dir() == str(tmp_path / ".config" / "expecto")


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
        assert config.compact_thresh == 100000
        assert config.new_output_max_chars == 20000

    def test_api_key_from_env(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        config, _ = ExpectoConfig.load()
        assert config.openai_api_key == "sk-test"

    def test_default_config_path_is_read(self, isolated_home: Path) -> None:
        config_path = isolated_home / ".config" / "expecto" / "config.toml"
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
        assert config.model == "main-model"
        assert config.openai_base_url == "https://main.api/v1"
        assert config.openai_reasoning_effort == "high"
        assert config.compact_thresh == 50000
        assert config.new_output_max_chars == 5000

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
