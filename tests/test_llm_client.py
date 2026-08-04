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
        assert resp.action.type == "command"
        assert resp.action.value == "df -h"
        assert resp.status == "continue"

    def test_done_response(self) -> None:
        resp = AutoResponse(
            action=AutoAction(type="command", value=""),
            status="done",
            explanation="goal achieved",
        )
        assert resp.status == "done"

    def test_fail_response(self) -> None:
        resp = AutoResponse(
            action=AutoAction(type="command", value=""),
            status="fail",
            explanation="unrecoverable error",
        )
        assert resp.status == "fail"


class TestLLMClient:
    def test_init(self) -> None:
        client = LLMClient(_make_config())
        assert client._client.api_key == "sk-test"

    def test_system_prompt_exists(self) -> None:
        assert "ターミナル" in SYSTEM_PROMPT
        assert "コマンド" in SYSTEM_PROMPT

    def test_auto_system_prompt_exists(self) -> None:
        assert "ターミナル" in AUTO_SYSTEM_PROMPT
        assert "ゴール" in AUTO_SYSTEM_PROMPT
        assert "control" in AUTO_SYSTEM_PROMPT

    def test_build_auto_messages_initial(self) -> None:
        client = LLMClient(_make_config())
        messages = client.build_auto_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
            history=[],
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == AUTO_SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        content = str(messages[1]["content"])
        assert "ディスク使用量を確認して" in content
        assert "user@host:~$ " in content

    def test_build_auto_messages_with_history(self) -> None:
        client = LLMClient(_make_config())
        history = [
            {"role": "assistant", "content": '{"action":{"type":"command","value":"df -h"},"status":"continue","explanation":"checking"}'},
            {"role": "user", "content": "## コマンド実行結果\n```\n/dev/sda1  50G  20G  30G  40% /\n```"},
        ]
        messages = client.build_auto_messages(
            query="ディスク使用量を確認して",
            screen_text="user@host:~$ ",
            history=history,
        )
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
        assert messages[3]["role"] == "user"

    def test_build_auto_messages_query_always_first(self) -> None:
        client = LLMClient(_make_config())
        history = [
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
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "setup nginx" in str(messages[1]["content"])
        assert len(messages) == 6
