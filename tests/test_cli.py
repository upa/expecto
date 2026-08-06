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

    def test_cwd_env_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        config_dir = home / ".config" / "expecto"
        config_dir.mkdir(parents=True)
        (config_dir / ".env").write_text("EXPECTO_TEST_VAR=from_config\n")
        Path(".env").write_text("EXPECTO_TEST_VAR=from_cwd\n")
        monkeypatch.delenv("EXPECTO_TEST_VAR", raising=False)
        try:
            load_env()
            assert os.environ.get("EXPECTO_TEST_VAR") == "from_cwd"
        finally:
            os.environ.pop("EXPECTO_TEST_VAR", None)

    def test_falls_back_to_config_dir_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        home = self._isolate(monkeypatch, tmp_path)
        config_dir = home / ".config" / "expecto"
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
        self._isolate(monkeypatch, tmp_path)
        Path(".env").write_text("EXPECTO_TEST_VAR=from_file\n")
        monkeypatch.setenv("EXPECTO_TEST_VAR", "from_shell")
        load_env()
        assert os.environ["EXPECTO_TEST_VAR"] == "from_shell"
