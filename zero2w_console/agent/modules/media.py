"""The picture stream, device side: a second link connection, over TLS.

The console's certificate arrives signed with this board's own token
(fleet.media_offer), so the board trusts that console and nothing else. The
console asks for pictures with a lease; the board sends while it runs.
"""
import ssl
import struct
import time

try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:                                    # a package on the host
    from . import linkclient as lc
except Exception:                       # a bare directory on the device
    lc = __import__("modules.linkclient", None, None, ("x",))

wire = lc.wire
FORMATS = {"jpeg": 1, "grayscale": 2, "rgb565": 3}


def offer(agent):
    """(port, name, certificate) from the console, checked, or None. Sets
    the clock too: TLS checks dates, and this one starts at 2000-01-01."""
    try:
        doc = agent.call("GET", "/api/iot/media")
        der = binascii.a2b_base64(doc["cert"])
        utc = doc["utc"]
        signed = der + ("%d,%d,%d,%d,%d,%d" % tuple(utc)).encode()
        sig = lc._hex(wire.hmac_sha256(str(agent.token).encode(), signed))
    except Exception:
        return None                     # no stream here: serve frames as before
    if not wire.same(sig.encode(), str(doc.get("sig") or "").encode()):
        agent.log("critical", "the console's picture certificate did not verify")
        return None
    try:
        import machine
        before = time.time()
        machine.RTC().datetime((utc[0], utc[1], utc[2], 0, utc[3], utc[4], utc[5], 0))
        # The agent counts uptime from the clock it booted with; move that
        # too, or uptime reads as the 26 years the clock just jumped.
        agent.started += time.time() - before
    except Exception:
        pass
    return doc["port"], doc.get("name") or "zero2w-console", der


def _waiting(exc):
    """Not yet: an errno that says so, or the host runtime's TLS want-errors."""
    return (exc.args and exc.args[0] in lc.AGAIN) or \
        type(exc).__name__.startswith("SSLWant")


class Stream(lc.Client):
    def __init__(self, host, port, device, token, cert, name):
        lc.Client.__init__(self, host, port, device, token, on_frame=_control)
        self.cert, self.name = cert, name
        self.wrapped = False
        self.sent = 0                   # how much of outbox has gone
        self.until = self.last = time.ticks_ms()    # no lease until asked
        self.every = 1000
        self.said = None

    # -- what the camera calls ---------------------------------------------
    def due(self):
        """Is a picture wanted now, and is the last one gone?"""
        if self.state != lc.READY or self.outbox:
            return False
        now = time.ticks_ms()
        return time.ticks_diff(self.until, now) > 0 and \
            time.ticks_diff(now, self.last) >= self.every

    def push(self, fmt, width, height, frame):
        body = struct.pack(">BHH", FORMATS[fmt], width, height) + bytes(frame)
        if len(body) > wire.MAX_BODY:
            self.said = "a %d byte picture is too big to stream; use JPEG or a smaller size" \
                % len(body)
            return False
        self.outbox = self.channel.send(wire.MEDIA, body)
        self.sent = 0
        self.last = time.ticks_ms()
        self.frames_out += 1
        self._flush()
        return True

    def send_still(self, kind, fmt, width, height, frame):
        """One picture a flow chose to send, as STILL: ">BHHB", then the kind
        a Device event matches on, then the picture."""
        k = str(kind).encode()[:32]
        body = struct.pack(">BHHB", FORMATS[fmt], width, height, len(k)) + k + bytes(frame)
        if len(body) > wire.MAX_BODY:
            return False
        self.outbox = self.channel.send(wire.STILL, body)
        self.sent = 0
        self._flush()
        return True

    # -- the link client, over TLS -------------------------------------------
    def _dial(self):
        self.wrapped = False
        lc.Client._dial(self)
        if self.state == lc.CONNECTING:
            hello = '{"device": "%s", "nonce": "%s", "role": "stream"}' % (
                self.device, lc._hex(self.nonce))
            self.outbox = wire.seal(lc.GREETING_KEY, wire.HELLO, 1, hello.encode())
            self.sent = 0

    def _dialled(self):
        if self.wrapped:
            return True
        if not lc.Client._dialled(self) or self.sock is None:
            return False
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=self.cert)
        self.sock = ctx.wrap_socket(self.sock, server_hostname=self.name,
                                    do_handshake_on_connect=False)
        self.wrapped = True
        return True

    def _flush(self):
        """As the link's, but from an offset: a picture is tens of KB, and
        slicing the rest off a bytes object each time would copy it again and
        again."""
        while self.sent < len(self.outbox):
            try:
                n = self.sock.write(memoryview(self.outbox)[self.sent:])
            except OSError as exc:
                if _waiting(exc):
                    return False
                self._drop("send: errno %s (%s)" % (lc._errno(exc), exc))
                return False
            if not n:
                return False            # None: would block, handshake included
            self.sent += n
        self.outbox = b""
        self.sent = 0
        if self.state == lc.CONNECTING:
            self.state = lc.GREETED
        return True

    def _read(self):
        for _ in range(4):
            try:
                chunk = self.sock.read(lc.READ_CHUNK)
            except OSError as exc:
                if _waiting(exc):
                    break
                return self._drop("recv: errno %s (%s)" % (lc._errno(exc), exc))
            if chunk is None:
                break
            if not chunk:
                return self._drop("the console closed the stream")
            self.inbox += chunk
        self._parse()

    def _drop(self, why):
        self.sent = 0
        self.wrapped = False
        return lc.Client._drop(self, why)


def _control(client, kind, body):
    """The console's lease: send one picture every `every` ms for `for` ms."""
    if kind != wire.CONTROL:
        return
    try:
        doc = lc.json.loads(body)
        client.every = max(50, int(doc.get("every") or 1000))
        client.until = time.ticks_add(time.ticks_ms(), int(doc.get("for") or 0))
    except Exception:
        pass
