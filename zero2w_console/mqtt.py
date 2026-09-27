#!/usr/bin/env python3
"""A minimal MQTT 3.1.1 client — publish and subscribe, QoS 0, stdlib only."""
import socket
import ssl
import struct
import threading
import time

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK = 1, 2, 3, 8, 9
UNSUBSCRIBE, PINGREQ, PINGRESP, DISCONNECT = 10, 12, 13, 14

CONNACK_MSG = {
    0: "accepted", 1: "unacceptable protocol version", 2: "client id rejected",
    3: "server unavailable", 4: "bad username or password", 5: "not authorised",
}


def _vbi(n):
    """Variable byte integer (remaining length)."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


class MQTTClient:
    """One connection, a receive thread, and automatic reconnect."""

    def __init__(self, host, port=1883, client_id=None, username=None,
                 password=None, keepalive=45, tls=False, on_message=None,
                 on_state=None):
        self.host, self.port = host, int(port)
        self.client_id = client_id or ("zero2w-%d" % (int(time.time()) % 100000))
        self.username, self.password = username or None, password or None
        self.keepalive, self.tls = int(keepalive), bool(tls)
        self.on_message = on_message or (lambda t, p: None)
        self.on_state = on_state or (lambda s, d: None)
        self.sock = None
        self.subs = {}                 # topic -> qos
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._pid = 0
        self._lock = threading.Lock()
        self._thread = None
        self.last_error = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mqtt-%s" % self.host)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.connected.clear()
        with self._lock:
            if self.sock:
                try:
                    self._send(bytes([DISCONNECT << 4, 0]))
                except Exception:
                    pass
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
                backoff = 1.0
                self._receive()
            except Exception as exc:
                self.last_error = str(exc)
                self.connected.clear()
                self.on_state("disconnected", str(exc))
            finally:
                with self._lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        if self.tls:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        s.settimeout(None)
        with self._lock:
            self.sock = s

        flags = 0x02                                  # clean session
        payload = _str(self.client_id)
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        var = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        self._send(bytes([CONNECT << 4]) + _vbi(len(var + payload)) + var + payload)

        kind, body = self._read_packet()
        if kind != CONNACK or len(body) < 2:
            raise IOError("no CONNACK from %s" % self.host)
        code = body[1]
        if code != 0:
            raise IOError("broker refused connection: %s" % CONNACK_MSG.get(code, code))
        self.connected.set()
        self.on_state("connected", "%s:%d" % (self.host, self.port))
        for topic, qos in list(self.subs.items()):
            self._send_subscribe(topic, qos)
        threading.Thread(target=self._pinger, daemon=True).start()

    def _pinger(self):
        while self.connected.is_set() and not self._stop.is_set():
            if self._stop.wait(max(5, self.keepalive * 0.7)):
                return
            try:
                self._send(bytes([PINGREQ << 4, 0]))
            except Exception:
                return

    # -- io ----------------------------------------------------------------
    def _send(self, data):
        with self._lock:
            if not self.sock:
                raise IOError("not connected")
            self.sock.sendall(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("connection closed by broker")
            buf += chunk
        return buf

    def _read_packet(self):
        head = self._recv_exact(1)[0]
        mult, length = 1, 0
        while True:
            b = self._recv_exact(1)[0]
            length += (b & 127) * mult
            if not (b & 128):
                break
            mult *= 128
            if mult > 128 ** 4:
                raise IOError("malformed remaining length")
        body = self._recv_exact(length) if length else b""
        return head >> 4, body

    def _receive(self):
        while not self._stop.is_set():
            kind, body = self._read_packet()
            if kind == PUBLISH:
                if len(body) < 2:
                    continue
                tlen = struct.unpack("!H", body[:2])[0]
                topic = body[2:2 + tlen].decode("utf-8", "replace")
                payload = body[2 + tlen:]
                try:
                    self.on_message(topic, payload.decode("utf-8"))
                except UnicodeDecodeError:
                    self.on_message(topic, repr(payload))
            elif kind == PINGRESP or kind == SUBACK:
                continue

    # -- api ---------------------------------------------------------------
    def _next_pid(self):
        self._pid = (self._pid % 65534) + 1
        return self._pid

    def publish(self, topic, payload, retain=False):
        if not self.connected.is_set():
            raise IOError("not connected to %s" % self.host)
        body = _str(topic) + str(payload).encode("utf-8")
        head = (PUBLISH << 4) | (0x01 if retain else 0x00)
        self._send(bytes([head]) + _vbi(len(body)) + body)

    def subscribe(self, topic, qos=0):
        self.subs[topic] = qos
        if self.connected.is_set():
            self._send_subscribe(topic, qos)

    def _send_subscribe(self, topic, qos):
        body = struct.pack("!H", self._next_pid()) + _str(topic) + bytes([qos & 0x03])
        self._send(bytes([(SUBSCRIBE << 4) | 0x02]) + _vbi(len(body)) + body)


class MQTTPool:
    """One client per broker, shared by every node that names it."""

    def __init__(self, on_state=None):
        self.clients = {}
        self.lock = threading.Lock()
        self.on_state = on_state

    def get(self, host, port, username=None, password=None, tls=False, on_message=None):
        key = (host, int(port), username or "", bool(tls))
        with self.lock:
            c = self.clients.get(key)
            if c is None:
                c = MQTTClient(host, port, username=username, password=password,
                               tls=tls, on_message=self._fanout(key),
                               on_state=lambda s, d, k=key: self.on_state and self.on_state(k, s, d))
                c._handlers = []
                self.clients[key] = c
                c.start()
            if on_message and on_message not in c._handlers:
                c._handlers.append(on_message)
            return c

    def _fanout(self, key):
        def deliver(topic, payload):
            c = self.clients.get(key)
            for h in list(getattr(c, "_handlers", [])):
                try:
                    h(topic, payload)
                except Exception:
                    pass
        return deliver

    def status(self):
        with self.lock:
            return [{"broker": "%s:%d" % (k[0], k[1]),
                     "connected": c.connected.is_set(),
                     "topics": sorted(c.subs),
                     "error": c.last_error}
                    for k, c in self.clients.items()]

    def stop_all(self):
        with self.lock:
            for c in self.clients.values():
                c.stop()
            self.clients.clear()
