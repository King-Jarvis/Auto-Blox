"""A gamepad on this host, read off the kernel's input layer.

BlueZ pairs the pad and the kernel presents it as an event device, so this is
the same for Bluetooth or USB and needs only the `input` group. It fills the
state dict `agent/modules/pad.py` defines, which is all the nodes read.
"""
import fcntl
import glob
import os
import select
import struct
import threading
import time

from .agent.modules import pad as padmod

# struct input_event { struct timeval time; __u16 type, code; __s32 value; }
# On 64-bit Linux timeval is two longs, so the record is 24 bytes.
EVENT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT)

EV_KEY, EV_ABS = 0x01, 0x03

# Overridable for tests.
DEVICES = "/proc/bus/input/devices"

# EVIOCGABS(abs) = _IOR('E', 0x40 + abs, struct input_absinfo), and
# input_absinfo is six 32-bit ints: value, min, max, fuzz, flat, resolution.
ABSINFO = "6i"
ABSINFO_SIZE = struct.calcsize(ABSINFO)


def _eviocgabs(code):
    return (2 << 30) | (ABSINFO_SIZE << 16) | (0x45 << 8) | (0x40 + code)


# Kernel axis codes to the shared names. The common layout (xpad, hid-generic):
# ABS_Z/ABS_RZ are the triggers, ABS_RX/ABS_RY the right stick.
STANDARD_AXES = {
    0x00: "lx", 0x01: "ly",         # ABS_X, ABS_Y
    0x03: "rx", 0x04: "ry",         # ABS_RX, ABS_RY
    0x02: "lt", 0x05: "rt",         # ABS_Z, ABS_RZ
    0x10: "hx", 0x11: "hy",         # ABS_HAT0X, ABS_HAT0Y
}

# Sony swaps those four (measured on a DualShock 4, 054c:09cc). Read the other
# way, `right trigger` is the right stick's Y, which rests at 0.53: half
# throttle the moment a flow arms.
SONY_AXES = dict(STANDARD_AXES)
SONY_AXES.update({0x02: "rx", 0x05: "ry", 0x03: "lt", 0x04: "rt"})

QUIRKS = {"054c": SONY_AXES}   # by the vendor id the kernel reports

BUTTON_CODES = {
    0x130: "a", 0x131: "b", 0x133: "y", 0x134: "x",
    0x136: "lb", 0x137: "rb",
    0x13a: "view", 0x13b: "menu", 0x13c: "xbox",
    0x13d: "ls", 0x13e: "rs",
}

# A d-pad is already -1..1; every other axis is scaled by the driver's range.
HATS = ("hx", "hy")


def candidates():
    """Event devices with a `js` handler — joydev's word for a controller."""
    found = []
    try:
        with open(DEVICES, encoding="utf-8") as fh:
            blocks = fh.read().split("\n\n")
    except OSError:
        return found
    for block in blocks:
        name, event = "", ""
        for line in block.splitlines():
            if line.startswith('N: Name="'):
                name = line[9:].rstrip('"')
            elif line.startswith("H: Handlers="):
                parts = line[12:].split()
                if not any(p.startswith("js") for p in parts):
                    parts = []
                for p in parts:
                    if p.startswith("event"):
                        event = "/dev/input/" + p
        if event:
            found.append((event, name))
    found.sort(reverse=True)
    return found


def pads():
    """Every pad the kernel is showing, and whether this user can read it."""
    out = []
    for path, name in candidates():
        out.append({"path": path, "name": name,
                    "readable": os.access(path, os.R_OK)})
    return out


class Reader(threading.Thread):
    """One event device, drained into a state dict until it is stopped."""

    daemon = True

    def __init__(self, state, want=""):
        super().__init__(name="pad-reader")
        self.state = state
        self.want = (want or "").lower()
        self.path = ""
        self.ranges = {}
        self.axes = dict(STANDARD_AXES)
        self.layout = "standard"
        self.error = ""
        self._stop = threading.Event()

    # -- setup -------------------------------------------------------------
    def pick(self):
        """Choose and remember the device, so `vendor()` works before `run`."""
        for path, name in candidates():
            if self.want and self.want not in name.lower():
                continue
            if os.access(path, os.R_OK):
                self.path = path
                return path, name
        return "", ""

    def vendor(self):
        """The USB vendor id the kernel reports for this device, lowercase."""
        node = self.path.rsplit("/", 1)[-1]
        try:
            with open("/sys/class/input/%s/device/uevent" % node,
                      encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PRODUCT="):
                        parts = line.strip().split("=")[1].split("/")
                        if len(parts) >= 2:
                            # PRODUCT=5/54c/9cc/100: hex, unpadded.
                            return parts[1].lower().rjust(4, "0")
        except OSError:
            pass
        return ""

    def choose_layout(self, resting):
        """Which code means what on this pad.

        A known vendor wins. Otherwise the resting values decide: a stick rests
        mid-range, a trigger at the bottom — which a trigger held down while the
        reader opens would fool.
        """
        vendor = self.vendor()
        if vendor in QUIRKS:
            self.axes, self.layout = dict(QUIRKS[vendor]), "quirk:" + vendor
            return
        centred, bottomed = [], []
        for code in (0x02, 0x03, 0x04, 0x05):
            span = self.ranges.get(STANDARD_AXES[code])
            if span is None or code not in resting:
                return                      # not enough to judge; keep standard
            low, high, _flat = span
            mid = (low + high) / 2.0
            if abs(resting[code] - mid) < (high - low) * 0.2:
                centred.append(code)
            elif abs(resting[code] - low) < (high - low) * 0.2:
                bottomed.append(code)
        if len(centred) == 2 and len(bottomed) == 2:
            found = dict(STANDARD_AXES)
            found[centred[0]], found[centred[1]] = "rx", "ry"
            found[bottomed[0]], found[bottomed[1]] = "lt", "rt"
            self.axes, self.layout = found, "measured at rest"

    def read_ranges(self, fd):
        """Axis ranges from EVIOCGABS; 0..65535 and -32768..32767 both occur."""
        resting = {}
        for code, key in STANDARD_AXES.items():
            if key in HATS:
                continue
            try:
                raw = fcntl.ioctl(fd, _eviocgabs(code),
                                  b"\0" * ABSINFO_SIZE)
            except OSError:
                continue
            value, low, high, _fuzz, flat, _res = struct.unpack(ABSINFO, raw)
            if high > low:
                self.ranges[key] = (low, high, flat)
                resting[code] = value
        # Read under the standard names, then re-keyed to the chosen layout.
        self.choose_layout(resting)
        if self.axes != STANDARD_AXES:
            self.ranges = {self.axes[code]: self.ranges[STANDARD_AXES[code]]
                           for code in STANDARD_AXES
                           if STANDARD_AXES[code] in self.ranges}

    def scale(self, key, raw):
        if key in HATS:
            return float(max(-1, min(1, raw)))
        span = self.ranges.get(key)
        if not span:
            return 0.0
        low, high, flat = span
        if key in padmod.ONE_WAY:
            value = (raw - low) / float(high - low)
            return max(0.0, min(1.0, value))
        mid = (high + low) / 2.0
        half = (high - low) / 2.0
        if flat and abs(raw - mid) <= flat:
            return 0.0
        value = (raw - mid) / half
        return max(-1.0, min(1.0, value))

    # -- the loop ----------------------------------------------------------
    def run(self):
        path, name = self.pick()
        if not path:
            self.error = ("no readable gamepad on this host — pair one, or add "
                          "this user to the input group")
            return
        self.path = path
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            self.error = "cannot open %s: %s" % (path, exc)
            return
        try:
            self.read_ranges(fd)
            self.state["connected"] = True
            self.state["name"] = name
            self.state["at"] = time.monotonic()
            while not self._stop.is_set():
                # select, so stop() does not wait for the pad to send.
                if not select.select([fd], [], [], 0.25)[0]:
                    continue
                raw = os.read(fd, EVENT_SIZE * 32)
                if not raw:
                    break
                for at in range(0, len(raw) - EVENT_SIZE + 1, EVENT_SIZE):
                    self.feed(struct.unpack(EVENT, raw[at:at + EVENT_SIZE]))
        except OSError as exc:
            self.error = "%s went away: %s" % (path, exc)
        finally:
            os.close(fd)
            self.state["connected"] = False

    def feed(self, event):
        _sec, _usec, kind, code, value = event
        if kind == EV_ABS:
            key = self.axes.get(code)
            if key is None:
                return
            scaled = self.scale(key, value)
            if key in ("ly", "ry", "hy"):
                # Pushed forward reads negative; flipped so forward is +.
                scaled = -scaled
            self.state[key] = scaled
        elif kind == EV_KEY:
            key = BUTTON_CODES.get(code)
            if key is None:
                return
            padmod.press(self.state, key, value)
        else:
            return
        self.state["at"] = time.monotonic()
        self.state["seq"] = (self.state.get("seq") or 0) + 1

    def stop(self):
        self._stop.set()


PAIR_HINT = "pair it on the IoT screen's Bluetooth tab, then run this again"
