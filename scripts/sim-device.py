#!/usr/bin/env python3
"""A device that never existed, speaking the real protocol to a running
console."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zero2w_console import paths  # noqa: E402

BASE = os.environ.get("ZERO2W_URL", "http://127.0.0.1:8787")
CONSOLE = os.environ.get("ZERO2W_TOKEN") or open(paths.token_file()).read().strip()


def call(method, path, body=None, token=None, device_token=None, timeout=40):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("X-Console-Token", token)
    if device_token:
        req.add_header("X-Device-Token", device_token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as fh:
            raw = fh.read()
            ctype = fh.headers.get("Content-Type", "")
            if "json" in ctype:
                return fh.status, json.loads(raw or b"{}")
            return fh.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, raw


def step(label, ok, detail=""):
    print("%-3s %-44s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        sys.exit(1)


# 1. a config, as if the scan had just found this board
SUFFIX = os.environ.get("SIM_SUFFIX") or str(int(time.time()))[-4:]
code, dev = call("POST", "/api/iot/devices", {
    "name": "Sim CAM " + SUFFIX, "board": "esp32cam", "chip": "ESP32-D0WD-V3",
    "mac": "70:4b:ca:00:%s:%s" % (SUFFIX[:2], SUFFIX[2:]), "flash": "4MB"}, token=CONSOLE)
step("config created from a scan result", code == 200 and dev.get("id"), dev.get("id", dev))
DEV = dev["id"]

# 2. a flow for that board, and deploy it
code, _ = call("POST", "/api/flows", {"flows": [{
    "id": "f_sim", "name": "Blink the LED", "enabled": True,
    "board": "esp32cam", "device": DEV,
    "nodes": [
        {"id": "t1", "type": "timer.interval", "x": 80, "y": 80, "config": {"every": 1000}},
        {"id": "a1", "type": "gpio.out", "x": 360, "y": 80,
         "config": {"gpio": 33, "action": "toggle"}},
        {"id": "l1", "type": "log.write", "x": 640, "y": 80,
         "config": {"level": "ok", "message": "blinked {{payload}}"}}],
    "edges": [{"id": "e1", "from": "t1", "fromPort": "out", "to": "a1", "toPort": "in"},
              {"id": "e2", "from": "a1", "fromPort": "out", "to": "l1", "toPort": "in"}]}]},
    token=CONSOLE)
step("flow written for the board", code == 200)

# 3. provision: a one-time token, as the flasher would ask for
code, prov = call("POST", "/api/iot/devices/%s/provision" % DEV, {}, token=CONSOLE)
step("provisioned", code == 200 and prov.get("enroll_token"), "enroll token issued")

# 4. the device enrols with it
code, out = call("POST", "/api/iot/enroll", {
    "device": DEV, "enroll": prov["enroll_token"],
    "probe": {"chip": "ESP32", "firmware": "micropython 1.24.0", "free_ram": 108000,
              "unique_id": "704bca0000b8"}})
step("device enrolled", code == 200 and out.get("token"), "got its own token")
TOKEN = out["token"]

# 5. the one-time token is spent
code, again = call("POST", "/api/iot/enroll", {
    "device": DEV, "enroll": prov["enroll_token"], "probe": {}})
step("the enrol token cannot be reused", code == 400, again.get("error"))

# 6. the console token must NOT work as a device token
code, _ = call("GET", "/api/iot/manifest", device_token=CONSOLE)
step("console token is not a device token", code == 401)

# 7. linking a flow to a device is not the same as deploying it
code, man = call("GET", "/api/iot/manifest", device_token=TOKEN)
step("linking alone deploys nothing", code == 200 and man.get("flow") is None,
     "manifest carries no flow until a deploy")

# 8. deploy, then the manifest carries it
code, out = call("POST", "/api/iot/devices/%s/deploy" % DEV, {"flow": "f_sim"}, token=CONSOLE)
step("deployed", code == 200, out)
code, man = call("GET", "/api/iot/manifest", device_token=TOKEN)
step("manifest", code == 200 and man.get("flow"),
     "%d modules, flow %r" % (len(man["modules"]), man["flow"]["name"]))
names = [m["name"] for m in man["modules"]]
step("only what this flow needs", "modules/i2c.py" not in names and "agent.py" in names,
     ", ".join(names))
step("pins are the board's usable ones", len(man["pins"]) == 9, "%d pins" % len(man["pins"]))

# 8. fetch each module and check the sha the manifest promised
import hashlib
for mod in man["modules"]:
    code, raw = call("GET", "/api/iot/module/" + mod["name"], device_token=TOKEN)
    got = hashlib.sha256(raw).hexdigest()[:16]
    step("fetched %s" % mod["name"], code == 200 and got == mod["sha"],
         "%d bytes, sha ok" % len(raw))

# 9. no escaping the agent directory
code, _ = call("GET", "/api/iot/module/../../server.py", device_token=TOKEN)
step("cannot climb out of the agent directory", code in (400, 404))

# 10. the deploy queued a command, waiting on the poll
start = time.time()
code, cmds = call("GET", "/api/iot/commands?wait=5", device_token=TOKEN)
step("command arrived on the poll", code == 200 and cmds["commands"],
     "%s after %.1fs" % (cmds["commands"], time.time() - start))

# 11. an empty poll returns after its wait, not immediately
start = time.time()
code, cmds = call("GET", "/api/iot/commands?wait=3", device_token=TOKEN)
took = time.time() - start
step("an idle poll parks", code == 200 and cmds["commands"] == [] and 2.5 < took < 8,
     "%.1fs" % took)

# 12. an event from the device
code, out = call("POST", "/api/iot/event", {
    "kind": "flow", "level": "ok", "message": "blinked 1", "node": "l1",
    "ip": "10.42.0.64", "rssi": -58}, device_token=TOKEN)
step("event accepted", code == 200)
code, devs = call("GET", "/api/iot/devices", token=CONSOLE)
mine = [d for d in devs["devices"] if d["id"] == DEV][0]
step("device state updated by its own report", mine["ip"] == "10.42.0.64" and mine["rssi"] == -58,
     "ip %s rssi %s" % (mine["ip"], mine["rssi"]))
step("device token never leaves the host", "token" not in mine)

# 13. a flow the device cannot run is refused
code, _ = call("POST", "/api/flows", {"flows": [{
    "id": "f_bad", "name": "Has a shell node", "enabled": True, "board": "esp32cam",
    "nodes": [{"id": "s1", "type": "shell.run", "x": 0, "y": 0, "config": {"command": "id"}}],
    "edges": []}, {"id": "f_sim", "name": "Blink the LED", "enabled": True,
                   "board": "esp32cam", "device": DEV, "nodes": [], "edges": []}]},
    token=CONSOLE)
code, out = call("POST", "/api/iot/devices/%s/deploy" % DEV, {"flow": "f_bad"}, token=CONSOLE)
step("a flow with a host-only node is refused", code == 400, out.get("error"))

print("\nall good — the host side works against a device that does not exist")
