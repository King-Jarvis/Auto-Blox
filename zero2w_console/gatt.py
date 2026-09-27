"""Any BLE device's GATT, through BlueZ's D-Bus API via `gdbus`.

`pad.py` covers a gamepad through the kernel's HID stack; everything else — a
sensor, a bulb, another board — is services and characteristics, reached here.
`GetManagedObjects` returns the whole tree in one call, so browsing a device
costs one subprocess. BlueZ's D-Bus policy lets any user call `org.bluez`.
"""
import select
import subprocess
import threading

from . import gvariant

BUS = "org.bluez"
OM = "org.freedesktop.DBus.ObjectManager"

ADAPTER_IF = "org.bluez.Adapter1"
DEVICE_IF = "org.bluez.Device1"
SERVICE_IF = "org.bluez.GattService1"
CHAR_IF = "org.bluez.GattCharacteristic1"
DESC_IF = "org.bluez.GattDescriptor1"

# The 16-bit UUIDs BLE assigns, rendered into the 128-bit form every tool uses.
BASE = "-0000-1000-8000-00805f9b34fb"


def short_uuid(uuid):
    """`0000180d-0000-...-fb` back to `180d`; a vendor UUID stays whole."""
    uuid = (uuid or "").lower()
    if len(uuid) == 36 and uuid.endswith(BASE) and uuid[:4] == "0000":
        return uuid[4:8]
    return uuid


# Names for the handful anyone meets, not the full assigned-numbers registry.
KNOWN = {
    "1800": "Generic Access", "1801": "Generic Attribute",
    "180a": "Device Information", "180d": "Heart Rate",
    "180f": "Battery", "1812": "Human Interface Device",
    "181a": "Environmental Sensing", "1819": "Location and Navigation",
    "2a00": "Device Name", "2a19": "Battery Level",
    "2a37": "Heart Rate Measurement", "2a4d": "Report",
    "2a4b": "Report Map", "2a6e": "Temperature", "2a6f": "Humidity",
    "2a58": "Analog", "2a56": "Digital",
}


def label(uuid):
    short = short_uuid(uuid)
    return KNOWN.get(short, short)


def _gdbus(path, method, *args, **kw):
    """One D-Bus method call with GVariant-text arguments, parsed, or None."""
    timeout = kw.get("timeout", 10)
    argv = ["gdbus", "call", "--system", "--dest", BUS,
            "--object-path", path, "--method", method] + list(args)
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return gvariant.parse(done.stdout)
    except (gvariant.GVariantError, IndexError):
        return None


def _bytes_out(value):
    """gdbus prints `ay` as a list or as `b'..'`; either becomes JSON-safe ints."""
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    return value


def tree():
    """Every BlueZ object, as {path: {interface: {property: value}}}."""
    got = _gdbus("/", OM + ".GetManagedObjects")
    if not got:
        return {}
    return got[0] or {}


def _dev_path(mac):
    return "/org/bluez/hci0/dev_" + (mac or "").upper().replace(":", "_")


def _mac_of(path):
    tail = path.rsplit("/dev_", 1)[-1]
    return tail.split("/")[0].replace("_", ":")


# -- what is out there -------------------------------------------------------
def adapter(known=None):
    """The adapter's own properties, plus what it can do beyond being a client."""
    known = known if known is not None else tree()
    for path, ifaces in known.items():
        if ADAPTER_IF in ifaces:
            out = dict(ifaces[ADAPTER_IF])
            out["path"] = path
            # Whether this host can be a peripheral too, not only a client.
            out["can_advertise"] = "org.bluez.LEAdvertisingManager1" in ifaces
            out["can_serve_gatt"] = "org.bluez.GattManager1" in ifaces
            return out
    return {}


def devices(known=None):
    """Every device BlueZ knows, with the facts a screen or a flow needs."""
    known = known if known is not None else tree()
    out = []
    for path, ifaces in known.items():
        props = ifaces.get(DEVICE_IF)
        if props is None:
            continue
        uuids = props.get("UUIDs") or []
        out.append({
            "path": path,
            "mac": props.get("Address") or _mac_of(path),
            "name": props.get("Name") or props.get("Alias") or "",
            # BlueZ's classification; a name match calls an IR remote a gamepad.
            "icon": props.get("Icon") or "",
            "paired": bool(props.get("Paired")),
            "bonded": bool(props.get("Bonded")),
            "trusted": bool(props.get("Trusted")),
            "connected": bool(props.get("Connected")),
            "resolved": bool(props.get("ServicesResolved")),
            "rssi": props.get("RSSI"),
            # Bearer.LE1 appears on every device in BlueZ 5.85; only a
            # Classic-capable device has a `Class`.
            "bredr": "Class" in props,
            "le": props.get("AddressType") == "random" or "Class" not in props,
            "uuids": [short_uuid(u) for u in uuids],
            # Classic HID has no GATT to browse.
            "classic_hid": any(short_uuid(u) == "1124" for u in uuids),
        })
    out.sort(key=lambda d: (not d["connected"], d["name"].lower(), d["mac"]))
    return out


def is_gamepad(device):
    """BlueZ's word for it, with the name only as a last resort."""
    if device.get("icon") == "input-gaming":
        return True
    low = (device.get("name") or "").lower()
    for word in ("xbox", "gamepad", "joystick", "dualsense", "dualshock",
                 "8bitdo", "wireless controller", "pro controller"):
        if word in low:
            return True
    return False


# -- the GATT tree -----------------------------------------------------------
def gatt(mac, known=None):
    """One device's services, characteristics and descriptors.

    Empty while not connected (GATT objects only exist with a link) and always
    empty for a Classic device; `devices()` says which (`connected`, `classic_hid`).
    """
    known = known if known is not None else tree()
    root = _dev_path(mac)
    services = {}
    for path, ifaces in known.items():
        if not path.startswith(root + "/"):
            continue
        if SERVICE_IF in ifaces:
            props = ifaces[SERVICE_IF]
            services[path] = {"path": path,
                              "uuid": (props.get("UUID") or "").lower(),
                              "name": label(props.get("UUID")),
                              "primary": bool(props.get("Primary")),
                              "chars": []}
    chars = {}
    for path, ifaces in known.items():
        props = ifaces.get(CHAR_IF)
        if props is None or not path.startswith(root + "/"):
            continue
        holder = services.get(props.get("Service") or "")
        entry = {"path": path,
                 "uuid": (props.get("UUID") or "").lower(),
                 "name": label(props.get("UUID")),
                 "flags": list(props.get("Flags") or []),
                 "notifying": bool(props.get("Notifying")),
                 "value": _bytes_out(props.get("Value") or []),
                 "descriptors": []}
        chars[path] = entry
        if holder is not None:
            holder["chars"].append(entry)
    for path, ifaces in known.items():
        props = ifaces.get(DESC_IF)
        if props is None or not path.startswith(root + "/"):
            continue
        holder = chars.get(props.get("Characteristic") or "")
        if holder is not None:
            holder["descriptors"].append({
                "path": path,
                "uuid": (props.get("UUID") or "").lower(),
                "name": label(props.get("UUID")),
            })
    for service in services.values():
        service["chars"].sort(key=lambda c: c["path"])
    return sorted(services.values(), key=lambda s: s["path"])


def find_char(mac, char_uuid, service_uuid=None, known=None):
    """One characteristic's object path, by UUID.

    Resolved every time: the path carries BlueZ's attribute numbering, which
    changes between connections.
    """
    want = short_uuid(char_uuid)
    for service in gatt(mac, known):
        if service_uuid and short_uuid(service_uuid) != short_uuid(service["uuid"]):
            continue
        for char in service["chars"]:
            if short_uuid(char["uuid"]) == want:
                return char["path"]
    return None


# -- read, write, subscribe --------------------------------------------------
def read(path, timeout=10):
    """A characteristic's value as bytes, or None."""
    got = _gdbus(path, CHAR_IF + ".ReadValue", "@a{sv} {}", timeout=timeout)
    if not got:
        return None
    value = got[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, list):
        try:
            return bytes(int(b) & 0xFF for b in value)
        except (TypeError, ValueError):
            return None
    return None


def write(path, payload, timeout=10):
    """Bytes to a characteristic. True if BlueZ took it."""
    if isinstance(payload, str):
        payload = payload.encode()
    payload = bytes(payload)
    # Typed explicitly, because an empty array has no inferable element type.
    arg = "@ay [%s]" % ", ".join("0x%02x" % b for b in payload)
    return _gdbus(path, CHAR_IF + ".WriteValue", arg, "@a{sv} {}",
                  timeout=timeout) is not None


def notify(path, on=True, timeout=10):
    """StartNotify or StopNotify, as a one-off call.

    BlueZ ties a subscription to the D-Bus connection that asked for it, and a
    `gdbus call` exits at once, so this subscription ends as soon as it
    starts. `hold_notify` is the one that lasts.
    """
    verb = "StartNotify" if on else "StopNotify"
    return _gdbus(path, CHAR_IF + "." + verb, timeout=timeout) is not None


def hold_notify(path):
    """A `bluetoothctl` session that subscribes and stays running, so the
    subscription lasts. Stop it with `release_notify`."""
    proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            text=True)
    proc.stdin.write("menu gatt\nselect-attribute %s\nnotify on\n" % path)
    proc.stdin.flush()
    return proc


def release_notify(proc):
    try:
        proc.stdin.write("notify off\nback\nquit\n")
        proc.stdin.flush()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def connect(mac, timeout=25):
    """Bring a link up so its GATT exists. Already-connected is success."""
    got = _gdbus(_dev_path(mac), DEVICE_IF + ".Connect", timeout=timeout)
    if got is not None:
        return {"ok": True, "detail": "connected"}
    for device in devices():
        if device["mac"].upper() == (mac or "").upper() and device["connected"]:
            return {"ok": True, "detail": "already connected"}
    return {"ok": False, "detail": "BlueZ would not bring up a link to %s" % mac}


def disconnect(mac, timeout=10):
    return _gdbus(_dev_path(mac), DEVICE_IF + ".Disconnect",
                  timeout=timeout) is not None


def is_connected(mac, known=None):
    for device in devices(known):
        if device["mac"].upper() == (mac or "").upper():
            return device["connected"]
    return False


# -- a subscription ------------------------------------------------------------
def _monitor(path):
    """`gdbus monitor` on one object, line-buffered: without stdbuf it holds
    every signal back until its pipe buffer fills."""
    return subprocess.Popen(["stdbuf", "-oL", "gdbus", "monitor", "--system",
                             "--dest", BUS, "--object-path", path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)


def signal_value(line):
    """The new Value in one monitor line, as bytes, or None.

    A line reads `<path>: org.freedesktop.DBus.Properties.PropertiesChanged
    ('org.bluez.GattCharacteristic1', {'Value': <[byte 0x01]>}, @as [])`.
    """
    _path, _, rest = line.partition(": ")
    member, _, args = rest.strip().partition(" ")
    if not member.endswith(".PropertiesChanged"):
        return None
    try:
        parsed = gvariant.parse(args)
    except (gvariant.GVariantError, IndexError, ValueError):
        return None
    props = parsed[1] if isinstance(parsed, tuple) and len(parsed) > 1 else {}
    if not isinstance(props, dict) or "Value" not in props:
        return None
    value = props["Value"]
    try:
        return bytes(value) if isinstance(value, (bytes, bytearray)) \
            else bytes(int(b) & 0xFF for b in value)
    except (TypeError, ValueError):
        return None


class Subscription(threading.Thread):
    """A characteristic's notifications, delivered as they arrive.

    It never connects anything: it waits for the device to be connected, then
    subscribes and follows the value, and goes back to waiting when the link
    drops. `deliver(bytes)` runs on this thread.
    """

    daemon = True
    WAIT = 2.0

    def __init__(self, mac, char_uuid, deliver, on_state=None):
        super().__init__(name="ble-notify")
        self.mac, self.char_uuid = mac, char_uuid
        self.deliver, self.on_state = deliver, on_state
        self.path = None
        self.proc = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _state(self, on):
        if self.on_state:
            try:
                self.on_state(on)
            except Exception:
                pass

    def run(self):
        while not self._stop.is_set():
            path = find_char(self.mac, self.char_uuid)
            if path:
                try:
                    holder = hold_notify(path)
                except (OSError, subprocess.SubprocessError):
                    holder = None
                if holder is not None:
                    self.path = path
                    self._state(True)
                    self._follow(path)
                    self._state(False)
                    release_notify(holder)
                    self.path = None
            self._stop.wait(self.WAIT)

    def _follow(self, path):
        try:
            self.proc = _monitor(path)
        except (OSError, subprocess.SubprocessError):
            return
        proc, since_check = self.proc, 0.0
        try:
            while not self._stop.is_set() and proc.poll() is None:
                ready = select.select([proc.stdout], [], [], 0.5)[0]
                if ready:
                    line = proc.stdout.readline()
                    if not line:
                        return
                    value = signal_value(line)
                    if value is not None:
                        self.deliver(value)
                    continue
                # Quiet: check now and then that the characteristic is still
                # there, since a dropped link leaves the monitor running mute.
                since_check += 0.5
                if since_check >= self.WAIT:
                    since_check = 0.0
                    if find_char(self.mac, self.char_uuid) != path:
                        return
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None
