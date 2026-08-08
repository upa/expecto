from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import cast

import pytest
from openai.lib.streaming.chat import ContentDeltaEvent
from openai.types.chat import ChatCompletionFunctionToolParam, ChatCompletionMessage
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openai.types.completion_usage import CompletionUsage

from expecto.config import ExpectoConfig
from expecto.llm_client import (
    COMPACT_REQUEST,
    HandoffSummary,
    LLMClient,
    LOAD_SKILL_TOOL,
    LoadSkillParams,
    MCPToolParams,
    RunCommandParams,
    SendControlParams,
    SYSTEM_PROMPT,
    TOOLS,
    build_system_prompt,
    load_agents_md,
)
from expecto.skills import Skill


def _make_config(**overrides: object) -> ExpectoConfig:
    config = ExpectoConfig(
        shell="/bin/bash",
        model="gpt-4o",
        openai_api_key="sk-test",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _function_call(
    call_id: str, name: str, arguments: str,
) -> ChatCompletionMessageFunctionToolCall:
    return ChatCompletionMessageFunctionToolCall(
        id=call_id,
        type="function",
        function=Function(name=name, arguments=arguments),
    )


def _delta_event(delta: str, snapshot: str) -> ContentDeltaEvent:
    return ContentDeltaEvent(
        type="content.delta", delta=delta, snapshot=snapshot, parsed=None,
    )


def _completion(
    message: ChatCompletionMessage,
    prompt_tokens: int | None = None,
) -> ChatCompletion:
    usage = (
        CompletionUsage.model_construct(prompt_tokens=prompt_tokens)
        if prompt_tokens is not None
        else None
    )
    return ChatCompletion.model_construct(
        id="cmpl_1",
        choices=[Choice.model_construct(message=message)],
        usage=usage,
    )


def _final_message(text: str) -> ChatCompletionMessage:
    return ChatCompletionMessage.model_construct(role="assistant", content=text)


def _tool_call_message(
    *calls: ChatCompletionMessageFunctionToolCall,
) -> ChatCompletionMessage:
    return ChatCompletionMessage.model_construct(
        role="assistant", content=None, tool_calls=list(calls),
    )


class _FakeStream:
    """Stands in for the AsyncChatCompletionStreamManager returned by
    client.chat.completions.stream(): async context manager, async
    iterator of events, and get_final_completion()."""

    def __init__(self, events: list[object], final: ChatCompletion) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> object:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_completion(self) -> ChatCompletion:
        return self._final


def _client_with_fake_stream(
    responses: list[tuple[list[object], ChatCompletion]],
) -> tuple[LLMClient, list[dict[str, object]]]:
    """An LLMClient whose stream() serves canned responses; every call's
    kwargs are captured (messages are copied at call time)."""
    client = LLMClient(_make_config())
    captured: list[dict[str, object]] = []
    queue = list(responses)

    def fake_stream(**kwargs: object) -> _FakeStream:
        kwargs["messages"] = list(cast("list[object]", kwargs["messages"]))
        captured.append(kwargs)
        events, final = queue.pop(0)
        return _FakeStream(list(events), final)

    setattr(client._client.chat.completions, "stream", fake_stream)  # noqa: B010
    return client, captured


def _msg(entry: object) -> dict[str, object]:
    return cast("dict[str, object]", entry)


class TestToolParams:
    def test_run_command_params(self) -> None:
        params = RunCommandParams(command="ls -la")
        assert params.command == "ls -la"
        assert params.risk == "read_only"

    def test_send_control_params(self) -> None:
        params = SendControlParams(key="ctrl-c")
        assert params.key == "ctrl-c"


class TestTools:
    def test_tool_names(self) -> None:
        names = [tool["function"]["name"] for tool in TOOLS]
        assert names == ["run_command", "send_control"]

    def test_tools_are_strict(self) -> None:
        for tool in TOOLS:
            function = tool["function"]
            assert function["strict"] is True
            params = function["parameters"]
            assert params["additionalProperties"] is False

    def test_run_command_schema_requires_risk(self) -> None:
        params = TOOLS[0]["function"]["parameters"]
        required = cast("list[str]", params["required"])
        assert set(required) == {"command", "risk", "risk_reason"}
        properties = cast("dict[str, dict[str, object]]", params["properties"])
        assert properties["risk"]["enum"] == ["read_only", "mutating", "destructive"]


class TestParseToolCall:
    def test_run_command(self) -> None:
        client = LLMClient(_make_config())
        item = _function_call(
            "call_1",
            "run_command",
            '{"command": "df -h", "risk": "read_only", "risk_reason": "reads only"}',
        )
        parsed = client._parse_tool_call(item)
        assert parsed.call_id == "call_1"
        assert parsed.name == "run_command"
        assert isinstance(parsed.params, RunCommandParams)
        assert parsed.params.command == "df -h"
        assert parsed.error is None

    def test_send_control(self) -> None:
        client = LLMClient(_make_config())
        item = _function_call("call_2", "send_control", '{"key": "ctrl-c"}')
        parsed = client._parse_tool_call(item)
        assert isinstance(parsed.params, SendControlParams)
        assert parsed.params.key == "ctrl-c"
        assert parsed.error is None

    def test_unknown_tool(self) -> None:
        client = LLMClient(_make_config())
        parsed = client._parse_tool_call(_function_call("call_3", "nonexistent", "{}"))
        assert parsed.params is None
        assert parsed.error is not None
        assert "unknown tool" in parsed.error

    def test_invalid_arguments(self) -> None:
        client = LLMClient(_make_config())
        parsed = client._parse_tool_call(
            _function_call("call_4", "send_control", '{"key": "ctrl-x"}'),
        )
        assert parsed.params is None
        assert parsed.error is not None
        assert "invalid arguments" in parsed.error


class TestConversation:
    async def test_start_query_streams_and_appends_history(self) -> None:
        client, captured = _client_with_fake_stream([
            (
                [_delta_event("Hello ", "Hello "), _delta_event("world", "Hello world")],
                _completion(_final_message("Hello world")),
            ),
        ])
        chunks: list[str] = []
        result = await client.start_query(
            "greet", screen_text="$ ", on_delta=chunks.append,
        )

        assert chunks == ["Hello ", "world"]
        assert result.final_text == "Hello world"
        assert result.tool_calls == []
        # History: system, user query, assistant final.
        assert [_msg(m)["role"] for m in client._messages] == [
            "system", "user", "assistant",
        ]
        assert _msg(client._messages[2])["content"] == "Hello world"

    async def test_first_query_uses_screen_goal_last(self) -> None:
        client, _ = _client_with_fake_stream([
            ([], _completion(_final_message("done"))),
        ])
        await client.start_query(
            "確認して", screen_text="user@host:~$ ", new_output="ignored",
        )
        content = str(_msg(client._messages[1])["content"])
        assert "## Current Terminal Screen" in content
        assert "user@host:~$ " in content
        assert "## New Terminal Output" not in content
        assert content.endswith("## Goal\n確認して")

    async def test_continuing_query_uses_new_output(self) -> None:
        client, _ = _client_with_fake_stream([
            ([], _completion(_final_message("one"))),
            ([], _completion(_final_message("two"))),
        ])
        await client.start_query("first", screen_text="$ ")
        await client.start_query(
            "second", screen_text="$ full screen", new_output="manual output",
        )
        content = str(_msg(client._messages[3])["content"])
        assert "## New Terminal Output" in content
        assert "manual output" in content
        assert "## Current Terminal Screen" not in content
        assert content.endswith("## Goal\nsecond")

    async def test_tool_calls_parsed_and_outputs_recorded(self) -> None:
        args = '{"command": "ls", "risk": "read_only", "risk_reason": "list"}'
        client, _ = _client_with_fake_stream([
            (
                [],
                _completion(
                    _tool_call_message(_function_call("call_1", "run_command", args)),
                ),
            ),
            ([], _completion(_final_message("done"))),
        ])
        result = await client.start_query("list", screen_text="$ ")
        assert result.final_text is None
        assert len(result.tool_calls) == 1
        assert isinstance(result.tool_calls[0].params, RunCommandParams)

        client.add_tool_output("call_1", "## Terminal Output\nfiles")
        result = await client.step()
        assert result.final_text == "done"

        roles = [_msg(m)["role"] for m in client._messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        tool_msg = _msg(client._messages[3])
        assert tool_msg["tool_call_id"] == "call_1"
        # The assistant tool call is replayed with the raw arguments string.
        assistant = _msg(client._messages[2])
        tool_calls = cast("list[dict[str, object]]", assistant["tool_calls"])
        assert cast("dict[str, str]", tool_calls[0]["function"])["arguments"] == args

    async def test_request_parameters(self) -> None:
        client, captured = _client_with_fake_stream([
            ([], _completion(_final_message("done"))),
        ])
        await client.start_query("hi", screen_text="$ ")

        request = captured[0]
        assert request["model"] == "gpt-4o"
        assert request["tools"] == TOOLS
        assert request["parallel_tool_calls"] is False
        assert request["stream_options"] == {"include_usage": True}
        messages = cast("list[object]", request["messages"])
        first = _msg(messages[0])
        assert first["role"] == "system"
        assert first["content"] == SYSTEM_PROMPT

    async def test_history_is_append_only_across_requests(self) -> None:
        """Each request's messages extend the previous request's messages
        as an exact prefix — the prompt-cache invariant."""
        args = '{"command": "ls", "risk": "read_only", "risk_reason": "list"}'
        client, captured = _client_with_fake_stream([
            (
                [],
                _completion(
                    _tool_call_message(_function_call("call_1", "run_command", args)),
                ),
            ),
            ([], _completion(_final_message("one"))),
            ([], _completion(_final_message("two"))),
        ])
        await client.start_query("first", screen_text="$ ")
        client.add_tool_output("call_1", "output")
        await client.step()
        await client.start_query("second", screen_text="$ ", new_output="x")

        for earlier, later in zip(captured, captured[1:]):
            earlier_messages = cast("list[object]", earlier["messages"])
            later_messages = cast("list[object]", later["messages"])
            assert later_messages[: len(earlier_messages)] == earlier_messages

    async def test_prompt_tokens_tracked_from_usage(self) -> None:
        client, _ = _client_with_fake_stream([
            ([], _completion(_final_message("done"), prompt_tokens=1234)),
        ])
        await client.start_query("hi", screen_text="$ ")
        assert client._last_prompt_tokens == 1234

    def test_prompt_size_estimates_without_usage(self) -> None:
        client = LLMClient(_make_config())
        client._messages.append({"role": "user", "content": "x" * 4000})
        assert client._last_prompt_tokens == 0
        assert client._prompt_size() >= 1000


class TestCompaction:
    def _client_for_compaction(
        self,
        parsed: HandoffSummary | None,
        parse_error: Exception | None = None,
    ) -> tuple[LLMClient, list[dict[str, object]], list[dict[str, object]]]:
        client, stream_captured = _client_with_fake_stream([
            ([], _completion(_final_message("done"))),
        ])
        parse_captured: list[dict[str, object]] = []

        async def fake_parse(**kwargs: object) -> object:
            kwargs["messages"] = list(cast("list[object]", kwargs["messages"]))
            parse_captured.append(kwargs)
            if parse_error is not None:
                raise parse_error
            message = ChatCompletionMessage.model_construct(
                role="assistant", parsed=parsed,
            )
            return ChatCompletion.model_construct(
                choices=[Choice.model_construct(message=message)],
            )

        setattr(client._client.chat.completions, "parse", fake_parse)  # noqa: B010
        # A conversation past the threshold.
        client._messages.append({"role": "user", "content": "old query"})
        client._messages.append({"role": "assistant", "content": "old answer"})
        client._last_prompt_tokens = client._compact_thresh
        return client, stream_captured, parse_captured

    async def test_compacts_after_finish_and_resets_history(self) -> None:
        summary = HandoffSummary(environment="env", activities="acts", notes="notes")
        client, stream_captured, parse_captured = self._client_for_compaction(summary)

        # The query's execution ended; compaction runs in the background
        # and the next query picks up its result.
        client.finish_query()
        await client.start_query("next", screen_text="$ prompt")

        # The compact request replayed the old history plus the directive,
        # with the same system prompt and tools (cache-friendly).
        compact = parse_captured[0]
        assert compact["tool_choice"] == "none"
        assert compact["tools"] == TOOLS
        compact_messages = cast("list[object]", compact["messages"])
        assert _msg(compact_messages[0])["content"] == SYSTEM_PROMPT
        assert _msg(compact_messages[1])["content"] == "old query"
        assert _msg(compact_messages[-1])["content"] == COMPACT_REQUEST

        # The history restarted: system + the new query carrying the
        # summary and the full screen.
        assert [_msg(m)["role"] for m in client._messages[:2]] == ["system", "user"]
        content = str(_msg(client._messages[1])["content"])
        assert content.startswith("## Session Summary")
        assert "env" in content
        assert "## Current Terminal Screen" in content
        assert "$ prompt" in content
        assert content.endswith("## Goal\nnext")

    async def test_compaction_failure_keeps_history(self) -> None:
        client, _, parse_captured = self._client_for_compaction(
            None, parse_error=RuntimeError("api down"),
        )
        client.finish_query()
        await client.start_query("next", screen_text="$ ", new_output="delta")

        assert len(parse_captured) == 1
        # Old history kept; the query was appended as a continuation.
        contents = [str(_msg(m).get("content", "")) for m in client._messages]
        assert "old query" in contents
        query_content = contents[3]
        assert "## New Terminal Output" in query_content
        assert "## Session Summary" not in query_content

    async def test_empty_compaction_response_keeps_history(self) -> None:
        client, _, _ = self._client_for_compaction(None)
        client.finish_query()
        await client.start_query("next", screen_text="$ ", new_output=None)
        contents = [str(_msg(m).get("content", "")) for m in client._messages]
        assert "old query" in contents

    async def test_no_compaction_below_threshold(self) -> None:
        client, _, parse_captured = self._client_for_compaction(
            HandoffSummary(environment="e", activities="a", notes="n"),
        )
        client._last_prompt_tokens = 10  # far below the threshold
        client.finish_query()
        assert client._compact_task is None
        await client.start_query("next", screen_text="$ ")
        assert parse_captured == []

    async def test_no_compaction_on_fresh_conversation(self) -> None:
        client = LLMClient(_make_config())
        client._last_prompt_tokens = client._compact_thresh
        client.finish_query()
        assert client._compact_task is None

    async def test_compaction_runs_in_background(self) -> None:
        """finish_query() returns immediately; the compaction task runs
        once control is back on the event loop."""
        summary = HandoffSummary(environment="e", activities="a", notes="n")
        client, _, parse_captured = self._client_for_compaction(summary)
        client.finish_query()
        assert parse_captured == []  # not run yet (synchronous return)
        task = client._compact_task
        assert task is not None
        await task
        assert len(parse_captured) == 1
        assert client._pending_summary is summary


class TestLLMClient:
    def test_init(self) -> None:
        client = LLMClient(_make_config())
        assert client._client.api_key == "sk-test"
        assert [_msg(m)["role"] for m in client._messages] == ["system"]

    def test_system_prompt_exists(self) -> None:
        assert "terminal" in SYSTEM_PROMPT.lower()
        assert "goal" in SYSTEM_PROMPT.lower()
        assert "run_command" in SYSTEM_PROMPT
        assert "send_control" in SYSTEM_PROMPT
        assert "in English by default" in SYSTEM_PROMPT
        assert "language of the query" in SYSTEM_PROMPT
        assert "uname" in SYSTEM_PROMPT
        assert "show version" in SYSTEM_PROMPT
        assert "stop calling tools" in SYSTEM_PROMPT

    async def test_log_query(self) -> None:
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            log_path = f.name
        config = _make_config()
        config.log_file = log_path
        client = LLMClient(config)
        setattr(  # noqa: B010
            client._client.chat.completions,
            "stream",
            lambda **kwargs: _FakeStream([], _completion(_final_message("ok"))),
        )
        await client.start_query("check disk", screen_text="$ ")
        with open(log_path) as f:
            entries = [json.loads(line) for line in f]
        assert entries[0]["type"] == "query"
        assert entries[0]["query"] == "check disk"
        request = next(e for e in entries if e["type"] == "llm_request")
        # Incremental logging: the first request logs the system prompt
        # and the query message.
        assert request["total_messages"] == 2
        assert len(request["new_messages"]) == 2

    def test_no_log_when_log_file_none(self) -> None:
        client = LLMClient(_make_config())
        client._log({"type": "query", "query": "test"})


MCP_TOOL_DEF: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "mcp__DOCS__search",
        "description": "[MCP server DOCS] Search the docs",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


def _skill(name: str, description: str = "does a thing") -> Skill:
    return Skill(
        name=name, description=description, path=f"/skills/{name}", body="steps",
    )


class TestSkills:
    def test_no_skills_means_no_tool_and_no_section(self) -> None:
        client = LLMClient(_make_config())
        assert [t["function"]["name"] for t in client._tools] == [
            "run_command", "send_control",
        ]
        assert _msg(client._messages[0])["content"] == SYSTEM_PROMPT

    def test_skills_add_the_tool_and_the_catalog(self) -> None:
        client = LLMClient(
            _make_config(), skills=[_skill("bgp", "Fix BGP"), _skill("upgrade")],
        )
        assert [t["function"]["name"] for t in client._tools] == [
            "run_command", "send_control", "load_skill",
        ]
        system = cast("str", _msg(client._messages[0])["content"])
        assert system.startswith(SYSTEM_PROMPT)
        assert "load_skill" in system
        # The catalog carries what the model selects on: name + description.
        assert "- bgp: Fix BGP" in system
        assert "- upgrade: does a thing" in system

    def test_load_skill_tool_is_strict(self) -> None:
        function = LOAD_SKILL_TOOL["function"]
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False

    def test_load_skill_call_is_parsed(self) -> None:
        client = LLMClient(_make_config(), skills=[_skill("bgp")])
        parsed = client._parse_tool_call(
            _function_call("call_1", "load_skill", '{"name": "bgp"}'),
        )
        assert parsed.params == LoadSkillParams(name="bgp")

    def test_load_skill_is_unknown_without_skills(self) -> None:
        client = LLMClient(_make_config())
        parsed = client._parse_tool_call(
            _function_call("call_1", "load_skill", '{"name": "bgp"}'),
        )
        assert parsed.params is None
        assert parsed.error == "unknown tool: load_skill"

    def test_mcp_tools_are_added_without_dropping_load_skill(self) -> None:
        client = LLMClient(_make_config(), skills=[_skill("bgp")])
        client.set_mcp_tools([MCP_TOOL_DEF])
        assert [t["function"]["name"] for t in client._tools] == [
            "run_command", "send_control", "load_skill", "mcp__DOCS__search",
        ]
        system = cast("str", _msg(client._messages[0])["content"])
        assert "- bgp: does a thing" in system
        assert "mcp__" in system


class TestMCPTools:
    def test_tools_include_mcp_and_prompt_mentions_them(self) -> None:
        client = LLMClient(_make_config())
        assert [t["function"]["name"] for t in client._tools] == [
            "run_command", "send_control",
        ]
        assert "mcp__" not in cast("str", _msg(client._messages[0])["content"])

        client.set_mcp_tools([MCP_TOOL_DEF])
        assert [t["function"]["name"] for t in client._tools] == [
            "run_command", "send_control", "mcp__DOCS__search",
        ]
        system = cast("str", _msg(client._messages[0])["content"])
        assert system.startswith(SYSTEM_PROMPT)
        assert "mcp__" in system

    def test_no_mcp_tools_leaves_the_prompt_alone(self) -> None:
        client = LLMClient(_make_config())
        client.set_mcp_tools([])
        assert _msg(client._messages[0])["content"] == SYSTEM_PROMPT

    def test_mcp_call_is_parsed_with_raw_arguments(self) -> None:
        client = LLMClient(_make_config())
        client.set_mcp_tools([MCP_TOOL_DEF])
        parsed = client._parse_tool_call(
            _function_call("call_1", "mcp__DOCS__search", '{"q": "bgp", "n": 3}'),
        )
        assert parsed.error is None
        assert parsed.params == MCPToolParams(
            name="mcp__DOCS__search", arguments={"q": "bgp", "n": 3},
        )

    def test_mcp_call_without_arguments(self) -> None:
        client = LLMClient(_make_config())
        client.set_mcp_tools([MCP_TOOL_DEF])
        parsed = client._parse_tool_call(
            _function_call("call_1", "mcp__DOCS__search", ""),
        )
        assert parsed.params == MCPToolParams(
            name="mcp__DOCS__search", arguments={},
        )

    def test_malformed_mcp_arguments_become_an_error(self) -> None:
        client = LLMClient(_make_config())
        client.set_mcp_tools([MCP_TOOL_DEF])
        parsed = client._parse_tool_call(
            _function_call("call_1", "mcp__DOCS__search", "[1, 2]"),
        )
        assert parsed.params is None
        assert parsed.error is not None
        assert "invalid arguments" in parsed.error

    def test_unconfigured_mcp_name_is_unknown(self) -> None:
        client = LLMClient(_make_config())
        parsed = client._parse_tool_call(
            _function_call("call_1", "mcp__DOCS__search", "{}"),
        )
        assert parsed.params is None
        assert parsed.error == "unknown tool: mcp__DOCS__search"

    async def test_mcp_tools_are_sent_with_every_request(self) -> None:
        client, captured = _client_with_fake_stream([
            ([], _completion(_final_message("ok"))),
            ([], _completion(_final_message("ok again"))),
        ])
        client.set_mcp_tools([MCP_TOOL_DEF])
        await client.start_query("first", screen_text="$ ")
        await client.step()
        for request in captured:
            names = [
                t["function"]["name"]
                for t in cast("list[ChatCompletionFunctionToolParam]", request["tools"])
            ]
            assert names[-1] == "mcp__DOCS__search"


class TestAgentsMd:
    def test_no_agents_md_keeps_base_prompt(self) -> None:
        assert build_system_prompt(None) == SYSTEM_PROMPT

    def test_agents_md_is_appended_verbatim(self) -> None:
        prompt = build_system_prompt("Always use `show` commands on rtr1.")
        assert prompt.startswith(SYSTEM_PROMPT)
        assert "Always use `show` commands on rtr1." in prompt
        # The section states where the instructions come from and that they
        # do not override the risk rules.
        assert "AGENTS.md" in prompt
        assert "do not override the tool protocol" in prompt

    def test_client_uses_agents_md_as_system_message(self) -> None:
        client = LLMClient(_make_config(), agents_md="Prefer ip over ifconfig.")
        system = _msg(client._messages[0])
        assert system["role"] == "system"
        assert "Prefer ip over ifconfig." in cast("str", system["content"])

    def test_load_reads_file(self, tmp_path: Path) -> None:
        path = tmp_path / "AGENTS.md"
        path.write_text("# Site notes\nrtr1 is a Junos device.\n")
        assert load_agents_md(str(path)) == "# Site notes\nrtr1 is a Junos device."

    def test_load_missing_file_is_none(self, tmp_path: Path) -> None:
        assert load_agents_md(str(tmp_path / "nonexistent.md")) is None

    def test_load_whitespace_only_file_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "AGENTS.md"
        path.write_text("\n\n  \n")
        assert load_agents_md(str(path)) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
