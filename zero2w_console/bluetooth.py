"""The host's Bluetooth adapter: status off BlueZ's tree, actions via `bluetoothctl`.

No privilege needed — the console's user is in the `bluetooth` group.

`hci0` shares one UNISOC chip with `wlan0`, and a contended chip has reset and
taken the access point with it (docs/RADIO.md). A scan ran beside the live AP
once without that, which is one observation, so a scan stays an explicit,
bounded button and never part of a poll.
"""
import subprocess
import threading
import time

from . import gatt

CTL = "bluetoothctl"

CACHE_SECONDS = 2.0

SCAN_SECONDS = 12
SCAN_MAX = 30

_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}
_scan = {"running": False, "started": 0.0, "seconds": 0, "found": [],
         "error": "", "at": 0.0}


def _run(args, timeout=8):
    """stdout, or "" — never raises, never needs privilege."""
    try:
        done = subprocess.run([CTL] + args, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


def _popen(args):
    """The long-lived session `pair` needs, behind a seam a test can replace."""
    return subprocess.Popen([CTL] + args, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)


def installed():
    return bool(_run(["--version"], timeout=4))


def _devices(which=None):
    """`bluetoothctl devices [Paired|Connected]` as a mac -> name map."""
    out = {}
    for line in _run(["devices"] + ([which] if which else [])).splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == "Device":
            out[parts[1]] = parts[2].strip() if len(parts) > 2 else parts[1]
    return out


def status(force=False):
    """Everything the screen needs, from one D-Bus call."""
    with _lock:
        fresh = _cache["value"] is not None and \
            time.monotonic() - _cache["at"] < CACHE_SECONDS
        if fresh and not force:
            return _cache["value"]
    known = gatt.tree()
    adapter = gatt.adapter(known)
    present = bool(adapter)
    devices = []
    for device in gatt.devices(known):
        devices.append(dict(device, pad=gatt.is_gamepad(device),
                            gatt=bool(gatt.gatt(device["mac"], known))))
    out = {
        "installed": present or installed(),
        "adapter": {
            "address": adapter.get("Address") or "",
            "name": adapter.get("Name") or "",
            "powered": bool(adapter.get("Powered")),
            "discovering": bool(adapter.get("Discovering")),
            "present": present,
            "can_advertise": bool(adapter.get("can_advertise")),
            "can_serve_gatt": bool(adapter.get("can_serve_gatt")),
        },
        "devices": devices,
        "paired": sum(1 for d in devices if d["paired"]),
        "connected": sum(1 for d in devices if d["connected"]),
        "scan": scan_state(),
        "at": time.time(),
    }
    if not present:
        out["missing"] = ("no Bluetooth adapter — check `rfkill list` and that "
                          "bluetoothd is running")
    with _lock:
        _cache["at"], _cache["value"] = time.monotonic(), out
    return out


def forget_cache():
    with _lock:
        _cache["at"], _cache["value"] = 0.0, None


# -- the scan ---------------------------------------------------------------
def scan_state():
    with _lock:
        left = 0
        if _scan["running"]:
            left = max(0, int(_scan["started"] + _scan["seconds"]
                              - time.monotonic()))
        return {"running": _scan["running"], "seconds_left": left,
                "found": list(_scan["found"]), "error": _scan["error"],
                "at": _scan["at"]}


def scan(seconds=SCAN_SECONDS):
    """Discover in the background, bounded by `--timeout` so it cannot be left on."""
    seconds = max(3, min(SCAN_MAX, int(seconds or SCAN_SECONDS)))
    before = set()
    with _lock:
        already = _scan["running"]
        if not already:
            _scan.update({"running": True, "started": time.monotonic(),
                          "seconds": seconds, "error": "", "found": []})
    # Outside the lock: `scan_state` takes it too, and it is not reentrant.
    if already:
        return scan_state()

    def work():
        try:
            before.update(_devices())
            _run(["--timeout", str(seconds), "scan", "on"], timeout=seconds + 10)
            known = {d["mac"].upper(): d for d in gatt.devices()}
            after = _devices()
            found = []
            for mac, name in sorted(after.items(), key=lambda kv: kv[1].lower()):
                device = known.get(mac.upper(), {"name": name, "icon": ""})
                found.append({"mac": mac, "name": name,
                              "pad": gatt.is_gamepad(device),
                              "le": bool(device.get("le", True)),
                              "new": mac not in before})
            with _lock:
                _scan["found"] = found
        except Exception as exc:
            with _lock:
                _scan["error"] = str(exc)
        finally:
            with _lock:
                _scan["running"] = False
                _scan["at"] = time.time()
            forget_cache()

    threading.Thread(target=work, name="bt-scan", daemon=True).start()
    return scan_state()


# -- the actions ------------------------------------------------------------
def is_address(mac):
    """Six hex pairs, colon separated. Checked before anything is forked."""
    parts = (mac or "").split(":")
    if len(parts) != 6:
        return False
    for part in parts:
        if len(part) != 2:
            return False
        for ch in part:
            if ch not in "0123456789ABCDEFabcdef":
                return False
    return True


def _act(verb, mac, timeout=25):
    """One verb against one address, answered in bluetoothctl's own words.

    It exits 0 on failure and says so in prose, so the text is the result.
    """
    mac = (mac or "").strip().upper()
    if not is_address(mac):
        return {"ok": False, "detail": "not a Bluetooth address: %r" % mac}
    text = _run([verb, mac], timeout=timeout).strip()
    forget_cache()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    bad = [l for l in lines if "Failed" in l or "not available" in l]
    if bad:
        return {"ok": False, "detail": bad[-1]}
    good = [l for l in lines if "successful" in l or "succeeded" in l]
    return {"ok": True, "detail": good[-1] if good else (lines[-1] if lines else "")}


def _clean(line):
    """bluetoothctl paints its output; the escapes are not part of the answer."""
    out, i = [], 0
    while i < len(line):
        if line[i] == "\x1b":
            while i < len(line) and line[i] not in "mK":
                i += 1
            i += 1
            continue
        out.append(line[i])
        i += 1
    return "".join(out).replace("\r", "").strip()


def pair(mac, find_seconds=15):
    """Pair, trust and connect inside one live scan.

    It has to be one session: BlueZ forgets an unpaired device when discovery
    ends, so a `pair` sent after a scan answers `not available`. An agent is
    registered because nobody is there to answer a prompt, and trusting is what
    lets the device reconnect on its own later.
    """
    mac = (mac or "").strip().upper()
    if not is_address(mac):
        return {"ok": False, "detail": "not a Bluetooth address: %r" % mac}
    try:
        proc = _popen([])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": "cannot run %s: %s" % (CTL, exc)}

    seen = []

    def read():
        try:
            for raw in proc.stdout:
                seen.append(_clean(raw))
        except Exception:
            pass

    reader = threading.Thread(target=read, name="bt-pair-read", daemon=True)
    reader.start()

    def send(line):
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except Exception:
            pass

    def wait_for(needles, seconds):
        """The first line containing any of these, or None."""
        end = time.monotonic() + seconds
        at = 0
        while time.monotonic() < end:
            while at < len(seen):
                line = seen[at]
                at += 1
                low = line.lower()
                for needle in needles:
                    if needle.lower() in low:
                        return line
            time.sleep(0.15)
        return None

    detail, ok = "", False
    try:
        send("agent on")
        send("default-agent")
        send("scan on")
        if wait_for([mac], find_seconds) is None:
            detail = ("%s never advertised in %ds — put the controller in "
                      "pairing mode (hold its pair button until the light "
                      "flashes quickly) and try again" % (mac, find_seconds))
        else:
            send("pair " + mac)
            line = wait_for(["Pairing successful", "Failed to pair",
                             "not available", "AlreadyExists"], 30)
            if line and ("successful" in line or "AlreadyExists" in line):
                send("trust " + mac)
                wait_for(["trust succeeded", "Failed"], 8)
                send("connect " + mac)
                joined = wait_for(["Connection successful", "Failed to connect",
                                   "not available"], 25)
                ok = bool(joined and "successful" in joined)
                detail = joined or "paired, but it never finished connecting"
            else:
                detail = line or "it did not answer the pairing request in 30s"
    finally:
        send("scan off")
        send("quit")
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=2)
        forget_cache()
    return {"ok": ok, "detail": detail}


def connect(mac):
    return _act("connect", mac)


def disconnect(mac):
    return _act("disconnect", mac, timeout=10)


def forget(mac):
    """Remove the pairing entirely, so the next connection bonds again."""
    return _act("remove", mac, timeout=10)
