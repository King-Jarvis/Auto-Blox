# Runs on the board, not here. Pushed and executed by scripts/ble-pad-probe.py.
#
# The one question this answers: can stock MicroPython on an ESP32 *bond* with a
# BLE gamepad and receive its HID reports? An Xbox Series X/S controller sends
# nothing until the link is encrypted and bonded, so everything else depends on
# this working. It needs no network, and nothing is assumed about the report
# layout: raw bytes are printed with their handles.
import bluetooth
import time
import ubinascii

HID_SERVICE = 0x1812
REPORT = 0x2A4D
REPORT_MAP = 0x2A4B
PROTOCOL_MODE = 0x2A4E

# ble.irq event numbers, spelled out because a bare 18 in a log is unreadable.
SCAN_RESULT, SCAN_DONE = 5, 6
CONNECT, DISCONNECT = 7, 8
SERVICE, SERVICE_DONE = 9, 10
CHAR, CHAR_DONE = 11, 12
DESC, DESC_DONE = 13, 14
READ, READ_DONE = 15, 16
WRITE_DONE, NOTIFY = 17, 18
GET_SECRET, SET_SECRET = 29, 30
ENCRYPTION_UPDATE = 28


def hexs(raw):
    return ubinascii.hexlify(bytes(raw), ":").decode()


def ad_fields(payload):
    """The advertisement, split into (type, bytes) — names and service UUIDs."""
    out, i = [], 0
    while i + 1 < len(payload):
        n = payload[i]
        if n == 0:
            break
        out.append((payload[i + 1], bytes(payload[i + 2:i + 1 + n])))
        i += 1 + n
    return out


def ad_name(payload):
    for kind, body in ad_fields(payload):
        if kind in (0x08, 0x09):            # shortened / complete local name
            try:
                return body.decode()
            except Exception:
                return repr(body)
    return ""


def ad_has_hid(payload):
    for kind, body in ad_fields(payload):
        if kind in (0x02, 0x03):            # 16-bit service class UUIDs
            for at in range(0, len(body) - 1, 2):
                if body[at] | (body[at + 1] << 8) == HID_SERVICE:
                    return True
    return False


class Probe:
    def __init__(self):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.seen = {}
        self.conn = None
        self.chars = []          # (value_handle, uuid)
        self.secrets = {}        # bonding keys, in RAM: one session is enough
        self.encrypted = False
        self.bonded = False
        self.reports = 0
        self.done_scanning = False
        self.services = []

    # -- setup ------------------------------------------------------------
    def secure(self):
        """Ask for bonding."""
        for kw in ({"le_secure": True, "mitm": False, "bond": True, "io": 3},
                   {"bond": True, "io": 3},
                   {"bond": True}):
            try:
                self.ble.config(**kw)
                print("SECURE ok %s" % kw)
                return True
            except Exception as exc:
                print("SECURE refused %s: %r" % (kw, exc))
        return False

    def irq(self, event, data):
        try:
            self.handle(event, data)
        except Exception as exc:          # an irq that raises tells you nothing
            print("IRQ %d raised %r" % (event, exc))

    def handle(self, event, data):
        if event == SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv = data
            key = bytes(addr)
            if key in self.seen:
                return
            name, hid = ad_name(adv), ad_has_hid(adv)
            self.seen[key] = (addr_type, name, rssi, hid)
            print("FOUND %s rssi=%-4d hid=%-5s %r"
                  % (hexs(addr), rssi, hid, name))
        elif event == SCAN_DONE:
            self.done_scanning = True
            print("SCAN done, %d device(s)" % len(self.seen))
        elif event == CONNECT:
            self.conn = data[0]
            print("CONNECTED handle=%d" % self.conn)
        elif event == DISCONNECT:
            print("DISCONNECTED handle=%s" % (data[0],))
            self.conn = None
        elif event == SERVICE:
            _c, start, end, uuid = data
            self.services.append((start, end, str(uuid)))
            print("SERVICE %s  handles %d-%d" % (uuid, start, end))
        elif event == SERVICE_DONE:
            print("SERVICE scan done")
        elif event == CHAR:
            _c, _end, value_handle, props, uuid = data
            self.chars.append((value_handle, str(uuid), props))
            print("CHAR %s handle=%d props=0x%02x" % (uuid, value_handle, props))
        elif event == CHAR_DONE:
            print("CHAR scan done")
        elif event == ENCRYPTION_UPDATE:
            _c, enc, auth, bonded, key_size = data
            self.encrypted, self.bonded = bool(enc), bool(bonded)
            print("ENCRYPTION encrypted=%s authenticated=%s bonded=%s keysize=%s"
                  % (enc, auth, bonded, key_size))
        elif event == NOTIFY:
            _c, handle, payload = data
            self.reports += 1
            if self.reports <= 40:
                print("REPORT handle=%d len=%d %s"
                      % (handle, len(payload), hexs(payload)))
        elif event == SET_SECRET:
            kind, key, value = data
            self.secrets[(kind, bytes(key))] = bytes(value)
            return True
        elif event == GET_SECRET:
            kind, index, key = data
            if key is None:
                items = [v for (k, _), v in self.secrets.items() if k == kind]
                return items[index] if index < len(items) else None
            return self.secrets.get((kind, bytes(key)))
        elif event == WRITE_DONE:
            print("WRITE done handle=%s status=%s" % (data[1], data[2]))


def run(want_name="", seconds=10):
    print("PROBE start, MicroPython BLE central")
    p = Probe()
    p.secure()
    p.ble.irq(p.irq)
    print("SCAN for %ds — hold the pad's pair button now" % seconds)
    p.ble.gap_scan(seconds * 1000, 30000, 30000, True)
    waited = 0
    while not p.done_scanning and waited < seconds + 4:
        time.sleep(1)
        waited += 1

    # Prefer something advertising HID; fall back to a name match.
    pick = None
    for addr, (addr_type, name, rssi, hid) in p.seen.items():
        if want_name and want_name.lower() in (name or "").lower():
            pick = (addr_type, addr, name)
            break
        if hid and pick is None:
            pick = (addr_type, addr, name)
    if pick is None:
        print("PROBE no HID advertiser found — is the pad in pairing mode?")
        print("PROBE end")
        return

    addr_type, addr, name = pick
    print("CONNECT to %s %r" % (hexs(addr), name))
    p.ble.gap_connect(addr_type, addr)
    for _ in range(100):
        if p.conn is not None:
            break
        time.sleep(0.1)
    if p.conn is None:
        print("PROBE never connected")
        print("PROBE end")
        return

    try:
        p.ble.gap_pair(p.conn)
        print("PAIR requested")
    except Exception as exc:
        print("PAIR not available: %r" % exc)
    for _ in range(100):
        if p.encrypted:
            break
        time.sleep(0.1)
    print("PAIR encrypted=%s bonded=%s" % (p.encrypted, p.bonded))

    p.ble.gattc_discover_services(p.conn)
    time.sleep(2)
    p.ble.gattc_discover_characteristics(p.conn, 1, 0xFFFF)
    time.sleep(3)

    # Subscribe to every notifying characteristic — the input report is one of
    # them and which one is exactly what is not assumed here.
    for handle, uuid, props in p.chars:
        if props & 0x10:                   # NOTIFY
            try:
                p.ble.gattc_write(p.conn, handle + 1, b"\x01\x00", 1)
                print("SUBSCRIBE handle=%d (%s)" % (handle, uuid))
            except Exception as exc:
                print("SUBSCRIBE handle=%d failed %r" % (handle, exc))
    print("LISTEN 20s — move the sticks")
    for _ in range(20):
        time.sleep(1)
        if p.conn is None:
            break
    print("PROBE reports=%d encrypted=%s bonded=%s" % (p.reports, p.encrypted, p.bonded))
    try:
        p.ble.gap_disconnect(p.conn)
    except Exception:
        pass
    print("PROBE end")
