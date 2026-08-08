from __future__ import annotations

import os
from dataclasses import dataclass

SKILL_FILE = "SKILL.md"
FRONTMATTER_FENCE = "---"


@dataclass(frozen=True)
class Skill:
    """One skill from <skills_dir>/<name>/SKILL.md (design/Skills.md).

    The description is what the model selects on (it is listed in the
    system prompt); the body is what load_skill returns; files are the
    other files bundled in the skill directory, which load_skill can
    serve too."""

    name: str            # the directory name, which is authoritative
    description: str
    path: str            # the skill directory, absolute
    body: str
    files: tuple[str, ...] = ()   # bundled files, relative to path


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
        files=_bundled_files(directory),
    )


def _bundled_files(directory: str) -> tuple[str, ...]:
    """The files sitting next to SKILL.md, as paths relative to the skill
    directory. These are offered to the model so it can pull one in with
    load_skill when the instructions refer to it."""
    found: list[str] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith(".") or (root == directory and name == SKILL_FILE):
                continue
            found.append(os.path.relpath(os.path.join(root, name), directory))
    return tuple(found)


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
    """The text load_skill hands back for a skill itself.

    Any bundled files are advertised at the end, with how to fetch one.
    They are never suggested via run_command: the skill lives on the
    machine expecto runs on, while the terminal may be an ssh session or
    a device CLI where that path does not exist."""
    rendered = f"# Skill: {skill.name}\n\n{skill.body}"
    if skill.files:
        rendered += (
            f"\n\n[Files bundled with this skill: {', '.join(skill.files)}. "
            f'Load one with load_skill(name="{skill.name}", file="<path>").]'
        )
    return rendered


def resolve_skill_file(skill: Skill, relative: str) -> str:
    """The absolute path of a file bundled with the skill.

    Raises ValueError when the path would leave the skill directory."""
    directory = os.path.realpath(skill.path)
    resolved = os.path.realpath(os.path.join(directory, relative))
    if resolved != directory and not resolved.startswith(directory + os.sep):
        raise ValueError(f"{relative!r} is outside the skill directory")
    return resolved


def render_skill_file(skill: Skill, relative: str) -> str:
    """The text load_skill hands back for a bundled file.

    Raises ValueError for a path outside the skill directory and OSError
    when the file cannot be read."""
    with open(resolve_skill_file(skill, relative)) as f:
        return f"# Skill: {skill.name} — {relative}\n\n{f.read().strip()}"
