#!/usr/bin/env python3
"""Bring the network up, flash a board onto it, deploy a flow, and watch."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zero2w_console import iot, paths  # noqa: E402

CONSOLE = os.environ.get("ZERO2W_URL", "http://127.0.0.1:8787")
TOKEN_FILE = paths.token_file()

OK, BAD, DOT = "✓", "✗", "·"


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def step(n, title):
    say("\n%d. %s" % (n, title))


def good(msg):
    say("   %s %s" % (OK, msg))


def die(msg, detail=None):
    say("   %s %s" % (BAD, msg))
    if detail:
        for line in str(detail).splitlines()[:12]:
            say("     " + line)
    raise SystemExit(1)


def console(method, path, body=None, timeout=30):
    token = os.environ.get("ZERO2W_TOKEN")
    if not token:
        with open(TOKEN_FILE) as fh:
            token = fh.read().strip()
    req = urllib.request.Request(CONSOLE + path, method=method)
    req.add_header("X-Console-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as fh:
            return json.loads(fh.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit("   %s console said %d: %s"
                         % (BAD, exc.code, exc.read().decode(errors="replace")[:200]))
    except urllib.error.URLError as exc:
        raise SystemExit("   %s cannot reach the console (%s)" % (BAD, exc.reason))


def main():
    ap = argparse.ArgumentParser(description="the whole chain, once")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--device", required=True)
    ap.add_argument("--flow", required=True)
    ap.add_argument("--ssid", help="override the network to join")
    ap.add_argument("--psk", help="override the passphrase (not recommended: argv is public)")
    ap.add_argument("--full-flash", action="store_true",
                    help="write the firmware too, not just the files")
    ap.add_argument("--watch", type=int, default=90, help="seconds to watch for")
    ap.add_argument("--keep-network", action="store_true",
                    help="leave the network up if a later step fails")
    a = ap.parse_args()

    # ---------------------------------------------------------------- network
    step(1, "bringing the IoT network up")
    try:
        up = iot.helper("up", timeout=90)
    except iot.NetError as exc:
        die("the network would not start", exc)
    backend = up.get("backend") or "?"
    good("%s on %s, gateway %s" % (backend, up.get("interface"), up.get("gateway")))

    status = iot.helper("status", timeout=20)
    gen = status.get("generated") or {}
    ssid = a.ssid or gen.get("ssid") or (status.get("config") or {}).get("ssid")
    psk = a.psk or gen.get("psk")
    if not ssid:
        die("the network is up but has no name to give the board")
    if not psk:
        die("no passphrase to give the board — with the p2p backend it is "
            "generated, so this means wpa_supplicant did not report one")
    good("network %r%s" % (ssid, " (it named itself)" if gen.get("ssid") else ""))
    host = "http://%s:8787" % up.get("gateway")

    failed = None
    try:
        # ------------------------------------------------------------- flash
        step(2, "flashing the board")
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "iot-flash.py"),
               "--port", a.port, "--device", a.device, "--flow", a.flow,
               "--ssid", ssid, "--psk", "-", "--host", host]
        if not a.full_flash:
            cmd.append("--config-only")
        proc = subprocess.run(cmd, input=psk + "\n", text=True,
                              capture_output=True, timeout=1200,
                              env=dict(os.environ, ZERO2W_URL=CONSOLE))
        for line in proc.stdout.splitlines():
            if line.strip() and not line.startswith("      "):
                say("   " + line.strip())
        if proc.returncode != 0:
            die("the flasher stopped", proc.stdout[-600:] + proc.stderr[-400:])
        good("files on the board")

        # ------------------------------------------------------------- enrol
        step(3, "waiting for it to join and enrol")
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            device = [d for d in console("GET", "/api/iot/devices")["devices"]
                      if d["id"] == a.device]
            device = device[0] if device else {}
            if device.get("enrolled"):
                good("enrolled at %s, address %s"
                     % (time.strftime("%H:%M:%S", time.localtime(device["enrolled"])),
                        device.get("ip") or "?"))
                break
            now = "%s waiting %ds" % (DOT, int(deadline - time.time()))
            if now != last:
                say("   " + now)
                last = now
            time.sleep(5)
        else:
            die("it never enrolled. Watch it boot with:  screen %s 115200" % a.port)

        # The window opens itself when the board is provisioned; it expires on
        # its own, but an open door nobody is walking through should be shut.
        console("POST", "/api/iot/enrollment/close", {})
        good("enrolment closed again")

        # ------------------------------------------------------------ deploy
        step(4, "deploying the flow")
        console("POST", "/api/iot/devices/%s/deploy" % a.device, {"flow": a.flow})
        good("sent; the device pulls it on its next poll")

        # ------------------------------------------------------------- watch
        step(5, "watching for %ds" % a.watch)
        seen_pins, frames, events = {}, 0, 0
        deadline = time.time() + a.watch
        while time.time() < deadline:
            view = console("GET", "/api/iot/devices/%s/pins" % a.device)
            dev = view["device"]
            live = {p["gpio"]: p["live"] for p in view["pins"] if p["live"]}
            for gpio, state in sorted(live.items()):
                key = (gpio, state.get("dir"), state.get("value"))
                if seen_pins.get(gpio) != key:
                    seen_pins[gpio] = key
                    say("   GPIO%-2d %-3s %s" % (gpio, state.get("dir"), state.get("value")))
            cam = (dev or {}).get("camera") or {}
            if cam.get("frames", 0) > frames:
                frames = cam["frames"]
                say("   camera %s %dx%d %s %d frames"
                    % (cam.get("sensor"), cam.get("width", 0), cam.get("height", 0),
                       DOT, frames))
            time.sleep(3)
        if seen_pins or frames:
            good("live: %d pin(s) reporting%s"
                 % (len(seen_pins), ", %d camera frames" % frames if frames else ""))
        else:
            die("nothing reported in %ds — it joined but is not sending state" % a.watch)
    except SystemExit:
        failed = True
        raise
    finally:
        if failed and not a.keep_network:
            say("\n   leaving the network up so you can look at it "
                "(iot-netctl down when finished)")
        say("\ndone.")


if __name__ == "__main__":
    main()
