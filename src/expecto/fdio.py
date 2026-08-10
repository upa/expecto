from __future__ import annotations

import os


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of data to fd, looping over short writes.

    os.write() is not obliged to consume the whole buffer: a write to the
    pty master stops once the child's tty input buffer is full, and a
    signal arriving after some bytes were transferred returns the partial
    count as well (PEP 475 retries only when nothing was written). Both
    grow more likely with non-ASCII text, which is several bytes per
    character. Everything expecto writes — a command line, a block of text
    typed with send_keys, its own status messages — has to arrive whole,
    so the remainder is written until none is left."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            # Cannot happen for a non-empty buffer; refuse to spin if it does.
            raise OSError(f"write to fd {fd} made no progress")
        view = view[written:]
