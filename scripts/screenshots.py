#!/usr/bin/env python3
"""The README's screenshots, taken from the demo world and checked for leaks.

    python3 scripts/screenshots.py                 # every scene into docs/screenshots
    python3 scripts/screenshots.py --only cameras  # one or more scenes by name
    python3 scripts/screenshots.py --list          # the scene names

It starts scripts/demo.py, so what is on screen is the real console showing an
invented machine. Then, before a picture is kept, everything the browser was
given for it — the page's text and attributes, every response body, every
event on the live stream — is searched for this machine's real identifiers:
its name, your user name and home, every address and MAC it has, the
networks it knows, its Bluetooth adapter and devices, your console's own
devices, flows, tags and tokens, and the names and emails in git. That list is
built here, held in memory, and never written anywhere. One match and the
picture is deleted and the run fails.

The camera shows docs/screenshots/source/bench.jpg. Without it a drawn test
card stands in, and the run says so. A JPEG carrying EXIF, XMP, a comment or
an embedded profile is refused, since that is where a camera writes things.
"""
import argparse
import getpass
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import jsc  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")
PHOTO = os.path.join(OUT, "source", "bench.jpg")
# What the address bar says. The demo listens on a spare port; a proxy in here
# answers for this name, so the pages see the address a real install has.
ADDRESS = "auto-blox.local:8787"
PHONE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

# name -> (path, phone, seconds to wait, script run once it has loaded)
CLICK = """(function (sel, text) {
  var all = document.querySelectorAll(sel);
  for (var i = 0; i < all.length; i++) {
    if (!text || (all[i].textContent || '').indexOf(text) !== -1) { all[i].click(); return true; }
  }
  return false;
})(%s, %s)"""


def click(sel, text=None):
    return CLICK % (json.dumps(sel), json.dumps(text))


# Two commands into the terminal, one after the other, as someone would.
TYPE = """(function () {
  var cmds = %s, i = 0;
  function next() {
    var box = document.getElementById('term-input');
    if (!box || i >= cmds.length) return;
    box.value = cmds[i++];
    box.form.requestSubmit();
    setTimeout(next, 900);
  }
  next();
  return true;
})()"""

# The flow list, enabled ones first.
SORT = """(function () {
  var s = Array.prototype.filter.call(document.querySelectorAll('select'),
    function (x) { return x.querySelector('option[value=status]'); })[0];
  if (!s) return false;
  s.value = 'status';
  s.dispatchEvent(new Event('change'));
  return true;
})()"""

# The variable library opened, and scrolled to where it starts.
LIBRARY = """(function () {
  var box = document.querySelector('details.var-lib');
  if (!box) return false;
  box.open = true;
  box.dispatchEvent(new Event('toggle'));
  var out = Array.prototype.filter.call(document.querySelectorAll('*'), function (n) {
    return n.children.length === 0 && /^output$/i.test((n.textContent || '').trim());
  })[0];
  (out || box).scrollIntoView({block: 'start'});
  return true;
})()"""

# The side panel down to the board's pin map, which lives under the tags.
PINS = """(function () {
  var heads = document.querySelectorAll('.pane-head');
  for (var i = 0; i < heads.length; i++) {
    if (/ pins$|40-pin header/.test(heads[i].textContent.split('\\n')[0].trim()) ||
        / pins/.test(heads[i].firstChild && heads[i].firstChild.textContent || '')) {
      heads[i].scrollIntoView({block: 'start'});
      return true;
    }
  }
  return false;
})()"""

SCENES = [
    ("overview", "/", False, 8, TYPE % json.dumps(["uptime", "iw dev wlan0 info"])),
    ("flows", "/flows", False, 5, SORT),
    ("flow-editor", "/flows?flow=ex_motor_drive&node=rampr", False, 6, None),
    ("flow-variables", "/flows?flow=ex_heartbeat_watch&node=l", False, 5,
     LIBRARY),
    ("flow-live", "/flows?flow=ex_motor_drive", False, 5, PINS),
    ("iot-setup", "/iot#setup", False, 5, None),
    ("iot-devices", "/iot#devices", False, 5, None),
    ("iot-device", "/iot#devices", False, 5, click(".iot-card, .dev-card, [data-device]")),
    ("iot-bluetooth", "/iot#bluetooth", False, 5, None),
    ("iot-enrollment", "/iot#enrollment", False, 5, None),
    ("iot-flashing", "/iot#flashing", False, 5, None),
    ("iot-boards", "/iot#boards", False, 5, None),
    ("cameras", "/cameras", False, 9, None),
    ("login", "/static/login.html", False, 3, None),
    ("phone-overview", "/", True, 8, None),
    ("phone-flows", "/flows", True, 5, SORT),
    ("phone-iot", "/iot#devices", True, 5, None),
    ("phone-cameras", "/cameras", True, 9, None),
]


# =========================================================================
# What must never appear
# =========================================================================
def _run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def real_identifiers():
    """{category: set of strings} describing this machine and its owner."""
    out = {}

    def add(kind, *values):
        for v in values:
            v = str(v or "").strip()
            if v:
                out.setdefault(kind, set()).add(v)

    home = os.path.expanduser("~")
    add("hostname", _read("/proc/sys/kernel/hostname"), socket.gethostname())
    add("user", getpass.getuser(), os.path.basename(home))
    add("home", home)
    add("machine-id", _read("/etc/machine-id"))
    for line in _run(["ip", "-o", "addr"]).splitlines():
        m = re.search(r"\binet6? ([0-9a-fA-F:.]+)/", line)
        if m and m.group(1) not in ("127.0.0.1", "::1"):
            add("ip address", m.group(1))
    for path in glob.glob("/sys/class/net/*/address"):
        mac = _read(path).strip()
        if mac and mac != "00:00:00:00:00:00":
            add("mac", mac)
    for name in os.listdir("/sys/class/net") if os.path.isdir("/sys/class/net") else []:
        add("interface", name)
    for line in _run(["iw", "dev"]).splitlines():
        s = line.strip()
        if s.startswith("ssid "):
            add("ssid", s[5:])
    for line in _run(["nmcli", "-t", "-f", "NAME", "connection", "show"]).splitlines():
        add("saved network", line)
    ts = _run(["tailscale", "status", "--json"])
    try:
        me = json.loads(ts).get("Self") or {}
        add("tailscale", me.get("DNSName", "").rstrip("."), me.get("HostName"),
            *(me.get("TailscaleIPs") or []))
        dns = me.get("DNSName", "").rstrip(".").split(".", 1)
        if len(dns) == 2:
            add("tailscale", dns[1])
    except ValueError:
        pass
    for line in _run(["bluetoothctl", "list"]).splitlines():
        bits = line.split(None, 2)
        if len(bits) >= 3 and bits[0] == "Controller":
            add("bluetooth", bits[1], bits[2].replace("[default]", "").strip())
    for line in _run(["bluetoothctl", "devices"]).splitlines():
        bits = line.split(None, 2)
        if len(bits) >= 2 and bits[0] == "Device":
            add("bluetooth", *bits[1:])
    for path in glob.glob("/dev/serial/by-id/*"):
        add("serial port", os.path.basename(path))

    cfg = os.path.join(home, ".config", "zero2w-console")
    add("console token", _read(os.path.join(cfg, "token")))
    for d in (_json(os.path.join(cfg, "iot.json")) or {}).get("devices", []):
        add("console device", d.get("name"), d.get("id"), d.get("mac"), d.get("ip"),
            d.get("note"), d.get("token"), d.get("enroll_token"))
        add("console device", (d.get("probe") or {}).get("unique_id"))
    for g in (_json(os.path.join(cfg, "iot.json")) or {}).get("groups", []) or []:
        add("console group", g.get("name") if isinstance(g, dict) else g)
    for f in (_json(os.path.join(cfg, "flows.json")) or {}).get("flows", []):
        add("console flow", f.get("name"), f.get("id"))
    for t in (_json(os.path.join(cfg, "tags.json")) or {}).get("tags", []):
        add("console tag", t.get("name"))
    for argv in (["git", "config", "--global", "user.email"],
                 ["git", "config", "--global", "user.name"],
                 ["git", "-C", ROOT, "log", "--format=%ae%n%an%n%ce%n%cn"]):
        for line in _run(argv).splitlines():
            add("git identity", line)
    return out


def demo_values():
    """Everything the demo deliberately says, which may coincide with a real
    value because it is generic (a default subnet, a product name)."""
    import demo
    words = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif v is not None:
            words.add(str(v).lower())

    walk(demo.DEMO)
    return words


def shipped_text():
    """What the repository itself says, and so already public: an example's
    flow or tag name, a board's label, a variable's sample value."""
    sys.path.insert(0, ROOT)
    from zero2w_console import examples, flows, iot
    return "\n".join(json.dumps(x, ensure_ascii=False).lower() for x in (
        examples.catalogue(), flows.REGISTRY, flows.VARIABLES, iot.board_summaries()))


# Names every Linux box has; an interface called something else is checked.
GENERIC = re.compile(r"^(?:lo|eth\d+|wlan\d+|end\d+|usb\d+|tailscale\d+|docker\d+|"
                     r"p2p-dev-wlan\d+|hci\d+)$")


class Denylist:
    """The strings to look for, and how to look for each."""

    # Never excused, whatever else says the same thing.
    ALWAYS = ("console token", "machine-id", "mac", "home", "serial port")

    def __init__(self, found, allow_words=(), allow_text=""):
        self.skipped = {}
        self.rules = []
        allow_words = {w.lower() for w in allow_words}
        for kind, values in sorted(found.items()):
            for value in sorted(values):
                low = value.lower()
                excused = kind not in self.ALWAYS and (
                    len(value) < 4 or low in allow_words or GENERIC.match(low)
                    or re.search(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])",
                                 allow_text))
                if excused:
                    self.skipped[kind] = self.skipped.get(kind, 0) + 1
                    continue
                for pattern in self._patterns(kind, value):
                    self.rules.append((kind, value, pattern))

    @staticmethod
    def _patterns(kind, value):
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value):
            hexes = re.split(r"[:-]", value.lower())
            # With colons, with dashes, bare — and as the tail of a name like
            # enx<mac> or esp32-<last six>.
            yield re.compile(r"(?i)" + "[:-]?".join(hexes))
            return
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
            yield re.compile(r"(?<![\d.])" + re.escape(value) + r"(?![\d])")
            return
        # Whole words only: a user called "ann" is not in "channel".
        yield re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])")

    def __len__(self):
        return len({(k, v) for k, v, _ in self.rules})

    def search(self, text):
        """[(kind, masked context)] for every rule that matches."""
        hits = []
        for kind, value, pattern in self.rules:
            m = pattern.search(text)
            if m:
                a, b = max(0, m.start() - 30), min(len(text), m.end() + 30)
                context = (text[a:m.start()] + "█" * 6 + text[m.end():b])
                hits.append((kind, " ".join(context.split())))
        return hits


# =========================================================================
# Files that carry more than their pixels
# =========================================================================
def png_problems(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return ["not a PNG"]
    bad, i = [], 8
    while i + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[i:i + 8])
        if kind in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"):
            bad.append("carries a %s chunk" % kind.decode())
        i += 12 + length
    return bad


def jpeg_problems(data):
    """Anything in a JPEG besides the picture: EXIF/XMP (APP1), profiles and
    maker data (APP2..APP15), comments."""
    if data[:2] != b"\xff\xd8":
        return ["not a JPEG"]
    bad, i = [], 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xDA:          # start of scan: the rest is picture
            break
        length = (data[i + 2] << 8) | data[i + 3]
        if 0xE1 <= marker <= 0xEF:
            bad.append("an APP%d segment (%s)" % (
                marker - 0xE0, data[i + 4:i + 10].split(b"\0")[0].decode("latin-1")))
        elif marker == 0xFE:
            bad.append("a comment")
        i += 2 + length
    return bad


def strip_jpeg(data):
    """The picture alone: every APP1..APP15 and comment segment dropped."""
    out, i = bytearray(data[:2]), 2
    while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:
        marker = data[i + 1]
        length = (data[i + 2] << 8) | data[i + 3]
        if not (0xE1 <= marker <= 0xEF or marker == 0xFE):
            out += data[i:i + 2 + length]
        i += 2 + length
    return bytes(out + data[i:])


# =========================================================================
# The pieces that run
# =========================================================================
class Proxy(threading.Thread):
    """Answers for ADDRESS by passing bytes to the demo, both ways. The console
    reads the path out of an absolute request line, so nothing is rewritten.

    The pages' one outside request, the webfont, is tunnelled to Google as a
    browser would fetch it, so the pictures are in the real font. Anything
    else asked for by name goes to the demo."""
    daemon = True
    OUTSIDE = ("fonts.googleapis.com:443", "fonts.gstatic.com:443")

    def __init__(self, target_port):
        threading.Thread.__init__(self, name="proxy")
        self.target = target_port
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            client, _ = self.sock.accept()
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = client.recv(65536)
                if not chunk:
                    return client.close()
                head += chunk
            first = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if first[:1] == ["CONNECT"]:
                if len(first) < 2 or first[1] not in self.OUTSIDE:
                    client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return client.close()
                host, port = first[1].rsplit(":", 1)
                upstream = socket.create_connection((host, int(port)), timeout=20)
                upstream.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                upstream = socket.create_connection(("127.0.0.1", self.target))
                upstream.sendall(head)
        except (OSError, IndexError, ValueError):
            return client.close()
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def browser_exe():
    for name in ("chromium", "chromium-browser", "google-chrome"):
        if shutil.which(name):
            return shutil.which(name)
    raise SystemExit("needs chromium")


def test_card(path):
    """A drawn stand-in for the camera, when there is no photo yet."""
    b = jsc.Browser(browser_exe(), width=640, height=480)
    try:
        b.call("Page.enable")
        b.call("Emulation.setDeviceMetricsOverride", width=640, height=480,
               deviceScaleFactor=1, mobile=False)
        html = ("<meta charset='utf-8'><style>html,body{margin:0;height:100%}</style>"
                "<body style='width:640px;height:480px;display:grid;"
                "grid-template-columns:repeat(8,1fr);grid-template-rows:480px;"
                "font:28px sans-serif'>"
                + "".join("<div style='background:%s'></div>" % c for c in (
                    "#c0c0c0", "#c0c000", "#00c0c0", "#00c000", "#c000c0",
                    "#c00000", "#0000c0", "#101010"))
                + "<div style='position:absolute;inset:180px 120px;background:#000c;"
                  "color:#fff;display:grid;place-items:center'>Auto-Blox · camera"
                  "</div></body>")
        b.call("Page.navigate", url="data:text/html," + urllib.parse.quote(html))
        b.settle(1.5)
        b.screenshot(path, fmt="jpeg", quality=80,
                     clip={"x": 0, "y": 0, "width": 640, "height": 480, "scale": 1})
        with open(path, "rb") as fh:
            clean = strip_jpeg(fh.read())
        with open(path, "wb") as fh:
            fh.write(clean)
    finally:
        b.close()
    return path


def start_demo(photo, log):
    argv = [sys.executable, os.path.join(HERE, "demo.py"), "--serve", "--photo", photo]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    for line in proc.stdout:
        log(line.rstrip())
        m = re.search(r"ready (http://\S+)", line)
        if m:
            threading.Thread(target=lambda: [None for _ in proc.stdout],
                             daemon=True).start()
            return proc, m.group(1)
    raise SystemExit("the demo stopped before it was ready")


class Scene:
    """One page in one browser, with everything the browser was handed."""

    def __init__(self, b):
        self.b = b

    def collect(self, events):
        """Bodies of what was loaded, and what arrived on the event stream."""
        texts, pending = [], {}
        for ev in events:
            m, p = ev["method"], ev.get("params", {})
            if m == "Network.responseReceived":
                pending[p["requestId"]] = p["response"].get("url")
            elif m == "Network.loadingFinished" and p["requestId"] in pending:
                try:
                    body = self.b.call("Network.getResponseBody", requestId=p["requestId"])
                    if not body.get("base64Encoded"):
                        texts.append(body.get("body") or "")
                except RuntimeError:
                    pass
            elif m == "Network.eventSourceMessageReceived":
                texts.append(p.get("data") or "")
            elif m == "Runtime.consoleAPICalled":
                texts.append(json.dumps([a.get("value") for a in p.get("args", [])]))
        return texts

    def dom_text(self):
        return self.b.evaluate("""(function () {
          var out = [document.title, location.href, document.documentElement.innerText];
          document.querySelectorAll('*').forEach(function (n) {
            for (var i = 0; i < n.attributes.length; i++) out.push(n.attributes[i].value);
            if (n.value !== undefined && typeof n.value === 'string') out.push(n.value);
          });
          document.querySelectorAll('svg text').forEach(function (t) { out.push(t.textContent); });
          return out.join('\\n');
        })()""") or ""


def shoot(scenes, out_dir, deny, base, proxy_port, log):
    bad = 0
    for phone in (False, True):
        todo = [s for s in scenes if s[2] == phone]
        if not todo:
            continue
        b = jsc.Browser(browser_exe(), phone=phone,
                        width=390 if phone else 1400, height=844 if phone else 900,
                        user_agent=PHONE_UA if phone else None,
                        extra=["--proxy-server=http://127.0.0.1:%d" % proxy_port,
                               "--proxy-bypass-list=<-loopback>"])
        try:
            for domain in ("Runtime", "Page", "Network", "Log"):
                b.call(domain + ".enable")
            b.call("Emulation.setTimezoneOverride", timezoneId="UTC")
            if phone:
                b.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                       deviceScaleFactor=2, mobile=True)
            for name, path, _, wait, script in todo:
                target = os.path.join(out_dir, name + ".png")
                b.call("Page.navigate", url="about:blank")
                b.settle(0.3)
                b.call("Page.navigate", url=base + path)
                events = b.settle(wait)
                if script:
                    if not b.evaluate(script):
                        log("  %-16s the script found nothing to act on" % name)
                    events += b.settle(3)
                scene = Scene(b)
                texts = scene.collect(events) + [scene.dom_text()]
                b.screenshot(target)
                hits = []
                for text in texts:
                    hits += deny.search(text)
                problems = png_problems(target)
                if hits or problems:
                    os.remove(target)
                    bad += 1
                    log("  LEAK %-12s deleted" % name)
                    for kind, context in sorted(set(hits))[:20]:
                        log("       %s: …%s…" % (kind, context))
                    for p in problems:
                        log("       " + p)
                else:
                    log("  ok   %-16s %6.0f KB" % (name, os.path.getsize(target) / 1024))
        finally:
            b.close()
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--photo", default=PHOTO)
    ap.add_argument("--only", nargs="*", help="scene names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.list:
        for s in SCENES:
            print("%-16s %s%s" % (s[0], s[1], "  (phone)" if s[2] else ""))
        return 0
    scenes = [s for s in SCENES if not a.only or s[0] in a.only]
    if not scenes:
        raise SystemExit("no such scene; --list shows them")

    def log(text):
        print(text)
        sys.stdout.flush()

    # The identity first, before anything could be confused with it.
    found = real_identifiers()
    deny = Denylist(found, allow_words=demo_values(), allow_text=shipped_text())
    log("checking against %d real identifiers (%s)" % (
        len(deny), ", ".join("%s %d" % (k, len(v)) for k, v in sorted(found.items()))))
    if deny.skipped:
        log("  not checked, being short or shared with the demo or the examples: %s"
            % ", ".join("%s %d" % kv for kv in sorted(deny.skipped.items())))

    work = tempfile.mkdtemp(prefix="auto-blox-shots-")
    photo = a.photo
    if not os.path.isfile(photo):
        photo = test_card(os.path.join(work, "test-card.jpg"))
        log("NOTE: no %s yet, so the camera shows a drawn test card"
            % os.path.relpath(a.photo, ROOT))
    with open(photo, "rb") as fh:
        problems = jpeg_problems(fh.read())
    if problems:
        raise SystemExit("%s carries %s; save a plain frame from the camera wall "
                         "instead" % (photo, ", ".join(problems)))

    os.makedirs(a.out, exist_ok=True)
    proc, url = start_demo(photo, log if a.verbose else (lambda t: None))
    try:
        proxy = Proxy(int(url.rsplit(":", 1)[1]))
        proxy.start()
        bad = shoot(scenes, a.out, deny, "http://" + ADDRESS, proxy.port, log)
    finally:
        try:
            os.killpg(proc.pid, 15)
            proc.wait(timeout=20)
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)
    kept = len(scenes) - bad
    log("\n%d of %d kept, %d leak%s" % (kept, len(scenes), bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
