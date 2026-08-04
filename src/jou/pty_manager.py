import os
import pty


class PTYManager:
    def __init__(self) -> None:
        self.master_fd: int | None = None
        self.child_pid: int | None = None

    def spawn(self, command: str | list[str]) -> None:
        pid, fd = pty.fork()
        if pid == 0:
            if isinstance(command, str):
                os.execvp(command, [command])
            else:
                os.execvp(command[0], command)
        else:
            self.child_pid = pid
            self.master_fd = fd

    def wait(self) -> int:
        if self.child_pid is not None:
            _, status = os.waitpid(self.child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        return 0

    def close(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
