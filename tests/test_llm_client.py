import json
import tempfile
from typing import cast

from openai.types.responses import ResponseFunctionToolCall

from jou.config import JouConfig
from jou.llm_client import (
    AUTO_SYSTEM_PROMPT,
    AUTO_TOOLS,
    AutoResponse,
    CompletionResponse,
    LLMClient,
    RunCommandParams,
    SendControlParams,
    SYSTEM_PROMPT,
)


def _make_config() -> JouConfig:
    return JouConfig(
        shell="/bin/bash",
        model="gpt-4o",
        openai_api_key="sk-test",
        openai_base_url=None,
        openai_reasoning_effort=None,
        history_lines=200,
        settle_ms=500,
        threshold_ms=30000,
        max_auto_steps=10,
        completion_hotkey=0x1F,
        auto_hotkey=0x1D,
        raw_io_log_file=None,
        log_file=None,
    )


class TestCompletionResponse:
    def test_model_fields(self) -> None:
        resp = CompletionResponse(command="ls -la", explanation="list files")
        assert resp.command == "ls -la"
        assert resp.explanation == "list files"


class TestToolParams:
    def test_run_command_params(self) -> None:
        params = RunCommandParams(command="ls -la")
        assert params.command == "ls -la"

    def test_send_control_params(self) -> None:
        params = SendControlParams(key="ctrl-c")
        assert params.key == "ctrl-c"


class TestAutoResponse:
    def test_done_response(self) -> None:
        resp = AutoResponse(
            status="done",
            explanation="goal achieved",
        )
        assert resp.status == "done"

    def test_fail_response(self) -> None:
        resp = AutoResponse(
            status="fail",
            explanation="unrecoverable error",
        )
        assert resp.status == "fail"


class TestAutoTools:
    def test_tool_names(self) -> None:
        names = [tool["name"] for tool in AUTO_TOOLS]
        assert names == ["run_command", "send_control"]

    def test_tools_are_strict(self) -> None:
        for tool in AUTO_TOOLS:
            assert tool["strict"] is True
            params = tool["parameters"]
            assert params is not None
            assert params["additionalProperties"] is False


class TestParseToolCall:
    def test_run_command(self) -> None:
        client = LLMClient(_make_config())
        item = ResponseFunctionToolCall(
            type="function_call",
            call_id="call_1",
            name="run_command",
            arguments='{"command": "df -h"}',
        )
        parsed = client._parse_tool_call(item)
        assert parsed.call_id == "call_1"
        assert parsed.name == "run_command"
        assert isinstance(parsed.params, RunCommandParams)
        assert parsed.params.command == "df -h"
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


class TestLLMClient:
    def test_init(self) -> None:
        client = LLMClient(_make_config())
        assert client._client.api_key == "sk-test"

    def test_system_prompt_exists(self) -> None:
        assert "terminal" in SYSTEM_PROMPT.lower()
        assert "command" in SYSTEM_PROMPT.lower()
        assert "same language" in SYSTEM_PROMPT.lower()

    def test_auto_system_prompt_exists(self) -> None:
        assert "terminal" in AUTO_SYSTEM_PROMPT.lower()
        assert "goal" in AUTO_SYSTEM_PROMPT.lower()
        assert "run_command" in AUTO_SYSTEM_PROMPT
        assert "send_control" in AUTO_SYSTEM_PROMPT
        assert "same language" in AUTO_SYSTEM_PROMPT.lower()
        assert "uname" in AUTO_SYSTEM_PROMPT
        assert "show version" in AUTO_SYSTEM_PROMPT

    def test_build_auto_messages(self) -> None:
        client = LLMClient(_make_config())
        messages = client.build_auto_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
        )
        assert len(messages) == 1
        first = cast("dict[str, object]", messages[0])
        assert first["role"] == "user"
        content = str(first["content"])
        assert "ディスク使用量を確認して" in content
        assert "user@host:~$ " in content

    def test_log_query_on_build_auto(self) -> None:
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            log_path = f.name
        config = _make_config()
        config.log_file = log_path
        client = LLMClient(config)
        client.build_auto_messages(query="check disk", screen_text="$ ")
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "query"
        assert entry["mode"] == "auto"
        assert entry["query"] == "check disk"
        assert "timestamp" in entry

    def test_no_log_when_log_file_none(self) -> None:
        client = LLMClient(_make_config())
        client.build_auto_messages(query="test", screen_text="$ ")
