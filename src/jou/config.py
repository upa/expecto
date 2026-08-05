from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ReasoningEffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

ConfirmLevel = Literal["destructive", "mutating", "none"]

DEFAULT_DANGEROUS_PATTERNS_FILE = "~/.config/jou/dangerous_patterns.txt"

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
    settle_ms: int
    threshold_ms: int
    max_auto_steps: int
    completion_hotkey: int
    auto_hotkey: int
    raw_io_log_file: str | None
    log_file: str | None
    scrollback_lines: int = 5000
    summary_trigger_lines: int = 500
    summary_max_input_lines: int = 2000
    summary_model: str = ""
    summary_openai_api_key: str = ""
    summary_openai_base_url: str | None = None
    summary_openai_reasoning_effort: ReasoningEffortLevel | None = None
    confirm_level: ConfirmLevel = "destructive"
    dangerous_patterns_file: str = ""

    @classmethod
    def from_env(cls) -> JouConfig:
        raw_effort = os.environ.get("OPENAI_REASONING_EFFORT", "")
        effort = _REASONING_EFFORT_MAP.get(raw_effort)

        # Working summary LLM settings: SUMMARY_OPENAI_* with fallback to OPENAI_*.
        raw_summary_effort = (
            os.environ.get("SUMMARY_OPENAI_REASONING_EFFORT") or raw_effort
        )
        summary_effort = _REASONING_EFFORT_MAP.get(raw_summary_effort)

        return cls(
            shell=os.environ.get("SHELL", "/bin/bash"),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL") or None,
            openai_reasoning_effort=effort,
            history_lines=500,
            settle_ms=500,
            threshold_ms=30000,
            max_auto_steps=15,
            completion_hotkey=0x1F,
            auto_hotkey=0x1D,
            raw_io_log_file=None,
            log_file=None,
            scrollback_lines=5000,
            summary_trigger_lines=500,
            summary_max_input_lines=2000,
            summary_model=(
                os.environ.get("SUMMARY_OPENAI_MODEL")
                or os.environ.get("OPENAI_MODEL", "gpt-4o")
            ),
            summary_openai_api_key=(
                os.environ.get("SUMMARY_OPENAI_API_KEY")
                or os.environ.get("OPENAI_API_KEY", "")
            ),
            summary_openai_base_url=(
                os.environ.get("SUMMARY_OPENAI_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or None
            ),
            summary_openai_reasoning_effort=summary_effort,
            confirm_level="destructive",
            dangerous_patterns_file=os.path.expanduser(DEFAULT_DANGEROUS_PATTERNS_FILE),
        )
