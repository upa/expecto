from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import openai
from openai.lib.streaming.chat import ContentDeltaEvent
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
)
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai.types.chat.chat_completion_message import (
        ChatCompletionMessageToolCallUnion,
    )

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
    """Session summary that carries context across a compaction."""

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


TOOLS: list[ChatCompletionFunctionToolParam] = [
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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


def _query_content(
    query: str,
    *,
    screen_text: str | None = None,
    new_output: str | None = None,
    summary: HandoffSummary | None = None,
) -> str:
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
    return "\n\n".join(sections)


def _assistant_param(message: ChatCompletionMessage) -> ChatCompletionAssistantMessageParam:
    """Convert a response message into the exact history entry to replay.

    The conversation is append-only and replayed verbatim on every request,
    so the prompt prefix stays cached; tool call arguments are kept as the
    raw strings the model produced."""
    param: ChatCompletionAssistantMessageParam = {"role": "assistant"}
    if message.content is not None:
        param["content"] = message.content
    if message.tool_calls:
        param["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
            if call.type == "function"
        ]
    return param


class LLMClient:
    """Chat Completions client that owns the running conversation.

    The message history starts at the system prompt and accumulates
    queries, tool calls/outputs, and final responses across the whole
    session. It is append-only (for prompt-cache hits) and compacted
    transparently when it grows past compact_thresh (design/Conversation.md)."""

    def __init__(self, config: ExpectoConfig) -> None:
        self._config = config
        self._client: openai.AsyncOpenAI = openai.AsyncOpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        self._log_file = config.log_file
        self._compact_thresh = config.compact_thresh
        self._messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        self._last_prompt_tokens: int = 0
        self._logged_messages: int = 0
        self._compact_task: asyncio.Task[None] | None = None
        self._pending_summary: HandoffSummary | None = None

    def _log(self, entry: dict[str, Any]) -> None:
        if self._log_file is None:
            return
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(self._log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- conversation API (used by Executor) ---

    async def start_query(
        self,
        query: str,
        *,
        screen_text: str,
        new_output: str | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        """Begin one query on the running conversation and run its first
        step. Waits for a background compaction started by finish_query()
        if one is still in flight. A fresh conversation (session start or
        right after a compaction) gets the full screen; a continuing one
        gets only the new output produced since the previous task."""
        self._log({"type": "query", "query": query})

        if self._compact_task is not None:
            # Never raises: _compact() swallows its own errors. Awaiting a
            # task does not cancel it if this query is interrupted; the
            # compaction finishes on its own and the next query picks it up.
            await self._compact_task
            self._compact_task = None
        summary = self._pending_summary
        self._pending_summary = None

        if len(self._messages) == 1:
            content = _query_content(query, screen_text=screen_text, summary=summary)
        else:
            content = _query_content(query, new_output=new_output)
        self._messages.append({"role": "user", "content": content})
        return await self._request(on_delta)

    def add_tool_output(self, call_id: str, output: str) -> None:
        """Record one tool call's result on the conversation."""
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": output,
        })

    async def step(
        self, on_delta: Callable[[str], None] | None = None,
    ) -> StepResult:
        """Run one follow-up step (after tool outputs were recorded)."""
        return await self._request(on_delta)

    def finish_query(self) -> None:
        """Called when a query's execution ends (final response shown,
        stopped, or interrupted). Starts a background compaction if the
        conversation has grown past the threshold; it runs after control
        has returned to the user, and the next start_query() waits for it."""
        if self._compact_task is not None and not self._compact_task.done():
            return
        if len(self._messages) == 1 or self._prompt_size() < self._compact_thresh:
            return
        self._compact_task = asyncio.get_running_loop().create_task(
            self._compact_background(),
        )

    async def _compact_background(self) -> None:
        summary = await self._compact()
        if summary is not None:
            self._pending_summary = summary

    # --- request plumbing ---

    async def _request(
        self, on_delta: Callable[[str], None] | None,
    ) -> StepResult:
        self._log_request()

        async with self._client.chat.completions.stream(
            model=self._config.model,
            messages=self._messages,
            tools=TOOLS,
            parallel_tool_calls=False,
            reasoning_effort=(
                self._config.openai_reasoning_effort
                if self._config.openai_reasoning_effort is not None
                else openai.omit
            ),
            stream_options={"include_usage": True},
        ) as stream:
            async for event in stream:
                if isinstance(event, ContentDeltaEvent) and on_delta is not None:
                    on_delta(event.delta)
            completion = await stream.get_final_completion()

        message = completion.choices[0].message
        self._messages.append(_assistant_param(message))

        tool_calls: list[ParsedToolCall] = []
        for item in message.tool_calls or []:
            tool_calls.append(self._parse_tool_call(item))

        final_text: str | None = None
        if not tool_calls:
            final_text = message.content

        if completion.usage is not None:
            self._last_prompt_tokens = completion.usage.prompt_tokens

        self._log({
            "type": "llm_response",
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "error": c.error}
                | ({"params": c.params.model_dump()} if c.params is not None else {})
                for c in tool_calls
            ],
            "final_text": final_text,
            "prompt_tokens": (
                completion.usage.prompt_tokens if completion.usage is not None else None
            ),
        })

        return StepResult(tool_calls=tool_calls, final_text=final_text)

    def _log_request(self) -> None:
        """Log one request incrementally: the history is append-only, so
        only the messages added since the previous request are new."""
        self._log({
            "type": "llm_request",
            "model": self._config.model,
            "new_messages": list(self._messages[self._logged_messages:]),
            "total_messages": len(self._messages),
        })
        self._logged_messages = len(self._messages)

    def _prompt_size(self) -> int:
        """Prompt size in tokens for the compaction check: the last
        request's reported usage, or a chars/4 estimate when the backend
        reports no usage."""
        if self._last_prompt_tokens > 0:
            return self._last_prompt_tokens
        chars = sum(
            len(json.dumps(message, ensure_ascii=False))
            for message in self._messages
        )
        return chars // 4

    def _parse_tool_call(
        self, item: ChatCompletionMessageToolCallUnion,
    ) -> ParsedToolCall:
        if item.type != "function":
            return ParsedToolCall(
                call_id=item.id,
                name=item.type,
                params=None,
                error=f"unsupported tool call type: {item.type}",
            )
        name = item.function.name
        params: RunCommandParams | SendControlParams
        try:
            if name == "run_command":
                params = RunCommandParams.model_validate_json(item.function.arguments)
            elif name == "send_control":
                params = SendControlParams.model_validate_json(item.function.arguments)
            else:
                return ParsedToolCall(
                    call_id=item.id,
                    name=name,
                    params=None,
                    error=f"unknown tool: {name}",
                )
        except ValidationError as exc:
            return ParsedToolCall(
                call_id=item.id,
                name=name,
                params=None,
                error=f"invalid arguments: {exc}",
            )
        return ParsedToolCall(call_id=item.id, name=name, params=params)

    # --- compaction ---

    async def _compact(self) -> HandoffSummary | None:
        """Compact the conversation into a handoff summary and reset the
        history to the system prompt. On failure the history is kept and
        compaction is retried at the next query."""
        request: list[ChatCompletionMessageParam] = [
            *self._messages,
            {"role": "user", "content": COMPACT_REQUEST},
        ]
        self._log({"type": "compact_request", "total_messages": len(request)})

        try:
            # instructions (system) and tools stay identical to _request()
            # so the history's prompt prefix stays cached; only tool use is
            # disabled for this one call.
            response = await self._client.chat.completions.parse(
                model=self._config.model,
                messages=request,
                response_format=HandoffSummary,
                tools=TOOLS,
                parallel_tool_calls=False,
                tool_choice="none",
                reasoning_effort=(
                    self._config.openai_reasoning_effort
                    if self._config.openai_reasoning_effort is not None
                    else openai.omit
                ),
            )
            parsed = response.choices[0].message.parsed
            if parsed is None:
                raise ValueError("compaction LLM returned empty response")
        except Exception as exc:
            self._log({"type": "compact_error", "error": str(exc)})
            return None

        self._messages = [self._messages[0]]
        self._last_prompt_tokens = 0
        self._logged_messages = 0
        self._log({"type": "compact_response", "response": parsed.model_dump()})
        return parsed
