import pytest

from jou.config import JouConfig

_ENV_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_REASONING_EFFORT",
    "SUMMARY_OPENAI_API_KEY",
    "SUMMARY_OPENAI_MODEL",
    "SUMMARY_OPENAI_BASE_URL",
    "SUMMARY_OPENAI_REASONING_EFFORT",
]


def _clean_env(mp: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        mp.delenv(var, raising=False)


class TestJouConfig:
    def test_from_env_defaults(self) -> None:
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
        try:
            config = JouConfig.from_env()
            assert config.model == "gpt-4o"
            assert config.openai_api_key == ""
            assert config.openai_base_url is None
            assert config.openai_reasoning_effort is None
            assert config.history_lines == 500
            assert config.settle_ms == 500
            assert config.threshold_ms == 30000
            assert config.max_auto_steps == 15
            assert config.completion_hotkey == 0x1F
            assert config.auto_hotkey == 0x1D
            assert config.raw_io_log_file is None
            assert config.log_file is None
            assert config.scrollback_lines == 5000
            assert config.summary_trigger_lines == 500
            assert config.summary_max_input_lines == 2000
            assert config.summary_model == "gpt-4o"
            assert config.summary_openai_api_key == ""
            assert config.summary_openai_base_url is None
            assert config.summary_openai_reasoning_effort is None
        finally:
            mp.undo()

    def test_from_env_with_values(self) -> None:
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
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

    def test_empty_base_url_becomes_none(self) -> None:
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
        mp.setenv("OPENAI_BASE_URL", "")
        try:
            config = JouConfig.from_env()
            assert config.openai_base_url is None
        finally:
            mp.undo()


class TestSummaryEnvFallback:
    def test_falls_back_to_openai_values(self) -> None:
        """SUMMARY_OPENAI_* unset: summary settings mirror the OPENAI_* values."""
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
        mp.setenv("OPENAI_API_KEY", "sk-main")
        mp.setenv("OPENAI_MODEL", "gpt-4o-mini")
        mp.setenv("OPENAI_BASE_URL", "https://main.api/v1")
        mp.setenv("OPENAI_REASONING_EFFORT", "high")
        try:
            config = JouConfig.from_env()
            assert config.summary_openai_api_key == "sk-main"
            assert config.summary_model == "gpt-4o-mini"
            assert config.summary_openai_base_url == "https://main.api/v1"
            assert config.summary_openai_reasoning_effort == "high"
        finally:
            mp.undo()

    def test_summary_values_take_precedence(self) -> None:
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
        mp.setenv("OPENAI_API_KEY", "sk-main")
        mp.setenv("OPENAI_MODEL", "gpt-4o")
        mp.setenv("OPENAI_BASE_URL", "https://main.api/v1")
        mp.setenv("OPENAI_REASONING_EFFORT", "high")
        mp.setenv("SUMMARY_OPENAI_API_KEY", "sk-summary")
        mp.setenv("SUMMARY_OPENAI_MODEL", "gpt-4o-mini")
        mp.setenv("SUMMARY_OPENAI_BASE_URL", "https://summary.api/v1")
        mp.setenv("SUMMARY_OPENAI_REASONING_EFFORT", "low")
        try:
            config = JouConfig.from_env()
            assert config.summary_openai_api_key == "sk-summary"
            assert config.summary_model == "gpt-4o-mini"
            assert config.summary_openai_base_url == "https://summary.api/v1"
            assert config.summary_openai_reasoning_effort == "low"
            # Main settings are unaffected.
            assert config.openai_api_key == "sk-main"
            assert config.model == "gpt-4o"
            assert config.openai_reasoning_effort == "high"
        finally:
            mp.undo()

    def test_partial_summary_overrides(self) -> None:
        """Only the model is overridden; the rest falls back."""
        mp = pytest.MonkeyPatch()
        _clean_env(mp)
        mp.setenv("OPENAI_API_KEY", "sk-main")
        mp.setenv("SUMMARY_OPENAI_MODEL", "gpt-4o-mini")
        try:
            config = JouConfig.from_env()
            assert config.summary_model == "gpt-4o-mini"
            assert config.summary_openai_api_key == "sk-main"
            assert config.summary_openai_base_url is None
        finally:
            mp.undo()
