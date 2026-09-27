#!/usr/bin/env python3
"""Auto-Blox — a LAN dashboard for this board."""
import argparse
import getpass
import json
import os
import queue
import re
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import buses as busmod
from . import flows as flowmod
from . import gpio as gpiomod
from . import fleet as fleetmod
from . import bluetooth as btmod
from . import gatt as gattmod
from . import iot as iotmod
from . import examples as examplemod
from . import link as linkmod
from . import tags as tagmod
from . import tlscert

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "zero2w-console")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")

CLK_TCK = os.sysconf("SC_CLK_TCK")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f]")

# journald PRIORITY -> the design system's five states.
PRIORITY_STATE = {0: "critical", 1: "critical", 2: "critical", 3: "critical",
                  4: "warn", 5: "ok", 6: "idle", 7: "idle"}


# --------------------------------------------------------------------------- auth
def load_or_create_token():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_FILE, "w") as fh:
        fh.write(tok + "\n")
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return tok


# ------------------------------------------------------------------ small readers
def read_text(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


def read_int(path):
    raw = read_text(path).strip()
    try:
        return int(raw)
    except ValueError:
        return None


def fmt_bytes(n, binary=True):
    """Kernel units: GiB / MiB, matching what free and df report."""
    if n is None:
        return None
    step = 1024.0 if binary else 1000.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit not in ("B", "KiB") else "%.0f %s" % (n, unit)
        n /= step
    return None


def fmt_uptime(seconds):
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "%dd %02dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


# ------------------------------------------------------------------- the collector
class Collector:
    """Holds the previous sample so rates and percentages are real deltas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.prev_cpu = None
        self.prev_net = None
        self.prev_net_at = None
        self.prev_proc = {}
        self.prev_proc_at = None
        self.zones = self._discover_zones()
        self.hostname = read_text("/proc/sys/kernel/hostname", "unknown").strip()
        self.user = getpass.getuser()      # who /api/exec runs as
        self.model = read_text("/proc/device-tree/model", "").replace("\x00", "").strip()

    # -- thermal -----------------------------------------------------------
    @staticmethod
    def _discover_zones():
        """Read each zone's real trip points instead of assuming thresholds."""
        zones = []
        base = "/sys/class/thermal"
        try:
            names = sorted(n for n in os.listdir(base) if n.startswith("thermal_zone"))
        except OSError:
            return zones
        for i, name in enumerate(names):
            path = os.path.join(base, name)
            ztype = read_text(os.path.join(path, "type")).strip()
            if not ztype:
                continue
            trips = []
            for t in sorted(os.listdir(path)):
                if re.fullmatch(r"trip_point_\d+_temp", t):
                    v = read_int(os.path.join(path, t))
                    if v:
                        trips.append(v / 1000.0)
            trips.sort()
            # Thresholds come from the kernel, never a guess: most zones here
            # have only a 100 C critical trip and so no warn band.
            warn = trips[0] if len(trips) >= 2 else None
            serious = trips[1] if len(trips) >= 3 else None
            zones.append({
                "id": name,
                "type": ztype,
                "path": os.path.join(path, "temp"),
                "warn": warn,
                "serious": serious,
                "crit": trips[-1] if trips else None,
                # Fixed series order, per the design system: cpu, gpu, ve, ddr.
                "series": None,
            })
        order = ["cpu-thermal", "gpu-thermal", "ve-thermal", "ddr-thermal"]
        for z in zones:
            idx = order.index(z["type"]) if z["type"] in order else len(order)
            z["series"] = "series-%d" % (min(idx, 3) + 1)
        zones.sort(key=lambda z: order.index(z["type"]) if z["type"] in order else 99)
        return zones

    def thermal(self):
        out = []
        for z in self.zones:
            raw = read_int(z["path"])
            temp = round(raw / 1000.0, 1) if raw is not None else None
            state = "idle"
            if temp is not None:
                state = "ok"
                if z["crit"] and temp >= z["crit"]:
                    state = "critical"
                elif z["serious"] and temp >= z["serious"]:
                    state = "serious"
                elif z["warn"] and temp >= z["warn"]:
                    state = "warn"
            out.append({"zone": z["type"], "temp": temp, "warn": z["warn"],
                        "serious": z["serious"], "trip": z["crit"],
                        "series": z["series"], "state": state,
                        "source": z["path"]})
        return out

    # -- cpu ---------------------------------------------------------------
    def cpu(self):
        cores, total = [], None
        for line in read_text("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            label = parts[0]
            vals = [int(v) for v in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            busy = sum(vals) - idle
            if label == "cpu":
                total = (busy, sum(vals))
            else:
                cores.append((label, busy, sum(vals)))
        snapshot = {"total": total, "cores": cores}
        pct_cores, pct_total = [], None
        if self.prev_cpu:
            pt = self.prev_cpu["total"]
            if total and pt and total[1] != pt[1]:
                pct_total = max(0.0, min(100.0, 100.0 * (total[0] - pt[0]) / (total[1] - pt[1])))
            prev_map = {c[0]: c for c in self.prev_cpu["cores"]}
            for label, busy, tot in cores:
                p = prev_map.get(label)
                if p and tot != p[2]:
                    pct = max(0.0, min(100.0, 100.0 * (busy - p[1]) / (tot - p[2])))
                else:
                    pct = 0.0
                pct_cores.append({"core": label, "pct": round(pct, 1)})
        else:
            pct_cores = [{"core": c[0], "pct": 0.0} for c in cores]
        self.prev_cpu = snapshot
        freq = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        return {"total": round(pct_total, 1) if pct_total is not None else None,
                "cores": pct_cores,
                "mhz": round(freq / 1000) if freq else None}

    # -- memory ------------------------------------------------------------
    @staticmethod
    def memory():
        info = {}
        for line in read_text("/proc/meminfo").splitlines():
            k, _, rest = line.partition(":")
            v = rest.strip().split()
            if v:
                try:
                    info[k] = int(v[0]) * 1024
                except ValueError:
                    pass
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        swap_total = info.get("SwapTotal", 0)
        swap_used = swap_total - info.get("SwapFree", 0)
        pct = (100.0 * used / total) if total else 0.0
        return {"total": total, "used": used, "available": avail,
                "pct": round(pct, 1),
                "used_h": fmt_bytes(used), "total_h": fmt_bytes(total),
                "available_h": fmt_bytes(avail),
                "swap_total": swap_total, "swap_used": swap_used,
                "swap_pct": round(100.0 * swap_used / swap_total, 1) if swap_total else 0.0,
                "swap_used_h": fmt_bytes(swap_used), "swap_total_h": fmt_bytes(swap_total),
                "state": "critical" if pct >= 90 else "warn" if pct >= 75 else "ok"}

    # -- filesystems -------------------------------------------------------
    @staticmethod
    def filesystems():
        keep = []
        seen = set()
        for line in read_text("/proc/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mount, fstype = parts[0], parts[1], parts[2]
            interesting = dev.startswith("/dev/") or (fstype == "tmpfs" and mount in ("/tmp", "/var/log"))
            if not interesting or mount in seen:
                continue
            if fstype in ("devtmpfs", "squashfs"):
                continue
            seen.add(mount)
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - (st.f_bfree * st.f_frsize)
            if total == 0:
                continue
            pct = 100.0 * used / total
            keep.append({"device": dev, "mount": mount, "fstype": fstype,
                         "pct": round(pct, 1), "used_h": fmt_bytes(used),
                         "total_h": fmt_bytes(total), "free_h": fmt_bytes(free),
                         "state": "critical" if pct >= 90 else "warn" if pct >= 75 else "ok"})
        keep.sort(key=lambda f: (f["mount"] != "/", len(f["mount"]), f["mount"]))
        # One device bind-mounted twice is one filesystem: keep the shortest mount.
        deduped, seen_dev = [], set()
        for f in keep:
            if f["device"].startswith("/dev/"):
                if f["device"] in seen_dev:
                    continue
                seen_dev.add(f["device"])
            deduped.append(f)
        return deduped

    # -- network -----------------------------------------------------------
    def network(self):
        counters, now = {}, time.monotonic()
        for line in read_text("/proc/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            f = rest.split()
            if len(f) < 9:
                continue
            counters[name.strip()] = (int(f[0]), int(f[8]))
        addrs = self._addresses()
        default = self._default_route()
        out = []
        for name, (rx, tx) in sorted(counters.items()):
            if name == "lo":
                continue
            operstate = read_text("/sys/class/net/%s/operstate" % name).strip() or "unknown"
            carrier = read_int("/sys/class/net/%s/carrier" % name)
            rx_rate = tx_rate = None
            if self.prev_net and name in self.prev_net and self.prev_net_at:
                dt = now - self.prev_net_at
                if dt > 0:
                    prx, ptx = self.prev_net[name]
                    rx_rate = max(0.0, (rx - prx) / dt / 1024.0)
                    tx_rate = max(0.0, (tx - ptx) / dt / 1024.0)
            # A TUN device keeps carrier=1 and operstate=unknown with its daemon
            # stopped, so ask the daemon, or fall back to "has an address".
            vpn = self._vpn_state(name)
            if vpn is not None:
                state, operstate = vpn
            elif operstate == "up" or (operstate == "unknown" and carrier == 1):
                if operstate == "unknown" and not addrs.get(name):
                    state, operstate = "idle", "no address"
                else:
                    state, operstate = "ok", "up"
            elif carrier == 0 and operstate != "down":
                state = "warn"
                operstate = "no-carrier"
            elif operstate == "down":
                state = "critical"
            else:
                state = "idle"
            out.append({"name": name, "operstate": operstate, "state": state,
                        "address": addrs.get(name), "is_default": name == default,
                        "rx_kib": round(rx_rate, 1) if rx_rate is not None else None,
                        "tx_kib": round(tx_rate, 1) if tx_rate is not None else None,
                        "rx_total_h": fmt_bytes(rx), "tx_total_h": fmt_bytes(tx)})
        self.prev_net, self.prev_net_at = counters, now
        out.sort(key=lambda i: (not i["is_default"], i["state"] != "ok", i["name"]))
        return out

    _vpn_cache = {"at": 0.0, "value": None}

    def _vpn_state(self, name):
        """Real tunnel state for interfaces whose carrier bit lies."""
        if not name.startswith("tailscale"):
            return None
        now = time.monotonic()
        if now - Collector._vpn_cache["at"] > 10:
            Collector._vpn_cache["at"] = now
            Collector._vpn_cache["value"] = self._tailscale_status()
        st = Collector._vpn_cache["value"]
        if st is None:
            return ("idle", "unknown")
        backend, online = st
        if backend == "Running" and online:
            return ("ok", "up")
        if backend == "Running":
            return ("warn", "no peers")
        if backend in ("Stopped", "NoState"):
            return ("critical", "stopped")
        if backend == "NeedsLogin":
            return ("warn", "needs login")
        return ("warn", backend.lower())

    @staticmethod
    def _tailscale_status():
        try:
            raw = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                 text=True, timeout=6).stdout
            d = json.loads(raw)
        except Exception:
            return None
        return (d.get("BackendState") or "unknown", bool((d.get("Self") or {}).get("Online")))

    @staticmethod
    def _addresses():
        addrs = {}
        try:
            raw = subprocess.run(["ip", "-4", "-br", "addr"], capture_output=True,
                                 text=True, timeout=4).stdout
        except Exception:
            return addrs
        for line in raw.splitlines():
            f = line.split()
            if len(f) >= 3:
                addrs[f[0]] = f[2]
        return addrs

    @staticmethod
    def _default_route():
        try:
            raw = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                                 text=True, timeout=4).stdout
        except Exception:
            return None
        m = re.search(r"\bdev\s+(\S+)", raw)
        return m.group(1) if m else None

    # -- load / uptime -----------------------------------------------------
    @staticmethod
    def load():
        f = read_text("/proc/loadavg").split()
        if len(f) < 3:
            return {}
        one = float(f[0])
        ncpu = os.cpu_count() or 1
        return {"one": one, "five": float(f[1]), "fifteen": float(f[2]),
                "ncpu": ncpu,
                "state": "critical" if one > ncpu * 2 else "warn" if one > ncpu else "ok"}

    @staticmethod
    def uptime():
        f = read_text("/proc/uptime").split()
        secs = float(f[0]) if f else 0.0
        boot = time.time() - secs
        return {"seconds": int(secs), "human": fmt_uptime(secs),
                "since": time.strftime("%d %b %H:%M", time.localtime(boot))}

    # -- processes ---------------------------------------------------------
    def processes(self, limit=8):
        now = time.monotonic()
        cur, rows = {}, []
        mem_total = self.memory()["total"] or 1
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return []
        for pid in pids:
            statline = read_text("/proc/%s/stat" % pid)
            if not statline:
                continue
            # comm can contain spaces and parens; split around the last ')'.
            try:
                close = statline.rindex(")")
                comm = statline[statline.index("(") + 1:close]
                rest = statline[close + 2:].split()
                utime, stime = int(rest[11]), int(rest[12])
                pstate = rest[0]
                rss_pages = int(rest[21])
            except (ValueError, IndexError):
                continue
            jiffies = utime + stime
            cur[pid] = jiffies
            prev = self.prev_proc.get(pid)
            pct = 0.0
            if prev is not None and self.prev_proc_at:
                dt = now - self.prev_proc_at
                if dt > 0:
                    pct = 100.0 * ((jiffies - prev) / CLK_TCK) / dt
            cmdline = read_text("/proc/%s/cmdline" % pid).replace("\x00", " ").strip()
            if not cmdline:
                cmdline = "[%s]" % comm
            rss = rss_pages * os.sysconf("SC_PAGE_SIZE")
            rows.append({
                "pid": int(pid), "user": self._owner(pid),
                "cpu": round(min(pct, 100.0 * (os.cpu_count() or 1)), 1),
                "mem": round(100.0 * rss / mem_total, 1),
                "cmd": cmdline[:120],
                # D = uninterruptible sleep, Z = zombie: both are stuck, not busy.
                "state": "critical" if pstate in ("D", "Z") else None,
            })
        self.prev_proc, self.prev_proc_at = cur, now
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return rows[:limit]

    @staticmethod
    def _owner(pid):
        try:
            import pwd
            uid = os.stat("/proc/%s" % pid).st_uid
            return pwd.getpwuid(uid).pw_name
        except Exception:
            return "?"

    # -- the whole snapshot ------------------------------------------------
    def snapshot(self):
        with self.lock:
            return {
                "at": time.strftime("%H:%M:%S"),
                "host": self.hostname,
                "user": self.user,
                "model": self.model,
                "thermal": self.thermal(),
                "cpu": self.cpu(),
                "memory": self.memory(),
                "filesystems": self.filesystems(),
                "network": self.network(),
                "load": self.load(),
                "uptime": self.uptime(),
                "processes": self.processes(limit=16),
            }


# ------------------------------------------------------------------- log follower
class LogFollower(threading.Thread):
    """Tails the system journal. The console's user is in systemd-journal, so no root needed."""

    daemon = True

    def __init__(self, bus):
        super().__init__(name="journal")
        self.bus = bus
        self.proc = None

    def run(self):
        cmd = ["journalctl", "-f", "-n", "30", "-o", "json", "--no-pager"]
        while True:
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
                for line in self.proc.stdout:
                    entry = self._parse(line)
                    if entry:
                        self.bus.publish("log", entry)
            except Exception:
                pass
            time.sleep(3)  # journalctl died; back off and re-attach

    @staticmethod
    def _parse(line):
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            return None
        msg = d.get("MESSAGE")
        if isinstance(msg, list):  # journald returns byte arrays for odd payloads
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except Exception:
                msg = None
        if not msg:
            return None
        try:
            prio = int(d.get("PRIORITY", 6))
        except (TypeError, ValueError):
            prio = 6
        unit = d.get("_SYSTEMD_UNIT") or d.get("SYSLOG_IDENTIFIER") or d.get("_COMM") or ""
        unit = re.sub(r"\.service$", "", unit)
        ts = d.get("__REALTIME_TIMESTAMP")
        try:
            when = time.strftime("%H:%M:%S", time.localtime(int(ts) / 1_000_000))
        except (TypeError, ValueError):
            when = time.strftime("%H:%M:%S")
        return {"time": when, "level": PRIORITY_STATE.get(prio, "idle"),
                "unit": unit[:32], "message": ANSI.sub("", msg)[:400]}


# ------------------------------------------------------------------------- pub/sub
class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=400)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, event, data):
        payload = (event, data)
        with self.lock:
            targets = list(self.subs)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # a stalled client must not block the sampler


class Sampler(threading.Thread):
    daemon = True

    def __init__(self, collector, bus, interval):
        super().__init__(name="sampler")
        self.collector, self.bus, self.interval = collector, bus, interval
        self.latest = None

    def run(self):
        while True:
            try:
                self.latest = self.collector.snapshot()
                self.bus.publish("metrics", self.latest)
            except Exception as exc:  # never let one bad read kill the loop
                self.bus.publish("error", {"message": str(exc)})
            time.sleep(self.interval)


# ------------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "Zero2WConsole"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def _authed(self, qs):
        cfg = self.server.cfg
        if not cfg["auth"]:
            return True
        tok = cfg["token"]
        if qs.get("t", [None])[0] == tok:
            return True
        if self.headers.get("X-Console-Token") == tok:
            return True
        raw = self.headers.get("Cookie")
        if raw:
            c = SimpleCookie()
            try:
                c.load(raw)
            except Exception:
                return False
            m = c.get("console_token")
            if m and m.value == tok:
                return True
        return False

    # ------------------------------------------------------------ devices
    def _device_get(self, path, qs, device):
        fleet = self.server.fleet
        if path == "/api/iot/manifest":
            fleet.touch(device["id"])
            if qs.get("boot"):
                fleet.booted(device)
            return self._json(200, fleet.manifest(
                device, agent_version=(qs.get("v") or [None])[0]))
        if path.startswith("/api/iot/module/"):
            name = path[len("/api/iot/module/"):]
            try:
                body = fleet.module_source(name)
            except iotmod.NetError as exc:
                return self._json(404, {"error": str(exc)})
            # Remember it has this one, so a later deploy can say what it would fetch.
            digest = fleetmod.sha(body)
            fleet.note_modules(device["id"], {name: digest})
            return self._send(200, body, "text/x-python; charset=utf-8",
                              [("X-Sha", digest)])
        if path == "/api/iot/media":
            # Where to stream pictures, signed with this board's token. 404
            # when this console has no certificate: the board serves its
            # frames the old way instead.
            offer = fleet.media_offer(device)
            if not offer:
                return self._json(404, {"error": "no picture stream on this console"})
            return self._json(200, offer)
        if path == "/api/iot/commands":
            try:
                wait = int((qs.get("wait") or [fleetmod.POLL_SECONDS])[0])
            except ValueError:
                wait = fleetmod.POLL_SECONDS
            fleet.touch(device["id"])
            return self._json(200, {"commands": fleet.take_commands(device, wait)})
        return self._send(404, "404\n")

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8", extra)

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/healthz":  # unauthenticated liveness only
            return self._json(200, {"ok": True})

        # A device's own token reaches exactly these routes. The console token is
        # never put on a device.
        if path in ("/api/iot/manifest", "/api/iot/commands", "/api/iot/media") or \
                path.startswith("/api/iot/module/"):
            device = self.server.fleet.authenticate(self.headers.get("X-Device-Token"))
            if not device:
                return self._send(401, "401\n")
            return self._device_get(path, qs, device)

        if not self._authed(qs):
            # A browser gets a sign-in page; anything else keeps the plain 401.
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if wants_html and not path.startswith("/api/"):
                bad = "?bad=1" if qs.get("t") else ""
                try:
                    with open(os.path.join(STATIC, "login.html"), "rb") as fh:
                        body = fh.read()
                except OSError:
                    return self._send(401, "401 — append ?t=<token> to the URL.\n")
                if bad:
                    body = body.replace(b'id="err"', b'id="err" class="err on"')
                return self._send(401, body, "text/html; charset=utf-8")
            return self._send(401, "401 — append ?t=<token> to the URL.\n"
                                   "The token is in ~/.config/zero2w-console/token\n")

        # A correct ?t= sets the cookie so later requests need no query string.
        extra = []
        if qs.get("t", [None])[0] == self.server.cfg["token"]:
            extra = [("Set-Cookie",
                      "console_token=%s; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"
                      % self.server.cfg["token"])]

        if path == "/":
            return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8", extra)
        if path == "/api/snapshot":
            snap = self.server.sampler.latest or self.server.collector.snapshot()
            return self._json(200, snap, extra)
        if path == "/api/config":
            return self._json(200, {"exec": self.server.cfg["exec"],
                                    "gpio_write": self.server.cfg["gpio_write"],
                                    "iot_net": self.server.cfg["iot_net"],
                                    "interval": self.server.cfg["interval"],
                                    "host": self.server.collector.hostname,
                                    "model": self.server.collector.model}, extra)
        if path == "/api/stream":
            return self._stream(extra)
        if path == "/api/logs":
            # One-shot tail, for snapshot mode and for clients that would rather poll.
            return self._json(200, recent_logs(40), extra)
        if path == "/flows":
            return self._file(os.path.join(STATIC, "flows.html"), "text/html; charset=utf-8", extra)
        if path == "/iot":
            return self._file(os.path.join(STATIC, "iot.html"), "text/html; charset=utf-8", extra)
        if path == "/cameras":
            return self._file(os.path.join(STATIC, "cameras.html"), "text/html; charset=utf-8", extra)
        if path == "/api/flows":
            return self._json(200, self.server.engine.doc, extra)
        if path in ("/api/flows/registry", "/api/flows/variables"):
            # Resolved against the newest sample, so the editor shows live values.
            snap = self.server.sampler.latest or {}
            payload = {"variables": flowmod.variables_with_values(
                snap, self.server.tags, self.server.engine._fleet_devices())}
            if path.endswith("registry"):
                # With a board, the palette is only what that board can run.
                want = (parse_qs(u.query).get("board") or [None])[0]
                profile = iotmod.board_profile(want) if want else None
                if want and not profile:
                    return self._json(404, {"error": "no such board"}, extra)
                payload["nodes"] = flowmod.registry_for(profile)
                payload["board"] = want
            return self._json(200, payload, extra)
        if path == "/api/flows/runs":
            return self._json(200, {"runs": self.server.engine.recent()}, extra)
        if path == "/api/flows/examples":
            return self._json(200, {"examples": examplemod.catalogue()}, extra)
        if path == "/api/tags":
            return self._json(200, {"tags": self.server.tags.list()}, extra)
        if path == "/api/flows/outputs":
            steps = self.server.engine.active_steps()
            if self.server.fleet:
                steps += self.server.fleet.active_steps()
            outputs = self.server.engine.outputs()
            if self.server.fleet:
                outputs.update(self.server.fleet.outputs_snapshot())
            return self._json(200, {"outputs": outputs, "steps": steps}, extra)
        if path == "/api/iot/status":
            # Read-only: what the radio can do, whether an AP could come up, and
            # which devices have enrolled.
            caps = iotmod.capabilities(self.server.devices)
            fleet = self.server.fleet.state()
            caps["devices"].update(fleet)
            caps["devices"]["available"] = bool(fleet["online"])
            if fleet["online"]:
                caps["devices"]["hint"] = None
            return self._json(200, caps, extra)
        if path == "/api/iot/devices":
            rows = iotmod.devices(self.server.devices)
            # Merged here rather than in iot.py, which owns the store and knows
            # nothing about the runtime. A pad is transient state, like a frame.
            for row in rows:
                row["pad"] = self.server.fleet.pad_state(row.get("id"))
            return self._json(200, {"devices": rows,
                                    "groups": iotmod.groups(self.server.devices)},
                              extra)
        if path == "/api/bt/gatt":
            # One device's services and characteristics. Read-only, and its own
            # route rather than part of /api/bt because it is only ever wanted
            # for the one device somebody is looking at.
            mac = (parse_qs(u.query).get("mac") or [""])[0]
            if not btmod.is_address(mac.upper()):
                return self._json(400, {"error": "not a Bluetooth address"}, extra)
            known = gattmod.tree()
            device = [d for d in gattmod.devices(known)
                      if d["mac"].upper() == mac.upper()]
            return self._json(200, {
                "mac": mac.upper(),
                "device": device[0] if device else None,
                "services": gattmod.gatt(mac, known),
            }, extra)
        if path == "/api/bt":
            # Cheap enough to poll: one D-Bus call behind a two-second cache.
            # A scan is not, and has its own route.
            return self._json(200, btmod.status(), extra)
        if path == "/api/iot/boards":
            want = (parse_qs(u.query).get("board") or [None])[0]
            if want:
                doc = iotmod.board_profile(want)
                if not doc:
                    return self._json(404, {"error": "no such board"}, extra)
                return self._json(200, doc, extra)
            return self._json(200, {"boards": iotmod.board_summaries()}, extra)
        if path.startswith("/api/iot/devices/") and path.endswith("/camera.png"):
            device_id = path[len("/api/iot/devices/"):-len("/camera.png")]
            device = iotmod.get_device(self.server.devices, device_id)
            if not device:
                return self._json(404, {"error": "no such device"}, extra)
            try:
                frame = self.server.fleet.frame(device)
            except iotmod.NetError as exc:
                return self._json(503, {"error": str(exc)}, extra)
            # A picture must never be cached: the next one is the point.
            head = list(extra or []) + [
                ("Cache-Control", "no-store"),
                ("X-Frame-Age", str(max(0, iotmod.now() - frame["at"]))),
                ("X-Frame-Stale", "1" if frame.get("stale") else "0"),
                ("X-Frame-Size", "%dx%d" % (frame["width"], frame["height"])),
            ]
            return self._send(200, frame["image"], frame["type"], head)
        if path.startswith("/api/iot/devices/") and path.endswith("/pins"):
            device_id = path[len("/api/iot/devices/"):-len("/pins")]
            device = iotmod.get_device(self.server.devices, device_id)
            if not device:
                return self._json(404, {"error": "no such device"}, extra)
            return self._json(200, self.server.fleet.device_pins(device), extra)
        if path.startswith("/api/iot/devices/") and path.endswith("/log"):
            # What this board has said, kept on this end — which is how it is read
            # after the fact, when a board that stopped talking is looked at.
            device_id = path[len("/api/iot/devices/"):-len("/log")]
            device = iotmod.get_device(self.server.devices, device_id)
            if not device:
                return self._json(404, {"error": "no such device"}, extra)
            return self._json(200, {
                "device": device_id,
                "events": self.server.fleet.device_log(
                    device_id, qs.get("limit", [None])[0] or fleetmod.HISTORY),
            }, extra)
        if path.startswith("/api/iot/devices/") and path.endswith("/cost"):
            # What deploying this flow would cost over the air, so a screen can
            # offer the wire before starting a sync that is minutes of silence.
            device_id = path[len("/api/iot/devices/"):-len("/cost")]
            device = iotmod.get_device(self.server.devices, device_id)
            if not device:
                return self._json(404, {"error": "no such device"}, extra)
            wanted = (parse_qs(u.query).get("flow") or [None])[0]
            flow = None
            for f in self.server.engine.doc.get("flows", []):
                if f.get("id") == wanted:
                    flow = f
            if wanted and flow is None:
                return self._json(404, {"error": "no such flow"}, extra)
            return self._json(200, self.server.fleet.deploy_cost(flow, device),
                              extra)
        if path == "/api/iot/cameras":
            # `now` is this board's clock, so a browser whose clock is off does not
            # invent an age.
            return self._json(200, {"cameras": self.server.fleet.screen_cameras(),
                                    "now": iotmod.now()}, extra)
        if path == "/api/iot/flash":
            return self._json(200, self.server.fleet.flash_state(), extra)
        if path == "/api/iot/scan":
            # Probing talks over serial and takes seconds, so it is never part of
            # the page's poll — only an explicit scan.
            probe = (parse_qs(u.query).get("probe") or ["1"])[0] != "0"
            return self._json(200, iotmod.scan(probe=probe), extra)
        if path == "/api/hardware":
            caps = busmod.capabilities()
            caps["gpio"] = self.server.inventory.access()
            caps["mqtt"] = self.server.engine.mqtt.status()
            caps["softpwm"] = self.server.engine.softpwm.active()
            caps["overlays"] = busmod.header_overlays()
            return self._json(200, caps, extra)
        if path == "/api/gpio/pins":
            eng = self.server.engine
            return self._json(200, self.server.inventory.snapshot(
                bindings=eng.bindings(), held=self.server.driver.held_values()), extra)
        if path.startswith("/static/"):
            name = os.path.basename(path)
            full = os.path.join(STATIC, name)
            if os.path.realpath(full).startswith(os.path.realpath(STATIC)) and os.path.isfile(full):
                return self._file(full, self._ctype(name), extra)
        return self._send(404, "404\n")

    def do_POST(self):
        u = urlparse(self.path)
        route = u.path.rstrip("/")
        device_route = route in ("/api/iot/enroll", "/api/iot/event", "/api/iot/state")
        if not device_route and not self._authed(parse_qs(u.query)):
            return self._send(401, "401\n")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad JSON"})

        if device_route:
            fleet = self.server.fleet
            try:
                if route == "/api/iot/enroll":
                    # Authenticated by the one-time token in the body, in an open window.
                    return self._json(200, fleet.enroll(
                        body.get("device"), body.get("enroll"), body.get("probe"),
                        ip=self.client_address[0]))
                device = fleet.authenticate(self.headers.get("X-Device-Token"))
                if not device:
                    return self._send(401, "401\n")
                if route == "/api/iot/state":
                    return self._json(200, fleet.report_state(device, body))
                return self._json(200, fleet.event(device, body))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})

        if route == "/api/flows":
            doc = body if isinstance(body, dict) and isinstance(body.get("flows"), list) else None
            if doc is None:
                return self._json(400, {"error": "expected {flows: [...]}"})
            # A body carrying `rev` claims to have started from it; if disk has moved
            # on, refuse rather than overwrite. Without one it is trusted — that is
            # how a restore from a backup file works.
            # Kept so the save can tell what moved: a flow switched off has to reach
            # the device running it, and only a comparison knows that.
            was = {f.get("id"): f
                   for f in self.server.engine.store.load().get("flows", [])}
            try:
                saved = self.server.engine.store.save(doc, expect_rev=body.get("rev"))
            except flowmod.RevConflict as conflict:
                return self._json(409, {
                    "error": "these flows changed somewhere else since this page loaded",
                    "rev": conflict.current["rev"],
                    "flows": len(conflict.current.get("flows", [])),
                    "current": conflict.current,
                })
            self.server.engine.reload()
            told = []
            if self.server.fleet:
                try:
                    told = self.server.fleet.reconcile(was, saved)
                except Exception:
                    told = []
            # Things that save fine and will not work. Said on the way out rather
            # than refused: the flow is the author's to write.
            notes = []
            for f in saved.get("flows", []):
                for note in flowmod.advice(f):
                    notes.append(dict(note, flow=f.get("id"),
                                      flow_name=f.get("name")))
            return self._json(200, {"ok": True, "flows": len(saved["flows"]),
                                    "rev": saved["rev"], "told": told,
                                    "advice": notes})
        if route == "/api/tags":
            # The whole table as one: a bad definition leaves it as it was.
            try:
                doc = self.server.tags.define(body.get("tags") or [])
            except tagmod.TagError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"ok": True, "rev": doc["rev"],
                                    "tags": self.server.tags.list()})
        if route == "/api/tags/set":
            try:
                value, changed = self.server.tags.set(body.get("name"),
                                                      body.get("value"))
            except tagmod.TagError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"ok": True, "value": value, "changed": changed})
        if route == "/api/flows/run":
            fid, nid = body.get("flow"), body.get("node")
            if not fid or not nid:
                return self._json(400, {"error": "flow and node required"})
            # A flow bound to a board runs there, so Run has to travel — firing it
            # here would walk the device's graph against this host.
            flow = self.server.engine._flow(fid)
            if flow and not flowmod.runs_here(flow):
                device = flow.get("device")
                if not device:
                    return self._json(409, {
                        "error": "this flow is written for %s but is not "
                                 "linked to a device, so there is nowhere to "
                                 "run it" % flow.get("board")})
                try:
                    self.server.fleet.push(device, {"op": "fire", "node": nid})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, {"ok": True, "sent_to": device})
            self.server.engine.fire((fid, nid), {"payload": body.get("payload", 1), "meta": {"manual": True}})
            return self._json(200, {"ok": True})
        if route in ("/api/iot/enrollment/open", "/api/iot/enrollment/close"):
            fleet = self.server.fleet
            if route.endswith("/open"):
                return self._json(200, {"enrollment": fleet.open_window(
                    int(body.get("seconds") or fleetmod.ENROLL_WINDOW))})
            return self._json(200, fleet.close_window())
        if route.startswith("/api/iot/devices/") and \
                route.endswith(("/provision", "/deploy", "/flash", "/command", "/ping")):
            device_id = route[len("/api/iot/devices/"):].rsplit("/", 1)[0]
            try:
                if route.endswith("/provision"):
                    return self._json(200, self.server.fleet.provision(device_id))
                if route.endswith("/flash"):
                    if not self.server.cfg["iot_net"]:
                        return self._json(403, {"error": "flashing disabled (--no-iot-net)"})
                    port = body.get("port") or ""
                    if not re.fullmatch(r"/dev/tty[A-Za-z0-9]+", port):
                        return self._json(400, {"error": "that is not a serial port"})
                    # `flow` writes it and its modules over USB, so the board boots
                    # complete. `fleet.flash` checks the id against the store.
                    return self._json(200, self.server.fleet.flash(
                        device_id, port, body.get("ssid"), body.get("psk"),
                        flow=body.get("flow") or None))
                if route.endswith("/ping"):
                    link = self.server.fleet.link
                    if not link:
                        return self._json(200, {"ok": False, "detail": "the link listener is off"})
                    rtt, why = link.ping(device_id)
                    return self._json(200, {"ok": rtt is not None, "rtt_ms": rtt,
                                            "detail": why or "%.1f ms" % rtt})
                if route.endswith("/command"):
                    op = body.get("op")
                    if op not in flowmod.DEVICE_OPS:
                        return self._json(400, {"error": "unknown command"})
                    return self._json(200, self.server.fleet.push(device_id, body))
                return self._json(200, self.server.fleet.deploy(device_id, body.get("flow")))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route.startswith("/api/bt/"):
            verb = route[len("/api/bt/"):]
            if verb == "scan":
                # Never part of the poll. `wlan0` and `hci0` are one combo chip
                # and docs/RADIO.md records what contending for its single bus
                # channel does to the access point, so this happens because
                # somebody asked for it.
                return self._json(200, btmod.scan(body.get("seconds")))
            mac = body.get("mac")
            if verb == "pair":
                return self._json(200, btmod.pair(mac))
            if verb == "connect":
                return self._json(200, gattmod.connect(mac))
            if verb == "read":
                raw = gattmod.read(body.get("path") or "")
                return self._json(200, {"ok": raw is not None,
                                        "hex": raw.hex() if raw else "",
                                        "bytes": len(raw) if raw else 0})
            if verb == "write":
                try:
                    payload = bytes.fromhex(body.get("hex") or "")
                except ValueError:
                    return self._json(400, {"error": "hex, as pairs of digits"})
                ok = gattmod.write(body.get("path") or "", payload)
                return self._json(200, {"ok": ok,
                                        "detail": "%d byte(s)" % len(payload)
                                        if ok else "BlueZ refused the write"})
            if verb == "notify":
                # Held by a session that stays running: a one-off StartNotify
                # ends the moment its caller exits.
                on = bool(body.get("on", True))
                path = body.get("path") or ""
                holds = self.server.bt_holds
                ok = True
                if on and path not in holds:
                    try:
                        holds[path] = gattmod.hold_notify(path)
                    except OSError:
                        ok = False
                elif not on and path in holds:
                    gattmod.release_notify(holds.pop(path))
                return self._json(200, {"ok": ok,
                                        "detail": ("subscribed" if on
                                                   else "unsubscribed")
                                        if ok else "BlueZ refused it"})
            if verb == "disconnect":
                return self._json(200, btmod.disconnect(mac))
            if verb == "forget":
                return self._json(200, btmod.forget(mac))
            return self._json(404, {"error": "no such bluetooth action"})
        if route == "/api/iot/groups":
            try:
                return self._json(200, {"groups": iotmod.save_groups(
                    self.server.devices, body.get("groups"))})
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route == "/api/iot/devices":
            try:
                return self._json(200, iotmod.public(
                    iotmod.create_device(self.server.devices, body)))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route.startswith("/api/iot/devices/"):
            rest = route[len("/api/iot/devices/"):]
            device_id, _, action = rest.partition("/")
            try:
                if action == "delete":
                    return self._json(200, iotmod.delete_device(self.server.devices, device_id))
                if action == "rehome":
                    moved = iotmod.rehome_device(self.server.devices, device_id, body)
                    return self._json(200, dict(iotmod.public(moved), warnings=
                                                self.server.fleet.rehome_warnings(moved)))
                if action:
                    return self._json(404, {"error": "unknown action"})
                was = iotmod.get_device(self.server.devices, device_id) or {}
                updated = iotmod.update_device(self.server.devices, device_id, body)
                # The board only reads the manifest when it syncs, so flipping the
                # switch has to reach it, as switching a flow off does.
                if self.server.fleet and "link" in body \
                        and bool(was.get("link")) != bool(updated.get("link")):
                    self.server.fleet.push(device_id, {"op": "reload",
                                                       "flow": updated.get("flow")})
                return self._json(200, iotmod.public(updated))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route in ("/api/iot/radio/on", "/api/iot/radio/off"):
            if not self.server.cfg["iot_net"]:
                return self._json(403, {"error": "iot network control disabled (--no-iot-net)"})
            try:
                return self._json(200, iotmod.set_radio(route.endswith("/on")))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route in ("/api/iot/network", "/api/iot/network/up", "/api/iot/network/down"):
            if not self.server.cfg["iot_net"]:
                return self._json(403, {"error": "iot network control disabled (--no-iot-net)"})
            try:
                if route.endswith("/up"):
                    return self._json(200, iotmod.helper("up"))
                if route.endswith("/down"):
                    return self._json(200, iotmod.helper("down"))
                # Readable error here; the helper validates again as root, which is
                # the check that matters.
                cfg = iotmod.validate_net_config(body)
                cfg.setdefault("uplink", iotmod.default_uplink())
                return self._json(200, iotmod.helper("config", payload=cfg))
            except iotmod.NetError as exc:
                return self._json(400, {"error": str(exc)})
        if route == "/api/gpio/write":
            if not self.server.cfg["gpio_write"]:
                return self._json(403, {"error": "gpio writes disabled (--no-gpio-write)"})
            try:
                pin = int(body.get("gpio"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "gpio required"})
            ok, reason = self.server.driver.write(pin, body.get("value"))
            return self._json(200 if ok else 409, {"ok": ok, "error": reason})
        if route == "/api/gpio/release":
            try:
                pin = int(body.get("gpio"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "gpio required"})
            self.server.driver.release(pin)
            return self._json(200, {"ok": True})
        if route.startswith("/api/hook/"):
            hook = route[len("/api/hook/"):]
            fired = self.server.engine.fire_webhook(hook, body.get("payload", body))
            return self._json(200 if fired else 404,
                              {"ok": fired, "error": None if fired else "no webhook node for %r" % hook})

        if route != "/api/exec":
            return self._send(404, "404\n")
        if not self.server.cfg["exec"]:
            return self._json(403, {"error": "shell disabled (--no-exec)"})
        cmd = (body.get("cmd") or "").strip()
        cwd = body.get("cwd") or os.path.expanduser("~")
        if not cmd:
            return self._json(400, {"error": "empty command"})
        return self._json(200, run_command(cmd, cwd))

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _ctype(name):
        return {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml", ".woff2": "font/woff2",
                }.get(os.path.splitext(name)[1], "application/octet-stream")

    def _file(self, full, ctype, extra=None):
        try:
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype, extra)
        except OSError:
            return self._send(404, "404\n")

    def _stream(self, extra=None):
        q = self.server.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            snap = self.server.sampler.latest
            if snap:
                self._event("metrics", snap)
            while True:
                try:
                    event, data = q.get(timeout=15)
                    self._event(event, data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # hold the connection open
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.bus.unsubscribe(q)

    def _event(self, event, data):
        payload = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


def recent_logs(n=40):
    try:
        raw = subprocess.run(["journalctl", "-n", str(n), "-o", "json", "--no-pager"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    rows = []
    for line in raw.splitlines():
        entry = LogFollower._parse(line)
        if entry:
            rows.append(entry)
    return rows


def run_command(cmd, cwd, timeout=15):
    """Run one command in a login shell, tracking cwd across calls."""
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    marker = "__Z2W_CWD__"
    script = "cd %s || exit 1\n%s\nprintf '\\n%s%%s' \"$PWD\"\n" % (shlex.quote(cwd), cmd, marker)
    started = time.monotonic()
    try:
        p = subprocess.run(["bash", "-lc", script], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return {"lines": [{"kind": "err", "text": "timed out after %ss" % timeout}],
                "cwd": cwd, "code": 124, "ms": int(timeout * 1000)}
    except Exception as exc:
        return {"lines": [{"kind": "err", "text": str(exc)}], "cwd": cwd, "code": 1, "ms": 0}

    new_cwd = cwd
    idx = out.rfind(marker)
    if idx != -1:
        new_cwd = out[idx + len(marker):].strip() or cwd
        out = out[:idx].rstrip("\n")

    lines = []
    if out:
        lines.append({"kind": "out", "text": ANSI.sub("", out)})
    if err:
        lines.append({"kind": "err", "text": ANSI.sub("", err.rstrip("\n"))})
    if not lines:
        lines.append({"kind": "dim", "text": "(no output)"})
    return {"lines": lines, "cwd": new_cwd, "code": code,
            "ms": int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Auto-Blox")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--interval", type=float, default=2.0, help="sample seconds")
    ap.add_argument("--no-auth", action="store_true", help="disable the token (trusted LAN only)")
    ap.add_argument("--no-exec", action="store_true", help="disable the terminal's shell")
    ap.add_argument("--no-iot-net", action="store_true",
                    help="refuse to bring the IoT access point up or down")
    ap.add_argument("--no-gpio-write", action="store_true",
                    help="allow flows and the pin map to read pins but never drive them")
    ap.add_argument("--link-port", type=int, default=linkmod.PORT,
                    help="the port devices hold a link open on")
    ap.add_argument("--no-link", action="store_true",
                    help="do not listen for device links; devices poll instead")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    token = load_or_create_token()
    collector = Collector()
    bus = Bus()
    sampler = Sampler(collector, bus, a.interval)
    sampler.start()
    LogFollower(bus).start()

    devices = iotmod.DeviceStore()
    flow_store = flowmod.FlowStore()
    inventory = gpiomod.PinInventory()
    driver = gpiomod.PinDriver(inventory)
    engine = flowmod.FlowEngine(flow_store, driver, inventory, bus,
                                metrics_source=lambda: sampler.latest)
    fleet = fleetmod.Fleet(devices, flow_store, bus)
    tag_table = tagmod.TagTable(bus=bus)
    # Each needs the other: a host flow commands a device, a device's event fires
    # a host flow. Bound after construction so either can be built alone in a test.
    engine.fleet, fleet.engine = fleet, engine
    # Shared memory, and one subscription on it: arming a flow adds a row to the
    # engine's list rather than another callback on the table.
    engine.tags = tag_table
    fleet.tags = tag_table
    tag_table.watch(engine.on_tag_changed)

    def _to_fleet(name, value, previous, source=None):
        """A shared tag moving here goes out to the fleet."""
        if tag_table.is_shared(name):
            fleet.broadcast_tag(name, value, source)

    tag_table.watch(_to_fleet)
    engine.start()

    # The link listens beside the HTTP server, not instead of it: a device that
    # never dials carries on polling and nothing about it changes.
    link = None
    if not a.no_link:
        try:
            # Its certificate, for the encrypted picture stream on the same port.
            # Without openssl there is none, and cameras serve frames the old way.
            tls = None
            made = tlscert.ensure(CONFIG_DIR)
            if made:
                tls = tlscert.server_context(*made)
                fleet.cert_der = tlscert.der(made[0])
            link = linkmod.LinkServer(
                lookup=fleet.device_token, on_frame=fleet.on_link_frame,
                on_open=fleet.on_link_open, on_close=fleet.on_link_close,
                port=a.link_port, host=a.host, tls=tls,
                stream_lookup=fleet.stream_token).start()
            fleet.link = link
        except OSError as exc:
            # A port already taken is not a reason to refuse to start.
            print("  link     : NOT LISTENING — %s" % exc)

    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.daemon_threads = True
    httpd.collector, httpd.bus, httpd.sampler = collector, bus, sampler
    httpd.verbose = a.verbose
    httpd.inventory, httpd.driver, httpd.engine = inventory, driver, engine
    httpd.devices = devices
    httpd.fleet = fleet
    httpd.bt_holds = {}              # characteristic path -> the session holding its subscription
    httpd.tags = tag_table
    fleet.self_url = "http://127.0.0.1:%d" % a.port
    httpd.cfg = {"token": token, "auth": not a.no_auth,
                 "exec": not a.no_exec, "gpio_write": not a.no_gpio_write,
                 "iot_net": not a.no_iot_net,
                 "interval": a.interval}

    # No token here: this goes to the journal, which more than this user can
    # read, and the token opens a shell. It stays in its 0600 file.
    suffix = "/"
    print("Auto-Blox — %s (%s)" % (collector.model or "unknown board", collector.hostname))
    print("  zones    : %s" % ", ".join(z["type"] for z in collector.zones))
    print("  shell    : %s" % ("enabled" if not a.no_exec else "DISABLED"))
    acc = inventory.access()
    print("  gpio     : %s%s" % ("ok" if acc["ok"] else "NO ACCESS",
                                 "" if acc["ok"] else " — run sudo scripts/setup-gpio.sh"))
    print("  flows    : %d loaded" % len(engine.doc.get("flows", [])))
    iotcaps = iotmod.capabilities(devices)
    print("  iot      : %s%s" % (
        "ap up" if iotcaps["ap"]["available"] else "no ap",
        " · %d device(s)" % iotcaps["devices"]["count"] if iotcaps["devices"]["count"]
        else " · " + (iotcaps["ap"]["hint"] or "")))
    print("  link     : %s" % ("port %d" % link.port if link else "OFF"))
    print("  pictures : %s" % ("encrypted stream (TLS)" if fleet.streams_pictures()
                               else "unencrypted: no certificate (install openssl)"))
    print("  auth     : %s" % ("token, in %s" % TOKEN_FILE if not a.no_auth else "OFF"))
    print("  local    : http://localhost:%d%s" % (a.port, suffix))
    for iface in collector.network():
        if iface.get("address"):
            ip = iface["address"].split("/")[0]
            print("  %-9s: http://%s:%d%s" % (iface["name"][:9], ip, a.port, suffix))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # Never leave a pin driven after the console exits.
        engine.shutdown()
        driver.release_all()
        if link:
            link.stop()


if __name__ == "__main__":
    main()
