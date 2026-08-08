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
import time
from pathlib import Path

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
        session.expect(b"[expecto interrupted]", timeout=60)

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
        session.expect(b"[expecto stopped at step 1]", timeout=30)

        # Back to passthrough.
        start = session.cursor()
        session.send(b"echo AFTER_STOP\r")
        session.expect(b"\rAFTER_STOP", timeout=30, start=start)


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
