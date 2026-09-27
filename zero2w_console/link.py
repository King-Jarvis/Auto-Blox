"""The link, host side: one held-open socket per device.

The same port also takes a board's picture stream: a connection that opens with
a TLS handshake is wrapped, and one whose hello says `"role": "stream"` is kept
apart from the link, which a board holds at the same time.
"""
import contextlib
import json
import os
import socket
import threading
import time

from .agent.modules import link as wire

# Its own port: 8787 is a plain HTTP server that knows nothing about frames and
# a device's camera answers on 8080, so mixing them would mean sniffing bytes.
PORT = 8789

# A connection that has said nothing at all for this long is gone, whatever the
# socket thinks. TCP holds a dead peer open far longer than a fleet wants to.
IDLE_SECONDS = 90

# How quiet it has to get before this end asks. The read times out once a second
# so that a stopping server notices promptly, and pinging on every one of those
# would put two frames a second on a link that has nothing to say.
PING_AFTER = 30

# How long the four handshake frames get, in total. Generous for a board joining
# a network, short enough that a socket opened to occupy one cannot sit there.
HANDSHAKE_SECONDS = 20

# The handshake's first two frames have no key yet, and are framed with this so
# the reader is one code path and line noise is rejected early. Not a secret.
GREETING_KEY = b"zero2w-link-v1"

MAX_BODY = wire.MAX_BODY

# A frame that has started arriving gets this long to finish. A picture can take
# longer than the idle poll, and giving up on half a frame loses the stream.
FRAME_SECONDS = 15

# How often a stream's reader lets go of the socket, so the lease the console
# sends back is never held up for long. TLS may not read and write at once.
STREAM_POLL = 0.2

TLS_RECORD = b"\x16"            # the first byte of any TLS handshake
LINK, STREAM = "link", "stream"


class Connection:
    """One device's socket, from the host's side."""

    def __init__(self, sock, address, device_id, channel, server,
                 role=LINK, secure=False):
        self.sock = sock
        self.address = address
        self.device_id = device_id
        self.channel = channel
        self.server = server
        self.role = role
        self.secure = secure
        self.opened = time.time()
        self.last_heard = time.time()
        self.frames_in = 0
        self.frames_out = 0
        self.closed = False
        self.replaced = False           # closed because the device dialled again
        self.pong = threading.Event()
        self.rtt_ms = None              # the last measured round trip
        self._lock = threading.Lock()
        # Held around every read too when the socket is TLS, which cannot be
        # read and written from two threads at once.
        self.io = self._lock if secure else None

    def ping(self, timeout=2.0):
        """Round trip in milliseconds: PING out, the device's PONG back."""
        self.pong.clear()
        began = time.monotonic()
        if not self.send(wire.PING):
            return None
        if not self.pong.wait(timeout):
            return None
        self.rtt_ms = round((time.monotonic() - began) * 1000.0, 1)
        return self.rtt_ms

    def send(self, kind, body=b""):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        with self._lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(self.channel.send(kind, body))
                self.frames_out += 1
                return True
            except (OSError, wire.LinkError):
                self._shutdown()
                return False

    def close(self):
        with self._lock:
            self._shutdown()

    def _shutdown(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __repr__(self):
        return "<Connection %s %s>" % (self.device_id, self.address[0])


class LinkServer(threading.Thread):
    """Accepts device connections and hands verified frames to a callback."""

    daemon = True

    def __init__(self, lookup, on_frame=None, on_open=None, on_close=None,
                 port=PORT, host="0.0.0.0", tls=None, stream_lookup=None):
        super().__init__(name="link-server")
        self.tls = tls                  # an ssl.SSLContext, or None: no streams
        # device_id -> token for a stream. Its own question: a board can stream
        # pictures without holding the link open.
        self.stream_lookup = stream_lookup or lookup
        self.lookup = lookup            # device_id -> token, or None
        self.on_frame = on_frame        # (connection, kind, body)
        self.on_open = on_open
        self.on_close = on_close
        self.port = port
        self.host = host
        self.sock = None
        self.connections = {}           # device_id -> Connection
        self.streams = {}               # device_id -> its picture stream
        self.refusals = []              # recent (time, address, reason)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        # Bound before the thread starts, so a caller that starts this and
        # immediately connects cannot lose the race.
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        super().start()
        return self

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        for conn in list(self.connections.values()):
            conn.close()

    def run(self):
        while not self._stop.is_set():
            try:
                client, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(client, address),
                             daemon=True).start()

    # -- one device --------------------------------------------------------
    def _serve(self, sock, address):
        conn = None
        try:
            # A control frame is small and latency is the whole point, so do
            # not let the kernel sit on one waiting for company.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            secure = False
            if self.tls is not None:
                sock.settimeout(HANDSHAKE_SECONDS)
                if sock.recv(1, socket.MSG_PEEK) == TLS_RECORD:
                    sock = self.tls.wrap_socket(sock, server_side=True)
                    secure = True
            conn = self._handshake(sock, address, secure)
            if conn is None:
                return
            self._pump(conn)
        except (OSError, EOFError, wire.LinkError, ValueError):
            # EOFError belongs here as much as the rest: a peer that goes away
            # mid-frame is ordinary, and letting it out of a daemon thread prints
            # a traceback for something that is not a fault.
            pass
        finally:
            if conn is not None:
                conn.close()
                table = self._table(conn.role)
                with self._lock:
                    if table.get(conn.device_id) is conn:
                        del table[conn.device_id]
                if self.on_close:
                    try:
                        self.on_close(conn)
                    except Exception:
                        pass
            else:
                try:
                    sock.close()
                except OSError:
                    pass

    def _refuse(self, sock, address, reason):
        """Hang up, and remember why."""
        with self._lock:
            self.refusals.append((time.time(), address[0], reason))
            del self.refusals[:-64]
        try:
            sock.close()
        except OSError:
            pass
        return None

    def _table(self, role):
        return self.streams if role == STREAM else self.connections

    def _handshake(self, sock, address, secure=False):
        sock.settimeout(HANDSHAKE_SECONDS)

        head, body = _read_frame(sock)
        try:
            kind, _seq, body = wire.open_frame(GREETING_KEY, head, body)
        except wire.LinkError as exc:
            return self._refuse(sock, address, "bad greeting: %s" % exc)
        if kind != wire.HELLO:
            return self._refuse(sock, address, "did not say hello first")

        try:
            hello = json.loads(body.decode())
            device_id = str(hello["device"])
            nonce_device = bytes.fromhex(hello["nonce"])
            role = hello.get("role") or LINK
        except Exception:
            return self._refuse(sock, address, "unreadable hello")
        if role not in (LINK, STREAM):
            return self._refuse(sock, address, "unknown role %r" % role)
        if role == STREAM and not secure:
            # The point of a stream is that it is encrypted. One in the clear
            # is refused rather than carried.
            return self._refuse(sock, address, "%s asked for a stream without TLS"
                                % device_id)
        if len(nonce_device) != wire.NONCE_BYTES:
            return self._refuse(sock, address, "wrong nonce length")

        token = None
        try:
            token = (self.stream_lookup if role == STREAM else self.lookup)(device_id)
        except Exception:
            token = None
        # A device that is not enrolled and one whose token is wrong are refused
        # the same way at the same point, so an unenrolled id cannot be told apart
        # from a bad one by watching how far the exchange gets.
        nonce_host = os.urandom(wire.NONCE_BYTES)
        sock.sendall(wire.seal(GREETING_KEY, wire.CHALLENGE, 1,
                               json.dumps({"nonce": nonce_host.hex()}).encode()))

        head, body = _read_frame(sock)
        if token is None:
            return self._refuse(sock, address, "no such device: %s" % device_id)

        key = wire.session_key(token, nonce_device, nonce_host)
        channel = wire.Channel(key)
        try:
            kind, _body = channel.receive(head, body)
        except wire.LinkError as exc:
            return self._refuse(sock, address,
                                "%s failed to authenticate: %s" % (device_id, exc))
        if kind != wire.AUTH:
            return self._refuse(sock, address, "expected auth, got %d" % kind)

        conn = Connection(sock, address, device_id, channel, self,
                          role=role, secure=secure)
        # READY is sealed with the session key, so this proves to the device that
        # it is talking to something holding its token, not a rogue AP.
        if not conn.send(wire.READY, b"{}"):
            return self._refuse(sock, address, "could not answer %s" % device_id)

        table = self._table(role)
        with self._lock:
            previous = table.get(device_id)
            table[device_id] = conn
        if previous is not None:
            # A board that rebooted leaves its old socket looking healthy.
            previous.replaced = True
            previous.close()
        if self.on_open:
            try:
                self.on_open(conn)
            except Exception:
                pass
        return conn

    def _pump(self, conn):
        conn.sock.settimeout(STREAM_POLL if conn.secure else 1.0)
        pinged = 0.0
        while not self._stop.is_set() and not conn.closed:
            try:
                head, body = _read_frame(conn.sock, conn.io)
            except socket.timeout:
                quiet = time.time() - conn.last_heard
                if quiet > IDLE_SECONDS:
                    return
                # One question, then wait for the answer. Asking again every
                # second would find out nothing sooner and would keep the device's
                # radio awake.
                if quiet > PING_AFTER and time.time() - pinged > PING_AFTER:
                    pinged = time.time()
                    conn.send(wire.PING)
                continue
            except (OSError, EOFError):
                return
            try:
                kind, body = conn.channel.receive(head, body)
            except wire.LinkError as exc:
                # Not skipped. A stream that has produced one frame which does
                # not verify is being tampered with or is out of step.
                with self._lock:
                    self.refusals.append(
                        (time.time(), conn.address[0],
                         "%s sent a frame that did not verify: %s"
                         % (conn.device_id, exc)))
                    del self.refusals[:-64]
                return
            conn.last_heard = time.time()
            conn.frames_in += 1
            if kind == wire.PING:
                conn.send(wire.PONG)
                continue
            if kind == wire.PONG:
                conn.pong.set()
                continue
            if self.on_frame:
                try:
                    self.on_frame(conn, kind, body)
                except Exception:
                    pass

    # -- what the rest of the console asks it ------------------------------
    def connection(self, device_id):
        with self._lock:
            return self.connections.get(device_id)

    def ping(self, device_id, timeout=2.0):
        """(rtt ms or None, why not) for one device's link."""
        conn = self.connection(device_id)
        if conn is None:
            return None, "no link open to this device"
        rtt = conn.ping(timeout)
        return rtt, ("" if rtt is not None else "no answer in %gs" % timeout)

    def send(self, device_id, kind, body=b""):
        """True if it went out on a live socket, False if nothing is there."""
        conn = self.connection(device_id)
        return bool(conn and conn.send(kind, body))

    def online(self):
        with self._lock:
            return sorted(self.connections)

    def stream(self, device_id):
        """This device's open picture stream, or None."""
        with self._lock:
            return self.streams.get(device_id)

    def status(self):
        with self._lock:
            return {
                "port": self.port,
                "devices": [
                    {"device": c.device_id, "address": c.address[0],
                     "since": int(c.opened), "in": c.frames_in,
                     "out": c.frames_out,
                     "quiet_for": round(time.time() - c.last_heard, 1)}
                    for c in self.connections.values()],
                "streams": sorted(self.streams),
                "tls": self.tls is not None,
                "refused": [{"at": int(t), "address": a, "why": w}
                            for t, a, w in self.refusals[-10:]],
            }


def _read_frame(sock, lock=None):
    """A whole frame, or raise. Never a partial one handed on as if it were.

    Only an idle socket times out: once the first byte of a frame is in, the
    rest gets FRAME_SECONDS, because a timeout that dropped half a frame would
    leave the stream out of step for good."""
    head = _read_exactly(sock, wire.HEADER, lock, idle_ok=True)
    _kind, length, _seq = wire.read_header(head)
    return head, _read_exactly(sock, length, lock) if length else b""


def _read_exactly(sock, count, lock=None, idle_ok=False):
    chunks = []
    have = 0
    deadline = None
    while have < count:
        try:
            with lock or contextlib.nullcontext():
                chunk = sock.recv(count - have)
        except socket.timeout:
            if idle_ok and not have:
                raise                   # nothing of a frame yet: just idle
            if deadline is None:
                deadline = time.monotonic() + FRAME_SECONDS
            elif time.monotonic() > deadline:
                raise EOFError("a frame stopped arriving halfway")
            continue
        if not chunk:
            raise EOFError("the other end went away mid-frame")
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)
