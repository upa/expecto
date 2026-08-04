from __future__ import annotations

import json
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
- 次のコマンドを実行する場合は status を "continue" にし、action にコマンドまたは制御文字を指定してください。
- ゴールが達成できたら status を "done" にしてください。action は不要です。
- エラーが発生して回復不能な場合は status を "fail" にしてください。action は不要です。
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
    action: AutoAction | None = None
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
            return self._fallback_parse(e)

        parsed = response.choices[0].message.parsed
        if parsed is None:
            content = response.choices[0].message.content
            if content:
                return self._parse_raw_content(content)
            return AutoResponse(
                status="fail",
                explanation="LLM returned empty response",
            )
        return parsed

    def _fallback_parse(self, original_error: ValidationError) -> AutoResponse:
        """Try raw_decode to handle trailing characters in LLM response."""
        error_str = str(original_error)
        input_start = error_str.find("input_value='")
        if input_start < 0:
            return AutoResponse(
                status="fail",
                explanation=f"LLM response parse error: {original_error}",
            )
        raw = error_str[input_start + len("input_value='"):]
        raw = raw.rsplit("'", 1)[0]
        return self._parse_raw_content(raw)

    def _parse_raw_content(self, content: str) -> AutoResponse:
        """Parse JSON from content, tolerating trailing characters."""
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(content.strip())
            return AutoResponse.model_validate(obj)
        except (json.JSONDecodeError, ValidationError) as e:
            return AutoResponse(
                status="fail",
                explanation=f"LLM response parse error: {e}",
            )

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
