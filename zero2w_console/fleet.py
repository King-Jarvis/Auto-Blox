"""The two-way link between this host and a field device."""
import base64
import hashlib
import json
import os
import secrets
import struct
import subprocess
import sys
import threading
import time

from . import flows as flowmod
from . import iot as iotmod
from . import pixels
from .agent.modules import link as wire

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HERE, "agent")

# Precompiled bytecode for a device. Importing flow.py on the motor board took
# 77,892 bytes, most of it heap grown to compile and never given back; flow.mpy
# took 17,040. The agent is compiled too once a board can fall back: main.py
# imports agent.mpy, and agent_src.py if that will not load.
MPY_CROSS = os.environ.get("ZERO2W_MPY_CROSS") or os.path.expanduser(
    "~/.cache/zero2w-console/micropython/mpy-cross/build/mpy-cross")
MPY_CACHE = os.path.expanduser("~/.cache/zero2w-console/mpy")
MPY_FIRMWARE = (1, 23)        # mpy v6.3, which this compiler emits
MPY_AGENT = (0, 8, 0)         # the first agent that clears a .py shadowing a .mpy
MPY_AGENT_ITSELF = (0, 9, 0)  # the first that keeps agent_src.py to fall back on


def version_of(text):
    """(1, 27, 0) out of "micropython 1.27.0." or "0.8.0"; () for nothing."""
    digits, out = "", []
    for ch in str(text or "") + " ":
        if ch.isdigit():
            digits += ch
        elif digits:
            out.append(int(digits))
            digits = ""
            if ch != "." or len(out) == 3:
                break
    return tuple(out)


def compiler():
    """The mpy-cross binary, or None when there is none to use."""
    if os.access(MPY_CROSS, os.X_OK):
        return MPY_CROSS
    import shutil
    return shutil.which("mpy-cross")


def compile_mpy(source, name):
    """Bytecode for one agent file, cached by what went in."""
    tool = compiler()
    if not tool:
        raise iotmod.NetError("no mpy-cross to compile %s with" % name)
    key = hashlib.sha256(source + name.encode()).hexdigest()[:24]
    cached = os.path.join(MPY_CACHE, key + ".mpy")
    try:
        with open(cached, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    os.makedirs(MPY_CACHE, exist_ok=True)
    src = os.path.join(MPY_CACHE, key + ".py")
    with open(src, "wb") as fh:
        fh.write(source)
    done = subprocess.run([tool, "-s", name, "-o", cached + ".tmp", src],
                          capture_output=True, text=True, timeout=60)
    os.unlink(src)
    if done.returncode != 0:
        raise iotmod.NetError("mpy-cross refused %s: %s"
                              % (name, (done.stderr or done.stdout).strip()[:300]))
    os.replace(cached + ".tmp", cached)
    with open(cached, "rb") as fh:
        return fh.read()
REPO = os.path.dirname(HERE)

# How long an enrollment window stays open unless it is closed by hand.
ENROLL_WINDOW = 300
# And after a flash, when the board is about to boot and ask. Longer, because
# a cold ESP32 joining a network for the first time is not quick.
AFTER_FLASH = 600
# How long a device may park on the command poll before it gets an empty answer.
POLL_SECONDS = 25

# How long a command sent down a link stays in doubt: inside this a dropped
# connection requeues it, after it the frame was read or the socket would
# already have failed.
INFLIGHT_GRACE = 5

# How many of a device's own messages this end keeps, per device — enough for a
# boot and the minutes after it. Only what a device *said*, not its reports.
HISTORY = 200

# Which commands may be sent twice: everything here is idempotent. `fire` is out
# because a duplicate really is a second message. `reboot` is out because
# succeeding at it drops the link, which is the very signal used to decide a
# command was in doubt — so a reboot that worked gets delivered again.
RETRYABLE = ("reload", "resync", "stop", "set", "tags",
             "camera-on", "camera-off")

# Past this many bytes, deploying over the air is worth avoiding. It is about
# how long a board is unreachable while it fetches: 70KB measured 370 seconds of
# silence, which is indistinguishable from a board that has died.
OVER_THE_AIR_COMFORTABLE = 32 * 1024
# A device is "online" if it has been heard from inside this. One number, shared
# with the {{device.online}} variable, so a flow and the IOT screen cannot
# disagree about whether a board is up.
FRESH = flowmod.DEVICE_FRESH

# Which agent modules a node type needs. Anything not named here needs nothing
# beyond the core the loader already has.
NODE_MODULES = {
    "gpio.in": "gpio", "gpio.out": "gpio",
    "pwm.out": "pwm",
    "i2c.read": "i2c", "i2c.write": "i2c", "i2c.scan": "i2c",
    "http.request": "http",
    # The same evaluator the host imports, so a formula cannot mean one thing on
    # the bench and another in the field.
    "math.expr": "expr",
    # And the same arithmetic behind the drive blocks, for the same reason.
    "math.deadband": "drive", "math.split": "drive", "math.ramp": "drive",
    # The standard blocks, out of flow.py so a device compiles only the ones its
    # flow contains. Must agree with flow.PULLED, and a test checks that it does.
    "logic.edge": "blocks", "logic.count": "blocks",
    "logic.hysteresis": "blocks", "logic.timer": "blocks",
    "logic.latch": "blocks", "safety.watchdog": "blocks",
    "math.scale": "blocks", "math.smooth": "blocks",
    "pad.link": "pad", "pad.axis": "pad", "pad.button": "pad",
    "tag.change": "tagwatch", "logic.step": "steps",
    "ble.link": "ble", "ble.read": "ble", "ble.write": "ble", "ble.notify": "ble",
    "picture.send": "pictures", "sd.save": "pictures",
}

# Modules another module imports, sent with it.
MODULE_NEEDS = {"ble": ("blefmt",),
                # Send to host dials the encrypted stream itself when the
                # camera has none open.
                "pictures": ("link", "linkclient", "media")}

# The picture stream. A lease is how long one look keeps a board sending; a
# viewer waits this long for the first picture before being told there is none.
STREAM_LEASE_MS = 10000
STREAM_WAIT = 4.0
# A MEDIA frame opens with its format, width and height: ">BHH".
MEDIA_HEAD = 5
MEDIA_FORMATS = {1: "jpeg", 2: "grayscale", 3: "rgb565"}


def media_signed(cert_der, utc):
    """What a media offer's signature covers. Formatted by hand, the same on
    the board, so the two cannot disagree about a space."""
    return bytes(cert_der) + ("%d,%d,%d,%d,%d,%d" % tuple(utc)).encode()


def _hex(raw):
    return "".join("%02x" % b for b in raw)


def sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


class Fleet:
    """Everything a device talks to, in one place."""

    def __init__(self, devices, flow_store, bus):
        self.devices = devices          # iot.DeviceStore
        self.flow_store = flow_store    # flows.FlowStore
        self.bus = bus                  # the SSE bus
        self.lock = threading.Lock()
        self.queues = {}                # device id -> [command]
        self.inflight = {}              # device id -> [(sent_at, command)]
        self.history = {}               # device id -> [event], newest last
        self.wakeups = {}               # device id -> threading.Event
        self.window = None              # {"opened": ts, "expires": ts, "reason": str}
        self.refusals = []              # boards that asked to join and were turned away
        self.steps = {}                 # device id -> {node: flow} of active Steps
        self.outputs = {}               # "flow/node" -> what a board's node last sent
        self.awaiting = None            # device id the window was opened for
        self.flashing = None            # the running flash thread, if any
        self.flash_log = []
        self.frames = {}                # device id -> the last good frame
        # The picture stream: the certificate a board pins, when this console
        # has one, and for each board how long its last lease runs.
        self.cert_der = None
        self.leases = {}                # device id -> monotonic time it ends
        self.frame_arrived = threading.Condition()
        # device id -> what controller it has, from its own account of it. Sent
        # as an event rather than in the report, so it costs `agent.py` nothing:
        # the Controller node lives in a pulled module and logs from there.
        self.pads = {}
        self.self_url = "http://127.0.0.1:8787"
        # Late-bound, like self_url: a device's event can fire a host flow.
        self.engine = None
        # Shared memory the whole fleet sees, when one is bound.
        self.tags = None
        # The held-open socket per device, when one is running. Late-bound and
        # allowed to stay None: everything here works over the polled routes too.
        self.link = None

    # -- the enrollment window --------------------------------------------
    def open_window(self, seconds=ENROLL_WINDOW, reason=None, awaiting=None):
        """Open it, and remember why."""
        with self.lock:
            self.window = {"opened": iotmod.now(),
                           "expires": iotmod.now() + int(seconds),
                           "reason": reason or "opened by hand"}
            self.awaiting = awaiting
            return dict(self.window)

    def close_window(self):
        with self.lock:
            self.window = None
            self.awaiting = None
        return {"ok": True, "enrollment": None}

    def window_state(self):
        with self.lock:
            if self.window and self.window["expires"] <= iotmod.now():
                self.window = None
                self.awaiting = None
            return dict(self.window) if self.window else None

    def _refuse(self, device_id, reason, ip=None):
        """A board asked to join and was turned away."""
        with self.lock:
            self.refusals.append({"at": iotmod.now(), "device": device_id,
                                  "reason": reason, "ip": ip})
            if len(self.refusals) > 20:
                del self.refusals[:-20]

    # -- provisioning ------------------------------------------------------
    def provision(self, device_id):
        """A one-time token for a board about to be flashed."""
        doc = self.devices.load()
        for d in doc["devices"]:
            if d["id"] != device_id:
                continue
            d["enroll_token"] = secrets.token_hex(16)
            d["token"] = None          # re-flashing invalidates the old one
            d["enrolled"] = None
            self.devices.save(doc)
            self.open_window(reason="flashing %s" % d["name"], awaiting=d["id"])
            return {"device": d["id"], "name": d["name"], "board": d.get("board"),
                    "enroll_token": d["enroll_token"]}
        raise iotmod.NetError("no such device")

    # -- what a device does on first boot ----------------------------------
    def enroll(self, device_id, enroll_token, probe=None, ip=None):
        window = self.window_state()
        if not window:
            self._refuse(device_id, "the window had closed", ip)
            raise iotmod.NetError("enrollment is closed")
        if not device_id or not enroll_token:
            self._refuse(device_id, "no device or token was sent", ip)
            raise iotmod.NetError("device and enroll token are required")
        doc = self.devices.load()
        for d in doc["devices"]:
            if d["id"] != device_id:
                continue
            if not d.get("enroll_token"):
                self._refuse(device_id, "no enrollment was pending for it", ip)
                raise iotmod.NetError("this device has no enrollment pending")
            if not secrets.compare_digest(str(d["enroll_token"]), str(enroll_token)):
                self._refuse(device_id, "the token did not match", ip)
                raise iotmod.NetError("enrollment token does not match")
            d["token"] = secrets.token_urlsafe(24)
            d["enroll_token"] = None          # one time, and it is spent
            d["enrolled"] = iotmod.now()
            d["last_seen"] = iotmod.now()
            if isinstance(probe, dict):
                d["probe"] = probe
                d["firmware"] = probe.get("firmware")
                # What the board says about itself, against what the scan
                # claimed. A mismatch is shown, never silently adopted.
                claimed, found = d.get("chip"), probe.get("chip")
                d["mismatch"] = None
                if claimed and found and found.lower() not in claimed.lower() \
                        and claimed.lower() not in found.lower():
                    d["mismatch"] = "configured as %s, reports %s" % (claimed, found)
            self.devices.save(doc)
            self._emit(d, "ok", "enrolled")
            # The window was opened for this board and it has joined, so there
            # is no reason to leave the door open.
            with self.lock:
                satisfied = self.awaiting == d["id"]
            if satisfied:
                self.close_window()
            return {"id": d["id"], "token": d["token"], "name": d["name"]}
        self._refuse(device_id, "no config with that id", ip)
        raise iotmod.NetError("no such device")

    # -- authentication ----------------------------------------------------
    def authenticate(self, token):
        if not token:
            return None
        for d in self.devices.load()["devices"]:
            if d.get("token") and secrets.compare_digest(str(d["token"]), str(token)):
                return d
        return None

    def rehome_warnings(self, device):
        """What will not carry over to a config's new board, in words."""
        flow = self.flow_for(device)
        if not flow or not flow.get("board") or flow.get("board") == device.get("board"):
            return []
        out = ["%s is written for %s, and this board is %s: change the flow's "
               "board before deploying it" % (flow.get("name") or flow["id"],
                                              flow.get("board"), device.get("board"))]
        profile = iotmod.board_profile(device.get("board")) or {}
        have = set(p.get("gpio") for p in profile.get("pins") or [])
        used = sorted(set(int(n["config"]["gpio"]) for n in flow.get("nodes", [])
                          if str((n.get("config") or {}).get("gpio", "")).isdigit()))
        missing = [g for g in used if have and g not in have]
        if missing:
            out.append("it uses GPIO %s, which %s does not have" % (
                ", ".join(str(g) for g in missing), device.get("board")))
        return out

    def targets(self, value):
        """A device field as device ids: one, or every member of a group."""
        return iotmod.targets(self.devices.load(), value)

    def active_steps(self):
        """Every Step a board says is active, keyed "flow/node"."""
        with self.lock:
            return ["%s/%s" % (f, n) for held in self.steps.values()
                    for n, f in held.items()]

    def booted(self, device):
        """A board's first sync since power-on: reset what must not survive it."""
        with self.lock:
            self.steps.pop(device["id"], None)      # a boot leaves every step idle
        if not self.tags:
            return []
        names = self.tags.reset_for_boot(source="boot:" + device["id"])
        if names:
            self._emit(device, "warn", "booted, so %s went back to %s"
                       % (", ".join(names), "its initial value" if len(names) == 1
                          else "their initial values"))
        return names

    def touch(self, device_id, **fields):
        doc = self.devices.load()
        for d in doc["devices"]:
            if d["id"] == device_id:
                d["last_seen"] = iotmod.now()
                for k, v in fields.items():
                    if v is not None:
                        d[k] = v
                self.devices.save(doc)
                return d
        return None

    # -- what the device should be running ---------------------------------
    def flow_for(self, device):
        """The flow deployed to this device, or None."""
        want = device.get("flow")
        if not want:
            return None
        for f in self.flow_store.load().get("flows", []):
            if f.get("id") == want:
                return f
        return None

    def compiled_for(self, device, agent_version=None):
        """How much of this device's code is sent compiled: 0 none, 1 all but
        the agent, 2 the agent too. Needs a compiler here, firmware that
        loads mpy v6.3, and an agent that knows how to switch over."""
        if not compiler():
            return 0
        probe = (device or {}).get("probe") or {}
        agent = version_of(agent_version or ((device or {}).get("state") or {})
                           .get("agent") or probe.get("agent"))
        if version_of(probe.get("firmware")) < MPY_FIRMWARE or agent < MPY_AGENT:
            return 0
        return 2 if agent >= MPY_AGENT_ITSELF else 1

    def agent_files(self, flow, device=None, compiled=0):
        """The core, plus only what this device actually needs.

        `compiled` 1 names everything but the agent .mpy; 2 sends the agent as
        agent.mpy too, after main.py and agent_src.py so a board is never left
        without its source to fall back on.
        """
        names = self._agent_files(flow, device)
        if compiled:
            names = [n if n == "agent.py" else n[:-3] + ".mpy" for n in names]
        if compiled and compiled >= 2:
            names = ["main.py", "agent_src.py", "agent.mpy"] + names[1:]
        return names

    def _agent_files(self, flow, device=None):
        names = ["agent.py", "flow.py"]
        for node in (flow or {}).get("nodes", []):
            mod = NODE_MODULES.get(node.get("type"))
            for want in ((mod,) + MODULE_NEEDS.get(mod, ())) if mod else ():
                if ("modules/%s.py" % want) not in names:
                    names.append("modules/%s.py" % want)
        # A camera is pulled when the flow asks for one, or when the config
        # says this board has one and nothing has been deployed yet.
        wants_camera = bool(flowmod.camera_nodes(flow)) or (device or {}).get("camera")
        if wants_camera and "modules/camera.py" not in names:
            names.append("modules/camera.py")
        # Its pictures go over an encrypted stream when this console can take
        # one: the stream is a second link connection, over TLS.
        if wants_camera and self.streams_pictures():
            for name in ("modules/link.py", "modules/linkclient.py", "modules/media.py"):
                if name not in names:
                    names.append(name)
        # Same rule as the camera, and for the same reason: a property of the
        # board rather than of any node. Keyed on the device's own configuration,
        # not on whether a listener happens to be running here.
        if (device or {}).get("link"):
            for name in ("modules/link.py", "modules/linkclient.py"):
                if name not in names:
                    names.append(name)
            # The policy around the client — when to dial, which route a report
            # takes, how long to believe in a silent host. Separate from agent.py
            # so a board that never links pays no RAM for code it will not run.
            names.append("modules/linkagent.py")
        return names

    def module_source(self, name):
        """One agent file, by its published name: source for .py, compiled
        from that source for .mpy. Never escapes the directory."""
        if name and name.endswith(".mpy"):
            return compile_mpy(self.module_source(name[:-4] + ".py"), name[:-4] + ".py")
        if name == "agent_src.py":                  # the agent's own fallback
            return self.module_source("agent.py")
        if not name or name.startswith("/") or ".." in name:
            raise iotmod.NetError("bad module name")
        path = os.path.join(AGENT_DIR, name)
        if not os.path.realpath(path).startswith(os.path.realpath(AGENT_DIR)) \
                or not os.path.isfile(path):
            raise iotmod.NetError("no such module")
        with open(path, "rb") as fh:
            return fh.read()

    def manifest(self, device, agent_version=None):
        flow = self.flow_for(device)
        mods = []
        compiled = self.compiled_for(device, agent_version)
        for name in self.agent_files(flow, device, compiled):
            try:
                body = self.module_source(name)
            except iotmod.NetError:
                continue
            mods.append({"name": name, "sha": sha(body), "bytes": len(body)})
        return {
            "device": device["id"],
            "name": device.get("name"),
            "board": device.get("board"),
            "modules": mods,
            "flow": flow,
            "flow_sha": sha(json.dumps(flow, sort_keys=True).encode()) if flow else None,
            # The shared tags as they stand, so a device that has just booted
            # starts from the same numbers as everything else.
            "tags": self.tags.shared() if self.tags else {},
            "camera": bool(device.get("camera") or flowmod.camera_nodes(flow)),
            # Not the board's pin profile: no agent has ever read it, it is
            # editor data served by /api/iot/boards, and it was 42% of the motor
            # board's 11,627-byte manifest. A device is sent what it reads.
            #
            # Where to dial, or None for "keep polling" — per device, and off
            # until turned on, because the three link modules are ~21KB of source and
            # an import on MicroPython costs memory nothing gives back. A console
            # with no listener says so rather than leave a board retrying a shut port.
            "link": ({"port": self.link.port}
                     if self.link and device.get("link") else None),
        }

    def note_modules(self, device_id, held):
        """Remember which modules a board has, and at which sha."""
        if not held:
            return
        doc = self.devices.load()
        for d in doc["devices"]:
            if d["id"] != device_id:
                continue
            have = dict(d.get("modules") or {})
            have.update(held)
            if have != d.get("modules"):
                d["modules"] = have
                self.devices.save(doc)
            return

    def deploy_cost(self, flow, device=None):
        """What this device would have to *fetch* over the air to run
        `flow`."""
        held = (device or {}).get("modules") or {}
        files = []
        total = new = 0
        for name in self.agent_files(flow, device, self.compiled_for(device)):
            try:
                body = self.module_source(name)
            except iotmod.NetError:
                continue
            digest = sha(body)
            fresh = held.get(name) != digest
            files.append({"name": name, "bytes": len(body), "needed": fresh})
            total += len(body)
            if fresh:
                new += len(body)
        if flow:
            # The flow document is always written: the one thing a deploy carries.
            size = len(json.dumps(flow, sort_keys=True).encode())
            files.append({"name": "flow.json", "bytes": size, "needed": True})
            total += size
            new += size
        return {"bytes": new, "total": total, "files": files,
                "wire": new > OVER_THE_AIR_COMFORTABLE,
                "limit": OVER_THE_AIR_COMFORTABLE}

    def reconcile(self, before, after):
        """A flow switched off has to stop on the board running it."""
        after_by_id = {f.get("id"): f for f in (after or {}).get("flows", [])}
        moved = set()
        for fid, flow in after_by_id.items():
            was = (before or {}).get(fid)
            if was is None:
                continue
            if bool(was.get("enabled", True)) != bool(flow.get("enabled", True)):
                moved.add(fid)
        if not moved:
            return []
        told = []
        for d in self.devices.load()["devices"]:
            if d.get("flow") in moved:
                self.push(d["id"], {"op": "reload", "flow": d["flow"]})
                told.append(d["id"])
        return told

    # -- the link ----------------------------------------------------------
    def device_token(self, device_id):
        """What `LinkServer` asks before it will derive a session key."""
        device = iotmod.get_device(self.devices, device_id)
        if not device or not device.get("link"):
            return None
        return device.get("token") or None

    def streams_pictures(self):
        return bool(self.cert_der and self.link and getattr(self.link, "tls", None))

    def stream_token(self, device_id):
        """What `LinkServer` asks before it takes a picture stream: a board
        with a camera, link or no link."""
        device = iotmod.get_device(self.devices, device_id)
        if not device or not (device.get("camera")
                              or flowmod.camera_nodes(self.flow_for(device))):
            return None
        return device.get("token") or None

    def media_offer(self, device):
        """Where and how to stream, signed with the board's own token.

        Served over plain HTTP, so the signature is what lets a board trust
        the certificate in it: only this console and the board hold the
        token. The clock rides along because TLS checks dates, and a board's
        clock starts at 2000-01-01."""
        if not (self.cert_der and self.link and self.link.tls and device.get("token")):
            return None
        utc = list(time.gmtime()[:6])
        return {"port": self.link.port, "name": "zero2w-console",
                "cert": base64.b64encode(self.cert_der).decode(), "utc": utc,
                "sig": _hex(wire.hmac_sha256(device["token"].encode(),
                                             media_signed(self.cert_der, utc)))}

    def on_link_open(self, conn):
        if getattr(conn, "role", "link") == "stream":
            device = iotmod.get_device(self.devices, conn.device_id)
            self.leases.pop(conn.device_id, None)       # a new stream asks afresh
            if device:
                self._emit(device, "ok", "picture stream open, encrypted")
            return
        device = iotmod.get_device(self.devices, conn.device_id)
        if device:
            self.touch(conn.device_id, ip=conn.address[0])
            self._emit(device, "ok", "on the link from %s" % conn.address[0])

    def on_link_close(self, conn):
        """The link ended."""
        if getattr(conn, "role", "link") == "stream":
            device = iotmod.get_device(self.devices, conn.device_id)
            if device and not getattr(conn, "replaced", False):
                self._emit(device, "idle", "picture stream closed")
            return
        device = iotmod.get_device(self.devices, conn.device_id)
        doubtful = self._recent(conn.device_id)
        again = [c for c in doubtful if c.get("op") in RETRYABLE]
        lost = [c for c in doubtful if c.get("op") not in RETRYABLE]
        for command in again:
            self._enqueue(conn.device_id, command)
        # A board that dialled again is still on the link: its old socket
        # closing after the new one opened is not a departure.
        if device and not getattr(conn, "replaced", False):
            self._emit(device, "idle", "left the link")
            if again:
                self._emit(device, "warn", "requeued %d command(s) the link may "
                           "not have delivered: %s"
                           % (len(again), ", ".join(sorted(
                               {str(c.get("op")) for c in again}))))
            for command in lost:
                self._emit(device, "serious", "%s was sent as the link closed "
                           "and cannot be repeated safely — send it again if it "
                           "mattered" % command.get("op"))

    def on_link_frame(self, conn, kind, body):
        """A verified frame from a device, handed to the handler that owns
        it."""
        device = iotmod.get_device(self.devices, conn.device_id)
        if not device:
            return
        if getattr(conn, "role", "link") == "stream":
            if kind == wire.MEDIA:
                self._streamed(device, body)
            elif kind == wire.STILL:
                self.still(device, body)
            return
        try:
            doc = json.loads(body.decode()) if body else {}
        except Exception:
            return
        if not isinstance(doc, dict):
            return
        try:
            if kind == wire.STATE:
                self.report_state(device, doc)
            elif kind == wire.EVENT:
                self.event(device, doc)
            elif kind == wire.TAGS:
                self.apply_device_tags(device, doc.get("values") or doc)
        except Exception as exc:
            # A handler that raises must not take the connection down with it:
            # the frame verified, so the link is sound even when its cargo was not.
            self._emit(device, "warn", "link frame %d: %s" % (kind, exc))

    def link_state(self):
        return self.link.status() if self.link else None

    # -- commands, host to device ------------------------------------------
    def push(self, device_id, command):
        """Say something to a device: down its link if it holds one, else
        queued."""
        if self.link:
            # Tags get the frame type that exists for them. The device handles
            # both, but a tag push is not a command.
            if command.get("op") == "tags":
                sent = self.link.send(device_id, wire.TAGSET,
                                      {"values": command.get("values") or {}})
            else:
                sent = self.link.send(device_id, wire.COMMAND, command)
            if sent:
                with self.lock:
                    self._forget(device_id)
                    self.inflight.setdefault(device_id, []).append(
                        (iotmod.now(), command))
                return command
        return self._enqueue(device_id, command)

    def _enqueue(self, device_id, command):
        with self.lock:
            q = self.queues.setdefault(device_id, [])
            q.append(command)
            if len(q) > 64:                 # a device that never polls
                del q[:-64]
            ev = self.wakeups.get(device_id)
        if ev:
            ev.set()
        return command

    def _forget(self, device_id):
        """Drop anything past the grace period."""
        held = self.inflight.get(device_id)
        if not held:
            return
        cutoff = iotmod.now() - INFLIGHT_GRACE
        self.inflight[device_id] = [row for row in held if row[0] >= cutoff]

    def _recent(self, device_id):
        """What was sent down a link recently enough to be in doubt."""
        with self.lock:
            self._forget(device_id)
            held = self.inflight.pop(device_id, [])
        return [command for _at, command in held]

    def take_commands(self, device, wait=POLL_SECONDS):
        """Long-poll: hand over what is queued, or park until something is."""
        device_id = device["id"]
        with self.lock:
            queued = self.queues.pop(device_id, [])
            if queued:
                return queued
            ev = self.wakeups.setdefault(device_id, threading.Event())
            ev.clear()
        held = max(0, min(wait, 60))
        if held:
            ev.wait(timeout=held)
        with self.lock:
            return self.queues.pop(device_id, [])

    def deploy(self, device_id, flow_id):
        """Hand a device a flow. It writes it to flash and runs it from boot."""
        doc = self.devices.load()
        device = None
        for d in doc["devices"]:
            if d["id"] == device_id:
                device = d
        if not device:
            raise iotmod.NetError("no such device")
        flow = None
        if flow_id:
            for f in self.flow_store.load().get("flows", []):
                if f.get("id") == flow_id:
                    flow = f
            if not flow:
                raise iotmod.NetError("no such flow")
            if flow.get("board") and device.get("board") and \
                    flow["board"] != device["board"]:
                raise iotmod.NetError("that flow is written for %s, and this device is %s"
                                      % (flow["board"], device["board"]))
            bad = [n["type"] for n in flow.get("nodes", [])
                   if not flowmod.runs_on_device(n["type"])]
            if bad:
                raise iotmod.NetError("this device cannot run: %s" % ", ".join(sorted(set(bad))))
        device["flow"] = flow_id or None
        device["deployed"] = iotmod.now() if flow_id else None
        self.devices.save(doc)
        self.push(device_id, {"op": "reload", "flow": flow_id})
        self._emit(device, "ok", "deployed %s" % (flow["name"] if flow else "nothing"))
        return {"ok": True, "device": device_id, "flow": flow_id}

    # -- what a device says it is doing ------------------------------------
    def broadcast_tag(self, name, value, source=None):
        """Carry a shared tag's new value to the fleet."""
        sent = 0
        for d in self.devices.load()["devices"]:
            if d["id"] == source or not d.get("token"):
                continue
            self.push(d["id"], {"op": "tags", "values": {name: value}})
            sent += 1
        return sent

    def apply_device_tags(self, device, values):
        """Tag writes a device made, coming home."""
        if not self.tags or not isinstance(values, dict):
            return 0
        took = 0
        for name, value in list(values.items())[:32]:
            if not self.tags.is_shared(name):
                continue
            try:
                self.tags.set(name, value, source=device["id"])
                took += 1
            except Exception as exc:
                self._emit(device, "warn", "could not take %s: %s" % (name, exc))
        return took

    def report_state(self, device, body):
        """A device's own account of itself, kept so the editor can show it."""
        keep = {}
        for key in ("ip", "rssi", "uptime", "free_ram", "idf_free", "flow", "flow_name",
                    "agent", "nodes", "camera",
                    # What the device says about the held-open socket, sent over
                    # the route the socket is meant to replace — the only end that
                    # can be trusted to say it did not come up.
                    "link",
                    # Deployed-and-switched-off reports a flow with no nodes, and
                    # so does deployed-and-broken. This tells them apart, so it has
                    # to survive the whitelist.
                    "running"):
            if body.get(key) is not None:
                keep[key] = body[key]
        pins = {}
        for gpio, entry in (body.get("pins") or {}).items():
            try:
                gpio = int(gpio)
            except (TypeError, ValueError):
                continue
            if isinstance(entry, dict):
                pins[str(gpio)] = {"dir": str(entry.get("dir") or "")[:4],
                                   "value": entry.get("value")}
        keep["pins"] = pins
        keep["at"] = iotmod.now()
        self.touch(device["id"], state=keep, ip=body.get("ip"), rssi=body.get("rssi"))
        # Tags this device wrote since it last reported, which then reach the
        # rest of the fleet through the table's own watcher.
        if body.get("tags"):
            self.apply_device_tags(device, body["tags"])

        # Each node the device ran becomes one event on the same bus the local
        # engine uses, so the studio flashes a remote node like a local one.
        fired = body.get("fired") or []
        for node_id in fired[:64]:
            self._emit(device, "idle", "", node=str(node_id)[:64], kind="fired")
        self._note_outputs(device, body.get("outputs"))
        return {"ok": True, "pins": len(pins), "fired": len(fired)}

    def _note_outputs(self, device, outputs):
        """What a board's nodes last sent, for the editor's Output panel.

        Marked `from_report` by this end: a device names its own event kinds,
        so `kind` alone cannot say a row is one of these.
        """
        if not isinstance(outputs, dict) or not device.get("flow"):
            return
        for node_id, row in list(outputs.items())[:64]:
            if not isinstance(row, list) or len(row) != 3:
                continue
            port, payload, meta = row
            out = {"time": time.strftime("%H:%M:%S"), "flow": device["flow"],
                   "node": str(node_id)[:64], "port": str(port)[:16],
                   "kind": "output", "payload": payload,
                   "meta": meta if isinstance(meta, dict) else None,
                   "device": device["id"], "from_report": True}
            with self.lock:
                self.outputs["%s/%s" % (device["flow"], out["node"])] = out
            if self.bus:
                self.bus.publish("flow", out)

    def outputs_snapshot(self):
        with self.lock:
            return dict(self.outputs)

    def device_pins(self, device):
        """The board's pins, what the deployed flow does with each, and the
        last level the device reported — one answer for the editor to
        draw."""
        profile = iotmod.board_profile(device.get("board")) or {}
        flow = self.flow_for(device)
        used = {}
        for node in (flow or {}).get("nodes", []):
            gpio = (node.get("config") or {}).get("gpio")
            if gpio is None:
                continue
            try:
                gpio = int(gpio)
            except (TypeError, ValueError):
                continue
            used.setdefault(gpio, []).append({"node": node.get("id"),
                                              "type": node.get("type")})
        reported = ((device.get("state") or {}).get("pins") or {})
        age = None
        if (device.get("state") or {}).get("at"):
            age = iotmod.now() - device["state"]["at"]
        out = []
        for pin in profile.get("pins", []):
            gpio = pin["gpio"]
            live = reported.get(str(gpio))
            out.append(dict(pin, **{
                "used_by": used.get(gpio, []),
                "status": ("reserved" if not pin["usable"]
                           else "bound" if gpio in used else "free"),
                "live": live,
                "value": (live or {}).get("value"),
                "dir": (live or {}).get("dir"),
            }))
        return {
            "board": device.get("board"),
            "label": profile.get("label"),
            "pins": out,
            "buses": profile.get("buses", {}),
            "peripherals": profile.get("peripherals", {}),
            "flow": (flow or {}).get("id"),
            "flow_name": (flow or {}).get("name"),
            "device": {"id": device["id"], "name": device.get("name"),
                       "ip": device.get("ip"), "rssi": device.get("rssi"),
                       "enrolled": device.get("enrolled"),
                       "last_seen": device.get("last_seen"),
                       "online": bool(device.get("last_seen") and
                                      iotmod.now() - device["last_seen"] < FRESH),
                       "flashed": device.get("flashed"),
                       "mismatch": device.get("mismatch"),
                       "agent": ((device.get("state") or {}).get("agent")
                                 or (device.get("probe") or {}).get("agent")),
                       "reported": bool((device.get("state") or {}).get("agent")),
                       "free_ram": (device.get("state") or {}).get("free_ram"),
                       "uptime": (device.get("state") or {}).get("uptime"),
                       "running": (device.get("state") or {}).get("running"),
                       # What the board says about its held-open socket, so the
                       # studio can show the link without asking the fleet.
                       "link": (device.get("state") or {}).get("link"),
                       "camera": (device.get("state") or {}).get("camera")},
            "cost": self.deploy_cost(flow, device),
            "reported_age": age,
        }

    # -- the camera --------------------------------------------------------
    def screen_cameras(self):
        """Devices whose deployed flow asks to be on the dashboard."""
        out = []
        for d in self.devices.load()["devices"]:
            flow = self.flow_for(d)
            node = flowmod.feeds_the_screen(flow)
            if not node:
                continue
            cfg = node.get("config") or {}
            # The node that gates the screen may be the publish node, which
            # knows the caption; the frame size only ever lives on the feed.
            feed = ([n for n in flowmod.camera_nodes(flow)
                     if n.get("type") == "camera.feed"] or [{}])[0]
            last = self.frames.get(d["id"])
            out.append({
                "id": d["id"],
                "name": (cfg.get("label") or "").strip() or d.get("name"),
                "board": d.get("board"),
                "ip": d.get("ip"),
                "flow": (flow or {}).get("name"),
                "node": node.get("id"),
                "frame_size": (feed.get("config") or {}).get("frame_size"),
                # How often this one wants asking. None means the screen's own
                # default, which is what a flow without a publish node gets.
                "every_ms": flowmod.screen_interval_ms(flow),
                "online": bool(d.get("last_seen") and
                               iotmod.now() - d["last_seen"] < FRESH),
                "last_frame": last["at"] if last else None,
                # The quarter of a turn a streamed JPEG still needs; the board
                # did the rest with the sensor's flips.
                "screen_turn": 90 if (last and last.get("format") == "jpeg" and
                                      self.camera_orientation(d)[0] in (90, 270)) else 0,
                "size": ("%dx%d" % (last["width"], last["height"])) if last else None,
            })
        return out

    def camera_orientation(self, device):
        """(turn, mirror) for this device, from its Camera feed node."""
        for node in flowmod.camera_nodes(self.flow_for(device)):
            if node.get("type") != "camera.feed":
                continue
            cfg = node.get("config") or {}
            try:
                turn = int(cfg.get("rotate") or 0) % 360
            except (TypeError, ValueError):
                turn = 0
            if turn % 90:
                turn = 0
            return turn, str(cfg.get("mirror") or "no") == "yes"
        return 0, False

    def camera_url(self, device, path="/frame"):
        ip = device.get("ip")
        if not ip:
            raise iotmod.NetError("this device has not reported an address yet")
        flow = self.flow_for(device)
        if not device.get("camera") and not flowmod.camera_nodes(flow):
            raise iotmod.NetError("nothing in this device's flow asks for a camera")
        return "http://%s:%d%s" % (ip, int(device.get("camera_port") or 8080), path)

    def frame(self, device, timeout=8):
        """The picture to show: from the encrypted stream when the board holds
        one open, else fetched from it and turned into a PNG."""
        stream = self.link.stream(device["id"]) if self.link else None
        if stream is not None:
            return self._stream_frame(device, stream)
        import urllib.request

        url = self.camera_url(device)
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                raw = fh.read()
                width = int(fh.headers.get("X-Width") or 0)
                height = int(fh.headers.get("X-Height") or 0)
                fmt = fh.headers.get("X-Format") or "rgb565"
                sensor = fh.headers.get("X-Sensor")
        except Exception as exc:
            cached = self.frames.get(device["id"])
            if cached and cached.get("streamed"):
                # The last picture the stream sent, still worth showing while
                # the stream is down (the camera switched off, say).
                return dict(self._picture(device, cached), stale=True, error=str(exc))
            if cached:
                return dict(cached, stale=True, error=str(exc))
            raise iotmod.NetError("no frame from %s (%s)" % (device["id"], exc))
        if not width or not height:
            raise iotmod.NetError("the device did not say how big the frame is")
        # How this device's camera is mounted, from the flow that turned it on.
        turn, mirror = self.camera_orientation(device)
        png = pixels.frame_to_png(raw, width, height, fmt,
                                  rotate=turn, mirror=mirror)
        if turn in (90, 270):
            width, height = height, width
        out = {"image": png, "type": "image/png", "width": width, "height": height,
               "format": fmt, "sensor": sensor, "at": iotmod.now(), "bytes": len(raw),
               "stale": False}
        with self.lock:
            self.frames[device["id"]] = out
        self.touch(device["id"])
        return out

    # -- the picture stream -----------------------------------------------
    def _streamed(self, device, body):
        """A MEDIA frame: format, width, height, then the picture."""
        if len(body) < MEDIA_HEAD:
            return
        code, width, height = struct.unpack(">BHH", body[:MEDIA_HEAD])
        fmt = MEDIA_FORMATS.get(code)
        if not fmt:
            return
        got = {"raw": body[MEDIA_HEAD:], "format": fmt, "width": width,
               "height": height, "at": iotmod.now(), "mono": time.monotonic(),
               "bytes": len(body) - MEDIA_HEAD, "streamed": True, "stale": False}
        with self.frame_arrived:
            self.frames[device["id"]] = got
            self.frame_arrived.notify_all()

    def still(self, device, body):
        """A picture a flow on the board chose to send (Send to host): into the
        run log, and to any Device event flow waiting on its kind. Returns the
        picture, or None for a frame that does not parse."""
        if len(body) < MEDIA_HEAD + 1:
            return None
        code, width, height, klen = struct.unpack(">BHHB", body[:MEDIA_HEAD + 1])
        fmt = MEDIA_FORMATS.get(code)
        start = MEDIA_HEAD + 1 + klen
        if fmt is None or len(body) <= start:
            return None
        kind = body[MEDIA_HEAD + 1:start].decode("utf-8", "replace") or "picture"
        pic = {"data": bytes(body[start:]), "format": fmt,
               "width": width, "height": height}
        size = len(pic["data"])
        message = "%s %dx%d, %.1f KB" % (fmt, width, height, size / 1024.0)
        self._emit(device, "ok", "picture: " + message, kind=kind, payload=size)
        if self.engine:
            try:
                self.engine.fire_device_event(device["id"], kind, size, message,
                                              picture=pic)
            except Exception as exc:
                self._emit(device, "warn", "picture did not reach a flow: %s" % exc)
        return pic

    def _lease(self, device, stream):
        """Keep the board streaming while someone is looking, and no longer."""
        now = time.monotonic()
        if self.leases.get(device["id"], 0) - now > STREAM_LEASE_MS / 2000.0:
            return
        every = flowmod.screen_interval_ms(self.flow_for(device)) or 1000
        if stream.send(wire.CONTROL, {"every": every, "for": STREAM_LEASE_MS}):
            self.leases[device["id"]] = now + STREAM_LEASE_MS / 1000.0

    def _stream_frame(self, device, stream):
        asked = time.monotonic()
        self._lease(device, stream)
        key = device["id"]
        every = (flowmod.screen_interval_ms(self.flow_for(device)) or 1000) / 1000.0
        with self.frame_arrived:
            got = self.frames.get(key)
            # Recent enough to be the newest the board has sent: show it now
            # rather than wait a whole interval for the next one.
            if not (got and got.get("streamed") and asked - got["mono"] < every + 0.5):
                self.frame_arrived.wait_for(
                    lambda: (self.frames.get(key) or {}).get("mono", 0) > asked,
                    STREAM_WAIT)
            got = self.frames.get(key)
        if not got or not got.get("streamed"):
            raise iotmod.NetError("the stream has sent no picture yet")
        stale = time.monotonic() - got["mono"] > STREAM_WAIT
        return dict(self._picture(device, got), stale=stale)

    def _picture(self, device, got):
        """The image a browser shows. JPEG goes as it came; the board turned
        it already. Anything raw is turned and packed here, once per frame."""
        if got["format"] == "jpeg":
            image, kind = got["raw"], "image/jpeg"
            width, height = got["width"], got["height"]
        else:
            if "png" not in got:
                turn, mirror = self.camera_orientation(device)
                got["png"] = pixels.frame_to_png(got["raw"], got["width"], got["height"],
                                                 got["format"], rotate=turn, mirror=mirror)
                got["turned"] = turn in (90, 270)
            image, kind = got["png"], "image/png"
            width, height = got["width"], got["height"]
            if got.get("turned"):
                width, height = height, width
        return {"image": image, "type": kind, "width": width, "height": height,
                "format": got["format"], "at": got["at"], "bytes": got["bytes"],
                "sensor": None, "streamed": True}

    # -- events, device to host --------------------------------------------
    def event(self, device, body):
        kind = str(body.get("kind") or "event")[:32]
        level = body.get("level") if body.get("level") in (
            "ok", "warn", "serious", "critical", "idle") else "idle"
        message = str(body.get("message") or "")[:400]
        node = str(body.get("node") or "")[:64] or None
        self.touch(device["id"], ip=body.get("ip"), rssi=body.get("rssi"),
                   uptime=body.get("uptime"))
        if kind == "pad":
            self.note_pad(device["id"], body.get("payload"))
        self._emit(device, level, message or kind, node=node, kind=kind,
                   payload=body.get("payload"))
        # A device's report can start a flow here. It says what happened; which
        # flow cares, if any, is decided on this side.
        if self.engine:
            try:
                self.engine.fire_device_event(device["id"], kind,
                                              body.get("payload"), message)
            except Exception as exc:          # a bad flow must not break enrol
                self._emit(device, "warn", "event did not reach a flow: %s" % exc)
        return {"ok": True}

    def note_pad(self, device_id, payload):
        """What controller a board says it has. Trusted only as its own claim."""
        if not isinstance(payload, dict):
            return
        with self.lock:
            self.pads[device_id] = {
                "name": str(payload.get("name") or "")[:64],
                "address": str(payload.get("address") or "")[:32],
                "connected": bool(payload.get("connected")),
                "at": iotmod.now(),
            }

    def pad_state(self, device_id):
        """None means the board has never mentioned a controller — which is what
        every board looks like until one runs a flow with a Controller node."""
        with self.lock:
            held = self.pads.get(device_id)
            return dict(held) if held else None

    def _emit(self, device, level, message, node=None, kind="device", payload=None):
        """Onto the same bus the local flow engine uses, so the studio's run
        log shows a remote firing exactly like a local one — and into a
        ring this end keeps, so it is still readable afterwards."""
        row = {
            "time": time.strftime("%H:%M:%S"),
            "at": iotmod.now(),
            "flow": device.get("flow"),
            "node": node or device["id"],
            "level": level,
            "message": message,
            "device": device["id"],
            "device_name": device.get("name"),
            "kind": kind,
            "payload": payload,
        }
        # Every node a device ran arrives as one `fired` event — up to 64 per
        # report, every two seconds, carrying no message at all. Keeping those
        # would push out the one line that says why a board stopped.
        if kind == "step" and isinstance(payload, dict):
            with self.lock:
                held = self.steps.setdefault(device["id"], {})
                if payload.get("active"):
                    held[node] = device.get("flow")
                else:
                    held.pop(node, None)
        if kind != "fired":
            with self.lock:
                ring = self.history.setdefault(device["id"], [])
                ring.append(row)
                if len(ring) > HISTORY:
                    del ring[:-HISTORY]
        if not self.bus:
            return
        self.bus.publish("flow", row)

    def device_log(self, device_id, limit=HISTORY):
        """What this device has said lately, newest last."""
        with self.lock:
            rows = list(self.history.get(device_id) or [])
        return rows[-max(1, min(int(limit or HISTORY), HISTORY)):]

    # -- flashing ----------------------------------------------------------
    def flash(self, device_id, port, ssid, psk, extra=None, flow=None):
        """Run the flasher as a subprocess and stream what it says."""
        device = iotmod.get_device(self.devices, device_id)
        if not device:
            raise iotmod.NetError("no such device")
        extra = list(extra or [])
        wanted = None
        if flow:
            for f in self.flow_store.load().get("flows", []):
                if f.get("id") == flow:
                    wanted = f
            if wanted is None:
                raise iotmod.NetError("no flow with that id")
            if wanted.get("board") and device.get("board") \
                    and wanted["board"] != device["board"]:
                raise iotmod.NetError("that flow is written for a different board")
            cannot = sorted({n["type"] for n in wanted.get("nodes", [])
                             if not flowmod.runs_on_device(n["type"])})
            if cannot:
                raise iotmod.NetError("this board cannot run: %s" % ", ".join(cannot))
            extra += ["--flow", wanted["id"]]
        with self.lock:
            if self.flashing and self.flashing.is_alive():
                raise iotmod.NetError("a board is already being flashed")
            self.flash_log = []
        # Only this board's picture is about to go away, not the whole fleet's.
        self.frames.pop(device_id, None)
        cmd = [sys.executable, os.path.join(REPO, "scripts", "iot-flash.py"),
               "--port", port, "--device", device_id, "--psk", "-"]
        if ssid:
            cmd += ["--ssid", ssid]
        cmd += extra

        def run():
            self._flash_line("$ " + " ".join(c for c in cmd if c != "-"), "idle")
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    env=dict(os.environ, ZERO2W_URL=self.self_url))
            except OSError as exc:
                return self._flash_line(str(exc), "critical")
            try:
                proc.stdin.write((psk or "") + "\n")
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._flash_line(line, "idle")
            code = proc.wait()
            self._flash_line("flasher exited %d" % code, "ok" if code == 0 else "critical")
            if code == 0:
                # The flasher wrote these over USB, so the board holds them and
                # its first sync has nothing to fetch — which is what keeps
                # `deploy_cost` from recommending the wire forever after.
                self.note_modules(device_id, self._wrote(wanted, device))
                self._flashed(device)

        with self.lock:
            self.flashing = threading.Thread(target=run, daemon=True, name="flash")
            self.flashing.start()
        return {"ok": True, "device": device_id, "port": port}

    def _wrote(self, flow, device):
        """{name: sha} for what a flash put on the board."""
        names = ["agent.py"] if flow is None else self.agent_files(flow, device)
        held = {}
        for name in names:
            try:
                held[name] = sha(self.module_source(name))
            except iotmod.NetError:
                continue
        return held

    def _flashed(self, device):
        """The board is written and about to boot."""
        self.touch(device["id"], flashed=iotmod.now())
        self.open_window(AFTER_FLASH,
                         reason="just flashed %s \u2014 waiting for it to boot"
                                % device.get("name", device["id"]),
                         awaiting=device["id"])
        self._flash_line("enrollment is open for %d minutes, for %s"
                         % (AFTER_FLASH // 60, device.get("name", device["id"])), "ok")

    def _flash_line(self, text, level):
        row = {"time": time.strftime("%H:%M:%S"), "level": level, "text": text}
        with self.lock:
            self.flash_log.append(row)
            if len(self.flash_log) > 300:
                del self.flash_log[:-300]
        if self.bus:
            self.bus.publish("flash", row)

    def flash_state(self):
        with self.lock:
            return {"running": bool(self.flashing and self.flashing.is_alive()),
                    "log": list(self.flash_log)}

    # -- what the screen shows ---------------------------------------------
    def state(self):
        devices = self.devices.load()["devices"]
        now = iotmod.now()
        return {
            "enrollment": self.window_state(),
            "awaiting": self.awaiting,
            "refusals": list(self.refusals),
            "online": len([d for d in devices
                           if d.get("last_seen") and now - d["last_seen"] < FRESH]),
            "enrolled": len([d for d in devices if d.get("enrolled")]),
            "queued": {k: len(v) for k, v in self.queues.items() if v},
            "flashing": bool(self.flashing and self.flashing.is_alive()),
            # None when no listener is running, which is what an older agent
            # or a console started without one looks like.
            "link": self.link_state(),
        }
