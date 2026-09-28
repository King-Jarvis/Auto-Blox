#!/usr/bin/env python3
"""Flash a board and hand it its identity, in one command."""
import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# `RawREPL` and `VERIFY` live in serialport.py. `Serial` does not: this file has
# its own, and the shared one takes an exclusive flock and opens non-blocking,
# which would change what happens here when something else holds the port.
from zero2w_console.serialport import RawREPL, VERIFY  # noqa: E402

CACHE = os.path.expanduser("~/.cache/zero2w-console/firmware")
CONSOLE = os.environ.get("ZERO2W_URL", "http://127.0.0.1:8787")
from zero2w_console import paths  # noqa: E402
TOKEN_FILE = paths.token_file()

# Which MicroPython board build belongs to which chip family.
BUILDS = {
    "esp32": "ESP32_GENERIC",
    "esp32s3": "ESP32_GENERIC_S3",
    "esp32c3": "ESP32_GENERIC_C3",
    "esp32cam": "ESP32_GENERIC",      # an AI-Thinker CAM is a classic ESP32
}
# Where the image starts. The newer parts put it at zero.
OFFSETS = {"esp32": "0x1000", "esp32s3": "0x0", "esp32c3": "0x0", "esp32cam": "0x1000"}

# A stock MicroPython has no camera module at all, so a board configured with a
# camera needs a build that has one. Flashing stock onto one silently leaves the
# camera dead, which has happened once.
#
# Pinned to v0.6.0: v0.6.1 and v0.6.2 cannot start an OV3660 or OV5640 in
# JPEG ("Failed to capture initial frame"), and 0.6.0 can
# (github.com/cnadler86/micropython-camera-API/issues/55). Every release names
# its asset alike, so the cache is keyed by the tag, not the file name.
CAMERA_BUILDS = {
    "esp32cam": {"repo": "cnadler86/micropython-camera-API", "tag": "v0.6.0",
                 "match": "AI_THINKER"},
    "esp32s3": {"repo": "cnadler86/micropython-camera-API", "tag": "v0.6.0",
                "match": "ESP32S3_CAM_LCD"},
}

# Uploaded over the wire; everything else arrives over WiFi.
# The same bootstrap the console serves: the compiled agent, or its source.
with open(os.path.join(ROOT, "zero2w_console", "agent", "main.py")) as _fh:
    BOOTSTRAP = _fh.read()


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def die(msg):
    raise SystemExit("iot-flash: " + msg)


# ------------------------------------------------------------ the console
def console(method, path, body=None):
    token = os.environ.get("ZERO2W_TOKEN")
    if not token:
        try:
            with open(TOKEN_FILE) as fh:
                token = fh.read().strip()
        except OSError:
            die("no console token — is the console installed?")
    req = urllib.request.Request(CONSOLE + path, method=method)
    req.add_header("X-Console-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=30) as fh:
            return json.loads(fh.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        die("console said %d: %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        die("cannot reach the console at %s (%s)" % (CONSOLE, exc.reason))


# ----------------------------------------------------------------- esptool
def esptool_cmd():
    for name in ("esptool", "esptool.py"):
        found = shutil.which(name)
        if found:
            return [found]
    die("esptool is not installed — run: sudo apt install esptool")


def esptool(args, port, stubless, timeout=900):
    cmd = esptool_cmd() + ["--port", port]
    if stubless:
        cmd.append("--no-stub")
    cmd += args
    say("    $", " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []
    for line in p.stdout:
        out.append(line)
        line = line.rstrip()
        if line:
            say("     ", line)
    p.wait(timeout=timeout)
    return p.returncode, "".join(out)


def stub_missing(text):
    return "stub_flasher" in text and "FileNotFoundError" in text


def identify(port):
    code, text = esptool(["flash_id"], port, stubless=False, timeout=120)
    stubless = False
    if code != 0 and stub_missing(text):
        say("    this esptool has no stub for that chip — falling back to the ROM loader")
        stubless = True
        code, text = esptool(["flash_id"], port, stubless=True, timeout=180)
    if code != 0:
        die("could not talk to the board on %s" % port)
    from zero2w_console import iot
    info = iot.parse_esptool(text)
    info["stubless"] = stubless
    return info


# ---------------------------------------------------------------- firmware
def psram_variant(board, psram):
    """The published build that uses a board's PSRAM, or None for the plain
    one: a classic ESP32 needs -SPIRAM, and an S3 needs -SPIRAM_OCT only for
    octal PSRAM (its plain build handles quad)."""
    if board == "esp32" and psram:
        return "SPIRAM"
    if board == "esp32s3" and psram == "octal":
        return "SPIRAM_OCT"
    return None


def firmware_url(build, variant=None):
    name = build + ("-" + variant.upper() if variant else "")
    page = "https://micropython.org/download/%s/" % build
    try:
        with urllib.request.urlopen(page, timeout=30) as fh:
            html = fh.read().decode(errors="replace")
    except urllib.error.URLError as exc:
        die("cannot reach micropython.org (%s)" % exc.reason)
    pattern = r"/resources/firmware/(%s-(\d{8})-v[\d.]+\.bin)" % re.escape(name)
    found = sorted(set(re.findall(pattern, html)), key=lambda t: t[1])
    if not found:
        die("no %s build listed on %s" % (name, page))
    return "https://micropython.org/resources/firmware/" + found[-1][0]


def camera_firmware(board):
    """A cached camera-capable build for this board, fetched if need be."""
    spec = CAMERA_BUILDS.get(board)
    if not spec:
        die("no camera-capable build is known for %r — pass --firmware" % board)
    os.makedirs(CACHE, exist_ok=True)
    stem = "mpy_cam-%s-%s" % (spec["tag"], spec["match"])
    out = os.path.join(CACHE, stem + ".bin")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        say("    cached %s (%d KB)" % (os.path.basename(out), os.path.getsize(out) // 1024))
        return out
    api = "https://api.github.com/repos/%s/releases/tags/%s" % (spec["repo"], spec["tag"])
    try:
        with urllib.request.urlopen(api, timeout=30) as fh:
            release = json.load(fh)
    except urllib.error.URLError as exc:
        die("cannot reach github for a camera build (%s) — pass --firmware" % exc.reason)
    assets = [a for a in release.get("assets", []) if spec["match"] in a["name"]]
    if not assets:
        die("no %s asset in %s %s" % (spec["match"], spec["repo"], spec["tag"]))
    asset = assets[0]
    say("    " + asset["browser_download_url"])
    blob = fetch(asset["browser_download_url"], name=stem + ".zip")
    import zipfile
    with zipfile.ZipFile(blob) as zf:
        member = [n for n in zf.namelist() if n.endswith(".bin")]
        if not member:
            die("no .bin inside %s" % os.path.basename(blob))
        with zf.open(member[0]) as src, open(out + ".part", "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.replace(out + ".part", out)
    say("    extracted %s (%d KB)" % (os.path.basename(out), os.path.getsize(out) // 1024))
    return out


def fetch(url, name=None):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name or url.rsplit("/", 1)[-1])
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        say("    cached", os.path.basename(path), "(%d KB)" % (os.path.getsize(path) // 1024))
        return path
    say("    downloading", os.path.basename(path))
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, path)
    say("    got %d KB" % (os.path.getsize(path) // 1024))
    return path


# ------------------------------------------------------------- the serial
class Serial:
    """Just enough of a serial port, from termios."""

    def __init__(self, path, baud=115200, timeout=1.0):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        speed = getattr(termios, "B%d" % baud)
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        iflag = oflag = lflag = 0
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = max(1, int(timeout * 10))
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        while data:
            n = os.write(self.fd, data)
            data = data[n:]

    def read(self, n=4096):
        try:
            return os.read(self.fd, n)
        except OSError:
            return b""

    def read_until(self, marker, timeout=10):
        marker = marker if isinstance(marker, bytes) else marker.encode()
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            buf += self.read()
            if marker in buf:
                return buf
        return buf

    def reset_into_run(self):
        """Reset into a normal boot, not the bootloader."""
        try:
            flags = struct_int(fcntl.ioctl(self.fd, termios.TIOCMGET, b"\0\0\0\0"))
            flags &= ~termios.TIOCM_DTR          # IO0 high: run what is in flash
            fcntl.ioctl(self.fd, termios.TIOCMSET, int_struct(flags | termios.TIOCM_RTS))
            time.sleep(0.15)                     # hold EN
            fcntl.ioctl(self.fd, termios.TIOCMSET, int_struct(flags & ~termios.TIOCM_RTS))
            time.sleep(0.05)
        except (OSError, AttributeError):
            pass       # not every bridge exposes the lines; Ctrl-C still works


def struct_int(raw):
    return int.from_bytes(raw[:4], sys.byteorder)


def int_struct(value):
    return value.to_bytes(4, sys.byteorder)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Flash a board for this console")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--device", help="device config id (dev_…)")
    ap.add_argument("--ssid")
    ap.add_argument("--psk", help="the passphrase, or - to read it from stdin")
    ap.add_argument("--host", help="URL the device should call back on")
    ap.add_argument("--variant", help="firmware variant, e.g. spiram")
    ap.add_argument("--board", help="override the profile the scan matched")
    ap.add_argument("--flow", help="also write this flow to the board, so it runs "
                                   "standalone before it has ever seen the network")
    ap.add_argument("--firmware", help="a .bin to write instead of the stock build, "
                                       "or a URL to fetch one from")
    ap.add_argument("--stock", action="store_true",
                    help="use the plain MicroPython build even for a camera device")
    ap.add_argument("--config-only", action="store_true",
                    help="skip the firmware and just rewrite the files over the REPL")
    ap.add_argument("--code-only", action="store_true",
                    help="write only the agent, the deployed flow and its modules "
                         "(compiled where the board can take them); leaves the "
                         "firmware, the network and the identity alone")
    ap.add_argument("--any-crystal", action="store_true",
                    help="write even if esptool misread the crystal — only for a "
                         "board that genuinely has one other than 40MHz")
    ap.add_argument("--no-erase", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.port):
        die("no such port: %s" % a.port)

    say("1. asking the board what it is")
    info = identify(a.port)
    # The config is where someone said which board this actually is: esptool only
    # ever sees the die, and an ESP32-CAM answers as any other classic ESP32.
    configured = None
    if a.device and not a.dry_run or a.device:
        for d in console("GET", "/api/iot/devices").get("devices", []):
            if d["id"] == a.device:
                configured = d
    board = a.board or (configured or {}).get("board") or info.get("board")
    say("    %s%s, %s flash, MAC %s -> profile %s"
        % (info.get("chip_detail") or info.get("chip") or "?",
           " rev " + info["revision"] if info.get("revision") else "",
           info.get("flash") or "?", info.get("mac") or "?", board or "none"))
    if configured and configured.get("mac") and info.get("mac") and \
            configured["mac"].lower() != info["mac"].lower():
        die("that config is for MAC %s, and this board is %s — wrong cable? "
            "To move the config onto this board, scan it on the IoT screen and "
            "use Move a config here." % (configured["mac"], info["mac"]))
    if not board or board not in BUILDS:
        die("no MicroPython build mapped for %r — pass --board" % (board or info.get("chip")))

    if a.code_only:
        return code_only(a, configured)

    say("2. provisioning with the console")
    if not a.device:
        die("--device is required: make a config on the IOT screen first")
    prov = {} if a.dry_run else console("POST", "/api/iot/devices/%s/provision" % a.device, {})
    status = console("GET", "/api/iot/status")
    ssid = a.ssid or (status.get("ap", {}).get("state", {}) or {}).get("ssid")
    if not ssid:
        die("no SSID — set the network up on the IOT screen, or pass --ssid")
    psk = a.psk or os.environ.get("ZERO2W_PSK")
    if psk == "-":
        # Read it from stdin rather than the command line: argv is visible to
        # anyone who can run ps.
        psk = sys.stdin.readline().strip()
    if not psk and not a.dry_run:
        psk = getpass.getpass("    passphrase for %s: " % ssid)
    host = a.host or default_host(status)
    say("    device %s joins %r and calls back on %s" % (a.device, ssid, host))

    if a.config_only:
        say("3. skipping firmware — rewriting the files only")
    say("3. fetching firmware") if not a.config_only else None
    wants_camera = bool((configured or {}).get("camera")) and not a.stock
    url, image = None, None
    if a.config_only:
        pass
    elif wants_camera and not a.firmware:
        # Chosen rather than warned about: a warning in a log nobody reads is how
        # a board comes back without its camera.
        say("    this config has a camera, so a camera-capable build is needed")
        image = None if a.dry_run else camera_firmware(board)
        url = image or "(a camera build)"
    elif a.firmware and not a.firmware.startswith(("http://", "https://")):
        url = a.firmware
        image = a.firmware
        if not os.path.isfile(image):
            die("no such firmware file: %s" % image)
        say("    " + image)
    else:
        variant = a.variant or psram_variant(board, (configured or {}).get("psram"))
        if variant and not a.variant:
            say("    this config has %s PSRAM, so the %s build"
                % ((configured or {}).get("psram"), variant))
        url = a.firmware or firmware_url(BUILDS[board], variant)
        say("    " + url)
        image = None if a.dry_run else fetch(url)

    # What the board will run before it has ever spoken to the host.
    payload = []
    if a.flow:
        from zero2w_console import fleet
        flows = console("GET", "/api/flows").get("flows", [])
        flow = [f for f in flows if f.get("id") == a.flow]
        if not flow:
            die("no flow with id %r" % a.flow)
        flow = flow[0]
        bad = [n["type"] for n in flow.get("nodes", [])
               if n["type"] not in fleet.NODE_MODULES and
               not _device_can_run(n["type"])]
        if bad:
            die("this board cannot run: %s" % ", ".join(sorted(set(bad))))
        for name in fleet.Fleet(None, None, None).agent_files(flow, configured):
            if name == "agent.py":
                continue            # uploaded below in every case
            with open(os.path.join(ROOT, "zero2w_console", "agent", name), "rb") as fh:
                payload.append((name, fh.read()))
        payload.append(("flow.json", json.dumps(flow).encode()))
        say("    plus flow %r and %d file(s) it needs"
            % (flow.get("name") or a.flow, len(payload) - 1))

    config = {"host": host, "ssid": ssid, "psk": psk or "", "device": a.device,
              "enroll": prov.get("enroll_token"), "token": None,
              "camera": bool((configured or {}).get("camera"))}
    # Applied before any sync, so it has to travel in the config rather than
    # the manifest. Absent means "leave the firmware default alone".
    if (configured or {}).get("txpower") is not None:
        config["txpower"] = (configured or {}).get("txpower")

    if a.dry_run:
        say("\ndry run — nothing was written. It would have:")
        say("  · %s erase_flash" % (
            "skipped — the ROM loader cannot" if info["stubless"]
            else "skipped" if a.no_erase else "run"))
        say("  · written %s at %s" % (os.path.basename(url or "(none)"), OFFSETS[board]))
        say("  · copied main.py, agent.py and config.json over the REPL")
        say("  · config: " + json.dumps(dict(config, psk="********", enroll="…")))
        return

    # esptool times its own stub against the crystal it believes is fitted, so a
    # misread is not cosmetic: a write at 460800 under the wrong assumption can
    # land corrupt. This fleet has seen it. It is nearly always a marginal cable
    # or a supply sagging during the read, so unplug and try again.
    if info.get("crystal_warning") and not a.config_only and not a.any_crystal:
        say("")
        say("    " + info["crystal_warning"])
        die("esptool misread the crystal (it says %s), and it times the write "
            "against that — so this would risk writing a corrupt image.\n"
            "    Unplug the board, plug it back in, and run this again; check "
            "the line reads 'Crystal is 40MHz' before it writes.\n"
            "    A different cable or USB port fixes it more often than not. If "
            "this board really does have a %s crystal, pass --any-crystal."
            % (info.get("crystal") or "something odd", info.get("crystal") or "26MHz"))

    if a.config_only:
        say("4. leaving the firmware alone")
    else:
        say("4. writing MicroPython (this is the slow part)")
    if a.config_only:
        pass
    elif not a.no_erase and not info["stubless"]:
        code, _ = esptool(["erase_flash"], a.port, info["stubless"])
        if code != 0:
            die("erase failed")
    elif info["stubless"]:
        say("    skipping erase: the ROM loader cannot do it without a stub")
    if not a.config_only:
        code, _ = esptool(["--baud", "460800", "write_flash", "-z", OFFSETS[board], image],
                          a.port, info["stubless"])
        if code != 0:
            die("write_flash failed")

    say("5. handing it its identity")
    time.sleep(2)
    serial = Serial(a.port)
    try:
        serial.reset_into_run()
        time.sleep(1.5)
        repl = RawREPL(serial)
        if not repl.enter():
            die("the board did not reach the REPL — try unplugging it and running step 5 again")
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py"), "rb") as fh:
            agent_src = fh.read()
        files = [("config.json", json.dumps(config).encode()),
                 ("agent.py", agent_src)]
        files += payload
        files.append(("main.py", BOOTSTRAP.encode()))   # last: it starts everything
        for name, blob in files:
            sha = repl.put(name, blob)
            say("    wrote %-12s %5d bytes  sha %s" % (name, len(blob), sha))
        repl.exit()
        # A hardware reset. Ctrl-D in the raw REPL runs the buffer rather than
        # resetting, and left a board at the prompt instead of in main.py.
        serial.reset_into_run()
    finally:
        serial.close()

    # Re-open the window now the board is actually about to ask. provision()
    # opened one back at step 2, five minutes long, and the erase, the firmware
    # write and the boot can easily outlast it — after which the board joins, asks
    # to enroll, and is refused, which looks like the flash failing. The IOT
    # screen's flasher does the same thing (fleet.py, AFTER_FLASH).
    if not a.dry_run:
        try:
            from zero2w_console import fleet as fleetmod
            seconds = fleetmod.AFTER_FLASH
        except Exception:
            seconds = 600
        try:
            console("POST", "/api/iot/enrollment/open", {"seconds": seconds})
            say("    enrollment is open for %d minutes" % (seconds // 60))
        except Exception as exc:
            say("    could not re-open enrollment (%s) — if the board is "
                "refused, open it on the IOT screen" % exc)

    say("\ndone. It should join %r and appear on the IOT screen within a few seconds." % ssid)
    say("If it does not, watch it boot with:  screen %s 115200" % a.port)


def code_only(a, configured):
    """The agent, the flow and its modules over USB, and nothing else — the way
    in for a board that cannot fetch its files over the air."""
    from zero2w_console import fleet as fleetmod
    if not configured:
        die("--code-only needs --device naming a config")
    flow_id = a.flow or configured.get("flow")
    flow = None
    if flow_id:
        flows = console("GET", "/api/flows").get("flows", [])
        flow = ([f for f in flows if f.get("id") == flow_id] or [None])[0]
        if flow is None:
            die("no flow with id %r" % flow_id)
    f = fleetmod.Fleet(None, None, None)
    with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
        version = re.search(r'VERSION = "([^"]+)"', fh.read()).group(1)
    compiled = f.compiled_for(configured, agent_version=version)
    names = f.agent_files(flow, configured, compiled)
    files = [(name, f.module_source(name)) for name in names]
    have = {name: fleetmod.sha(body) for name, body in files}
    if flow:
        files.append(("flow.json", json.dumps(flow).encode()))
    files.append(("modules.json", json.dumps(have).encode()))
    stale = [n[:-4] + ".py" for n in names if n.endswith(".mpy")]
    say("2. writing agent %s%s, %s" % (
        version, " and compiled modules" if compiled else "",
        ("flow %r" % (flow.get("name") or flow_id)) if flow else "no flow"))
    if a.dry_run:
        for name, body in files:
            say("    would write %-24s %6d bytes" % (name, len(body)))
        for name in stale:
            say("    would remove %s" % name)
        return
    serial = Serial(a.port, timeout=0.1)
    try:
        repl = RawREPL(serial)
        if not repl.enter_through_boot(seconds=8):
            die("the board did not reach the REPL — unplug it and try again")
        repl.run("import os\nfor p in %r:\n    try:\n        os.remove(p)\n"
                 "    except OSError:\n        pass\ntry:\n    os.mkdir('modules')\n"
                 "except OSError:\n    pass" % (stale,))
        for name, body in files:
            sha = repl.put(name, body)
            say("    wrote %-24s %6d bytes  sha %s" % (name, len(body), sha))
        for name in stale:
            say("    removed %s" % name)
        repl.exit()
        serial.reset_into_run()                # into main.py; see step 5 above
    finally:
        serial.close()
    say("\ndone. It restarts into agent %s now." % version)


def _device_can_run(ntype):
    from zero2w_console import flows
    return flows.runs_on_device(ntype)


def default_host(status):
    """The address the device should call back on — the AP's own, if it is up."""
    addrs = ((status.get("ap") or {}).get("state") or {}).get("addresses") or []
    for addr in addrs:
        return "http://%s:8787" % addr.split("/")[0]
    return "http://10.42.0.1:8787"


if __name__ == "__main__":
    main()
