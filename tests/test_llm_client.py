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
    SUMMARY_SYSTEM_PROMPT,
    SummaryResult,
    SYSTEM_PROMPT,
    _build_summary_input,
    _compose_instructions,
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

    def test_run_command_schema_requires_risk(self) -> None:
        run_command = AUTO_TOOLS[0]
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
        assert "command" in SYSTEM_PROMPT.lower()
        assert "same language" in SYSTEM_PROMPT.lower()

    def test_auto_system_prompt_exists(self) -> None:
        assert "terminal" in AUTO_SYSTEM_PROMPT.lower()
        assert "goal" in AUTO_SYSTEM_PROMPT.lower()
        assert "run_command" in AUTO_SYSTEM_PROMPT
        assert "send_control" in AUTO_SYSTEM_PROMPT
        assert "in English by default" in AUTO_SYSTEM_PROMPT
        assert "language of the query" in AUTO_SYSTEM_PROMPT
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
