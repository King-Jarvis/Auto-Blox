"""The link, device side."""
try:
    import ujson as json
except ImportError:
    import json

try:
    import usocket as socket
except ImportError:
    import socket

import os
import time

try:
    import uerrno as errno
except ImportError:
    import errno

try:                                    # for one question: is it writable yet
    import uselect as select
except ImportError:
    try:
        import select
    except ImportError:
        select = None

try:                                    # a package on the host
    from . import link as wire
except Exception:                       # a bare directory on the device
    # Broad on purpose: what a relative import raises outside a package is not
    # the same on both runtimes.
    wire = __import__("modules.link", None, None, ("x",))

GREETING_KEY = b"zero2w-link-v1"        # matches the host; not a secret

# Not yet connected, dialling, said hello, proved itself, carrying traffic.
IDLE, CONNECTING, GREETED, AUTHED, READY = 0, 1, 2, 3, 4
NAMES = ("idle", "connecting", "greeted", "authed", "ready")


def _again():
    """Errors that mean "not yet", not "broken" — asked, not assumed."""
    found = set()
    for name in ("EAGAIN", "EWOULDBLOCK", "EINPROGRESS", "EALREADY",
                 "ENOTCONN", "EISCONN"):
        value = getattr(errno, name, None)
        if isinstance(value, int):
            found.add(value)
    found.update((11, 35,                 # EAGAIN / EWOULDBLOCK, both worlds
                  107, 114, 115,          # ENOTCONN, EALREADY, EINPROGRESS: linux
                  112, 119, 120, 127, 128))  # and what newlib calls them
    return tuple(sorted(found))


AGAIN = _again()

MAX_IN = 8192                           # the ceiling on one inbound frame
MAX_OUT = 8192                          # and on what may queue up unsent
BACKOFF_MAX = 30
READ_CHUNK = 512

# How long the four handshake frames get before the whole dial is given up on
# and backed off. Without this a socket that connects and then says nothing sits
# in `connecting` for as long as the board is powered.
HANDSHAKE_SECONDS = 20


class Client:
    def __init__(self, host, port, device, token, on_frame=None,
                 on_ready=None):
        self.host = host
        self.port = int(port)
        self.device = device
        self.token = token
        self.on_frame = on_frame
        self.on_ready = on_ready
        self.sock = None
        self.poller = None              # one registration: this socket, POLLOUT
        self.channel = None
        self.state = IDLE
        self.inbox = b""
        self.outbox = b""
        self.failures = 0
        self.next_try = 0
        self.deadline = 0               # when this dial's handshake gives up
        self.dropped = None             # why the last connection ended
        self.frames_in = 0
        self.frames_out = 0

    # -- what the agent calls ---------------------------------------------
    def poll(self):
        """One small step. Never blocks, never raises."""
        try:
            if self.state == IDLE:
                self._dial()
                return False
            if self.state != READY and time.time() > self.deadline:
                return self._drop("handshake did not finish in %ds"
                                  % HANDSHAKE_SECONDS)
            if self.state == CONNECTING and not self._dialled():
                return False
            # The flush can end the connection, and then there is no socket to
            # read. Reading anyway left `dropped` — the one field that says what
            # went wrong — holding the second error rather than the first.
            self._flush()
            if self.sock is None:       # the flush ended it
                return False
            self._read()
        except Exception as exc:        # a polled loop may not throw
            self._drop("poll: %s" % exc)
        return self.state == READY

    def send(self, kind, body=b""):
        """Queue a frame. False means it did not go and will not."""
        if self.state != READY:
            return False
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        if len(self.outbox) + len(body) > MAX_OUT:
            # Dropped rather than queued when the host is not keeping up, the
            # rule the agent already follows: a field device runs its flow anyway.
            return False
        try:
            self.outbox += self.channel.send(kind, body)
        except Exception:
            return False
        self.frames_out += 1
        self._flush()
        return True

    def close(self, why="closed"):
        self._drop(why)

    def status(self):
        return {"state": NAMES[self.state], "in": self.frames_in,
                "out": self.frames_out, "dropped": self.dropped,
                "failures": self.failures}

    # -- the steps ---------------------------------------------------------
    def _dial(self):
        if time.time() < self.next_try:
            return
        try:
            info = socket.getaddrinfo(self.host, self.port)[0][-1]
            self.sock = socket.socket()
            self.sock.setblocking(False)
            try:
                self.sock.connect(info)
            except OSError as exc:
                if exc.args[0] not in AGAIN:
                    raise
        except OSError as exc:
            return self._drop("dial: errno %s (%s)" % (_errno(exc), exc))
        except Exception as exc:
            return self._drop("dial: %s" % exc)
        self.nonce = os.urandom(wire.NONCE_BYTES)
        hello = json.dumps({"device": self.device,
                            "nonce": _hex(self.nonce)}).encode()
        self.inbox = b""
        self.outbox = wire.seal(GREETING_KEY, wire.HELLO, 1, hello)
        self.state = CONNECTING
        self.deadline = time.time() + HANDSHAKE_SECONDS
        self.poller = None
        if select is not None:
            try:
                self.poller = select.poll()
                self.poller.register(self.sock, select.POLLOUT)
            except Exception:
                self.poller = None      # ask by trying instead

    def _dialled(self):
        """Is the dial finished? Asked, without waiting for the answer."""
        if self.poller is None:
            return True                 # no poller: fall back to trying
        try:
            events = self.poller.poll(0)
        except Exception:
            self.poller = None
            return True
        if not events:
            return False
        flags = events[0][1]
        # A refused connection is writable too, with the error bits set. Without
        # this it would be written to, fail, and be retried until the deadline.
        bad = getattr(select, "POLLERR", 8) | getattr(select, "POLLHUP", 16)
        if flags & bad:
            # A read gets the real reason out of it: MicroPython has no
            # getsockopt, so SO_ERROR cannot be asked for directly.
            try:
                self.sock.recv(1)
            except OSError as exc:
                return self._drop("dial: errno %s (%s)" % (_errno(exc), exc))
            except Exception:
                pass
            return self._drop("dial refused (poll flags %d)" % flags)
        return True

    def _flush(self):
        """Push what is queued."""
        while self.outbox:
            try:
                sent = self.sock.send(self.outbox)
            except OSError as exc:
                if exc.args[0] in AGAIN:
                    return False
                if self.state == CONNECTING:
                    # Still dialling, which on this build arrives as
                    # ECONNABORTED rather than as a "not yet".
                    return False
                # The number, not just the text: MicroPython's OSError often
                # stringifies to the bare errno with no name.
                self._drop("send: errno %s (%s)" % (_errno(exc), exc))
                return False
            if not sent:
                return False
            self.outbox = self.outbox[sent:]
        if self.state == CONNECTING:
            self.state = GREETED
        return True

    def _read(self):
        for _ in range(4):
            try:
                chunk = self.sock.recv(READ_CHUNK)
            except OSError as exc:
                if exc.args[0] in AGAIN:
                    break
                return self._drop("recv: errno %s (%s)" % (_errno(exc), exc))
            if not chunk:
                return self._drop("the host closed the link")
            self.inbox += chunk
            if len(chunk) < READ_CHUNK:
                break
        self._parse()

    def _parse(self):
        while True:
            if len(self.inbox) < wire.HEADER:
                return
            try:
                _kind, length, _seq = wire.read_header(self.inbox[:wire.HEADER])
            except Exception as exc:
                return self._drop("header: %s" % exc)
            if length > MAX_IN:
                return self._drop("a %d byte frame is more than this holds"
                                  % length)
            total = wire.HEADER + length
            if len(self.inbox) < total:
                return
            head = self.inbox[:wire.HEADER]
            body = self.inbox[wire.HEADER:total]
            self.inbox = self.inbox[total:]
            if self._handle(head, body) is False:
                return

    def _handle(self, head, body):
        if self.state == GREETED:
            try:
                kind, _seq, body = wire.open_frame(GREETING_KEY, head, body)
            except Exception as exc:
                return self._drop("challenge: %s" % exc)
            if kind != wire.CHALLENGE:
                return self._drop("expected a challenge")
            try:
                nonce_host = _unhex(json.loads(body.decode())["nonce"])
            except Exception:
                return self._drop("unreadable challenge")
            key = wire.session_key(self.token, self.nonce, nonce_host)
            self.channel = wire.Channel(key)
            self.outbox += self.channel.send(wire.AUTH, b"{}")
            self.state = AUTHED
            self._flush()
            return True

        try:
            kind, body = self.channel.receive(head, body)
        except Exception as exc:
            # Not skipped, not resynchronised. A stream that has produced one
            # frame that does not verify is being tampered with or is out of step.
            return self._drop("frame: %s" % exc)
        self.frames_in += 1

        if self.state == AUTHED:
            if kind != wire.READY:
                return self._drop("expected ready")
            # Opening this under the session key is what proves the far end holds
            # this device's token; without it a rogue AP could drive this board.
            self.state = READY
            self.failures = 0
            self.dropped = None
            if self.on_ready:
                try:
                    self.on_ready(self)
                except Exception:
                    pass
            return True

        if kind == wire.PING:
            self.send(wire.PONG)
            return True
        if kind == wire.PONG:
            return True
        if self.on_frame:
            try:
                self.on_frame(self, kind, body)
            except Exception:
                pass
        return True

    def _drop(self, why):
        self.dropped = why
        if self.poller is not None:
            try:
                self.poller.unregister(self.sock)
            except Exception:
                pass
            self.poller = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.channel = None
        self.inbox = b""
        self.outbox = b""
        self.state = IDLE
        self.failures += 1
        # Retrying a host that is not there wakes the radio for nothing.
        wait = 2 ** self.failures
        self.next_try = time.time() + (BACKOFF_MAX if wait > BACKOFF_MAX else wait)
        return False


def _errno(exc):
    try:
        return exc.args[0]
    except (AttributeError, IndexError):
        return "?"


def _hex(raw):
    out = ""
    for byte in raw:
        out += "%02x" % byte
    return out


def _unhex(text):
    out = bytearray(len(text) // 2)
    for i in range(len(out)):
        out[i] = int(text[i * 2:i * 2 + 2], 16)
    return bytes(out)
