from __future__ import annotations

from typing import TYPE_CHECKING

import openai
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

if TYPE_CHECKING:
    from jou.config import JouConfig

SYSTEM_PROMPT = """\
あなたはユーザーの操作しているターミナル（またはSSH先、あるいはネットワーク機器のCLI）\
に対してコマンドを提案するアシスタントです。
- 直近のターミナル画面の内容を参照し、現在どのシステム/シェル/CLIに接続されているかを判断してください。
- 1つのコマンドだけを提案してください。実行はユーザーが行います。
"""


class CompletionResponse(BaseModel):
    command: str
    explanation: str


class LLMClient:
    def __init__(self, config: JouConfig) -> None:
        self._config = config
        self._client: openai.OpenAI = openai.OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )

    def complete(self, screen_text: str, query: str) -> CompletionResponse:
        user_content = f"## ターミナル画面\n```\n{screen_text}\n```\n\n## ユーザーのリクエスト\n{query}"
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = self._client.beta.chat.completions.parse(
            model=self._config.model,
            messages=messages,
            response_format=CompletionResponse,
            reasoning_effort=self._config.openai_reasoning_effort,
        )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            return CompletionResponse(command="", explanation="LLM returned empty response")
        return parsed
