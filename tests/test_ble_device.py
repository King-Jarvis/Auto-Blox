"""BLE on a device: modules/ble.py driving a fake NimBLE that answers like the
peripheral fixture (scripts/ble_peripheral_device.py)."""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_pwm import load_runner                    # noqa: E402
from zero2w_console.agent.modules import blefmt                  # noqa: E402

ADDR = bytes([0x70, 0x4B, 0xCA, 0x00, 0x00, 0xBA])
READ_UUID = "f0de0002-7a2b-4c3d-9e5f-0a1b2c3d4e5f"
WRITE_UUID = "f0de0003-7a2b-4c3d-9e5f-0a1b2c3d4e5f"
NOTIFY_UUID = "f0de0004-7a2b-4c3d-9e5f-0a1b2c3d4e5f"
HANDLES = {READ_UUID: 16, WRITE_UUID: 18, NOTIFY_UUID: 20}


class FakeUUID:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return "UUID('%s')" % self.text


class FakeBLE:
    """Queues each answer, delivered by pump() the way NimBLE schedules them."""

    def __init__(self):
        self.queue, self.irq_fn, self.values = [], None, {16: b"zero2w", 18: b"\x00"}
        self.writes, self.advertising, self.connected = [], True, False
        self.on = False

    def active(self, on=None):
        if on is not None:
            self.on = bool(on)
            if not on:
                # Switching off drops the link without a disconnect event.
                self.connected, self.queue = False, []
        return self.on

    def irq(self, fn):
        self.irq_fn = fn

    def gap_scan(self, duration, *a):
        if duration is None:
            return
        if self.advertising:
            adv = bytes([2, 1, 6, 12, 9]) + b"zero2w-gatt"
            self.queue.append((5, (1, ADDR, 0, -50, adv)))
        self.queue.append((6, ()))

    def gap_connect(self, kind, addr):
        self.connected = True
        self.queue.append((7, (0, kind, addr)))

    def gap_disconnect(self, conn):
        self.connected = False
        self.queue.append((8, (conn, 1, ADDR)))

    def gattc_discover_characteristics(self, conn, start, end):
        for uuid, handle in HANDLES.items():
            self.queue.append((11, (conn, handle, handle, 0x1a, FakeUUID(uuid))))
        self.queue.append((12, (conn, 0)))

    def gattc_read(self, conn, handle):
        self.queue.append((15, (conn, handle, memoryview(self.values[handle]))))
        self.queue.append((16, (conn, handle, 0)))

    def gattc_write(self, conn, handle, data, mode):
        self.writes.append((handle, bytes(data)))
        self.values[handle] = bytes(data)
        self.queue.append((17, (conn, handle, 0)))

    def notify(self, handle, data):
        self.queue.append((18, (0, handle, memoryview(data))))

    def pump(self):
        while self.queue:
            self.irq_fn(*self.queue.pop(0))


class Agent:
    tags, tags_written = {}, {}

    def __init__(self):
        self.said = []

    def log(self, level, message, **kw):
        self.said.append((level, message))


def node(nid, ntype, **cfg):
    return {"id": nid, "type": ntype, "config": cfg}


def edge(a, b):
    return {"id": a + b, "from": a, "fromPort": "out", "to": b, "toPort": "in"}


FLOW = {"id": "f", "nodes": [
    node("go", "manual.fire"), node("link", "ble.link", device="zero2w-gatt"),
    node("ask", "manual.fire"), node("read", "ble.read", device="zero2w-gatt",
                                      char=READ_UUID, format="utf8"),
    node("say", "manual.fire"), node("write", "ble.write", device="zero2w-gatt",
                                      char=WRITE_UUID, format="uint16-le", value="{{payload}}"),
    node("tick", "ble.notify", device="zero2w-gatt", char=NOTIFY_UUID, format="uint32-le"),
    node("got_read", "log.write"), node("got_write", "log.write"),
    node("got_tick", "log.write")],
    "edges": [edge("go", "link"), edge("ask", "read"), edge("say", "write"),
              edge("read", "got_read"), edge("write", "got_write"),
              edge("tick", "got_tick")]}


class DeviceBleCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBLE()
        bt = type(sys)("bluetooth")
        bt.BLE = lambda: self.fake
        sys.modules["bluetooth"] = bt
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.blefmt = blefmt
        sys.modules["modules"] = pkg
        sys.modules["modules.blefmt"] = blefmt
        spec = importlib.util.spec_from_file_location(
            "modules.ble", os.path.join(ROOT, "zero2w_console", "agent", "modules", "ble.py"))
        self.ble = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ble)
        pkg.ble = self.ble
        sys.modules["modules.ble"] = self.ble
        self.flowmod = load_runner()
        self.agent = Agent()
        self.r = self.flowmod.Runner(FLOW, self.agent)
        self.logged = []
        self.r._do_log_write = lambda nid, cfg, msg, hops: self.logged.append((nid, msg))
        self.r.start()

    def tearDown(self):
        for name in ("bluetooth", "modules", "modules.blefmt", "modules.ble", "machine"):
            sys.modules.pop(name, None)

    def run_for(self, ticks=20):
        for _ in range(ticks):
            self.r.tick()
            self.fake.pump()

    def connect(self):
        self.r.fire("go")
        self.run_for()
        self.assertEqual(self.ble.S["phase"], self.ble.READY, self.agent.said)

    def got(self, nid):
        return [m for n, m in self.logged if n == nid]


class TestTheLink(DeviceBleCase):
    def test_it_finds_the_device_by_name_connects_and_learns_its_characteristics(self):
        self.connect()
        self.assertEqual(self.ble.S["mac"], "70:4B:CA:00:00:BA")
        self.assertEqual(self.ble.S["chars"][READ_UUID][0], 16)
        self.assertIn(("ok", "BLE connected to 70:4B:CA:00:00:BA, 3 characteristics"),
                      self.agent.said)

    def test_a_falsy_message_disconnects(self):
        self.connect()
        self.r.queue.append(("link", {"payload": 0, "meta": {}}, 0, 0, "in"))
        self.run_for(10)
        self.assertFalse(self.fake.connected)
        self.assertIsNone(self.ble.S["want"])

    def test_nothing_advertising_means_no_connection_and_no_error(self):
        self.fake.advertising = False
        self.r.fire("go")
        self.run_for()
        self.assertNotEqual(self.ble.S["phase"], self.ble.READY)


    def test_stopping_the_flow_lets_go_of_the_link_and_the_radio(self):
        """A new flow starts without a reboot: the old link must not stay up."""
        self.connect()
        self.r.stop()
        self.assertFalse(self.fake.connected)
        self.assertFalse(self.fake.on)
        self.assertEqual(self.ble.S, {})

    def test_the_next_flow_brings_the_radio_back(self):
        self.connect()
        self.r.stop()
        self.r = self.flowmod.Runner(FLOW, self.agent)
        self.r._do_log_write = lambda nid, cfg, msg, hops: None
        self.r.start()
        self.connect()
        self.assertTrue(self.fake.on)


class TestReadWriteNotify(DeviceBleCase):
    def test_read_decodes(self):
        self.connect()
        self.r.fire("ask")
        self.run_for(10)
        msg = self.got("got_read")[0]
        self.assertEqual(msg["payload"], "zero2w")
        self.assertEqual(msg["meta"]["hex"], "7a65726f3277")

    def test_write_encodes(self):
        self.connect()
        self.r.fire("say", 4660)
        self.run_for(10)
        self.assertIn((18, b"\x34\x12"), self.fake.writes)
        self.assertEqual(self.got("got_write")[0]["meta"]["written"], "3412")

    def test_notify_subscribes_and_fires_per_value(self):
        self.connect()
        self.run_for(10)
        self.assertIn((21, b"\x01\x00"), self.fake.writes, "the CCCD was never written")
        for n in (7, 8, 9):
            self.fake.notify(20, n.to_bytes(4, "little"))
            # Each hop is timestamped in ms and may wait a tick: three hops.
            self.run_for(8)
        self.assertEqual([m["payload"] for m in self.got("got_tick")], [7, 8, 9])
        self.assertEqual(self.got("got_tick")[0]["meta"]["ble_device"], "70:4B:CA:00:00:BA")

    def test_reads_are_one_at_a_time(self):
        self.connect()
        for _ in range(3):
            self.r.fire("ask")
        self.run_for(10)
        self.assertEqual(len(self.got("got_read")), 3)

    def test_before_the_link_is_up_a_read_says_why_and_stops(self):
        self.r.fire("ask")
        self.run_for(8)
        self.assertEqual(self.got("got_read"), [])
        self.assertTrue([t for l, t in self.agent.said if "not connected" in t])

    def test_while_the_link_is_coming_up_a_read_says_so(self):
        self.fake.advertising = False          # so it stays looking
        self.r.fire("go")
        self.run_for(2)
        self.r.fire("ask")
        self.run_for(2)
        self.assertIn(("idle", "still connecting to zero2w-gatt; skipped"),
                      self.agent.said)


class TestTheNames(unittest.TestCase):
    def test_uuids_normalise_like_the_host(self):
        sys.modules["bluetooth"] = type(sys)("bluetooth")
        pkg = type(sys)("modules")
        pkg.blefmt = blefmt
        sys.modules["modules"] = pkg
        try:
            spec = importlib.util.spec_from_file_location(
                "ble_norm", os.path.join(ROOT, "zero2w_console", "agent", "modules", "ble.py"))
            ble = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ble)
            from zero2w_console import gatt
            for u in ("UUID(0x2a19)", "0x2A19", "2a19",
                      "00002a19-0000-1000-8000-00805f9b34fb", "UUID('%s')" % READ_UUID):
                with self.subTest(uuid=u):
                    self.assertEqual(ble.norm(u), gatt.short_uuid(
                        u.replace("UUID(", "").replace(")", "").replace("'", "")
                         .replace("0x", "").replace("0X", "").lower()))
        finally:
            sys.modules.pop("bluetooth", None)
            sys.modules.pop("modules", None)


if __name__ == "__main__":
    unittest.main()
