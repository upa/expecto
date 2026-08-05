from __future__ import annotations

from pathlib import Path

from jou.dangerous import DangerousCommandMatcher


def _builtin_matcher() -> DangerousCommandMatcher:
    matcher, errors = DangerousCommandMatcher.load(None)
    assert errors == []
    return matcher


class TestBuiltinPatterns:
    def test_rm_rf_root_matches(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("rm -rf /") is not None
        assert matcher.match("rm -fr /") is not None
        assert matcher.match("sudo rm -rf /*") is not None

    def test_scoped_rm_does_not_match(self) -> None:
        """Deleting a specific path is left to the LLM's own judgement."""
        matcher = _builtin_matcher()
        assert matcher.match("rm -rf ./build") is None
        assert matcher.match("rm -rf /home/user/tmp") is None

    def test_mkfs_matches(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("mkfs.ext4 /dev/sda1") is not None
        assert matcher.match("mkfs /dev/sdb") is not None

    def test_dd_to_device_matches(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("dd if=/dev/zero of=/dev/sda bs=1M") is not None
        assert matcher.match("dd if=disk.img of=backup.img") is None

    def test_fork_bomb_matches(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match(":(){ :|:& };:") is not None

    def test_vendor_cli_commands_match(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("write erase") is not None
        assert matcher.match("erase startup-config") is not None
        assert matcher.match("format flash:") is not None
        assert matcher.match("request system zeroize") is not None

    def test_case_insensitive(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("WRITE ERASE") is not None

    def test_safe_commands_do_not_match(self) -> None:
        matcher = _builtin_matcher()
        assert matcher.match("ls -la") is None
        assert matcher.match("show version") is None
        assert matcher.match("ip -brief addr") is None
        assert matcher.match("git status") is None

    def test_match_returns_pattern_text(self) -> None:
        matcher = _builtin_matcher()
        matched = matcher.match("write erase")
        assert matched is not None
        assert "write" in matched


class TestUserPatternFile:
    def test_user_patterns_extend_builtins(self, tmp_path: Path) -> None:
        pattern_file = tmp_path / "patterns.txt"
        pattern_file.write_text(
            "# custom dangerous commands\n"
            "\n"
            r"^clear ip bgp \*" + "\n"
            "prod-deploy\\.sh\n"
        )
        matcher, errors = DangerousCommandMatcher.load(str(pattern_file))
        assert errors == []
        assert matcher.match("clear ip bgp *") is not None
        assert matcher.match("./prod-deploy.sh --now") is not None
        # Builtins still apply.
        assert matcher.match("write erase") is not None

    def test_invalid_regex_skipped_with_error(self, tmp_path: Path) -> None:
        pattern_file = tmp_path / "patterns.txt"
        pattern_file.write_text("[invalid(\nvalid-pattern\n")
        matcher, errors = DangerousCommandMatcher.load(str(pattern_file))
        assert len(errors) == 1
        assert "invalid regex" in errors[0]
        assert ":1:" in errors[0]
        # The valid line still works.
        assert matcher.match("run valid-pattern now") is not None

    def test_missing_file_is_fine(self, tmp_path: Path) -> None:
        matcher, errors = DangerousCommandMatcher.load(str(tmp_path / "nope.txt"))
        assert errors == []
        assert matcher.match("write erase") is not None

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        pattern_file = tmp_path / "patterns.txt"
        pattern_file.write_text("# only comments\n\n   \n")
        matcher, errors = DangerousCommandMatcher.load(str(pattern_file))
        assert errors == []
        assert matcher.match("harmless") is None
