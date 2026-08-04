from jou.config import JouConfig
from jou.llm_client import CompletionResponse, LLMClient, SYSTEM_PROMPT


class TestCompletionResponse:
    def test_model_fields(self) -> None:
        resp = CompletionResponse(command="ls -la", explanation="list files")
        assert resp.command == "ls -la"
        assert resp.explanation == "list files"


class TestLLMClient:
    def test_init(self) -> None:
        config = JouConfig(
            shell="/bin/bash",
            model="gpt-4o",
            openai_api_key="sk-test",
            openai_base_url=None,
            openai_reasoning_effort=None,
            history_lines=200,
            completion_hotkey=0x1E,
            auto_hotkey=0x1D,
            log_file=None,
        )
        client = LLMClient(config)
        assert client._client.api_key == "sk-test"

    def test_system_prompt_exists(self) -> None:
        assert "ターミナル" in SYSTEM_PROMPT
        assert "コマンド" in SYSTEM_PROMPT
