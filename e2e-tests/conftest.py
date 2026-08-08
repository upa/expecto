"""Fixtures for the end-to-end tests.

Everything runs inside the e2e container (see Dockerfile): llama-server
provides an OpenAI-compatible API for a small local model, and expecto
is driven through a pty exactly like a user's terminal would.
"""
from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import time
import urllib.request
from collections.abc import Iterator
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

    def __init__(self, config_file: str) -> None:
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
        self.output = bytearray()
        self._exited: int | None = None

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def query(self, text: str) -> None:
        """Enter query mode via the hotkey, type the query, and submit."""
        self.send(HOTKEY)
        self.send(text.encode())
        self.send(b"\r")

    def wait_prompt(self, timeout: float = 30.0) -> bytes:
        """Wait for the wrapped shell's prompt to appear."""
        return self.expect(PROMPT.encode(), timeout=timeout)

    def expect(self, needle: bytes, timeout: float = 30.0) -> bytes:
        """Read pty output until needle appears; returns all output so
        far. Raises AssertionError (with the output) on timeout or EOF."""
        deadline = time.time() + timeout
        while needle not in self.output:
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


@pytest.fixture
def expecto_session(llama_server: str, tmp_path: Path) -> Iterator[ExpectoSession]:
    """An expecto instance wrapping bash, configured to talk to the
    local llama-server."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'base_url = "{llama_server}/v1"\n'
        'model = "qwen3-0.6b"\n',
    )
    session = ExpectoSession(str(config_file))
    yield session
    session.close()
