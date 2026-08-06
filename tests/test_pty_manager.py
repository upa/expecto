import os

from expecto.pty_manager import NESTING_ENV_VAR, PTYManager


class TestPTYManager:
    def test_spawn_and_wait_string_command(self):
        mgr = PTYManager()
        mgr.spawn("true")
        assert mgr.master_fd is not None
        assert mgr.child_pid is not None
        exit_code = mgr.wait()
        assert exit_code == 0
        mgr.close()

    def test_spawn_and_wait_list_command(self):
        mgr = PTYManager()
        mgr.spawn(["echo", "hello"])
        assert mgr.master_fd is not None

        output = b""
        while True:
            try:
                chunk = os.read(mgr.master_fd, 4096)
                if not chunk:
                    break
                output += chunk
            except OSError:
                break
        assert b"hello" in output

        exit_code = mgr.wait()
        assert exit_code == 0
        mgr.close()

    def test_nonzero_exit_code(self):
        mgr = PTYManager()
        mgr.spawn(["sh", "-c", "exit 42"])
        exit_code = mgr.wait()
        assert exit_code == 42
        mgr.close()

    def test_close_idempotent(self):
        mgr = PTYManager()
        mgr.spawn("true")
        mgr.wait()
        mgr.close()
        mgr.close()
        assert mgr.master_fd is None

    def test_child_env_marks_expecto_session(self):
        mgr = PTYManager()
        mgr.spawn(["sh", "-c", f"echo marker=${NESTING_ENV_VAR}"])
        assert mgr.master_fd is not None

        output = b""
        while True:
            try:
                chunk = os.read(mgr.master_fd, 4096)
                if not chunk:
                    break
                output += chunk
            except OSError:
                break
        assert b"marker=1" in output

        mgr.wait()
        mgr.close()
