"""Infrastructure smoke tests: verify the e2e foundation itself —
llama-server serves an OpenAI-compatible API, expecto runs in a pty and
relays a shell, and one query makes the full LLM round trip. Real
end-to-end scenarios will be added separately.
"""
from __future__ import annotations

import json
import urllib.request

from conftest import ExpectoSession


class TestLlamaServer:
    def test_chat_completion_works(self, llama_server: str) -> None:
        payload = {
            "model": "qwen3-0.6b",
            "messages": [{"role": "user", "content": "Reply with the word OK."}],
            "max_tokens": 32,
        }
        request = urllib.request.Request(
            f"{llama_server}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            assert resp.status == 200
            body = json.load(resp)
        content = body["choices"][0]["message"]["content"]
        assert isinstance(content, str) and content.strip()


class TestExpectoPassthrough:
    def test_shell_commands_pass_through(self, expecto_session: ExpectoSession) -> None:
        """expecto relays a wrapped bash transparently."""
        expecto_session.wait_prompt()
        expecto_session.send(b"echo hello-e2e\r")
        # "\r" + result distinguishes the output line (column 0) from the
        # echoed command line ("echo hello-e2e" after the prompt).
        expecto_session.expect(b"\rhello-e2e", timeout=15)

        expecto_session.send(b"exit\r")
        assert expecto_session.wait_exit(timeout=15) == 0


class TestQueryRoundTrip:
    def test_query_executes_a_command(self, expecto_session: ExpectoSession) -> None:
        """A query goes to the local LLM, which calls run_command; the
        command's marker shows up on the terminal."""
        expecto_session.wait_prompt()
        expecto_session.query("Run exactly this command: echo E2E_MARKER_7")
        # CPU inference is slow; be generous.
        expecto_session.expect(b"E2E_MARKER_7", timeout=300)
