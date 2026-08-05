from __future__ import annotations

import re

# Deterministic backstop for unmistakably catastrophic commands. This list
# is intentionally minimal: its job is to be a floor under the LLM's own
# risk classification (escalation only), not to enumerate every dangerous
# command. Users extend it via the dangerous patterns file (§ config).
BUILTIN_DANGEROUS_PATTERNS: list[str] = [
    r"\brm\s+(-[a-z]+\s+)+/(\*|\s|$)",        # rm -rf / (root, not scoped paths)
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b[^|;]*\bof=/dev/",
    r":\(\)\s*\{.*\};\s*:",                    # fork bomb
    r"\bwrite\s+erase\b",                      # Cisco IOS
    r"\berase\s+(startup-config|nvram)\b",
    r"\bformat\s+(flash|disk|bootflash|nvram)",
    r"\brequest\s+system\s+zeroize\b",         # Junos
]


class DangerousCommandMatcher:
    """Matches commands against dangerous patterns (builtin + user-defined).

    A match escalates the command's risk to destructive regardless of the
    LLM's own classification; it never de-escalates."""

    def __init__(self, patterns: list[re.Pattern[str]]) -> None:
        self._patterns = patterns

    @classmethod
    def load(cls, user_file: str | None) -> tuple[DangerousCommandMatcher, list[str]]:
        """Build a matcher from the builtin patterns plus the user file
        (one regex per line, '#' comments, case-insensitive). A missing
        file is fine. Returns (matcher, error messages for skipped lines)."""
        patterns = [
            re.compile(p, re.IGNORECASE) for p in BUILTIN_DANGEROUS_PATTERNS
        ]
        errors: list[str] = []

        if user_file is not None:
            try:
                with open(user_file) as f:
                    lines = f.readlines()
            except OSError:
                lines = []
            for lineno, line in enumerate(lines, start=1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                try:
                    patterns.append(re.compile(text, re.IGNORECASE))
                except re.error as exc:
                    errors.append(
                        f"{user_file}:{lineno}: invalid regex {text!r}: {exc}"
                    )

        return cls(patterns), errors

    def match(self, command: str) -> str | None:
        """Return the pattern text that matches the command, or None."""
        for pattern in self._patterns:
            if pattern.search(command):
                return pattern.pattern
        return None
