"""End-to-end scenarios: expecto used the way a user would use it.

Primary assertions are on what appears on the terminal (the user-visible
behavior); the JSONL log is consulted only for what the screen cannot
show — compaction, and how a declined command was recorded.

To stay reliable with a small local model, queries always name the
run_command tool explicitly, and the destructive scenario uses a
harmless `echo` escalated by a test-supplied dangerous pattern — the
deterministic backstop, not the model's own risk classification.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import cast

from conftest import ExpectoSession, MakeExpecto

# One LLM step takes a few seconds on CPU with the 0.6B model; 90s is
# already generous, and it bounds how long a failing test can hang.
LLM_TIMEOUT = 90.0


def _final_response_logged(entry: dict[str, object]) -> bool:
    return entry.get("type") == "llm_response" and bool(entry.get("final_text"))


class TestSequentialQueries:
    def test_three_queries_complete_in_one_session(
        self, expecto_session: ExpectoSession,
    ) -> None:
        """Queries run and complete repeatedly within a single session."""
        session = expecto_session
        session.wait_prompt()
        for marker in ("E2E_SEQ_A", "E2E_SEQ_B", "E2E_SEQ_C"):
            start = session.cursor()
            # Naming the tool keeps the small model from "answering" that
            # it ran the command without actually calling the tool.
            session.enter_query(
                f"Use the run_command tool to run: echo {marker}",
            )
            # "\r" + marker: the command's output line, as opposed to the
            # echoed command or the query text.
            session.expect(b"\r" + marker.encode(), timeout=LLM_TIMEOUT, start=start)

        queries = [e for e in session.read_log() if e.get("type") == "query"]
        assert len(queries) == 3


class TestCancelDuringLLMStep:
    def test_ctrl_c_while_thinking_interrupts(
        self, expecto_session: ExpectoSession,
    ) -> None:
        """Ctrl-C while the LLM call is in flight cancels the execution,
        and the terminal keeps working afterwards."""
        session = expecto_session
        session.wait_prompt()
        session.enter_query("Use the run_command tool to run: echo NEVER_RUN_MARKER")
        session.expect(b"[thinking...]", timeout=60)
        session.send(b"\x03")  # Ctrl-C
        session.expect(b"[expecto: interrupted]", timeout=60)

        # Passthrough is alive again.
        start = session.cursor()
        session.wait_prompt()
        session.send(b"echo AFTER_CANCEL\r")
        session.expect(b"\rAFTER_CANCEL", timeout=30, start=start)


class TestDestructiveConfirmation:
    """The command is a plain echo the model happily runs; a
    test-supplied dangerous pattern escalates it to destructive, so the
    gate is exercised deterministically."""

    QUERY = "Use the run_command tool to run: echo E2E_DANGER_RAN"

    def _make(self, make_expecto: MakeExpecto, tmp_path: Path) -> ExpectoSession:
        patterns = tmp_path / "patterns.txt"
        patterns.write_text("E2E_DANGER\n")
        return make_expecto(f'dangerous_patterns_file = "{patterns}"\n')

    def test_answer_y_runs_the_command(
        self, make_expecto: MakeExpecto, tmp_path: Path,
    ) -> None:
        session = self._make(make_expecto, tmp_path)
        session.wait_prompt()
        session.enter_query(self.QUERY)
        session.expect(b"destructive command", timeout=LLM_TIMEOUT)
        start = session.cursor()
        session.send(b"y")
        # The command's output line proves it actually ran.
        session.expect(b"\rE2E_DANGER_RAN", timeout=60, start=start)

    def test_answer_n_skips_the_command(
        self, make_expecto: MakeExpecto, tmp_path: Path,
    ) -> None:
        session = self._make(make_expecto, tmp_path)
        session.wait_prompt()
        session.enter_query(self.QUERY)
        session.expect(b"destructive command", timeout=LLM_TIMEOUT)
        session.send(b"n")

        # The decline is fed back to the LLM, which may try the same
        # command again — keep answering "n" until it gives up with a
        # final response.
        answered = 1
        deadline = time.time() + LLM_TIMEOUT
        while not any(_final_response_logged(e) for e in session.read_log()):
            assert time.time() < deadline, (
                f"no final response after declining; output:\n"
                f"{bytes(session.output)!r}"
            )
            prompts = session.output.count(b"run? (y/n)")
            if prompts > answered:
                session.send(b"n")
                answered += 1
            session.drain(0.2)

        # The command never ran: its output line (column 0) is absent.
        # (The marker itself appears in the echoed query and prompts.)
        assert b"\rE2E_DANGER_RAN" not in session.output
        # ...and the decline was recorded as the tool's result.
        requests = [
            e for e in session.read_log() if e.get("type") == "llm_request"
        ]
        assert any(
            "declined" in json.dumps(e).lower() for e in requests
        ), "no declined tool output found in the conversation"


class TestCheckpointConfirmation:
    """max_steps = 1 makes the checkpoint fire right after the first
    tool-call step — the same code path as any other max_steps value,
    but with no dependence on how the model splits work into steps
    (models freely batch tool calls or finish early, which made a
    two-step scenario unstable)."""

    QUERY = "Use the run_command tool to run: echo STEP_ONE_DONE"

    def _start(self, make_expecto: MakeExpecto) -> ExpectoSession:
        # confirm_level = none: the tiny model over-classifies harmless
        # commands as destructive, and a surprise y/n prompt would derail
        # this scenario — the dangerous gate has its own tests.
        session = make_expecto('max_steps = 1\nconfirm_level = "none"\n')
        session.wait_prompt()
        session.enter_query(self.QUERY)
        session.expect(b"reached step 1", timeout=LLM_TIMEOUT)
        session.expect(b"continue? (y/n)", timeout=10)
        return session

    def test_answer_y_continues(self, make_expecto: MakeExpecto) -> None:
        """Answering y at the checkpoint lets the loop run to its normal
        completion."""
        session = self._start(make_expecto)
        # The command already ran before the checkpoint.
        assert b"\rSTEP_ONE_DONE" in session.output
        session.send(b"y")
        session.wait_log(
            _final_response_logged, timeout=LLM_TIMEOUT,
            description="final response after continuing",
        )

    def test_answer_n_stops(self, make_expecto: MakeExpecto) -> None:
        session = self._start(make_expecto)
        session.send(b"n")
        session.expect(b"[expecto: stopped at step 1]", timeout=30)

        # Back to passthrough.
        start = session.cursor()
        session.send(b"echo AFTER_STOP\r")
        session.expect(b"\rAFTER_STOP", timeout=30, start=start)


class TestSendKeys:
    def test_llm_stops_a_running_program_with_ctrl_c(
        self, expecto_session: ExpectoSession,
    ) -> None:
        """A program the user started never finishes; asked to stop it,
        the LLM calls send_keys with <C-c> and the shell prompt comes
        back."""
        session = expecto_session
        session.wait_prompt()
        # The user starts a command that will not finish by itself.
        session.send(b"sleep 1000\r")
        session.drain(0.5)

        session.enter_query(
            "The terminal is running a command that will not finish. "
            "Use the send_keys tool to send <C-c> to stop it.",
        )
        # The LLM's Ctrl-C reaches the shell: bash shows ^C and prompts.
        start = session.output.index(b"sleep 1000")
        session.expect(b"^C", timeout=LLM_TIMEOUT, start=start)
        # The send_keys call is recorded with the notation it sent.
        entry = session.wait_log(
            lambda e: e.get("type") == "llm_response"
            and any(
                c.get("name") == "send_keys" and "C-c" in str(c.get("params", ""))
                for c in cast("list[dict[str, object]]", e.get("tool_calls") or [])
            ),
            timeout=LLM_TIMEOUT,
            description="a send_keys tool call carrying <C-c>",
        )
        assert entry is not None

        # The shell is usable again: wait for the execution to finish,
        # then run a command through passthrough.
        session.wait_log(
            lambda e: e.get("type") == "llm_response" and bool(e.get("final_text")),
            timeout=LLM_TIMEOUT,
            description="final response after the interrupt",
        )
        start = session.cursor()
        session.send(b"echo AFTER_INTERRUPT\r")
        session.expect(b"\rAFTER_INTERRUPT", timeout=30, start=start)


    def test_pending_input_is_submitted_by_the_nudge(
        self, make_expecto: MakeExpecto,
    ) -> None:
        """Input the user typed but did not submit is submitted by the
        prompt nudge, error and all — the accepted cost of not clearing
        the line first — and the query runs regardless."""
        session = make_expecto('confirm_level = "none"\n')
        session.wait_prompt()
        # A misspelled command, Enter deliberately not pressed.
        session.send(b"ehco BAD_PENDING")
        session.drain(0.5)

        session.enter_query("Use the run_command tool to run: echo GOOD_OK")
        session.expect(b"\rGOOD_OK", timeout=LLM_TIMEOUT)
        # The typo ran and failed, which is the accepted cost.
        assert b"command not found" in session.output

class TestMCP:
    def test_mcp_server_tool_is_called(self, make_expecto: MakeExpecto) -> None:
        """expecto connects to the configured stdio MCP server at session
        start, offers its tool to the LLM, dispatches the call, and the
        tool's result reaches the conversation."""
        server_py = Path(__file__).with_name("mcp_server.py")
        session = make_expecto(
            "\n[mcp.e2e]\n"
            'type = "stdio"\n'
            f'command = "{sys.executable}"\n'
            f'args = ["{server_py}"]\n',
        )
        session.wait_prompt()
        session.enter_query(
            "Use the mcp__e2e__get_marker tool to get the marker, "
            "then tell me what it returned.",
        )
        # The dispatch is announced on the terminal...
        session.expect(b"[expecto: mcp e2e.get_marker]", timeout=LLM_TIMEOUT)
        # ...and the tool's result is recorded in the conversation (it is
        # never printed to the terminal by expecto itself, so this part
        # is asserted on the log).
        session.wait_log(
            lambda e: e.get("type") == "llm_request"
            and "MCP_MARKER_31337" in json.dumps(e),
            timeout=LLM_TIMEOUT,
            description="tool result carrying the MCP marker",
        )


class TestSkills:
    def test_skill_is_listed_loaded_and_followed(
        self, make_expecto: MakeExpecto, tmp_path: Path,
    ) -> None:
        """A skill in skills_dir is cataloged in the system prompt; the
        LLM loads it with load_skill and follows its instructions."""
        skill_dir = tmp_path / "skills" / "e2e-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: e2e-skill\n"
            "description: How to print the e2e skill marker.\n"
            "---\n"
            "You must now use the run_command tool to run: "
            "echo SKILL_MARKER_777\n",
        )
        # confirm_level = none: the tiny model over-classifies harmless
        # commands as destructive (the dangerous gate has its own tests).
        session = make_expecto(
            f'skills_dir = "{tmp_path / "skills"}"\nconfirm_level = "none"\n',
        )
        session.wait_prompt()
        session.enter_query(
            "Use the load_skill tool to load the skill named e2e-skill, "
            "then follow its instructions.",
        )
        # The dispatch is announced on the terminal...
        session.expect(b"[expecto: skill e2e-skill]", timeout=LLM_TIMEOUT)
        # ...and following the loaded instructions runs the command.
        session.expect(b"\rSKILL_MARKER_777", timeout=LLM_TIMEOUT)

        # The catalog was in the system prompt of the first request.
        first_request = next(
            e for e in session.read_log() if e.get("type") == "llm_request"
        )
        system = json.dumps(first_request)
        assert "e2e-skill" in system
        assert "How to print the e2e skill marker." in system


class TestCompaction:
    def test_compaction_runs_and_the_session_continues(
        self, make_expecto: MakeExpecto,
    ) -> None:
        """With compact_thresh = 1, every query end triggers a background
        compaction. Invisible on screen, so this is asserted on the log;
        the next query must still work on the compacted conversation."""
        session = make_expecto("compact_thresh = 1\n")
        session.wait_prompt()

        start = session.cursor()
        session.enter_query("Use the run_command tool to run: echo E2E_CMP_A")
        session.expect(b"\rE2E_CMP_A", timeout=LLM_TIMEOUT, start=start)

        session.wait_log(
            lambda e: e.get("type") == "compact_response",
            timeout=LLM_TIMEOUT,
            description="compact_response",
        )

        start = session.cursor()
        session.enter_query("Use the run_command tool to run: echo E2E_CMP_B")
        session.expect(b"\rE2E_CMP_B", timeout=LLM_TIMEOUT, start=start)
