"""Press keys to see their byte values. Ctrl-C to exit."""
import sys
import termios
import tty

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
try:
    sys.stdout.write("Press keys to see byte values. Ctrl-C to exit.\r\n")
    sys.stdout.flush()
    while True:
        b = sys.stdin.buffer.read(1)
        if not b:
            break
        val = b[0]
        if val == 0x03:
            sys.stdout.write("\r\nCtrl-C detected, exiting.\r\n")
            sys.stdout.flush()
            break
        label = ""
        if val < 0x20:
            label = f" (Ctrl-{chr(val + 0x40)})"
        sys.stdout.write(f"byte: 0x{val:02x}{label}\r\n")
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
