"""A serial port and the raw REPL, from termios."""
import base64
import errno
import fcntl
import glob
import hashlib
import os
import re
import struct
import termios
import time

BAUD = 115200
GLOBS = tuple(os.environ.get("ZERO2W_IOT_SERIAL_GLOBS",
                             "/dev/ttyUSB*:/dev/ttyACM*").split(":"))


def ports():
    """Candidate ports, newest first — a board just plugged in is the one
    someone is most likely to mean."""
    found = []
    for pattern in GLOBS:
        found.extend(glob.glob(pattern))
    return sorted(set(found), key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)


class Serial:
    """115200 8N1, raw, with a short read timeout."""

    def __init__(self, path, baud=BAUD, timeout=0.5, exclusive=True):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        if exclusive:
            # Two readers on one port silently eat each other's bytes.
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.fd)
                raise OSError(errno.EBUSY, "%s is already open elsewhere" % path)
        os.set_blocking(self.fd, True)
        speed = getattr(termios, "B%d" % baud)
        attrs = termios.tcgetattr(self.fd)
        cc = list(attrs[6])
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = max(1, int(timeout * 10))
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0,
                           speed, speed, cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- lines -------------------------------------------------------------
    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        while data:
            try:
                n = os.write(self.fd, data)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                    continue
                raise
            data = data[n:]

    def read(self, n=4096):
        try:
            return os.read(self.fd, n)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            raise

    def readline(self, timeout=1.0):
        """One line, or b"" if none arrived in time. Never blocks forever."""
        end = time.monotonic() + timeout
        while True:
            if b"\n" in self.buf:
                line, _, self.buf = self.buf.partition(b"\n")
                return line.rstrip(b"\r")
            chunk = self.read()
            if chunk:
                self.buf += chunk
                if len(self.buf) > 262144:      # a board gone mad
                    self.buf = self.buf[-65536:]
                continue
            if time.monotonic() >= end:
                return b""
            time.sleep(0.02)

    def read_until(self, marker, timeout=10):
        marker = marker if isinstance(marker, bytes) else marker.encode()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.buf += self.read()
            if marker in self.buf:
                out, self.buf = self.buf, b""
                return out
            time.sleep(0.02)
        out, self.buf = self.buf, b""
        return out

    # -- the reset lines ---------------------------------------------------
    def reset_into_run(self):
        """Reset into a normal boot, not the bootloader."""
        try:
            flags = struct.unpack("I", fcntl.ioctl(self.fd, termios.TIOCMGET,
                                                   b"\0\0\0\0"))[0]
            flags &= ~termios.TIOCM_DTR
            fcntl.ioctl(self.fd, termios.TIOCMSET,
                        struct.pack("I", flags | termios.TIOCM_RTS))
            time.sleep(0.15)
            fcntl.ioctl(self.fd, termios.TIOCMSET,
                        struct.pack("I", flags & ~termios.TIOCM_RTS))
            time.sleep(0.05)
        except (OSError, AttributeError):
            pass       # not every bridge exposes the lines


# What the board runs to prove a file landed intact, hashed in blocks — because
# an ESP32 has little RAM and no large contiguous piece of it. Reading the whole
# file asked a board for 32,000 bytes in one allocation and got MemoryError,
# after every byte of a 38KB agent.py had already been written correctly.
VERIFY = ("import ubinascii,hashlib\n"
          "h=hashlib.sha256()\n"
          "f=open(%r,'rb')\n"
          "while True:\n"
          " b=f.read(512)\n"
          " if not b:\n"
          "  break\n"
          " h.update(b)\n"
          "f.close()\n"
          "print(ubinascii.hexlify(h.digest()).decode())")


class RawREPL:
    """MicroPython's raw REPL: paste a statement, run it, read what it said."""

    def __init__(self, serial):
        self.s = serial

    def enter(self, tries=6):
        for _ in range(tries):
            self.s.write(b"\r\x03\x03")          # two Ctrl-C to stop main.py
            time.sleep(0.2)
            self.s.read()
            self.s.write(b"\r\x01")              # Ctrl-A: raw REPL
            if b"raw REPL" in self.s.read_until(b"raw REPL; CTRL-B to exit", 3):
                return True
        return False

    def enter_through_boot(self, seconds=4):
        """Reset, then hold Ctrl-C through the boot.

        A running agent's loop catches KeyboardInterrupt, so `enter()` may
        never land; startup is the window.
        """
        self.s.reset_into_run()
        end = time.time() + seconds
        while time.time() < end:
            self.s.write(b"\x03")
            self.s.read(256)
            time.sleep(0.05)
        return self.enter(tries=3)

    def exit(self):
        self.s.write(b"\r\x02")                  # Ctrl-B: back to friendly REPL

    def run(self, code, timeout=20):
        self.s.write(code.encode() + b"\x04")    # Ctrl-D: execute
        out = self.s.read_until(b"\x04>", timeout)
        if b"Traceback" in out or b"Error" in out.split(b"\x04")[-2:][0]:
            raise RuntimeError(out.decode(errors="replace")[-400:])
        return out

    def put(self, name, data, chunk=192):
        """Write one file, base64 in small pieces — a device has little RAM
        and the REPL has no flow control worth trusting."""
        if "/" in name:
            self.run("import os\ntry:\n os.mkdir(%r)\nexcept OSError:\n pass" % name.rsplit("/", 1)[0])
        self.run("f=open(%r,'wb')\nimport ubinascii" % name)
        for i in range(0, len(data), chunk):
            blob = base64.b64encode(data[i:i + chunk]).decode()
            self.run("f.write(ubinascii.a2b_base64('%s'))" % blob)
        self.run("f.close()")
        got = self.run(VERIFY % name)
        digest = re.search(rb"([0-9a-f]{64})", got)
        want = hashlib.sha256(data).hexdigest()
        if not digest or digest.group(1).decode() != want:
            raise RuntimeError("%s did not land intact" % name)
        return want[:16]
