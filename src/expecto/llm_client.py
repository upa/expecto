from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import openai
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
    from openai.types.completion_usage import CompletionUsage

    from expecto.config import ExpectoConfig
    from expecto.skills import Skill

SYSTEM_PROMPT = """\
You are an assistant that operates the terminal the user is using (which \
may be a local shell, an SSH session, or a vendor CLI on a network device \
such as a router or switch) to achieve the user's goal.
- You assist with a sequence of tasks in one terminal session. Each new \
goal message may include the terminal output produced since the previous \
task (e.g. commands the user ran by hand).
- Use the run_command tool to execute a command in the terminal. It returns \
the terminal screen content after the output settles.
- Use the send_keys tool to type raw keystrokes into the terminal: plain \
text plus special keys in angle-bracket notation (<CR>, <Esc>, <Tab>, \
<Up>, <C-c>, ...). Use it to interrupt a long-running command (<C-c>), \
answer an interactive prompt, or drive a full-screen program such as an \
editor, pager, or menu UI. Keys are sent exactly as typed: nothing is \
executed until you send <CR>.
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
still be running. Decide whether to interrupt it with send_keys or take \
another action.
- When the goal is achieved, stop calling tools and respond with a brief \
summary of what was done. If the goal cannot be achieved, stop calling \
tools and explain why. Do not produce any text output other than this \
final response.
- The command itself must always be in the appropriate syntax for the target system.
- Write the explanation in English by default; if the user's query is not \
in English, write it in the language of the query instead.
"""

AGENTS_MD_SECTION = """
# User Instructions

The following instructions come from the user's AGENTS.md. Follow them in \
addition to the rules above — they carry site-specific knowledge such as \
hosts, device conventions, preferred commands, or output style. They do not \
override the tool protocol or the risk classification and confirmation \
rules above.

"""


def load_agents_md(path: str) -> str | None:
    """Read the user's site instructions from an AGENTS.md file.

    Returns None when the file does not exist, cannot be read, or holds
    nothing but whitespace."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None
    return text.strip() or None


SKILLS_SECTION_HEADER = """
# Skills

Skills are instructions the user wrote for a particular kind of task. \
When the goal matches one of them, call load_skill with its name and \
follow what it returns before acting. Load only the ones you need.

Available skills:
"""


MCP_SECTION = """
# MCP Tools

Tools whose names begin with `mcp__` come from MCP servers the user \
configured. They act outside the terminal (services, databases, \
documentation, and the like); use one when it fits the goal better than \
running a command, and keep using run_command for anything that belongs \
on the terminal.
"""


def build_system_prompt(
    agents_md: str | None = None,
    *,
    skills: list[Skill] | None = None,
    has_mcp_tools: bool = False,
) -> str:
    """The system prompt: the base prompt, plus a catalog of the user's
    skills, a note about MCP tools when any server provided some, and the
    user's AGENTS.md, each when present.

    All of them are settled at startup and fixed for the whole session,
    so the prompt prefix stays cached (design/Conversation.md §2)."""
    prompt = SYSTEM_PROMPT
    if skills:
        prompt += SKILLS_SECTION_HEADER + "".join(
            f"- {skill.name}: {skill.description}\n" for skill in skills
        )
    if has_mcp_tools:
        prompt += MCP_SECTION
    if agents_md is not None:
        prompt += AGENTS_MD_SECTION + agents_md
    return prompt


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


class SendKeysParams(BaseModel):
    keys: str


class LoadSkillParams(BaseModel):
    name: str
    file: str = ""


LOAD_SKILL_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load one of the user's skills and return its instructions. Call "
            "this before acting when the goal matches a skill listed in the "
            "system prompt. Skills live on the machine running this session's "
            "wrapper, not necessarily on the system the terminal is connected "
            "to, so always read them with this tool rather than with a command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the skill to load.",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "A file bundled with the skill, as listed at the end of "
                        "the skill's instructions. Use an empty string for the "
                        "skill's own instructions."
                    ),
                },
            },
            "required": ["name", "file"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


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
            "name": "send_keys",
            "description": (
                "Type raw keystrokes into the user's terminal, wait until the "
                "output settles, and return the visible screen with the cursor "
                "position. Plain characters are typed as-is; special keys use "
                "angle-bracket notation: <CR> (Enter), <Esc>, <Tab>, <BS>, "
                "<Space>, <Up>/<Down>/<Left>/<Right>, <Home>/<End>, "
                "<PgUp>/<PgDn>, <Del>, <F1>-<F12>, control characters as <C-x> "
                "(e.g. <C-c>), and <lt> for a literal '<'. Works for anything "
                "the terminal is running: interrupting a command with <C-c>, "
                "answering prompts, or driving full-screen programs. Nothing "
                "is executed until you send <CR>."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": (
                            'The keystrokes to send, e.g. "y<CR>" or '
                            '"<C-c>" or "iHello<Esc>".'
                        ),
                    },
                },
                "required": ["keys"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
]


@dataclass
class MCPToolParams:
    """A call to a tool provided by an MCP server (design/MCP.md §5)."""

    name: str                          # the qualified mcp__server__tool name
    arguments: dict[str, object]


ToolParams = RunCommandParams | SendKeysParams | LoadSkillParams | MCPToolParams


@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    params: ToolParams | None
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


def _decode_arguments(arguments: str) -> dict[str, object]:
    """Decode a tool call's raw argument string as a JSON object."""
    decoded: object = json.loads(arguments or "{}")
    if not isinstance(decoded, dict):
        raise ValueError(f"expected a JSON object, got {type(decoded).__name__}")
    return cast("dict[str, object]", decoded)


def _params_dump(params: ToolParams) -> dict[str, object]:
    if isinstance(params, MCPToolParams):
        return {"name": params.name, "arguments": params.arguments}
    return params.model_dump()


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

    def __init__(
        self,
        config: ExpectoConfig,
        agents_md: str | None = None,
        skills: list[Skill] | None = None,
    ) -> None:
        self._config = config
        self._agents_md = agents_md
        self._skills = skills or []
        # load_skill is only offered when there is something to load.
        self._base_tools: list[ChatCompletionFunctionToolParam] = [
            *TOOLS, *([LOAD_SKILL_TOOL] if self._skills else []),
        ]
        self._tools: list[ChatCompletionFunctionToolParam] = list(self._base_tools)
        self._mcp_tool_names: frozenset[str] = frozenset()
        self._client: openai.AsyncOpenAI = openai.AsyncOpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        self._log_file = config.log_file
        self._compact_thresh = config.compact_thresh
        self._messages: list[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": build_system_prompt(agents_md, skills=self._skills),
            },
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

    def set_mcp_tools(self, tools: list[ChatCompletionFunctionToolParam]) -> None:
        """Publish the connected MCP servers' tools to the LLM.

        Called once at startup, after the servers are connected and before
        any request: the tools definition and the system prompt have to be
        identical on every request of the session (design/Conversation.md
        §2), so this rewrites the still-untouched system message and may
        not be called once the conversation has started."""
        assert len(self._messages) == 1, "MCP tools must be set before the first query"
        if not tools:
            return
        self._tools = [*self._base_tools, *tools]
        self._mcp_tool_names = frozenset(
            str(tool["function"]["name"]) for tool in tools
        )
        self._messages[0] = {
            "role": "system",
            "content": build_system_prompt(
                self._agents_md, skills=self._skills, has_mcp_tools=True,
            ),
        }

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

        # Raw chunk streaming, assembled by hand: the SDK's auto-parsing
        # stream helper refuses to parse a call to a non-strict function
        # tool, and MCP tool schemas are passed through without strict
        # (design/MCP.md §4).
        stream = await self._client.chat.completions.create(
            model=self._config.model,
            messages=self._messages,
            tools=self._tools,
            parallel_tool_calls=False,
            reasoning_effort=(
                self._config.openai_reasoning_effort
                if self._config.openai_reasoning_effort is not None
                else openai.omit
            ),
            stream=True,
            stream_options={"include_usage": True},
        )
        content_parts: list[str] = []
        calls_by_index: dict[int, dict[str, str]] = {}
        usage: CompletionUsage | None = None
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    if on_delta is not None:
                        on_delta(delta.content)
                for tc in delta.tool_calls or []:
                    acc = calls_by_index.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""},
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function.arguments:
                            acc["arguments"] += tc.function.arguments
        finally:
            # Cancellation (user Ctrl-C) must abort the HTTP request too.
            await stream.close()

        message = ChatCompletionMessage.model_validate({
            "role": "assistant",
            "content": "".join(content_parts) if content_parts else None,
            "tool_calls": [
                {
                    "id": acc["id"],
                    "type": "function",
                    "function": {"name": acc["name"], "arguments": acc["arguments"]},
                }
                for _, acc in sorted(calls_by_index.items())
            ]
            or None,
        })
        self._messages.append(_assistant_param(message))

        tool_calls: list[ParsedToolCall] = []
        for item in message.tool_calls or []:
            tool_calls.append(self._parse_tool_call(item))

        final_text: str | None = None
        if not tool_calls:
            final_text = message.content

        if usage is not None:
            self._last_prompt_tokens = usage.prompt_tokens

        self._log({
            "type": "llm_response",
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "error": c.error}
                | ({"params": _params_dump(c.params)} if c.params is not None else {})
                for c in tool_calls
            ],
            "final_text": final_text,
            "prompt_tokens": usage.prompt_tokens if usage is not None else None,
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
        params: ToolParams
        try:
            if name == "run_command":
                params = RunCommandParams.model_validate_json(item.function.arguments)
            elif name == "send_keys":
                params = SendKeysParams.model_validate_json(item.function.arguments)
            elif name == "load_skill" and self._skills:
                params = LoadSkillParams.model_validate_json(item.function.arguments)
            elif name in self._mcp_tool_names:
                # MCP schemas are the servers' own and are not validated
                # here; the server itself rejects bad arguments.
                params = MCPToolParams(
                    name=name, arguments=_decode_arguments(item.function.arguments),
                )
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
        except ValueError as exc:
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
                tools=self._tools,
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
