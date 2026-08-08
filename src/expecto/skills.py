from __future__ import annotations

import os
from dataclasses import dataclass

SKILL_FILE = "SKILL.md"
FRONTMATTER_FENCE = "---"


@dataclass(frozen=True)
class Skill:
    """One skill from <skills_dir>/<name>/SKILL.md (design/Skills.md).

    The description is what the model selects on (it is listed in the
    system prompt); the body is what load_skill returns."""

    name: str            # the directory name, which is authoritative
    description: str
    path: str            # the skill directory, absolute
    body: str


def load_skills(skills_dir: str) -> tuple[list[Skill], list[str]]:
    """Load every skill under skills_dir, sorted by name.

    Returns (skills, warnings). A skill that cannot be used — no
    SKILL.md, no frontmatter, no description — is reported and left out;
    a missing skills_dir is normal and yields no skills."""
    warnings: list[str] = []
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError:
        return [], warnings

    skills: list[Skill] = []
    for entry in entries:
        if entry.startswith("."):
            continue
        directory = os.path.join(skills_dir, entry)
        if not os.path.isdir(directory):
            continue
        skill = _load_skill(entry, directory, warnings)
        if skill is not None:
            skills.append(skill)
    return skills, warnings


def _load_skill(name: str, directory: str, warnings: list[str]) -> Skill | None:
    path = os.path.join(directory, SKILL_FILE)
    try:
        with open(path) as f:
            text = f.read()
    except OSError as exc:
        warnings.append(f"skill {name}: {path}: {exc.strerror or exc}; skipped")
        return None

    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        warnings.append(
            f"skill {name}: {path}: no '---' frontmatter block; skipped",
        )
        return None

    description = frontmatter.get("description", "")
    if not description:
        warnings.append(
            f"skill {name}: {path}: frontmatter has no description "
            "(the model selects a skill by its description); skipped",
        )
        return None

    declared = frontmatter.get("name")
    if declared and declared != name:
        warnings.append(
            f"skill {name}: {path}: frontmatter name {declared!r} does not match "
            f"the directory; using {name!r}",
        )

    return Skill(
        name=name,
        description=description,
        path=os.path.abspath(directory),
        body=body.strip(),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """Split "---\\n key: value ... \\n---\\n body" into its two parts.

    Only the flat "key: value" subset of YAML is understood, which is all
    a SKILL.md header uses; keys expecto does not know are ignored.
    Returns (None, text) when there is no frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        return None, text

    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER_FENCE:
            return fields, "\n".join(lines[index + 1:])
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip().strip("\"'")
    # Unterminated block: the whole file was a header.
    return None, text


def render_skill(skill: Skill) -> str:
    """The text load_skill hands back to the model.

    The directory is named so the model can read the files bundled with
    the skill through run_command — expecto drives a terminal, so no
    separate file-reading tool is needed."""
    return (
        f"# Skill: {skill.name}\n"
        f"Directory: {skill.path}\n"
        "(Files bundled with this skill can be read from there with run_command.)\n\n"
        f"{skill.body}"
    )
