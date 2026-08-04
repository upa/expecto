import os
import pty


class PTYManager:
    def __init__(self):
        self.master_fd = None
        self.child_pid = None

    def spawn(self, command):
        pid, fd = pty.openpty()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(fd)
            os.setsid()
            slave_fd = pid

            for i in range(3):
                os.dup2(slave_fd, i)
            if slave_fd > 2:
                os.close(slave_fd)

            if isinstance(command, str):
                os.execvp(command, [command])
            else:
                os.execvp(command[0], command)
        else:
            os.close(pid)
            self.master_fd = fd
            self.child_pid = child_pid

    def wait(self):
        if self.child_pid is not None:
            _, status = os.waitpid(self.child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        return 0

    def close(self):
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
