import json
import tempfile

from openai.types.responses import EasyInputMessageParam

from jou.config import JouConfig
from jou.llm_client import (
    AUTO_SYSTEM_PROMPT,
    AutoAction,
    AutoResponse,
    CompletionResponse,
    LLMClient,
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


class TestAutoAction:
    def test_command_action(self) -> None:
        action = AutoAction(type="command", value="ls -la")
        assert action.type == "command"
        assert action.value == "ls -la"

    def test_control_action(self) -> None:
        action = AutoAction(type="control", value="ctrl-c")
        assert action.type == "control"
        assert action.value == "ctrl-c"


class TestAutoResponse:
    def test_continue_response(self) -> None:
        resp = AutoResponse(
            action=AutoAction(type="command", value="df -h"),
            status="continue",
            explanation="checking disk usage",
        )
        assert resp.action is not None
        assert resp.action.type == "command"
        assert resp.action.value == "df -h"
        assert resp.status == "continue"

    def test_done_response(self) -> None:
        resp = AutoResponse(
            status="done",
            explanation="goal achieved",
        )
        assert resp.status == "done"
        assert resp.action is None

    def test_fail_response(self) -> None:
        resp = AutoResponse(
            status="fail",
            explanation="unrecoverable error",
        )
        assert resp.status == "fail"
        assert resp.action is None


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
        assert "control" in AUTO_SYSTEM_PROMPT.lower()
        assert "same language" in AUTO_SYSTEM_PROMPT.lower()

    def test_build_auto_messages_initial(self) -> None:
        client = LLMClient(_make_config())
        messages = client.build_auto_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
            history=[],
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        content = str(messages[0]["content"])
        assert "ディスク使用量を確認して" in content
        assert "user@host:~$ " in content

    def test_build_auto_messages_with_history(self) -> None:
        client = LLMClient(_make_config())
        history: list[EasyInputMessageParam] = [
            {"role": "assistant", "content": '{"action":{"type":"command","value":"df -h"},"status":"continue","explanation":"checking"}'},
            {"role": "user", "content": "## コマンド実行結果\n```\n/dev/sda1  50G  20G  30G  40% /\n```"},
        ]
        messages = client.build_auto_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
            history=history,
        )
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"

    def test_build_auto_messages_query_always_first(self) -> None:
        client = LLMClient(_make_config())
        history: list[EasyInputMessageParam] = [
            {"role": "assistant", "content": "action1"},
            {"role": "user", "content": "output1"},
            {"role": "assistant", "content": "action2"},
            {"role": "user", "content": "output2"},
        ]
        messages = client.build_auto_messages(
            query="setup nginx",
            screen_text="$ ",
            history=history,
        )
        assert messages[0]["role"] == "user"
        assert "setup nginx" in str(messages[0]["content"])
        assert len(messages) == 5

    def test_log_query_on_build_auto_initial(self) -> None:
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            log_path = f.name
        config = _make_config()
        config.log_file = log_path
        client = LLMClient(config)
        client.build_auto_messages(query="check disk", screen_text="$ ", history=[])
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "query"
        assert entry["mode"] == "auto"
        assert entry["query"] == "check disk"
        assert "timestamp" in entry

    def test_no_log_query_on_build_auto_with_history(self) -> None:
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            log_path = f.name
        config = _make_config()
        config.log_file = log_path
        client = LLMClient(config)
        history: list[EasyInputMessageParam] = [
            {"role": "assistant", "content": "action"},
            {"role": "user", "content": "output"},
        ]
        client.build_auto_messages(query="check disk", screen_text="$ ", history=history)
        with open(log_path) as f:
            content = f.read()
        assert content == ""

    def test_no_log_when_log_file_none(self) -> None:
        client = LLMClient(_make_config())
        client.build_auto_messages(query="test", screen_text="$ ", history=[])
