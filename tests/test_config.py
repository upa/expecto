import os

from jou.config import JouConfig


class TestJouConfig:
    def test_from_env_defaults(self, monkeypatch: object) -> None:
        import pytest
        mp = pytest.MonkeyPatch()
        mp.delenv("OPENAI_API_KEY", raising=False)
        mp.delenv("OPENAI_MODEL", raising=False)
        mp.delenv("OPENAI_BASE_URL", raising=False)
        mp.delenv("OPENAI_REASONING_EFFORT", raising=False)
        try:
            config = JouConfig.from_env()
            assert config.model == "gpt-4o"
            assert config.openai_api_key == ""
            assert config.openai_base_url is None
            assert config.openai_reasoning_effort is None
            assert config.history_lines == 200
            assert config.completion_hotkey == 0x1F
            assert config.auto_hotkey == 0x1D
            assert config.log_file is None
        finally:
            mp.undo()

    def test_from_env_with_values(self, monkeypatch: object) -> None:
        import pytest
        mp = pytest.MonkeyPatch()
        mp.setenv("OPENAI_API_KEY", "sk-test")
        mp.setenv("OPENAI_MODEL", "gpt-4o-mini")
        mp.setenv("OPENAI_BASE_URL", "https://custom.api/v1")
        mp.setenv("OPENAI_REASONING_EFFORT", "low")
        try:
            config = JouConfig.from_env()
            assert config.openai_api_key == "sk-test"
            assert config.model == "gpt-4o-mini"
            assert config.openai_base_url == "https://custom.api/v1"
            assert config.openai_reasoning_effort == "low"
        finally:
            mp.undo()

    def test_empty_base_url_becomes_none(self, monkeypatch: object) -> None:
        import pytest
        mp = pytest.MonkeyPatch()
        mp.setenv("OPENAI_BASE_URL", "")
        try:
            config = JouConfig.from_env()
            assert config.openai_base_url is None
        finally:
            mp.undo()
