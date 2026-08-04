from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ReasoningEffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_REASONING_EFFORT_MAP: dict[str, ReasoningEffortLevel] = {
    "none": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


@dataclass
class JouConfig:
    shell: str
    model: str
    openai_api_key: str
    openai_base_url: str | None
    openai_reasoning_effort: ReasoningEffortLevel | None
    history_lines: int
    completion_hotkey: int
    auto_hotkey: int
    log_file: str | None

    @classmethod
    def from_env(cls) -> JouConfig:
        raw_effort = os.environ.get("OPENAI_REASONING_EFFORT", "")
        effort = _REASONING_EFFORT_MAP.get(raw_effort)

        return cls(
            shell=os.environ.get("SHELL", "/bin/bash"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL") or None,
            openai_reasoning_effort=effort,
            history_lines=200,
            completion_hotkey=0x1E,
            auto_hotkey=0x1D,
            log_file=None,
        )
