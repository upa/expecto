"""Fixtures for the end-to-end tests.

Everything runs inside the e2e container (see Dockerfile): llama-server
provides an OpenAI-compatible API for a small local model, and expecto
is driven through a pty exactly like a user's terminal would.
"""
from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

LLAMA_PORT = 8080
LLAMA_BASE_URL = f"http://127.0.0.1:{LLAMA_PORT}"
MODEL_FILE = os.environ.get("MODEL_FILE", "/opt/models/model.gguf")
SERVER_START_TIMEOUT = 180.0

HOTKEY = b"\x1f"  # Ctrl-/ (expecto's default)

# A fixed, distinctive shell prompt so tests can wait for it regardless
# of the user the container runs as (root's default is "bash-5.2#").
PROMPT = "e2e> "

# Exactly what QueryModeUI draws when query mode opens.
QUERY_PROMPT = b"\x1b[90m? \x1b[0m"


@pytest.fixture(scope="session")
def llama_server() -> Iterator[str]:
    """llama-server serving the bundled model; yields its base URL."""
    proc = subprocess.Popen(
        [
            "llama-server",
            "-m", MODEL_FILE,
            "--host", "127.0.0.1",
            "--port", str(LLAMA_PORT),
            "--jinja",   # tool calling via the model's chat template
            "--temp", "0",
            "-c", "4096",
            "--chat-template-kwargs", '{"enable_thinking": false}',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + SERVER_START_TIMEOUT
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{LLAMA_BASE_URL}/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except OSError:
            pass
        if time.time() > deadline:
            proc.kill()
            raise TimeoutError("llama-server did not become healthy in time")
        time.sleep(0.5)
    yield LLAMA_BASE_URL
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class ExpectoSession:
    """expecto running in a pty, driven like a user's terminal.

    Collects everything expecto writes to the terminal in `output`;
    `expect()` waits for a byte pattern to appear in it.
    """

    def __init__(self, config_file: str, log_file: str | None = None) -> None:
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = "dummy"  # llama-server accepts anything
        env["TERM"] = "xterm-256color"
        env["PS1"] = PROMPT  # kept by bash --norc (no rc file overrides it)
        env.pop("EXPECTO_ACTIVE", None)
        pid, master = pty.fork()
        if pid == 0:  # child: never returns
            os.execvpe(
                "expecto",
                ["expecto", "-c", config_file, "--", "bash", "--norc"],
                env,
            )
        self.pid = pid
        self.master = master
        self.log_file = log_file
        self.output = bytearray()
        self._exited: int | None = None

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def query(self, text: str) -> None:
        """Enter query mode via the hotkey, type the query, and submit."""
        self.send(HOTKEY)
        self.send(text.encode())
        self.send(b"\r")

    def enter_query(self, text: str, timeout: float = 120.0) -> None:
        """Like query(), but retries the hotkey until query mode actually
        opens — the hotkey is discarded while a previous execution is
        still finishing, so this is how a user starts the next query."""
        deadline = time.time() + timeout
        while True:
            start = self.cursor()
            self.send(HOTKEY)
            try:
                self.expect(QUERY_PROMPT, timeout=1.0, start=start)
                break
            except AssertionError:
                if time.time() > deadline:
                    raise
        self.send(text.encode())
        self.send(b"\r")

    def wait_prompt(self, timeout: float = 30.0) -> bytes:
        """Wait for the wrapped shell's prompt to appear."""
        return self.expect(PROMPT.encode(), timeout=timeout)

    def cursor(self) -> int:
        """Current end of the collected output; pass to expect(start=...)
        to match only output produced after this point."""
        return len(self.output)

    def expect(self, needle: bytes, timeout: float = 30.0, start: int = 0) -> bytes:
        """Read pty output until needle appears at or after `start`;
        returns all output so far. Raises AssertionError (with the
        output) on timeout or EOF."""
        deadline = time.time() + timeout
        while needle not in self.output[start:]:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for {needle!r}; output so far:\n"
                    f"{bytes(self.output)!r}",
                )
            r, _, _ = select.select([self.master], [], [], min(remaining, 0.2))
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                raise AssertionError(
                    f"expecto exited before {needle!r} appeared; output:\n"
                    f"{bytes(self.output)!r}",
                )
            self.output.extend(chunk)
        return bytes(self.output)

    def drain(self, duration: float = 0.2) -> None:
        """Collect whatever arrives on the pty for `duration` seconds."""
        end = time.time() + duration
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                return
            r, _, _ = select.select([self.master], [], [], remaining)
            if not r:
                return
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.output.extend(chunk)

    def read_log(self) -> list[dict[str, object]]:
        """Parse the JSONL log expecto was started with."""
        if self.log_file is None:
            return []
        try:
            with open(self.log_file) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def wait_log(
        self,
        predicate: Callable[[dict[str, object]], bool],
        timeout: float = 120.0,
        description: str = "log entry",
    ) -> dict[str, object]:
        """Wait until a log entry matching predicate appears (keeps
        draining the pty meanwhile); returns the entry."""
        deadline = time.time() + timeout
        while True:
            for entry in self.read_log():
                if predicate(entry):
                    return entry
            if time.time() > deadline:
                raise AssertionError(
                    f"timed out waiting for {description}; log entries:\n"
                    + "\n".join(str(e.get("type")) for e in self.read_log()),
                )
            self.drain(0.2)

    def wait_exit(self, timeout: float = 10.0) -> int:
        """Wait for the expecto process to exit; returns its exit code."""
        deadline = time.time() + timeout
        while True:
            if self._exited is not None:
                return self._exited
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                self._exited = os.waitstatus_to_exitcode(status)
                return self._exited
            if time.time() > deadline:
                raise TimeoutError("expecto did not exit in time")
            # Keep draining the pty so expecto is not blocked on writes.
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.master, 4096)
                    self.output.extend(chunk)
                except OSError:
                    pass

    def close(self) -> None:
        """Best-effort teardown: ask the shell to exit, then kill."""
        if self._exited is None:
            try:
                self.send(b"exit\r")
                self.wait_exit(timeout=5)
            except (OSError, TimeoutError):
                try:
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
                except (OSError, ChildProcessError):
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass


MakeExpecto = Callable[..., ExpectoSession]


@pytest.fixture
def make_expecto(llama_server: str, tmp_path: Path) -> Iterator[MakeExpecto]:
    """Factory for expecto instances wrapping bash, configured to talk
    to the local llama-server. Extra config.toml lines can be appended
    per test (e.g. "max_steps = 2")."""
    sessions: list[ExpectoSession] = []

    def _make(config_extra: str = "") -> ExpectoSession:
        index = len(sessions)
        config_file = tmp_path / f"config{index}.toml"
        log_file = tmp_path / f"expecto{index}.jsonl"
        config_file.write_text(
            f'base_url = "{llama_server}/v1"\n'
            'model = "qwen3-0.6b"\n'
            f'log_file = "{log_file}"\n'
            + config_extra,
        )
        session = ExpectoSession(str(config_file), log_file=str(log_file))
        sessions.append(session)
        return session

    yield _make
    for session in sessions:
        session.close()


@pytest.fixture
def expecto_session(make_expecto: MakeExpecto) -> ExpectoSession:
    """A single expecto instance with the default test configuration."""
    return make_expecto()
