import json
import tempfile
from typing import cast

from openai.lib.streaming.responses import ResponseTextDeltaEvent
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.parsed_response import ParsedResponse

from expecto.config import ExpectoConfig
from expecto.llm_client import (
    LLMClient,
    RunCommandParams,
    SendControlParams,
    SUMMARY_SYSTEM_PROMPT,
    SummaryResult,
    SYSTEM_PROMPT,
    TOOLS,
    _build_summary_input,
    _compose_instructions,
)


def _make_config() -> ExpectoConfig:
    return ExpectoConfig(
        shell="/bin/bash",
        model="gpt-4o",
        openai_api_key="sk-test",
        openai_base_url=None,
        openai_reasoning_effort=None,
        history_lines=200,
        settle_ms=500,
        threshold_ms=30000,
        max_steps=10,
        hotkey=0x1F,
        raw_io_log_file=None,
        log_file=None,
    )


class TestToolParams:
    def test_run_command_params(self) -> None:
        params = RunCommandParams(command="ls -la")
        assert params.command == "ls -la"
        assert params.risk == "read_only"

    def test_run_command_params_with_risk(self) -> None:
        params = RunCommandParams(
            command="rm -rf /tmp/work",
            risk="destructive",
            risk_reason="deletes files irreversibly",
        )
        assert params.risk == "destructive"
        assert params.risk_reason == "deletes files irreversibly"

    def test_send_control_params(self) -> None:
        params = SendControlParams(key="ctrl-c")
        assert params.key == "ctrl-c"


class TestTools:
    def test_tool_names(self) -> None:
        names = [tool["name"] for tool in TOOLS]
        assert names == ["run_command", "send_control"]

    def test_tools_are_strict(self) -> None:
        for tool in TOOLS:
            assert tool["strict"] is True
            params = tool["parameters"]
            assert params is not None
            assert params["additionalProperties"] is False

    def test_run_command_schema_requires_risk(self) -> None:
        run_command = TOOLS[0]
        params = run_command["parameters"]
        assert params is not None
        required = cast("list[str]", params["required"])
        assert set(required) == {"command", "risk", "risk_reason"}
        properties = cast("dict[str, dict[str, object]]", params["properties"])
        assert properties["risk"]["enum"] == ["read_only", "mutating", "destructive"]


class TestParseToolCall:
    def test_run_command(self) -> None:
        client = LLMClient(_make_config())
        item = ResponseFunctionToolCall(
            type="function_call",
            call_id="call_1",
            name="run_command",
            arguments='{"command": "df -h", "risk": "read_only", "risk_reason": "only reads disk usage"}',
        )
        parsed = client._parse_tool_call(item)
        assert parsed.call_id == "call_1"
        assert parsed.name == "run_command"
        assert isinstance(parsed.params, RunCommandParams)
        assert parsed.params.command == "df -h"
        assert parsed.params.risk == "read_only"
        assert parsed.error is None

    def test_send_control(self) -> None:
        client = LLMClient(_make_config())
        item = ResponseFunctionToolCall(
            type="function_call",
            call_id="call_2",
            name="send_control",
            arguments='{"key": "ctrl-c"}',
        )
        parsed = client._parse_tool_call(item)
        assert isinstance(parsed.params, SendControlParams)
        assert parsed.params.key == "ctrl-c"
        assert parsed.error is None

    def test_unknown_tool(self) -> None:
        client = LLMClient(_make_config())
        item = ResponseFunctionToolCall(
            type="function_call",
            call_id="call_3",
            name="nonexistent",
            arguments="{}",
        )
        parsed = client._parse_tool_call(item)
        assert parsed.params is None
        assert parsed.error is not None
        assert "unknown tool" in parsed.error

    def test_invalid_arguments(self) -> None:
        client = LLMClient(_make_config())
        item = ResponseFunctionToolCall(
            type="function_call",
            call_id="call_4",
            name="send_control",
            arguments='{"key": "ctrl-x"}',
        )
        parsed = client._parse_tool_call(item)
        assert parsed.params is None
        assert parsed.error is not None
        assert "invalid arguments" in parsed.error


def _text_delta_event(delta: str, snapshot: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        type="response.output_text.delta",
        content_index=0,
        delta=delta,
        item_id="msg_1",
        logprobs=[],
        output_index=0,
        sequence_number=0,
        snapshot=snapshot,
    )


def _message_item(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _run_command_item(call_id: str, command: str) -> ResponseFunctionToolCall:
    arguments = json.dumps(
        {"command": command, "risk": "read_only", "risk_reason": "test"},
    )
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name="run_command",
        arguments=arguments,
    )


def _parsed_response(
    output: list[object], response_id: str = "resp_1",
) -> ParsedResponse[None]:
    return cast(
        "ParsedResponse[None]",
        ParsedResponse.model_construct(id=response_id, output=output),
    )


class _FakeStream:
    """Stands in for the AsyncResponseStreamManager returned by
    client.responses.stream(): async context manager, async iterator of
    events, and get_final_response()."""

    def __init__(self, events: list[object], final: ParsedResponse[None]) -> None:
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

    async def get_final_response(self) -> ParsedResponse[None]:
        return self._final


class TestStep:
    def _client_with_fake_stream(
        self,
        events: list[object],
        final: ParsedResponse[None],
    ) -> tuple[LLMClient, dict[str, object]]:
        client = LLMClient(_make_config())
        captured: dict[str, object] = {}

        def fake_stream(**kwargs: object) -> _FakeStream:
            captured.update(kwargs)
            return _FakeStream(events, final)

        setattr(client._client.responses, "stream", fake_stream)  # noqa: B010
        return client, captured

    async def test_streams_deltas_and_returns_final_text(self) -> None:
        events: list[object] = [
            _text_delta_event("Hello ", "Hello "),
            _text_delta_event("world", "Hello world"),
        ]
        final = _parsed_response([_message_item("Hello world")])
        client, _ = self._client_with_fake_stream(events, final)

        chunks: list[str] = []
        result = await client.step(
            [{"role": "user", "content": "hi"}], on_delta=chunks.append,
        )
        assert chunks == ["Hello ", "world"]
        assert result.final_text == "Hello world"
        assert result.tool_calls == []
        assert result.response_id == "resp_1"

    async def test_works_without_on_delta(self) -> None:
        events: list[object] = [_text_delta_event("hi", "hi")]
        final = _parsed_response([_message_item("hi")])
        client, _ = self._client_with_fake_stream(events, final)

        result = await client.step([{"role": "user", "content": "hi"}])
        assert result.final_text == "hi"

    async def test_tool_call_response_has_no_final_text(self) -> None:
        final = _parsed_response([_run_command_item("call_1", "df -h")])
        client, _ = self._client_with_fake_stream([], final)

        chunks: list[str] = []
        result = await client.step(
            [{"role": "user", "content": "check disk"}], on_delta=chunks.append,
        )
        assert chunks == []
        assert result.final_text is None
        assert len(result.tool_calls) == 1
        assert isinstance(result.tool_calls[0].params, RunCommandParams)
        assert result.tool_calls[0].params.command == "df -h"

    async def test_request_parameters(self) -> None:
        final = _parsed_response([_message_item("done")])
        client, captured = self._client_with_fake_stream([], final)

        await client.step(
            [{"role": "user", "content": "hi"}],
            previous_response_id="resp_0",
        )
        assert captured["model"] == "gpt-4o"
        assert captured["instructions"] == SYSTEM_PROMPT
        assert captured["tools"] == TOOLS
        assert captured["parallel_tool_calls"] is False
        assert captured["store"] is True
        assert captured["previous_response_id"] == "resp_0"


class TestSummaryResult:
    def test_model_fields(self) -> None:
        result = SummaryResult(
            environment="local shell (bash)",
            activities="editing config files",
            notes="no errors",
        )
        assert result.environment == "local shell (bash)"
        assert result.activities == "editing config files"
        assert result.notes == "no errors"


class TestComposeInstructions:
    def test_without_summary(self) -> None:
        assert _compose_instructions(SYSTEM_PROMPT, None) == SYSTEM_PROMPT

    def test_with_summary(self) -> None:
        summary = "## Session Context\nEnvironment: ssh router"
        composed = _compose_instructions(SYSTEM_PROMPT, summary)
        assert composed.startswith(SYSTEM_PROMPT)
        assert composed.endswith(summary)


class TestBuildSummaryInput:
    def test_first_generation_has_no_previous_section(self) -> None:
        content = _build_summary_input(None, "some output", truncated=False)
        assert "## Previous Summary" not in content
        assert "some output" in content

    def test_merge_includes_previous_summary(self) -> None:
        previous = SummaryResult(
            environment="ssh to router1 (Junos)",
            activities="checking BGP sessions",
            notes="session to peer 10.0.0.1 is down",
        )
        content = _build_summary_input(previous, "new output", truncated=False)
        assert "## Previous Summary" in content
        assert "Junos" in content
        assert "10.0.0.1" in content
        assert "new output" in content

    def test_truncated_note(self) -> None:
        content = _build_summary_input(None, "output", truncated=True)
        assert "omitted" in content

    def test_not_truncated_has_no_note(self) -> None:
        content = _build_summary_input(None, "output", truncated=False)
        assert "omitted" not in content


class TestLLMClient:
    def test_init(self) -> None:
        client = LLMClient(_make_config())
        assert client._client.api_key == "sk-test"

    def test_summary_client_uses_summary_settings(self) -> None:
        config = _make_config()
        config.summary_openai_api_key = "sk-summary"
        config.summary_openai_base_url = "https://summary.api/v1"
        client = LLMClient(config)
        assert client._summary_client.api_key == "sk-summary"
        assert str(client._summary_client.base_url).startswith("https://summary.api/v1")
        assert client._client.api_key == "sk-test"

    def test_summary_system_prompt_exists(self) -> None:
        assert "summary" in SUMMARY_SYSTEM_PROMPT.lower()
        assert "environment" in SUMMARY_SYSTEM_PROMPT.lower()
        assert "merging" in SUMMARY_SYSTEM_PROMPT.lower()

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

    def test_build_initial_messages(self) -> None:
        client = LLMClient(_make_config())
        messages = client.build_initial_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
        )
        assert len(messages) == 1
        first = cast("dict[str, object]", messages[0])
        assert first["role"] == "user"
        content = str(first["content"])
        assert "ディスク使用量を確認して" in content
        assert "user@host:~$ " in content

    def test_log_query_on_build_initial(self) -> None:
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            log_path = f.name
        config = _make_config()
        config.log_file = log_path
        client = LLMClient(config)
        client.build_initial_messages(query="check disk", screen_text="$ ")
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "query"
        assert entry["query"] == "check disk"
        assert "timestamp" in entry

    def test_no_log_when_log_file_none(self) -> None:
        client = LLMClient(_make_config())
        client.build_initial_messages(query="test", screen_text="$ ")
