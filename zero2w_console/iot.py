"""IOT — the wireless side of this board, and the devices that join it."""
import glob
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "zero2w-console")
DEVICES_FILE = os.path.join(CONFIG_DIR, "iot.json")

# Written by the console, validated again by the helper before it is trusted.
NET_CONFIG = os.environ.get("ZERO2W_IOT_NET_CONFIG", "/etc/zero2w-console/iot-net.json")
# Overridable so the screen can be exercised without installing anything as
# root. The installed path is the one the sudoers entry names.
HELPER = os.environ.get("ZERO2W_IOT_HELPER", "/usr/local/sbin/iot-netctl")
# The console runs under NoNewPrivileges=yes, so sudo can never work from it —
# by design, since it also exposes a shell. The privileged half runs as its own
# root service and listens here instead.
HELPER_SOCKET = os.environ.get("ZERO2W_IOT_SOCKET", "/run/zero2w-iot/ctl.sock")

# 2.4 GHz only, and only the three that do not overlap.
AP_CHANNELS = [1, 6, 11]

HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS_DIR = os.path.join(HERE, "data", "boards")
REPO = os.path.dirname(HERE)

# What the scan reports, mapped to a profile. esptool names the die, not the
# board — a real ESP32-CAM answers "ESP32-D0WD-V3" — so the family is matched by
# prefix, most specific first, and the board stays the user's choice.
CHIP_FAMILIES = (
    ("esp32-s3", "esp32s3"), ("esp32s3", "esp32s3"),
    ("esp32-c3", "esp32c3"), ("esp32c3", "esp32c3"),
    ("esp32-s2", None), ("esp32-c2", None), ("esp32-c6", None),
    ("esp32-h2", None), ("esp32-p4", None),
    ("esp8266", None),
    ("esp32", "esp32"),
)

# esptool 4.7 prints this while it works out which protocol the ROM speaks: not
# a chip name, and taking the first "Detecting chip type" line picks it.
DETECT_NOISE = "unsupported detection protocol"


def board_for_chip(chip):
    """The profile for a chip string, or None when there is not one yet."""
    low = (chip or "").lower().strip()
    for prefix, board in CHIP_FAMILIES:
        if low.startswith(prefix):
            return board
    return None


def _run(cmd, timeout=6):
    """A command's stdout, or "" — never raises, never needs privilege."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else (p.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ----------------------------------------------------------------- the radio
def rfkill():
    """Soft/hard block per wireless phy, straight from sysfs (no root needed)."""
    out = []
    for d in sorted(glob.glob("/sys/class/rfkill/rfkill*")):
        if _read(os.path.join(d, "type")) != "wlan":
            continue
        out.append({
            "name": _read(os.path.join(d, "name")) or os.path.basename(d),
            "soft_blocked": _read(os.path.join(d, "soft")) == "1",
            "hard_blocked": _read(os.path.join(d, "hard")) == "1",
        })
    return out


def _phy_info(phy):
    """Modes, interface combinations and bands for one phy."""
    txt = _run(["iw", "phy", phy, "info"])
    modes, combos, bands = [], [], {"2.4": 0, "5": 0}
    section = None
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("Supported interface modes"):
            section = "modes"
            continue
        if stripped.startswith("valid interface combinations"):
            section = "combos"
            continue
        if stripped.startswith("Band "):
            section = None
        if section == "modes":
            if stripped.startswith("* "):
                modes.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("*"):
                section = None
        elif section == "combos":
            # The combination list wraps onto continuation lines starting with
            # neither * nor #, so keep taking lines until something else begins.
            if stripped.startswith(("*", "#")) or (combos and stripped.endswith(",")):
                combos.append(stripped.lstrip("* ").rstrip(","))
            elif combos and re.match(r"^(total|#channels)", stripped):
                combos.append(stripped.rstrip(","))
            elif stripped and not stripped.startswith(("*", "#")):
                section = None
        m = re.match(r"\* (\d{4})(?:\.\d)? MHz", stripped)
        if m and "(disabled)" not in stripped:
            mhz = int(m.group(1))
            if 2400 <= mhz < 2500:
                bands["2.4"] += 1
            elif mhz >= 5000:
                bands["5"] += 1
    combo = " ".join(combos).strip()
    return {
        "phy": phy,
        "modes": modes,
        "combinations": combo,
        "bands": bands,
        "ap_capable": "AP" in modes,
        # "#{ managed, AP } <= 1" is the driver saying: pick one.
        "concurrent_ap_and_station": bool(combo) and not re.search(
            r"#\{[^}]*managed[^}]*AP[^}]*\}\s*<=\s*1", combo),
    }


def radio():
    """Every wireless interface, with its phy's real capability."""
    ifaces = []
    name = None
    for line in _run(["iw", "dev"]).splitlines():
        s = line.strip()
        if s.startswith("Interface "):
            name = s.split()[1]
            ifaces.append({"name": name, "type": None, "addr": None, "ssid": None})
        elif ifaces and s.startswith("type "):
            ifaces[-1]["type"] = s.split()[1]
        elif ifaces and s.startswith("addr "):
            ifaces[-1]["addr"] = s.split()[1]
        elif ifaces and s.startswith("ssid "):
            ifaces[-1]["ssid"] = s.split(" ", 1)[1]
    for i in ifaces:
        i["operstate"] = _read("/sys/class/net/%s/operstate" % i["name"]) or "unknown"
        i["addresses"] = _addresses(i["name"])
    phys = [os.path.basename(p) for p in sorted(glob.glob("/sys/class/ieee80211/phy*"))]
    return {
        "interfaces": ifaces,
        "phys": [_phy_info(p) for p in phys],
        "rfkill": rfkill(),
    }


def _addresses(iface):
    out = []
    for line in _run(["ip", "-o", "-4", "addr", "show", "dev", iface]).splitlines():
        m = re.search(r"\binet (\S+)", line)
        if m:
            out.append(m.group(1))
    return out


# ------------------------------------------------------- the access point
def net_config(reveal=False):
    """The AP config the helper would act on, if it has been written yet."""
    try:
        with open(NET_CONFIG) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    return cfg if reveal else {k: v for k, v in cfg.items() if k not in SECRET_KEYS}


def stations(iface="wlan0"):
    """Associated clients. Needs privilege on most drivers, so it may be empty."""
    txt = _run(["iw", "dev", iface, "station", "dump"])
    out, cur = [], None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Station "):
            cur = {"mac": s.split()[1], "signal": None, "rx": None, "tx": None,
                   "inactive_ms": None}
            out.append(cur)
        elif cur and s.startswith("signal:"):
            m = re.search(r"(-?\d+)", s)
            cur["signal"] = int(m.group(1)) if m else None
        elif cur and s.startswith("inactive time:"):
            m = re.search(r"(\d+)", s)
            cur["inactive_ms"] = int(m.group(1)) if m else None
    return out


def ap_state():
    """Is an access point up, and if not, what is stopping it."""
    r = radio()
    wlans = [i for i in r["interfaces"] if i["name"].startswith(("wlan", "wl"))]
    iface = wlans[0] if wlans else None
    blocked = any(x["soft_blocked"] or x["hard_blocked"] for x in r["rfkill"])
    phy = r["phys"][0] if r["phys"] else None
    up = bool(iface and iface["type"] == "AP" and iface["operstate"] == "up")
    # The stored config is root-only — it holds the passphrase — so the helper is
    # the only thing that can say what is configured. Reading the file is the
    # fallback for a machine without the helper service.
    helper_status = None
    if os.path.exists(HELPER):
        try:
            helper_status = helper("status", timeout=10)
        except NetError:
            helper_status = None
    cfg = (helper_status or {}).get("config") or net_config()

    # What is missing depends on what is carrying the network: a wire needs an
    # address and a DHCP server, only a radio needs unblocking and hostapd.
    chosen = (cfg or {}).get("interface") or (iface or {}).get("name")
    wireless = is_wireless(chosen) if chosen else True

    missing = []
    if wireless:
        if not phy or not phy["ap_capable"]:
            missing.append("this radio does not advertise AP mode")
        # Not listed as missing: `up` unblocks the radio itself.
        if not shutil.which("hostapd"):
            missing.append("hostapd is not installed")
    if not os.path.exists(HELPER):
        missing.append("the privileged helper is not installed")
    elif not helper_socket_ready():
        missing.append("the helper service is not running")
    if not cfg and not (helper_status or {}).get("configured"):
        missing.append("no network has been configured yet")

    # When the helper is installed it is the authority on what is running: it
    # owns the processes and the lease file.
    leases = []
    if helper_status:
        up = bool(helper_status.get("up"))
        leases = helper_status.get("leases") or []

    return {
        "up": up,
        "leases": leases,
        "interface": chosen or (iface["name"] if iface else None),
        "wireless_link": wireless,
        "mode": iface["type"] if iface else None,
        "ssid": (cfg or {}).get("ssid") or (iface or {}).get("ssid"),
        "channel": (cfg or {}).get("channel"),
        "subnet": (cfg or {}).get("subnet"),
        "gateway": (cfg or {}).get("gateway"),
        "serving": (helper_status or {}).get("interface") or (cfg or {}).get("interface"),
        "wireless": (helper_status or {}).get("wireless",
                                              (cfg or {}).get("wireless")),
        "links": (helper_status or {}).get("links") or link_candidates(),
        "isolate": (cfg or {}).get("isolate"),
        "lan_access": (cfg or {}).get("lan_access"),
        "addresses": (iface or {}).get("addresses") or [],
        "clients": stations(iface["name"]) if up and iface else [],
        "blocked": blocked,
        "configured": bool(cfg) or bool((helper_status or {}).get("configured")),
        "missing": missing,
        "channels": AP_CHANNELS,
    }


# --------------------------------------------------------- board profiles
def board_profiles():
    """Every generated profile, by board id."""
    out = {}
    for name in sorted(os.listdir(BOARDS_DIR)) if os.path.isdir(BOARDS_DIR) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(BOARDS_DIR, name)) as fh:
                doc = json.load(fh)
            out[doc["board"]] = doc
        except (OSError, ValueError, KeyError):
            continue
    return out


def board_profile(board):
    return board_profiles().get(board)


def board_summaries():
    """Enough to choose a board in a list, without shipping every pin table."""
    out = []
    for doc in board_profiles().values():
        out.append({
            "board": doc["board"], "label": doc["label"], "chip": doc["chip"],
            "cores": doc["cores"], "voltage": doc["voltage"],
            "flash_default_mb": doc["flash_default_mb"],
            "peripherals": doc["peripherals"], "counts": doc["counts"],
            "source": doc["source"],
        })
    return sorted(out, key=lambda d: d["label"])


# ------------------------------------------------------------- the scanner
# Where to look for a board. Overridable because not every system exposes them
# here, and because it is the only way to exercise the scan with nothing plugged in.
SERIAL_GLOBS = tuple(
    os.environ.get("ZERO2W_IOT_SERIAL_GLOBS", "/dev/ttyUSB*:/dev/ttyACM*").split(":"))


def serial_ports():
    """Candidate ports, with whatever the kernel already knows about each."""
    out = []
    for pattern in SERIAL_GLOBS:
        for dev in sorted(glob.glob(pattern)):
            info = {"port": dev, "driver": None, "vendor": None, "product": None,
                    "writable": os.access(dev, os.W_OK)}
            base = "/sys/class/tty/%s/device" % os.path.basename(dev)
            # realpath() happily invents a path for a link that is not there,
            # which is how a fake port came back with driver "driver".
            if os.path.islink(base + "/driver"):
                info["driver"] = os.path.basename(os.path.realpath(base + "/driver"))
            # Walk up to the USB device node, where the strings live.
            node = os.path.realpath(base)
            for _ in range(4):
                if os.path.exists(os.path.join(node, "idVendor")):
                    info["vendor"] = _read(os.path.join(node, "manufacturer")) or \
                        _read(os.path.join(node, "idVendor"))
                    info["product"] = _read(os.path.join(node, "product")) or \
                        _read(os.path.join(node, "idProduct"))
                    break
                node = os.path.dirname(node)
            out.append(info)
    return out


def esptool_cmd():
    """esptool is packaged under two names and neither is guaranteed."""
    for name in ("esptool", "esptool.py"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def identify(port, timeout=40):
    """Ask one board what it is. Never writes to flash."""
    cmd = esptool_cmd()
    if not cmd:
        return {"port": port, "ok": False,
                "error": "esptool is not installed",
                "hint": "run: sudo apt install esptool"}
    p = subprocess.run(cmd + ["--port", port, "flash_id"],
                       capture_output=True, text=True, timeout=timeout)
    text = p.stdout + p.stderr
    stubless = False
    if p.returncode != 0 and _stub_missing(text):
        # Debian's esptool is a +dfsg repack with the ESP32, S2 and S3 stub
        # flashers stripped out. The ROM loader can answer this on its own.
        stubless = True
        p = subprocess.run(cmd + ["--port", port, "--no-stub", "flash_id"],
                           capture_output=True, text=True, timeout=timeout + 30)
        text = p.stdout + p.stderr
    if p.returncode != 0:
        return {"port": port, "ok": False, "error": _esptool_error(text), "raw": text[-600:]}

    out = {"port": port, "ok": True, "raw": text[-1200:], "stubless": stubless}
    out.update(parse_esptool(text))
    if stubless:
        out["warning"] = ("this esptool is missing its stub flasher for this chip, "
                          "so it fell back to the ROM loader — slower, and it "
                          "cannot erase flash")
    if out.get("board") is None:
        out["hint"] = ("no profile for %s yet — add one in scripts/build_boards.py"
                       % (out.get("chip") or "this chip"))
    return out


def parse_esptool(text):
    """Pull the board's identity out of `esptool flash_id` output."""
    out = {"chip": None, "chip_detail": None, "revision": None, "mac": None,
           "flash": None, "crystal": None, "crystal_warning": None,
           "features": None, "psram": None, "board": None}
    detected = [c.strip() for c in
                re.findall(r"Detecting chip type\.\.\.\s*(\S[^\r\n]*)", text)
                if DETECT_NOISE not in c.lower()]
    out["chip"] = detected[-1] if detected else None
    m = re.search(r"^Chip is (.+)$", text, re.M)
    out["chip_detail"] = m.group(1).strip() if m else None
    if not out["chip"] and out["chip_detail"]:
        out["chip"] = re.sub(r"\s*\(.*$", "", out["chip_detail"]).strip()
    m = re.search(r"\(revision (v?[\d.]+)\)", out["chip_detail"] or "")
    out["revision"] = m.group(1) if m else None
    m = re.search(r"MAC: ([0-9a-fA-F:]{17})", text)
    out["mac"] = m.group(1).lower() if m else None
    m = re.search(r"Detected flash size: (\S+)", text)
    out["flash"] = m.group(1) if m else None
    m = re.search(r"Crystal is (\S+)", text)
    out["crystal"] = m.group(1) if m else None
    # esptool times its own stub against the crystal it thinks is fitted, so a
    # misread is not cosmetic: a write at 460800 under the wrong assumption can
    # land corrupt. Seen on this fleet, on the run that wrote the board.
    m = re.search(r"WARNING: Detected crystal freq [^\r\n]+", text)
    out["crystal_warning"] = m.group(0).strip() if m else None
    m = re.search(r"^Features: (.+)$", text, re.M)
    if m:
        out["features"] = m.group(1).strip()
        # Only *embedded* PSRAM shows up here. An ESP32-CAM carries its PSRAM on
        # the module, so absence is unknown rather than False and the device's own
        # probe settles it.
        out["psram"] = True if "PSRAM" in m.group(1) else None
    out["board"] = board_for_chip(out["chip"] or out["chip_detail"])
    return out


def _stub_missing(text):
    return "stub_flasher" in text and "FileNotFoundError" in text


def stub_status():
    """Which stub flashers this esptool actually has."""
    cmd = esptool_cmd()
    if not cmd:
        return None
    # find_spec locates the package without importing it — this console stays
    # stdlib-only, and a test enforces it.
    root = None
    try:
        spec = importlib.util.find_spec("esptool")
        if spec and spec.submodule_search_locations:
            root = os.path.join(list(spec.submodule_search_locations)[0],
                                "targets", "stub_flasher")
    except (ImportError, ValueError, AttributeError):
        root = None
    if not root or not os.path.isdir(root):
        return None
    want = {"32": "ESP32", "32s2": "ESP32-S2", "32s3": "ESP32-S3", "32c3": "ESP32-C3"}
    missing = [label for key, label in want.items()
               if not os.path.isfile(os.path.join(root, "stub_flasher_%s.json" % key))]
    return {"dir": root, "missing": missing, "ok": not missing}


def _esptool_error(text):
    low = text.lower()
    if "permission denied" in low:
        return "permission denied on the port — is this user in the dialout group?"
    if "failed to connect" in low or "no serial data" in low:
        return ("no answer from the board — hold BOOT (or GPIO0 to GND) while it "
                "resets, then scan again")
    if "device or resource busy" in low:
        return "the port is open in something else (a serial monitor?)"
    if "stub_flasher" in low and "filenotfounderror" in low:
        return ("this esptool has no stub flasher for this chip — Debian's "
                "package ships without the ESP32 ones")
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()[:200]
    return "esptool failed"


def scan(probe=True):
    """Every candidate port, identified if esptool is available."""
    ports = serial_ports()
    stubs = stub_status()
    out = {"ports": [], "esptool": bool(esptool_cmd()),
           "dialout": _in_dialout(), "stubs": stubs,
           "hint": None if esptool_cmd() else "run: sudo apt install esptool"}
    if stubs and stubs["missing"]:
        out["hint"] = ("run: sudo install -m 0644 ~/.cache/zero2w-console/esptool-stubs/*.json %s"
                       % stubs["dir"])
        out["stub_warning"] = (
            "This esptool has no stub flasher for %s — Debian ships it as a "
            "+dfsg repack with those stripped out. Scanning falls back to the "
            "ROM loader, which works but is slow and cannot erase flash."
            % ", ".join(stubs["missing"]))
    for info in ports:
        row = dict(info)
        if probe and out["esptool"] and info["writable"]:
            try:
                row.update(identify(info["port"]))
            except subprocess.SubprocessError as exc:
                row.update({"ok": False, "error": str(exc)})
        elif not info["writable"]:
            row.update({"ok": False,
                        "error": "not writable by this user",
                        "hint": "run: sudo usermod -aG dialout $USER, then log out and in"})
        out["ports"].append(row)
    if not ports:
        out["hint"] = ("nothing on %s — plug a board in, and check the cable carries "
                       "data rather than only power" % " or ".join(SERIAL_GLOBS))
    return out


def _in_dialout():
    try:
        import grp
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        return user in grp.getgrnam("dialout").gr_mem or \
            grp.getgrnam("dialout").gr_gid in os.getgroups()
    except (KeyError, OSError, ImportError):
        return False


# ------------------------------------------------- talking to the helper
class NetError(Exception):
    """The helper refused, or is not installed. Carries a usable message."""


def is_wireless(iface):
    """A radio has to invent the medium; a wire already is one."""
    return (os.path.exists("/sys/class/net/%s/phy80211" % iface) or
            os.path.exists("/sys/class/net/%s/wireless" % iface))


def link_candidates():
    """Interfaces that could carry the IoT network, and what each would need."""
    out = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        iface = os.path.basename(path)
        if iface == "lo" or iface.startswith(("docker", "veth", "tailscale", "p2p-")):
            continue
        wireless = is_wireless(iface)
        out.append({
            "interface": iface,
            "kind": "wireless" if wireless else "wired",
            "operstate": _read(os.path.join(path, "operstate")),
            "carrier": _read(os.path.join(path, "carrier")) == "1",
            "needs": "an access point (hostapd)" if wireless else "nothing but an address",
        })
    return out


def validate_net_config(cfg):
    """Check a network before handing it to the helper."""
    import ipaddress

    if not isinstance(cfg, dict):
        raise NetError("expected an object")
    iface = cfg.get("interface") or "wlan0"
    if not os.path.isdir("/sys/class/net/%s" % iface):
        raise NetError("there is no interface called %r on this machine" % iface)
    wireless = is_wireless(iface)

    ssid = (cfg.get("ssid") or "").strip()
    psk = cfg.get("psk") or ""
    channel = cfg.get("channel")
    if wireless:
        if not 1 <= len(ssid.encode("utf-8")) <= 32:
            raise NetError("SSID must be 1-32 bytes")
        if not re.fullmatch(r"[\x20-\x7e]+", ssid):
            raise NetError("SSID must be printable ASCII (no newlines, no unicode)")
        if not 8 <= len(psk) <= 63:
            raise NetError("passphrase must be 8-63 characters — WPA2 requires it")
        if not re.fullmatch(r"[\x20-\x7e]+", psk):
            raise NetError("passphrase must be printable ASCII")
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            raise NetError("channel is required for a wireless interface")
        if channel not in AP_CHANNELS:
            raise NetError("channel must be 1, 6 or 11 — the non-overlapping 2.4 GHz "
                           "channels, and the only band an ESP32 can see")
    else:
        ssid, psk = ssid or None, psk or None
        channel = channel if channel in AP_CHANNELS else None
    try:
        net = ipaddress.ip_network(cfg.get("subnet") or "", strict=True)
    except ValueError as exc:
        raise NetError("subnet: %s" % exc)
    if net.version != 4 or not net.is_private:
        raise NetError("subnet must be a private IPv4 network, e.g. 10.42.0.0/24")
    if not 16 <= net.prefixlen <= 29:
        raise NetError("subnet must be between /16 and /29")
    out = {"ssid": ssid, "psk": psk, "channel": channel, "subnet": str(net),
           "wireless": wireless, "interface": iface,
           "isolate": cfg.get("isolate", True) is not False,
           "lan_access": cfg.get("lan_access", False) is True}
    if cfg.get("uplink"):
        out["uplink"] = cfg["uplink"]
    if cfg.get("country"):
        out["country"] = str(cfg["country"]).upper()
    return out


# Exactly the verbs packaging/iot-netctl defines and the sudoers entry names.
HELPER_VERBS = ("config", "up", "down", "firewall", "radio-on", "radio-off",
                "status", "clients", "diagnose")


def helper_socket_ready():
    return os.path.exists(HELPER_SOCKET) and os.access(HELPER_SOCKET, os.W_OK)


def _helper_over_socket(verb, payload, timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(HELPER_SOCKET)
        sock.sendall((json.dumps({"verb": verb, "payload": payload}) + "\n").encode())
        raw = b""
        while b"\n" not in raw and len(raw) < 262144:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    except OSError as exc:
        raise NetError("the network helper is not answering (%s)" % exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    try:
        out = json.loads(raw.decode() or "{}")
    except ValueError:
        raise NetError("the network helper answered with nonsense")
    if isinstance(out, dict) and out.get("error"):
        raise NetError(out["error"])
    return out


def helper(verb, payload=None, timeout=30):
    """Ask the privileged helper for one verb."""
    if verb not in HELPER_VERBS:
        raise NetError("unknown verb")
    if helper_socket_ready():
        return _helper_over_socket(verb, payload, timeout)
    if not os.path.exists(HELPER):
        raise NetError("the privileged helper is not installed — see the IOT screen")
    cmd = ["sudo", "-n", HELPER, verb]
    try:
        p = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NetError(str(exc))
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or ("exit %d" % p.returncode)
        if "no new privileges" in msg.lower():
            msg = ("this service runs with NoNewPrivileges=yes, so it can never use "
                   "sudo. Install the helper service instead: "
                   "sudo cp packaging/zero2w-iotnet.service /etc/systemd/system/ && "
                   "sudo systemctl enable --now zero2w-iotnet")
        elif "password is required" in msg or "a terminal is required" in msg:
            msg = ("sudo asked for a password — install the helper service: "
                   "sudo systemctl enable --now zero2w-iotnet")
        raise NetError(msg)
    try:
        return json.loads(p.stdout or "{}")
    except ValueError:
        return {"ok": True, "output": p.stdout.strip()}


# ---------------------------------------------------------------- the switch
RFKILL_DEV = "/dev/rfkill"
RFKILL_TYPE_WLAN = 1
RFKILL_OP_CHANGE_ALL = 3


def _rfkill_write(soft):
    """One `struct rfkill_event`: change every wlan phy's soft block."""
    with open(RFKILL_DEV, "wb", buffering=0) as fh:
        fh.write(struct.pack("<IBBBB", 0, RFKILL_TYPE_WLAN, RFKILL_OP_CHANGE_ALL,
                             1 if soft else 0, 0))


def radio_writable():
    """Can this process set the soft block itself?"""
    try:
        fd = os.open(RFKILL_DEV, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


def set_radio(on):
    """Unblock or block the wireless radio."""
    word = "unblock" if on else "block"
    try:
        _rfkill_write(not on)
        return {"ok": True, "via": "/dev/rfkill", "blocked": not on}
    except OSError as direct:
        try:
            out = helper("radio-on" if on else "radio-off")
            out.setdefault("via", "helper")
            out.setdefault("blocked", not on)
            return out
        except NetError as through_helper:
            raise NetError("could not %s the radio: %s. The helper could not either: "
                           "%s. Run: sudo rfkill %s wifi"
                           % (word, direct, through_helper, word))


def default_uplink():
    """The interface holding the default route — what an AP would NAT towards."""
    for line in _run(["ip", "-o", "route", "show", "default"]).splitlines():
        m = re.search(r"\bdev (\S+)", line)
        if m:
            return m.group(1)
    return None


# ------------------------------------------------------------ the devices
class DeviceStore:
    """Enrolled devices, as one JSON document beside the flows."""

    def __init__(self, path=DEVICES_FILE):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    def load(self):
        with self.lock:
            return self._read()

    def _read(self):
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or not isinstance(d.get("devices"), list):
                return {"devices": [], "enrollment": None}
            d.setdefault("enrollment", None)
            return d
        except (OSError, ValueError):
            return {"devices": [], "enrollment": None}

    def save(self, doc):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(doc, fh, indent=2)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        return doc


SECRET_KEYS = ("token", "secret", "psk", "enroll_token")


def new_id(prefix="dev"):
    return "%s_%s" % (prefix, secrets.token_hex(4))


def now():
    return int(time.time())


def public(device):
    """One device with nothing secret in it, and the one answer to "is it
    up"."""
    from .flows import DEVICE_FRESH
    out = {k: v for k, v in device.items() if k not in SECRET_KEYS}
    seen = out.get("last_seen")
    out["online"] = bool(seen and now() - seen < DEVICE_FRESH)
    return out


def devices(store):
    doc = store.load()
    return [public(d) for d in doc.get("devices", [])]


def get_device(store, device_id):
    for d in store.load().get("devices", []):
        if d.get("id") == device_id:
            return d
    return None


def create_device(store, body):
    """A config is made from what the scan found, not from what was typed."""
    name = (body.get("name") or "").strip()
    if not name:
        raise NetError("a name is required")
    if len(name) > 48:
        raise NetError("name must be 48 characters or fewer")
    board = body.get("board")
    if board and board not in board_profiles():
        raise NetError("no profile for board %r" % board)

    doc = store.load()
    if any(d.get("name") == name for d in doc["devices"]):
        raise NetError("a config named %r already exists" % name)
    mac = body.get("mac")
    if mac and any(d.get("mac") == mac for d in doc["devices"]):
        raise NetError("a config for the board with MAC %s already exists" % mac)

    device = {
        "id": new_id(),
        "name": name,
        "board": board,
        "created": now(),
        # Everything below is what the scan saw, kept so a later mismatch is
        # visible rather than silently overwritten.
        "chip": body.get("chip"),
        "mac": mac,
        "flash": body.get("flash"),
        "psram": psram_kind(body.get("psram")),
        "port": body.get("port"),
        "note": (body.get("note") or "").strip()[:200] or None,
        # A camera is a property of the board, not of any flow.
        "camera": bool(body.get("camera")),
        "camera_port": int(body.get("camera_port") or 8080),
        # Whether this board holds a socket open instead of polling. Off until
        # turned on: the modules it needs cost memory a small board may not have.
        "link": bool(body.get("link")),
        # State the device fills in for itself.
        "flow": None,
        "enrolled": None,
        "last_seen": None,
        "ip": None,
        "rssi": None,
        "probe": None,
        "firmware": None,
        "flashed": None,
        "token": None,
        "enroll_token": None,
    }
    doc["devices"].append(device)
    store.save(doc)
    return device


# `txpower` is dBm for the station radio, or None for the firmware default. The
# transmitter is the largest current draw on an ESP32 and this fleet has a board
# that cannot complete a join on USB power at all, so turning it down is the one
# lever there is on a sagging rail. It rides in config.json, before any sync.
EDITABLE = ("name", "board", "note", "flow", "camera", "camera_port", "link",
            "txpower", "psram")

# What PSRAM a board carries, which decides the firmware it is flashed with.
# A scan only sees PSRAM inside the chip; a WROVER's is outside it, so it is
# said here by hand.
PSRAM_KINDS = ("quad", "octal")


def psram_kind(value):
    """None, "quad" or "octal", from what a scan or a person said."""
    if value in (None, False, "", "none"):
        return None
    if value is True:
        return "quad"
    if value in PSRAM_KINDS:
        return value
    raise NetError("psram is none, quad or octal, not %r" % (value,))


def update_device(store, device_id, body):
    doc = store.load()
    for d in doc["devices"]:
        if d.get("id") != device_id:
            continue
        for key in EDITABLE:
            if key not in body:
                continue
            value = body[key]
            if key == "name":
                value = (value or "").strip()
                if not value:
                    raise NetError("a name is required")
                if any(o is not d and o.get("name") == value for o in doc["devices"]):
                    raise NetError("a config named %r already exists" % value)
            if key == "board" and value and value not in board_profiles():
                raise NetError("no profile for board %r" % value)
            if key == "psram":
                value = psram_kind(value)
            d[key] = value
        store.save(doc)
        return d
    raise NetError("no such device")


def delete_device(store, device_id):
    doc = store.load()
    keep = [d for d in doc["devices"] if d.get("id") != device_id]
    if len(keep) == len(doc["devices"]):
        raise NetError("no such device")
    doc["devices"] = keep
    for group in doc.get("groups") or []:
        group["devices"] = [d for d in group["devices"] if d != device_id]
    store.save(doc)
    return {"ok": True, "deleted": device_id}


def rehome_device(store, device_id, body):
    """Move a config onto a different physical board.

    The identity is taken from a fresh scan, as when a config is made; the
    name, flow and settings stay. Whatever the old board held is void: its
    token, its enrolment and its last report.
    """
    mac = (body.get("mac") or "").strip()
    if not mac:
        raise NetError("scan the new board first: its MAC is what the config records")
    board = body.get("board")
    if board and board not in board_profiles():
        raise NetError("no profile for board %r" % board)
    doc = store.load()
    device = None
    for d in doc["devices"]:
        if d.get("id") == device_id:
            device = d
        elif d.get("mac") == mac:
            raise NetError("the config %r is already for the board with MAC %s"
                           % (d.get("name"), mac))
    if device is None:
        raise NetError("no such device")
    if device.get("mac") == mac:
        raise NetError("this config is already for that board")
    device["previous_mac"] = device.get("mac")
    device["rehomed"] = now()
    for key in ("chip", "flash", "port"):
        device[key] = body.get(key)
    device["mac"] = mac
    device["board"] = board or device.get("board")
    if psram_kind(body.get("psram")):          # seen by the scan, so certain
        device["psram"] = device.get("psram") or psram_kind(body.get("psram"))
    for key in ("token", "enroll_token", "enrolled", "flashed", "probe", "state",
                "last_seen", "modules", "ip", "rssi"):
        device[key] = None
    store.save(doc)
    return device


# ------------------------------------------------------------------ groups
# A named list of devices. Anything that takes a device takes "group:<name>".
GROUP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
GROUP_PREFIX = "group:"


def groups(store):
    return store.load().get("groups") or []


def save_groups(store, rows):
    """Replace every group. Names are unique; members must be devices."""
    if not isinstance(rows, list):
        raise NetError("groups must be a list")
    doc = store.load()
    known = set(d.get("id") for d in doc["devices"])
    out, seen = [], set()
    for row in rows:
        name = str((row or {}).get("name") or "").strip()
        if not GROUP_RE.match(name):
            raise NetError("%r is not a usable group name — letters, digits, "
                           "_ and -, starting with a letter" % name)
        if name.lower() in seen:
            raise NetError("there are two groups called %r" % name)
        seen.add(name.lower())
        members = []
        for device_id in row.get("devices") or []:
            if device_id not in known:
                raise NetError("%r is not a device" % device_id)
            if device_id not in members:
                members.append(device_id)
        out.append({"name": name, "devices": members})
    doc["groups"] = out
    store.save(doc)
    return out


def targets(doc, value):
    """The device ids a device field means: itself, or a group's members."""
    value = (value or "").strip()
    if not value.startswith(GROUP_PREFIX):
        return [value] if value else []
    want = value[len(GROUP_PREFIX):].lower()
    for group in doc.get("groups") or []:
        if group["name"].lower() == want:
            return list(group["devices"])
    return []


# -------------------------------------------------------- honest reporting
def _ap_hint(ap, helper_ok, hostapd_ok):
    """What to do next about the network, in the order things block."""
    wireless = ap.get("wireless_link", True)
    if wireless and not hostapd_ok:
        return "run: sudo apt install hostapd && sudo systemctl mask hostapd"
    if not helper_ok:
        return ("run: sudo cp %s/packaging/zero2w-iotnet.service /etc/systemd/system/ "
                "&& sudo systemctl enable --now zero2w-iotnet" % REPO)
    if not ap.get("configured"):
        return ("choose an interface and a subnet on the IOT screen"
                if not wireless else
                "set an SSID, passphrase and channel on the IOT screen")
    if ap.get("missing"):
        # Something specific is in the way and the list already names it.
        return ap["missing"][0]
    if not ap.get("up"):
        return ("everything is in place — press Start (or run: "
                "sudo /usr/local/sbin/iot-netctl up)")
    return None


def capabilities(store=None):
    """What works, what does not, and the exact command that fixes it."""
    r = radio()
    phy = r["phys"][0] if r["phys"] else None
    ap = ap_state()
    hints = {}

    if phy and not phy["ap_capable"]:
        hints["radio"] = ("this phy does not advertise AP mode; a USB wifi adapter "
                          "would add one")
    elif ap["blocked"]:
        hints["radio"] = "run: nmcli radio wifi on   (or: sudo rfkill unblock wifi)"
    if not shutil.which("hostapd"):
        hints["hostapd"] = "run: sudo apt install hostapd && sudo systemctl mask hostapd"
    if not os.path.exists(HELPER):
        hints["helper"] = ("run: sudo install -m 0755 %s/packaging/iot-netctl %s"
                           % (REPO, HELPER))
    elif not helper_socket_ready():
        # sudo cannot work from this service: the unit sets NoNewPrivileges.
        hints["helper"] = ("run: sudo cp %s/packaging/zero2w-iotnet.service "
                           "/etc/systemd/system/ && sudo systemctl enable --now "
                           "zero2w-iotnet" % REPO)
    if not ap["configured"]:
        hints["network"] = "set an SSID, passphrase and channel on the IOT screen"
    elif not ap["up"] and not ap["missing"]:
        # Nothing is missing, so say what would bring it up.
        hints["ready"] = ("everything is in place — press Start (or run: "
                          "sudo /usr/local/sbin/iot-netctl up)")

    return {
        "radio": {
            "available": bool(phy and phy["ap_capable"] and not ap["blocked"]),
            "phy": phy,
            "interfaces": r["interfaces"],
            "rfkill": r["rfkill"],
            # Worth saying in words: it is the reason there is one IoT network.
            "note": ("this radio is an access point or a station, never both"
                     if phy and not phy["concurrent_ap_and_station"] else None),
            "hint": hints.get("radio"),
        },
        "ap": {
            "available": ap["up"],
            "state": ap,
            "hostapd": bool(shutil.which("hostapd")),
            "helper": os.path.exists(HELPER) and helper_socket_ready(),
            "helper_installed": os.path.exists(HELPER),
            "helper_socket": helper_socket_ready(),
            "hint": _ap_hint(ap, helper_ok=os.path.exists(HELPER) and helper_socket_ready(),
                             hostapd_ok=bool(shutil.which("hostapd"))),
        },
        "devices": {
            "available": bool(store and devices(store)),
            "count": len(devices(store)) if store else 0,
            "enrollment": (store.load().get("enrollment") if store else None),
            "hint": None if (store and devices(store)) else
                    "no device has enrolled yet — see docs/IOT-PLAN.md",
        },
    }


if __name__ == "__main__":
    print(json.dumps(capabilities(DeviceStore()), indent=2))
