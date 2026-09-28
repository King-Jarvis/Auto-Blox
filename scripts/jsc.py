"""Ask a headless Chromium to compile a file, and say where it fails."""
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import time
import urllib.request


class _WS:
    def __init__(self, url):
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n"
                           % (path, hostport, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        assert b" 101 " in head, head[:120]
        self.buf = b""

    def send(self, obj):
        data = json.dumps(obj).encode()
        head = b"\x81"
        n = len(data)
        if n < 126:
            head += struct.pack("!B", 0x80 | n)
        elif n < 65536:
            head += struct.pack("!BH", 0x80 | 126, n)
        else:
            head += struct.pack("!BQ", 0x80 | 127, n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        _b1, b2 = self._read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        return json.loads(self._read(n))


def compile_all(exe, static_dir, profile):
    port = _free_port()
    proc = subprocess.Popen(
        [exe, "--headless=new", "--remote-debugging-port=%d" % port, "--no-first-run",
         "--disable-gpu", "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        ws_url = _wait_for_target(port)
        ws = _WS(ws_url)
        seq = [0]

        def call(method, **params):
            seq[0] += 1
            ws.send({"id": seq[0], "method": method, "params": params})
            while True:
                msg = ws.recv()
                if msg.get("id") == seq[0]:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result", {})

        call("Runtime.enable")
        bad = 0
        for name in sorted(os.listdir(static_dir)):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(static_dir, name), encoding="utf-8") as fh:
                source = fh.read()
            res = call("Runtime.compileScript", expression=source,
                       sourceURL=name, persistScript=False)
            det = res.get("exceptionDetails")
            if not det:
                print("ok    %s (%d lines)" % (name, source.count("\n") + 1))
                continue
            bad += 1
            line = det.get("lineNumber", 0)
            print("FAIL  %s:%d:%d  %s" % (name, line + 1, det.get("columnNumber", 0),
                                          det.get("exception", {}).get("description")
                                          or det.get("text")))
            for n in range(max(0, line - 2), min(source.count("\n"), line + 2)):
                print("      %5d %s" % (n + 1, source.splitlines()[n]))
        return 1 if bad else 0
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_target(port, tries=80):
    for _ in range(tries):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as fh:
                for t in json.load(fh):
                    if t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    raise SystemExit("chromium never exposed a target")


class Browser:
    """One headless Chromium, driven over the DevTools protocol.

    Shared by check-pages.py and screenshots.py: a page loaded, what it said
    while loading, and a picture of it."""

    def __init__(self, exe, phone=False, width=1400, height=1000, extra=(),
                 user_agent=None):
        self.profile = __import__("tempfile").mkdtemp(prefix="auto-blox-chromium-")
        port = _free_port()
        argv = [exe, "--headless=new", "--remote-debugging-port=%d" % port,
                "--no-first-run", "--disable-gpu", "--hide-scrollbars",
                "--user-data-dir=" + self.profile]
        if phone:
            argv += ["--window-size=%d,%d" % (width, height),
                     "--force-device-scale-factor=2"]
            if user_agent:
                argv.append("--user-agent=" + user_agent)
        else:
            argv.append("--window-size=%d,%d" % (width, height))
        argv += list(extra) + ["about:blank"]
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.ws = _WS(_wait_for_target(port))
        self.seq = 0
        self.events = []

    def call(self, method, **params):
        self.seq += 1
        self.ws.send({"id": self.seq, "method": method, "params": params})
        while True:
            msg = self.ws.recv()
            if msg.get("id") == self.seq:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def settle(self, seconds):
        """Collect events for a while; returns them, and the ones seen before."""
        end = time.time() + seconds
        self.ws.sock.settimeout(0.4)
        while time.time() < end:
            try:
                msg = self.ws.recv()
            except Exception:
                continue
            if "method" in msg:
                self.events.append(msg)
        self.ws.sock.settimeout(None)
        out, self.events = self.events, []
        return out

    def evaluate(self, expression):
        got = self.call("Runtime.evaluate", returnByValue=True, awaitPromise=True,
                        expression=expression)
        return got.get("result", {}).get("value")

    def screenshot(self, path, fmt="png", full=False, **extra):
        shot = self.call("Page.captureScreenshot", format=fmt,
                         captureBeyondViewport=full, **extra)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
        return path

    def close(self):
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            self.proc.wait(timeout=10)
        except Exception:
            pass
        __import__("shutil").rmtree(self.profile, ignore_errors=True)
