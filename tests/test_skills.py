from __future__ import annotations

from pathlib import Path

from expecto.skills import load_skills, render_skill

SKILL_MD = """\
---
name: bgp
description: Diagnose BGP sessions stuck in Idle.
---

# BGP

1. show bgp summary
"""


def _write_skill(root: Path, name: str, text: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text)
    return directory


class TestLoadSkills:
    def test_reads_frontmatter_and_body(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "bgp", SKILL_MD)
        skills, warnings = load_skills(str(tmp_path))
        assert warnings == []
        (skill,) = skills
        assert skill.name == "bgp"
        assert skill.description == "Diagnose BGP sessions stuck in Idle."
        assert skill.path == str(directory)
        assert skill.body == "# BGP\n\n1. show bgp summary"

    def test_sorted_by_name(self, tmp_path: Path) -> None:
        """A stable order keeps the system prompt byte-identical across
        runs (the prompt cache depends on it)."""
        for name in ("zebra", "alpha", "middle"):
            _write_skill(tmp_path, name, f"---\ndescription: {name} skill\n---\nbody")
        skills, _ = load_skills(str(tmp_path))
        assert [s.name for s in skills] == ["alpha", "middle", "zebra"]

    def test_missing_directory_is_normal(self, tmp_path: Path) -> None:
        skills, warnings = load_skills(str(tmp_path / "nonexistent"))
        assert skills == []
        assert warnings == []

    def test_quoted_values_and_unknown_keys(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "s", """\
---
name: "s"
description: 'Quoted description.'
license: MIT
allowed-tools: run_command
---
body
""")
        skills, warnings = load_skills(str(tmp_path))
        assert warnings == []
        assert skills[0].description == "Quoted description."

    def test_hidden_and_plain_files_are_ignored(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, ".hidden", SKILL_MD)
        (tmp_path / "README.md").write_text("not a skill")
        _write_skill(tmp_path, "bgp", SKILL_MD)
        skills, warnings = load_skills(str(tmp_path))
        assert [s.name for s in skills] == ["bgp"]
        assert warnings == []


class TestSkipsUnusableSkills:
    def test_directory_without_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        skills, warnings = load_skills(str(tmp_path))
        assert skills == []
        assert any("empty" in w for w in warnings)

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "bare", "# Just a document\n")
        skills, warnings = load_skills(str(tmp_path))
        assert skills == []
        assert any("frontmatter" in w for w in warnings)

    def test_unterminated_frontmatter(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "open", "---\ndescription: never closed\n")
        skills, warnings = load_skills(str(tmp_path))
        assert skills == []
        assert any("frontmatter" in w for w in warnings)

    def test_missing_description(self, tmp_path: Path) -> None:
        """Without a description the model cannot tell when to load it."""
        _write_skill(tmp_path, "nameless", "---\nname: nameless\n---\nbody")
        skills, warnings = load_skills(str(tmp_path))
        assert skills == []
        assert any("description" in w for w in warnings)

    def test_one_bad_skill_does_not_hide_the_others(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good", "---\ndescription: a good skill\n---\nbody")
        _write_skill(tmp_path, "bad", "no frontmatter here")
        skills, warnings = load_skills(str(tmp_path))
        assert [s.name for s in skills] == ["good"]
        assert len(warnings) == 1


class TestNameMismatch:
    def test_directory_name_wins_and_warns(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "actual", "---\nname: declared\ndescription: d\n---\nb")
        skills, warnings = load_skills(str(tmp_path))
        assert skills[0].name == "actual"
        assert any("declared" in w and "actual" in w for w in warnings)


class TestRenderSkill:
    def test_names_the_skill_and_its_directory(self, tmp_path: Path) -> None:
        directory = _write_skill(tmp_path, "bgp", SKILL_MD)
        (skill,) = load_skills(str(tmp_path))[0]
        rendered = render_skill(skill)
        assert rendered.startswith("# Skill: bgp")
        assert str(directory) in rendered
        assert "run_command" in rendered  # how to reach the bundled files
        assert rendered.endswith("1. show bgp summary")
