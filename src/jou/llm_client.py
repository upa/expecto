from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import openai
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCall,
    ResponseInputParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from jou.config import JouConfig, ReasoningEffortLevel

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
You are an assistant that operates the terminal the user is using (which \
may be a local shell, an SSH session, or a vendor CLI on a network device \
such as a router or switch) to achieve the user's goal.
- Use the run_command tool to execute a command in the terminal. It returns \
the terminal screen content after the output settles.
- Use the send_control tool to send a control character (e.g. "ctrl-c") to \
the terminal, for example to interrupt a long-running command.
- Call one tool at a time: review each result before deciding the next action.
- If you are not sure what environment the CLI is running on (which OS, or \
which router/switch product, etc.), first run a command that reveals it \
(e.g. `uname` on a Unix system, `show version` on a network device).
- If a tool result notes that the output did not settle, the command may \
still be running. Decide whether to interrupt it with send_control or take \
another action.
- When the goal is achieved, stop calling tools and respond with status \
"done". If the goal cannot be achieved, respond with status "fail".
- The command itself must always be in the appropriate syntax for the target system.
- For all other output (explanation, etc.), respond in the same language \
the user wrote their query in.
"""

SUMMARY_SYSTEM_PROMPT = """\
You are maintaining a running summary of a terminal session.
Given the previous summary (if any) and new terminal output, produce an \
updated summary by MERGING the new information into the previous summary.
- environment: infer what system, shell, or CLI the user is currently on \
(a local shell, an SSH session, or a network device vendor CLI such as \
Cisco IOS / Junos / Arista EOS). Keep the previous inference unless the \
new output contradicts it.
- activities: summarize what the user has been doing in this session.
- notes: errors encountered, apparent goals, and other key facts. Never \
drop information from the previous summary unless it is clearly obsolete.
- Do not describe the most recent output verbatim; focus on information \
that will scroll out of view.
"""


class CompletionResponse(BaseModel):
    command: str
    explanation: str


class SummaryResult(BaseModel):
    environment: str
    activities: str
    notes: str


class RunCommandParams(BaseModel):
    command: str


class SendControlParams(BaseModel):
    key: Literal["ctrl-c", "ctrl-d", "ctrl-z"]


class AutoResponse(BaseModel):
    """Final response of the auto mode loop. Continuation is a tool call."""

    status: Literal["done", "fail"]
    explanation: str


AUTO_TOOLS: list[FunctionToolParam] = [
    {
        "type": "function",
        "name": "run_command",
        "description": (
            "Execute a command in the user's terminal (the command followed by "
            "Enter is written to the terminal), wait until the output settles, "
            "and return the terminal screen text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute, in the syntax of the target system.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "send_control",
        "description": (
            "Send a control character to the user's terminal (e.g. to interrupt "
            "a long-running command), wait until the output settles, and return "
            "the terminal screen text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "enum": ["ctrl-c", "ctrl-d", "ctrl-z"],
                    "description": "The control character to send.",
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    params: RunCommandParams | SendControlParams | None
    error: str | None = None


@dataclass
class AutoStepResult:
    tool_calls: list[ParsedToolCall]
    final: AutoResponse | None
    response_id: str


def _compose_instructions(base: str, working_summary: str | None) -> str:
    if working_summary is None:
        return base
    return f"{base}\n{working_summary}"


def _build_summary_input(
    previous: SummaryResult | None,
    new_output: str,
    truncated: bool,
) -> str:
    sections: list[str] = []
    if previous is not None:
        sections.append(
            "## Previous Summary\n"
            f"Environment: {previous.environment}\n"
            f"Activities: {previous.activities}\n"
            f"Notes: {previous.notes}"
        )
    heading = "## New Terminal Output"
    if truncated:
        heading += " (earlier lines omitted due to length)"
    sections.append(f"{heading}\n```\n{new_output}\n```")
    return "\n\n".join(sections)


class LLMClient:
    def __init__(self, config: JouConfig) -> None:
        self._config = config
        self._client: openai.OpenAI = openai.OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        # Summary settings fall back to the main ones when unset, mirroring
        # the SUMMARY_OPENAI_* -> OPENAI_* env var fallback for configs
        # constructed without from_env().
        self._summary_client: openai.OpenAI = openai.OpenAI(
            api_key=config.summary_openai_api_key or config.openai_api_key,
            base_url=config.summary_openai_base_url or config.openai_base_url,
        )
        self._summary_model: str = config.summary_model or config.model
        self._summary_reasoning_effort: ReasoningEffortLevel | None = (
            config.summary_openai_reasoning_effort
            if config.summary_openai_reasoning_effort is not None
            else config.openai_reasoning_effort
        )
        self._log_file = config.log_file

    def _log(self, entry: dict[str, Any]) -> None:
        if self._log_file is None:
            return
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def complete(
        self,
        screen_text: str,
        query: str,
        working_summary: str | None = None,
    ) -> CompletionResponse:
        user_content = f"## Terminal Screen\n```\n{screen_text}\n```\n\n## User Request\n{query}"
        input_msgs: ResponseInputParam = [
            {"role": "user", "content": user_content},
        ]
        instructions = _compose_instructions(SYSTEM_PROMPT, working_summary)

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
            "instructions": instructions,
            "input": list(input_msgs),
        })

        response = self._client.responses.parse(
            model=self._config.model,
            instructions=instructions,
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

    def auto_step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
    ) -> AutoStepResult:
        instructions = _compose_instructions(AUTO_SYSTEM_PROMPT, working_summary)
        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "llm_request",
            "mode": "auto",
            "model": self._config.model,
            "instructions": instructions,
            "input": list(input_items),
            "previous_response_id": previous_response_id,
        })

        response = self._client.responses.parse(
            model=self._config.model,
            instructions=instructions,
            input=input_items,
            text_format=AutoResponse,
            tools=AUTO_TOOLS,
            parallel_tool_calls=False,
            reasoning=reasoning,
            store=True,
            previous_response_id=previous_response_id,
        )

        tool_calls: list[ParsedToolCall] = []
        for item in response.output:
            if isinstance(item, ResponseFunctionToolCall):
                tool_calls.append(self._parse_tool_call(item))

        final: AutoResponse | None = None
        if not tool_calls:
            final = response.output_parsed
            if final is None:
                final = AutoResponse(
                    status="fail",
                    explanation="LLM returned empty response",
                )

        self._log({
            "type": "llm_response",
            "mode": "auto",
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "error": c.error}
                | ({"params": c.params.model_dump()} if c.params is not None else {})
                for c in tool_calls
            ],
            "final": final.model_dump() if final is not None else None,
            "response_id": response.id,
        })

        return AutoStepResult(
            tool_calls=tool_calls,
            final=final,
            response_id=response.id,
        )

    def _parse_tool_call(self, item: ResponseFunctionToolCall) -> ParsedToolCall:
        call_id = item.call_id
        params: RunCommandParams | SendControlParams
        try:
            if item.name == "run_command":
                params = RunCommandParams.model_validate_json(item.arguments)
            elif item.name == "send_control":
                params = SendControlParams.model_validate_json(item.arguments)
            else:
                return ParsedToolCall(
                    call_id=call_id,
                    name=item.name,
                    params=None,
                    error=f"unknown tool: {item.name}",
                )
        except ValidationError as exc:
            return ParsedToolCall(
                call_id=call_id,
                name=item.name,
                params=None,
                error=f"invalid arguments: {exc}",
            )
        return ParsedToolCall(call_id=call_id, name=item.name, params=params)

    def summarize(
        self,
        previous: SummaryResult | None,
        new_output: str,
        truncated: bool = False,
    ) -> SummaryResult:
        user_content = _build_summary_input(previous, new_output, truncated)
        input_msgs: ResponseInputParam = [
            {"role": "user", "content": user_content},
        ]
        reasoning: Reasoning | None = (
            Reasoning(effort=self._summary_reasoning_effort)
            if self._summary_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "summary_request",
            "model": self._summary_model,
            "instructions": SUMMARY_SYSTEM_PROMPT,
            "input": list(input_msgs),
        })

        try:
            response = self._summary_client.responses.parse(
                model=self._summary_model,
                instructions=SUMMARY_SYSTEM_PROMPT,
                input=input_msgs,
                text_format=SummaryResult,
                reasoning=reasoning,
                store=False,
            )
        except Exception as exc:
            self._log({"type": "summary_error", "error": str(exc)})
            raise

        parsed = response.output_parsed
        if parsed is None:
            self._log({"type": "summary_error", "error": "LLM returned empty response"})
            raise ValueError("summary LLM returned empty response")

        self._log({
            "type": "summary_response",
            "response": parsed.model_dump(),
        })

        return parsed

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam:
        self._log({
            "type": "query",
            "mode": "auto",
            "query": query,
        })
        user_content = f"## Goal\n{query}\n\n## Current Terminal Screen\n```\n{screen_text}\n```"
        return [{"role": "user", "content": user_content}]
