"""BLE on a device: one central connection, driven by the BLE nodes.

The runner is polled, so nothing here waits. poll() starts a scan or the next
GATT operation, and the irq's answer moves it on; values reach the flow from
poll(), never from the irq.
"""
import bluetooth
from modules import blefmt

try:
    import ubinascii as binascii
except ImportError:
    import binascii

SCAN_RESULT, SCAN_DONE, CONNECT, DISCONNECT = 5, 6, 7, 8
CHAR_RESULT, CHAR_DONE = 11, 12
READ_RESULT, READ_DONE, WRITE_DONE, NOTIFY = 15, 16, 17, 18

# The link's phases, and the kinds of event the irq leaves for poll().
IDLE, SCAN, FOUND, CONNECTING, DISCOVER, READY = range(6)
GONE, UP, READ, WROTE, NOTIFIED = range(5)
DO_READ, DO_WRITE, DO_SUB = range(3)

RETRY_MS = 3000     # between attempts to find and connect
MAX_OPS = 8         # queued reads and writes; more are refused, not stored

_ble = None
S = {}


def _reset(r):
    S.clear()
    S.update({"runner": r, "want": None, "phase": IDLE, "conn": None,
              "addr": None, "mac": "", "name": "", "chars": {}, "ops": [],
              "busy": None, "got": None, "events": [], "subbed": {},
              "tried": 0, "said": ""})


def _mac(addr):
    return binascii.hexlify(bytes(addr), ":").decode().upper()


def norm(uuid):
    """A UUID as the host's short_uuid writes it: 2a19, or 128 bits whole."""
    s = str(uuid).lower()
    for junk in ("uuid(", ")", "'", '"', "0x"):
        s = s.replace(junk, "")
    s = s.strip()
    if len(s) == 36 and s.endswith("-0000-1000-8000-00805f9b34fb") and s[:4] == "0000":
        return s[4:8]
    return s


def _name(adv):
    i, adv = 0, bytes(adv)
    while i + 1 < len(adv):
        n = adv[i]
        if n == 0:
            break
        if adv[i + 1] in (0x08, 0x09):
            try:
                return adv[i + 2:i + 1 + n].decode()
            except Exception:
                return ""
        i += 1 + n
    return ""


def _wanted(mac, name):
    want = S["want"] or ""
    if len(want) == 17 and want.count(":") == 5:
        return want.upper() == mac
    return bool(want) and want.lower() in (name or "").lower()


def _irq(event, data):
    if not S:                            # stopped; the radio is going down
        return
    if event == SCAN_RESULT:
        kind, addr, _adv_type, _rssi, adv = data
        if S.get("phase") == SCAN:
            mac, name = _mac(addr), _name(adv)
            if _wanted(mac, name):
                S["addr"], S["mac"], S["name"] = (kind, bytes(addr)), mac, name
                S["phase"] = FOUND
                _ble.gap_scan(None)
    elif event == SCAN_DONE:
        if S.get("phase") == FOUND:
            S["phase"] = CONNECTING
            try:
                _ble.gap_connect(S["addr"][0], S["addr"][1])
            except OSError:
                S["phase"] = IDLE
        elif S.get("phase") == SCAN:
            S["phase"] = IDLE
    elif event == CONNECT:
        S["conn"], S["phase"], S["chars"] = data[0], DISCOVER, {}
        _ble.gattc_discover_characteristics(data[0], 1, 0xffff)
    elif event == DISCONNECT:
        S.update({"conn": None, "phase": IDLE, "busy": None, "subbed": {}})
        S["events"].append((GONE, None, None))
    elif event == CHAR_RESULT:
        _conn, _end, value, props, uuid = data
        S["chars"][norm(uuid)] = (value, props)
    elif event == CHAR_DONE:
        S["phase"] = READY
        S["events"].append((UP, None, None))
    elif event == READ_RESULT:
        S["got"] = bytes(data[2])
    elif event == READ_DONE:
        S["events"].append((READ, data[1], data[2]))
    elif event == WRITE_DONE:
        S["events"].append((WROTE, data[1], data[2]))
    elif event == NOTIFY:
        S["events"].append((NOTIFIED, data[1], bytes(data[2])))


def _radio(r):
    global _ble
    if _ble is None:
        try:
            _ble = bluetooth.BLE()
            _ble.active(True)
            _ble.irq(_irq)
        except Exception as exc:
            _ble = None
            _say(r, "critical", "bluetooth would not start: %s" % exc)
    return _ble


def _say(r, level, text):
    if S.get("said") != text:
        S["said"] = text
        r.agent.log(level, text)


def _handle(r, node_id, cfg):
    """The value handle of the configured characteristic, or None, said once."""
    if S.get("phase") != READY:
        if S.get("want"):
            _say(r, "idle", "still connecting to %s; skipped" % S["want"])
        else:
            _say(r, "warn", "not connected — a BLE device node holds the connection")
        return None
    got = S["chars"].get(norm(cfg.get("char") or ""))
    if got is None:
        _say(r, "warn", "%s has no characteristic %s" % (S["mac"], cfg.get("char")))
    return got and got[0]


def _queue(r, op):
    if len(S["ops"]) >= MAX_OPS:
        _say(r, "warn", "BLE is %d operations behind; dropping" % MAX_OPS)
        return
    S["ops"].append(op)


def _meta(msg, **extra):
    meta = dict(msg.get("meta") or {})
    meta.update(extra)
    return meta


# -- the node handlers -------------------------------------------------------
def ble_link(r, node_id, cfg, msg, hops):
    if S.get("runner") is not r:
        _reset(r)
    if _radio(r) is None:
        return
    want = (cfg.get("device") or "").strip()
    if r.truthy(msg.get("payload")) and want:
        if S["want"] != want and S["conn"] is not None:
            _ble.gap_disconnect(S["conn"])
        S["want"] = want
    else:
        S["want"] = None
        if S["conn"] is not None:
            _ble.gap_disconnect(S["conn"])
    out = dict(msg)
    out["meta"] = _meta(msg, ble_connected=S["phase"] == READY,
                        ble_device=S["mac"] or want)
    r._emit(node_id, out, hops)


def ble_read(r, node_id, cfg, msg, hops):
    if S.get("runner") is not r:
        _reset(r)
    handle = _handle(r, node_id, cfg)
    if handle:
        _queue(r, {"do": DO_READ, "handle": handle, "node": node_id, "msg": msg,
                   "hops": hops, "cfg": cfg})


def ble_write(r, node_id, cfg, msg, hops):
    if S.get("runner") is not r:
        _reset(r)
    try:
        data = blefmt.encode(cfg.get("format") or "hex",
                             r.render(cfg.get("value", "{{payload}}"), msg))
    except blefmt.FormatError as exc:
        return _say(r, "warn", str(exc))
    handle = _handle(r, node_id, cfg)
    if handle:
        _queue(r, {"do": DO_WRITE, "handle": handle, "data": data, "node": node_id,
                   "msg": msg, "hops": hops, "cfg": cfg})


def ble_notify(r, node_id, cfg, msg, hops):
    r._emit(node_id, msg, hops)


# -- driven from the runner's tick --------------------------------------------
def stop(r):
    """The flow is going. A new flow starts without a reboot, so the link and
    the radio are let go here, or the old connection stays up holding memory."""
    global _ble
    S.clear()
    if _ble is not None:
        try:
            _ble.active(False)
        except Exception:
            pass
        _ble = None


def poll(r):
    if S.get("runner") is not r:
        _reset(r)
    if _ble is None and not S["want"]:
        return
    if S["want"] and S["phase"] == IDLE and r.since(S["tried"]) >= RETRY_MS:
        S["tried"] = r.ticks()
        S["phase"] = SCAN
        try:
            _ble.gap_scan(4000, 30000, 30000, True)
        except OSError:
            S["phase"] = IDLE
    while S["events"]:
        _event(r, *S["events"].pop(0))
    if S["phase"] == READY:
        _subscribe(r)
        if S["busy"] is None and S["ops"]:
            _start(r, S["ops"].pop(0))


def _subscribe(r):
    """Every BLE notify node gets its characteristic's notifications turned
    on, by writing 1 to the descriptor after its value (the usual CCCD)."""
    for node_id, node in r.nodes.items():
        if node.get("type") != "ble.notify":
            continue
        got = S["chars"].get(norm((node.get("config") or {}).get("char") or ""))
        if got and got[0] not in S["subbed"]:
            S["subbed"][got[0]] = []
            _queue(r, {"do": DO_SUB, "handle": got[0] + 1, "data": b"\x01\x00"})
        if got:
            nodes = S["subbed"].setdefault(got[0], [])
            if node_id not in nodes:
                nodes.append(node_id)


def _start(r, op):
    S["busy"], S["got"] = op, None
    try:
        if op["do"] == DO_READ:
            _ble.gattc_read(S["conn"], op["handle"])
        else:
            _ble.gattc_write(S["conn"], op["handle"], op["data"], 1)
    except OSError as exc:
        S["busy"] = None
        _say(r, "warn", "BLE %s failed: %s"
             % (("read", "write", "subscribe")[op["do"]], exc))


def _event(r, kind, handle, value):
    op = S["busy"]
    if kind == GONE:
        _say(r, "idle", "BLE link to %s dropped" % (S["mac"] or "device"))
    elif kind == UP:
        _say(r, "ok", "BLE connected to %s, %d characteristics"
             % (S["mac"], len(S["chars"])))
    elif kind == NOTIFIED:
        for node_id in S["subbed"].get(handle, []):
            cfg = (r.nodes.get(node_id) or {}).get("config") or {}
            try:
                got = blefmt.decode(cfg.get("format") or "hex", value)
            except blefmt.FormatError as exc:
                _say(r, "warn", str(exc))
                continue
            r.fire(node_id, got, {"uuid": cfg.get("char"), "hex": _hex(value),
                                  "ble_device": S["mac"]})
    elif kind in (READ, WROTE) and op and op["handle"] == handle:
        S["busy"] = None
        if value:                        # the status: 0 is success
            return _say(r, "warn", "BLE %s refused (status %s)"
                        % (("read", "write", "subscribe")[op["do"]], value))
        if op["do"] == DO_READ:
            try:
                got = blefmt.decode(op["cfg"].get("format") or "hex", S["got"] or b"")
            except blefmt.FormatError as exc:
                return _say(r, "warn", str(exc))
            out = dict(op["msg"])
            out["payload"] = got
            out["meta"] = _meta(op["msg"], uuid=op["cfg"].get("char"),
                                hex=_hex(S["got"] or b""))
            r._emit(op["node"], out, op["hops"])
        elif op["do"] == DO_WRITE:
            out = dict(op["msg"])
            out["meta"] = _meta(op["msg"], uuid=op["cfg"].get("char"),
                                written=_hex(op["data"]))
            r._emit(op["node"], out, op["hops"])


def _hex(data):
    return binascii.hexlify(bytes(data)).decode()
