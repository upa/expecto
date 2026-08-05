import pytest

from jou.cli import main
from jou.pty_manager import NESTING_ENV_VAR


class TestNestingGuard:
    def test_refuses_to_start_inside_jou_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(NESTING_ENV_VAR, "1")
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "already inside a jou session" in captured.err
