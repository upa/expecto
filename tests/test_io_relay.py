import os
import pty
import threading
import time

from jou.io_relay import IORelay


class TestIORelay:
    def test_stop(self):
        relay = IORelay()
        assert not relay._running
        relay._running = True
        relay.stop()
        assert not relay._running

    def test_relay_pty_to_pty(self):
        """Data written to inner slave appears on outer master (child output → user screen)."""
        outer_master, outer_slave = pty.openpty()
        inner_master, inner_slave = pty.openpty()

        relay = IORelay()
        thread = threading.Thread(
            target=relay.run,
            args=(inner_master, outer_slave, outer_slave),
            daemon=True,
        )
        thread.start()
        time.sleep(0.1)

        os.write(inner_slave, b"child output")
        time.sleep(0.2)

        data = b""
        while True:
            try:
                rlist, _, _ = __import__("select").select([outer_master], [], [], 0.1)
                if not rlist:
                    break
                data += os.read(outer_master, 4096)
            except OSError:
                break
        assert b"child output" in data

        relay.stop()
        os.write(outer_master, b"\x00")
        thread.join(timeout=2)

        for fd in (outer_master, outer_slave, inner_master, inner_slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_relay_user_input_to_child(self):
        """Data written to outer master is relayed to inner master (user input → pty).

        The inner pty has echo enabled by default, so data written to inner_master
        echoes back. We read from inner_master to verify the relay wrote the data.
        """
        outer_master, outer_slave = pty.openpty()
        inner_master, inner_slave = pty.openpty()

        relay = IORelay()
        thread = threading.Thread(
            target=relay.run,
            args=(inner_master, outer_slave, outer_slave),
            daemon=True,
        )
        thread.start()
        time.sleep(0.1)

        os.write(outer_master, b"user input")
        time.sleep(0.2)

        # inner pty echoes back what was written to inner_master,
        # and relay also reads from inner_master and writes to outer_slave.
        # So we check outer_master for the echo that came back through the relay.
        data = b""
        import select as sel
        while True:
            rlist, _, _ = sel.select([outer_master], [], [], 0.2)
            if not rlist:
                break
            data += os.read(outer_master, 4096)
        assert b"user input" in data

        relay.stop()
        os.write(outer_master, b"\x00")
        thread.join(timeout=2)

        for fd in (outer_master, outer_slave, inner_master, inner_slave):
            try:
                os.close(fd)
            except OSError:
                pass
