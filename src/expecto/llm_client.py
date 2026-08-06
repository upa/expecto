from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import openai
from openai.lib.streaming.responses import ResponseTextDeltaEvent
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCall,
    ResponseInputParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from expecto.config import ExpectoConfig

SYSTEM_PROMPT = """\
You are an assistant that operates the terminal the user is using (which \
may be a local shell, an SSH session, or a vendor CLI on a network device \
such as a router or switch) to achieve the user's goal.
- Use the run_command tool to execute a command in the terminal. It returns \
the terminal screen content after the output settles.
- Use the send_control tool to send a control character (e.g. "ctrl-c") to \
the terminal, for example to interrupt a long-running command.
- Call one tool at a time: review each result before deciding the next action.
- Classify every run_command call's risk on the target system: "read_only" \
(no state change: show/get/list/ping etc.), "mutating" (changes state but \
recoverable and limited in scope: file creation, candidate config edits, \
service restarts), or "destructive" (irreversible or high blast radius: \
data deletion, device reload/erase/format, changes that may cut your own \
connectivity). Give a short risk_reason. Destructive commands are shown to \
the user for confirmation before execution; classify honestly and never \
understate the risk.
- If you are not sure what environment the CLI is running on (which OS, or \
which router/switch product, etc.), first run a command that reveals it \
(e.g. `uname` on a Unix system, `show version` on a network device).
- If a tool result notes that the output did not settle, the command may \
still be running. Decide whether to interrupt it with send_control or take \
another action.
- When the goal is achieved, stop calling tools and respond with a brief \
summary of what was done. If the goal cannot be achieved, stop calling \
tools and explain why. Do not produce any text output other than this \
final response.
- The command itself must always be in the appropriate syntax for the target system.
- Write the explanation in English by default; if the user's query is not \
in English, write it in the language of the query instead.
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


class SummaryResult(BaseModel):
    environment: str
    activities: str
    notes: str


RiskLevel = Literal["read_only", "mutating", "destructive"]


class RunCommandParams(BaseModel):
    command: str
    risk: RiskLevel = "read_only"
    risk_reason: str = ""


class SendControlParams(BaseModel):
    key: Literal["ctrl-c", "ctrl-d", "ctrl-z"]


TOOLS: list[FunctionToolParam] = [
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
                "risk": {
                    "type": "string",
                    "enum": ["read_only", "mutating", "destructive"],
                    "description": (
                        "Risk classification of this command on the target "
                        "system: read_only = no state change; mutating = "
                        "changes state but recoverable and limited in scope; "
                        "destructive = irreversible or high blast radius "
                        "(data deletion, device reload/erase, changes that "
                        "may cut connectivity)."
                    ),
                },
                "risk_reason": {
                    "type": "string",
                    "description": "Short justification for the risk classification.",
                },
            },
            "required": ["command", "risk", "risk_reason"],
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
class StepResult:
    """One LLM step: tool calls (continuation) or a final text (loop end)."""

    tool_calls: list[ParsedToolCall]
    final_text: str | None
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
    def __init__(self, config: ExpectoConfig) -> None:
        self._config = config
        self._client: openai.AsyncOpenAI = openai.AsyncOpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        # The summary LLM shares the main connection (same API key and
        # base_url); only the model may differ (a lighter model for the
        # background summaries).
        self._summary_model: str = config.summary_model or config.model
        self._log_file = config.log_file

    def _log(self, entry: dict[str, Any]) -> None:
        if self._log_file is None:
            return
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        working_summary: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        instructions = _compose_instructions(SYSTEM_PROMPT, working_summary)
        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "llm_request",
            "model": self._config.model,
            "instructions": instructions,
            "input": list(input_items),
            "previous_response_id": previous_response_id,
        })

        async with self._client.responses.stream(
            model=self._config.model,
            instructions=instructions,
            input=input_items,
            tools=TOOLS,
            parallel_tool_calls=False,
            reasoning=reasoning,
            store=True,
            previous_response_id=previous_response_id,
        ) as stream:
            async for event in stream:
                if isinstance(event, ResponseTextDeltaEvent) and on_delta is not None:
                    on_delta(event.delta)
            response = await stream.get_final_response()

        tool_calls: list[ParsedToolCall] = []
        for item in response.output:
            if isinstance(item, ResponseFunctionToolCall):
                tool_calls.append(self._parse_tool_call(item))

        final_text: str | None = None
        if not tool_calls:
            final_text = response.output_text

        self._log({
            "type": "llm_response",
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "error": c.error}
                | ({"params": c.params.model_dump()} if c.params is not None else {})
                for c in tool_calls
            ],
            "final_text": final_text,
            "response_id": response.id,
        })

        return StepResult(
            tool_calls=tool_calls,
            final_text=final_text,
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

    async def summarize(
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
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "summary_request",
            "model": self._summary_model,
            "instructions": SUMMARY_SYSTEM_PROMPT,
            "input": list(input_msgs),
        })

        try:
            response = await self._client.responses.parse(
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

    def build_initial_messages(
        self,
        query: str,
        screen_text: str,
    ) -> ResponseInputParam:
        self._log({
            "type": "query",
            "query": query,
        })
        user_content = f"## Goal\n{query}\n\n## Current Terminal Screen\n```\n{screen_text}\n```"
        return [{"role": "user", "content": user_content}]
