import os
from pathlib import Path

import pytest

from expecto.cli import load_env, main
from expecto.pty_manager import NESTING_ENV_VAR


class TestNestingGuard:
    def test_refuses_to_start_inside_expecto_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(NESTING_ENV_VAR, "1")
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "already inside an expecto session" in captured.err


class TestLoadEnv:
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Point cwd and HOME at empty temp dirs so no real .env is found."""
        cwd = tmp_path / "cwd"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(home))
        return home

    def test_explicit_env_file_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        config_dir = home / ".expecto"
        config_dir.mkdir(parents=True)
        (config_dir / ".env").write_text("EXPECTO_TEST_VAR=from_config\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("EXPECTO_TEST_VAR=from_explicit\n")
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        try:
            load_env(str(explicit))
            assert os.environ.get("EXPECTO_TEST_VAR") == "from_explicit"
        finally:
            os.environ.pop("EXPECTO_TEST_VAR", None)

    def test_missing_explicit_env_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        with pytest.raises(FileNotFoundError):
            load_env(str(tmp_path / "nonexistent.env"))

    def test_cwd_env_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Only --env and the config dir are searched; a .env that happens
        to sit in the working directory is not picked up."""
        self._isolate(monkeypatch, tmp_path)
        Path(".env").write_text("EXPECTO_TEST_VAR=from_cwd\n")
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        try:
            load_env()
            assert "EXPECTO_TEST_VAR" not in os.environ
        finally:
            os.environ.pop("EXPECTO_TEST_VAR", None)

    def test_config_dir_override_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        (home / ".expecto").mkdir()
        (home / ".expecto" / ".env").write_text("EXPECTO_TEST_VAR=from_default_dir\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("EXPECTO_TEST_VAR=from_override_dir\n")
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        try:
            load_env(config_dir=str(elsewhere))
            assert os.environ.get("EXPECTO_TEST_VAR") == "from_override_dir"
        finally:
            os.environ.pop("EXPECTO_TEST_VAR", None)

    def test_falls_back_to_config_dir_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        config_dir = home / ".expecto"
        config_dir.mkdir(parents=True)
        (config_dir / ".env").write_text("EXPECTO_TEST_VAR=from_config\n")
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        try:
            load_env()
            assert os.environ.get("EXPECTO_TEST_VAR") == "from_config"
        finally:
            os.environ.pop("EXPECTO_TEST_VAR", None)

    def test_no_env_file_anywhere_is_fine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        load_env()
        assert "EXPECTO_TEST_VAR" not in os.environ

    def test_existing_env_var_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        config_dir = home / ".expecto"
        config_dir.mkdir(parents=True)
        (config_dir / ".env").write_text("EXPECTO_TEST_VAR=from_file\n")
        monkeypatch.setenv("EXPECTO_TEST_VAR", "from_shell")
        load_env()
        assert os.environ["EXPECTO_TEST_VAR"] == "from_shell"


class TestStartupErrors:
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        cwd = tmp_path / "cwd"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv(NESTING_ENV_VAR, raising=False)
        return home

    def test_missing_api_key_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
        assert "OPENAI_API_KEY is not set" in capsys.readouterr().err

    def test_missing_explicit_config_file_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        missing = tmp_path / "nonexistent.toml"
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", str(missing)])
        assert exc_info.value.code == 1
        assert "config file not found" in capsys.readouterr().err

    def test_missing_explicit_env_file_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        missing = tmp_path / "nonexistent.env"
        with pytest.raises(SystemExit) as exc_info:
            main(["--env", str(missing)])
        assert exc_info.value.code == 1
        assert "env file not found" in capsys.readouterr().err

    def test_missing_config_dir_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["--config-dir", str(tmp_path / "nonexistent")])
        assert exc_info.value.code == 1
        assert "config directory not found" in capsys.readouterr().err

    def test_missing_explicit_agents_md_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._isolate(monkeypatch, tmp_path)
        # The API key is checked first; give it one so the run reaches the
        # AGENTS.md path check.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        missing = tmp_path / "nonexistent.md"
        with pytest.raises(SystemExit) as exc_info:
            main(["--agents-md", str(missing)])
        assert exc_info.value.code == 1
        assert "AGENTS.md not found" in capsys.readouterr().err

    def test_config_warnings_are_printed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Warnings from the config file appear on stderr (the run itself
        stops at the missing API key)."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text("unknown_key = 1\n")
        with pytest.raises(SystemExit):
            main(["--config", str(config_file)])
        err = capsys.readouterr().err
        assert "expecto: warning:" in err
        assert "unknown_key" in err
