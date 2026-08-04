from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import openai
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from jou.config import JouConfig

SYSTEM_PROMPT = """\
あなたはユーザーの操作しているターミナル（またはSSH先、あるいはネットワーク機器のCLI）\
に対してコマンドを提案するアシスタントです。
- 直近のターミナル画面の内容を参照し、現在どのシステム/シェル/CLIに接続されているかを判断してください。
- 1つのコマンドだけを提案してください。実行はユーザーが行います。
"""

AUTO_SYSTEM_PROMPT = """\
あなたはユーザーの操作しているターミナル（またはSSH先、あるいはネットワーク機器のCLI）\
に対してコマンドを実行するアシスタントです。
- ユーザーが指定したゴールを達成するために、コマンドを1つずつ実行してください。
- 各ステップで、前のコマンドの出力を確認し、次のアクションを判断してください。
- ゴールが達成できたら status を "done" にしてください。
- エラーが発生して回復不能な場合は status を "fail" にしてください。
- コマンドが長時間実行されている場合、action.type を "control" にして \
"ctrl-c" 等の制御文字を送信できます。
"""


class CompletionResponse(BaseModel):
    command: str
    explanation: str


class AutoAction(BaseModel):
    type: Literal["command", "control"]
    value: str


class AutoResponse(BaseModel):
    action: AutoAction
    status: Literal["continue", "done", "fail"]
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

    def auto_step(self, messages: list[ChatCompletionMessageParam]) -> AutoResponse:
        try:
            response = self._client.beta.chat.completions.parse(
                model=self._config.model,
                messages=messages,
                response_format=AutoResponse,
                reasoning_effort=self._config.openai_reasoning_effort,
            )
        except ValidationError as e:
            return AutoResponse(
                action=AutoAction(type="command", value=""),
                status="fail",
                explanation=f"LLM response parse error: {e}",
            )

        parsed = response.choices[0].message.parsed
        if parsed is None:
            return AutoResponse(
                action=AutoAction(type="command", value=""),
                status="fail",
                explanation="LLM returned empty response",
            )
        return parsed

    def build_auto_messages(
        self,
        query: str,
        screen_text: str,
        history: list[ChatCompletionMessageParam],
    ) -> list[ChatCompletionMessageParam]:
        user_content = f"## ゴール\n{query}\n\n## 現在のターミナル画面\n```\n{screen_text}\n```"
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": AUTO_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        messages.extend(history)
        return messages
