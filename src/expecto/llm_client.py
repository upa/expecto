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
- You assist with a sequence of tasks in one terminal session. Each new \
goal message may include the terminal output produced since the previous \
task (e.g. commands the user ran by hand).
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

COMPACT_REQUEST = """\
This conversation has grown long and is being compacted: your structured \
response will seed a fresh conversation that replaces this one. Do not \
call any tools. Summarize this session so far for the handoff:
- environment: what system, shell, or CLI is being operated (a local \
shell, an SSH session, or a network device vendor CLI), including \
hostnames or device types if known.
- activities: what the user and you have done in this session.
- notes: errors encountered, goals achieved or still open, and any other \
facts the next conversation must know. Do not invent information.
"""


class HandoffSummary(BaseModel):
    """Session summary that carries context across a chain compaction."""

    environment: str
    activities: str
    notes: str


class ChainLostError(Exception):
    """The server no longer has the previous response of the chain."""


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
    input_tokens: int | None = None


class LLMClient:
    def __init__(self, config: ExpectoConfig) -> None:
        self._config = config
        self._client: openai.AsyncOpenAI = openai.AsyncOpenAI(
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

    async def step(
        self,
        input_items: ResponseInputParam,
        previous_response_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "llm_request",
            "model": self._config.model,
            "input": list(input_items),
            "previous_response_id": previous_response_id,
        })

        try:
            async with self._client.responses.stream(
                model=self._config.model,
                instructions=SYSTEM_PROMPT,
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
        except openai.BadRequestError as exc:
            if (
                previous_response_id is not None
                and "previous response" in str(exc).lower()
            ):
                raise ChainLostError(str(exc)) from exc
            raise

        tool_calls: list[ParsedToolCall] = []
        for item in response.output:
            if isinstance(item, ResponseFunctionToolCall):
                tool_calls.append(self._parse_tool_call(item))

        final_text: str | None = None
        if not tool_calls:
            final_text = response.output_text

        input_tokens = response.usage.input_tokens if response.usage is not None else None

        self._log({
            "type": "llm_response",
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "error": c.error}
                | ({"params": c.params.model_dump()} if c.params is not None else {})
                for c in tool_calls
            ],
            "final_text": final_text,
            "response_id": response.id,
            "input_tokens": input_tokens,
        })

        return StepResult(
            tool_calls=tool_calls,
            final_text=final_text,
            response_id=response.id,
            input_tokens=input_tokens,
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

    async def compact(
        self,
        previous_response_id: str,
        pending_items: ResponseInputParam | None = None,
    ) -> HandoffSummary:
        """Ask the current chain for a handoff summary that seeds a fresh
        chain. instructions and tools stay identical to step() so the
        chain's prompt prefix stays cached; the compaction directive is
        carried by the user message and tool_choice="none"."""
        input_items: ResponseInputParam = list(pending_items) if pending_items else []
        input_items.append({"role": "user", "content": COMPACT_REQUEST})
        reasoning: Reasoning | None = (
            Reasoning(effort=self._config.openai_reasoning_effort)
            if self._config.openai_reasoning_effort is not None
            else None
        )

        self._log({
            "type": "compact_request",
            "model": self._config.model,
            "input": list(input_items),
            "previous_response_id": previous_response_id,
        })

        try:
            response = await self._client.responses.parse(
                model=self._config.model,
                instructions=SYSTEM_PROMPT,
                input=input_items,
                text_format=HandoffSummary,
                tools=TOOLS,
                parallel_tool_calls=False,
                tool_choice="none",
                reasoning=reasoning,
                store=False,
                previous_response_id=previous_response_id,
            )
        except Exception as exc:
            self._log({"type": "compact_error", "error": str(exc)})
            raise

        parsed = response.output_parsed
        if parsed is None:
            self._log({"type": "compact_error", "error": "LLM returned empty response"})
            raise ValueError("compaction LLM returned empty response")

        self._log({
            "type": "compact_response",
            "response": parsed.model_dump(),
        })

        return parsed

    def build_query_message(
        self,
        query: str,
        *,
        screen_text: str | None = None,
        new_output: str | None = None,
        summary: HandoffSummary | None = None,
    ) -> ResponseInputParam:
        """Build the user message that starts one query. A fresh chain
        gets the full screen (plus the handoff summary right after a
        compaction); a continuing chain gets only the terminal output
        produced since the previous task."""
        self._log({
            "type": "query",
            "query": query,
        })
        sections: list[str] = []
        if summary is not None:
            sections.append(
                "## Session Summary (compacted from the earlier conversation)\n"
                f"Environment: {summary.environment}\n"
                f"Activities: {summary.activities}\n"
                f"Notes: {summary.notes}"
            )
        if screen_text is not None:
            sections.append(f"## Current Terminal Screen\n```\n{screen_text}\n```")
        if new_output is not None:
            sections.append(
                "## New Terminal Output (since the previous task)\n"
                f"```\n{new_output}\n```"
            )
        sections.append(f"## Goal\n{query}")
        return [{"role": "user", "content": "\n\n".join(sections)}]
