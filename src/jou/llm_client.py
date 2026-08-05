from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

import openai
from openai.types.responses import EasyInputMessageParam, ResponseInputParam
from openai.types.shared_params import Reasoning
from pydantic import BaseModel

if TYPE_CHECKING:
    from jou.config import JouConfig

SYSTEM_PROMPT = """\
You are an assistant that suggests commands for the terminal the user is \
operating (which may be a local shell, an SSH session, or a vendor CLI on \
a network device such as a router or switch).
- Examine the recent terminal screen content to determine what system, \
shell, or CLI the user is currently connected to.
- Suggest exactly one command. The user will review and execute it themselves.
- The command itself must always be in the appropriate syntax for the target system.
- For all other output (explanation, etc.), respond in the same language \
the user wrote their query in.
"""

AUTO_SYSTEM_PROMPT = """\
You are an assistant that executes commands on the terminal the user is \
operating (which may be a local shell, an SSH session, or a vendor CLI on \
a network device such as a router or switch).
- Execute commands one at a time to achieve the user's goal.
- At each step, review the output of the previous command and decide the next action.
- To execute the next command, set status to "continue" and specify the \
command or control character in action.
- When the goal is achieved, set status to "done". No action is needed.
- If an unrecoverable error occurs, set status to "fail". No action is needed.
- If a command is running for too long, you can send a control character \
(e.g. "ctrl-c") by setting action.type to "control".
- The command itself must always be in the appropriate syntax for the target system.
- For all other output (explanation, etc.), respond in the same language \
the user wrote their query in.
"""


class CompletionResponse(BaseModel):
    command: str
    explanation: str


class AutoAction(BaseModel):
    type: Literal["command", "control"]
    value: str


class AutoResponse(BaseModel):
    action: AutoAction | None = None
    status: Literal["continue", "done", "fail"]
    explanation: str


class LLMClient:
    def __init__(self, config: JouConfig) -> None:
        self._config = config
        self._client: openai.OpenAI = openai.OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        self._log_file = config.log_file

    def _log(self, entry: dict[str, Any]) -> None:
        if self._log_file is None:
            return
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def complete(self, screen_text: str, query: str) -> CompletionResponse:
        user_content = f"## Terminal Screen\n```\n{screen_text}\n```\n\n## User Request\n{query}"
        input_msgs: ResponseInputParam = [
            {"role": "user", "content": user_content},
        ]

        self._log({
            "type": "query",
            "mode": "completion",
            "query": query,
        })

        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "llm_request",
            "mode": "completion",
            "model": self._config.model,
            "instructions": SYSTEM_PROMPT,
            "input": list(input_msgs),
        })

        response = self._client.responses.parse(
            model=self._config.model,
            instructions=SYSTEM_PROMPT,
            input=input_msgs,
            text_format=CompletionResponse,
            reasoning=reasoning,
        )

        parsed = response.output_parsed
        if parsed is None:
            result = CompletionResponse(command="", explanation="LLM returned empty response")
        else:
            result = parsed

        self._log({
            "type": "llm_response",
            "mode": "completion",
            "response": result.model_dump(),
        })

        return result

    def auto_step(self, messages: list[EasyInputMessageParam]) -> AutoResponse:
        input_msgs: ResponseInputParam = list(messages)
        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "llm_request",
            "mode": "auto",
            "model": self._config.model,
            "instructions": AUTO_SYSTEM_PROMPT,
            "input": list(input_msgs),
        })

        response = self._client.responses.parse(
            model=self._config.model,
            instructions=AUTO_SYSTEM_PROMPT,
            input=input_msgs,
            text_format=AutoResponse,
            reasoning=reasoning,
        )

        parsed = response.output_parsed
        if parsed is None:
            result = AutoResponse(
                status="fail",
                explanation="LLM returned empty response",
            )
        else:
            result = parsed

        self._log({
            "type": "llm_response",
            "mode": "auto",
            "response": result.model_dump(),
        })

        return result

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
        history: list[EasyInputMessageParam],
    ) -> list[EasyInputMessageParam]:
        if not history:
            self._log({
                "type": "query",
                "mode": "auto",
                "query": query,
            })
        user_content = f"## Goal\n{query}\n\n## Current Terminal Screen\n```\n{screen_text}\n```"
        messages: list[EasyInputMessageParam] = [
            {"role": "user", "content": user_content},
        ]
        messages.extend(history)
        return messages
