"""Generic BLE on the host: the value format, the notification stream, and
the four nodes, against a stubbed BlueZ."""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows, gatt                          # noqa: E402
from zero2w_console.agent.modules import blefmt                 # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
CHAR = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/service000c/char000d"
# Exactly the shape gdbus monitor printed for a real signal on this board.
SIGNAL = ("%s: org.freedesktop.DBus.Properties.PropertiesChanged "
          "('org.bluez.GattCharacteristic1', {'Value': <[byte 0x2a, 0x00]>}, @as [])\n"
          % CHAR)


class TestTheFormat(unittest.TestCase):
    def test_every_number_goes_there_and_back(self):
        for fmt, value in (("uint8", 200), ("int8", -5), ("uint16-le", 4660),
                           ("int16-le", -2), ("uint32-le", 70000),
                           ("int32-le", -70000), ("float32-le", 21.5)):
            with self.subTest(fmt=fmt):
                self.assertEqual(blefmt.decode(fmt, blefmt.encode(fmt, value)), value)

    def test_little_endian_as_ble_sends_it(self):
        self.assertEqual(blefmt.decode("uint16-le", b"\x34\x12"), 0x1234)
        self.assertEqual(blefmt.encode("int16-le", -2), b"\xfe\xff")

    def test_text_from_a_template_is_a_number(self):
        self.assertEqual(blefmt.encode("uint8", "7"), b"\x07")
        self.assertEqual(blefmt.encode("uint8", "6.6"), b"\x07")

    def test_hex_forgives_spacing(self):
        for text in ("01ff", "01 ff", "01:ff", "0x01ff"):
            with self.subTest(text=text):
                self.assertEqual(blefmt.encode("hex", text), b"\x01\xff")
        self.assertEqual(blefmt.decode("hex", b"\x01\xff"), "01ff")

    def test_utf8(self):
        self.assertEqual(blefmt.decode("utf8", b"zero2w"), "zero2w")
        self.assertEqual(blefmt.encode("utf8", "hi"), b"hi")

    def test_what_cannot_be_right_is_refused(self):
        for fmt, value in (("uint8", 300), ("uint8", -1), ("uint8", "lots"),
                           ("hex", "zz"), ("hex", "123")):
            with self.subTest(fmt=fmt, value=value):
                with self.assertRaises(blefmt.FormatError):
                    blefmt.encode(fmt, value)
        with self.assertRaises(blefmt.FormatError):
            blefmt.decode("uint32-le", b"\x01\x02")
        with self.assertRaises(blefmt.FormatError):
            blefmt.decode("utf8", b"\xff\xfe")

    def test_the_dropdown_is_the_list(self):
        for ntype in ("ble.read", "ble.write", "ble.notify"):
            field = [f for f in flows.REGISTRY[ntype]["fields"] if f["key"] == "format"][0]
            self.assertEqual(field["options"], list(blefmt.FORMATS))


class TestTheSignal(unittest.TestCase):
    def test_a_value_change(self):
        self.assertEqual(gatt.signal_value(SIGNAL), b"\x2a\x00")

    def test_other_properties_are_not_values(self):
        line = ("/org/bluez/hci0: org.freedesktop.DBus.Properties.PropertiesChanged "
                "('org.bluez.Adapter1', {'Pairable': <true>}, @as [])")
        self.assertIsNone(gatt.signal_value(line))

    def test_the_monitor_s_own_chatter_is_ignored(self):
        self.assertIsNone(gatt.signal_value(
            "Monitoring signals on object %s owned by org.bluez" % CHAR))
        self.assertIsNone(gatt.signal_value("The name org.bluez is owned by :1.6"))


class FakeProc:
    """A monitor whose output the test writes into."""

    def __init__(self):
        r, self.w = os.pipe()
        self.stdout = os.fdopen(r, "r")
        self.done = False

    def say(self, line):
        os.write(self.w, line.encode())

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        if not self.done:
            self.done = True
            os.close(self.w)

    def wait(self, timeout=None):
        return 0


class TestTheSubscription(unittest.TestCase):
    def setUp(self):
        self.saved = (gatt.find_char, gatt.hold_notify, gatt.release_notify,
                      gatt._monitor)
        self.path = CHAR
        self.proc = FakeProc()
        self.subscribed = []
        gatt.find_char = lambda mac, uuid, *a, **kw: self.path
        gatt.hold_notify = lambda path: self.subscribed.append(True) or "holder"
        gatt.release_notify = lambda holder: self.subscribed.append(False)
        gatt._monitor = lambda path: self.proc
        self.got, self.states = [], []
        self.sub = gatt.Subscription(MAC, "2a37", self.got.append,
                                     on_state=self.states.append)
        self.sub.WAIT = 0.05

    def tearDown(self):
        self.sub.stop()
        self.sub.join(2)
        (gatt.find_char, gatt.hold_notify, gatt.release_notify,
         gatt._monitor) = self.saved

    def wait_for(self, check, seconds=2):
        end = time.monotonic() + seconds
        while not check() and time.monotonic() < end:
            time.sleep(0.02)
        return check()

    def test_values_arrive_as_they_are_sent(self):
        self.sub.start()
        self.assertTrue(self.wait_for(lambda: self.states == [True]))
        self.proc.say(SIGNAL)
        self.assertTrue(self.wait_for(lambda: self.got == [b"\x2a\x00"]))
        self.assertEqual(self.subscribed, [True])

    def test_a_dropped_link_goes_back_to_waiting(self):
        self.sub.start()
        self.assertTrue(self.wait_for(lambda: self.states == [True]))
        self.path = None                       # BlueZ took the object away
        self.assertTrue(self.wait_for(lambda: self.states[-1:] == [False], 4))
        self.assertIn(False, self.subscribed, "it did not unsubscribe")

    def test_nothing_happens_while_the_device_is_away(self):
        self.path = None
        self.sub.start()
        time.sleep(0.3)
        self.assertEqual((self.states, self.subscribed), ([], []))


class BleEngine(unittest.TestCase):
    """The engine with gatt stubbed at the functions it calls."""

    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.said = []
        self.e._emit = lambda fid, nid, level, message: self.said.append((level, message))
        self.saved = {n: getattr(flows.gattmod, n) for n in
                      ("tree", "find_char", "is_connected", "read", "write",
                       "connect", "disconnect", "devices", "Subscription")}
        g = flows.gattmod
        self.connected = True
        self.writes, self.calls = [], []
        g.tree = lambda: {}
        g.find_char = lambda mac, uuid, **kw: CHAR if self.connected and uuid == "2a19" else None
        g.is_connected = lambda mac, known=None: self.connected
        g.read = lambda path: b"\x57"
        g.write = lambda path, data: self.writes.append(data) or True
        g.connect = lambda mac, timeout=25: (self.calls.append("connect"),
                                            {"ok": True, "detail": "connected"})[1]
        g.disconnect = lambda mac: self.calls.append("disconnect")
        g.devices = lambda known=None: [{"mac": MAC, "name": "Polar H10",
                                         "connected": self.connected}]

    def tearDown(self):
        for n, fn in self.saved.items():
            setattr(flows.gattmod, n, fn)

    def run_node(self, ntype, cfg, payload=1):
        node = {"id": "n", "type": ntype, "config": cfg}
        return self.e._execute({"id": "f", "name": "f"}, node,
                               {"payload": payload, "meta": {}})


class TestTheNodes(BleEngine):
    def test_link_connects_on_truthy_and_only_when_needed(self):
        self.connected = False
        _p, out = self.run_node("ble.link", {"device": MAC})
        self.assertEqual(self.calls, ["connect"])
        self.assertTrue(out["meta"]["ble_connected"])
        self.connected = True
        self.run_node("ble.link", {"device": MAC})
        self.assertEqual(self.calls, ["connect"], "it reconnected a live link")

    def test_link_disconnects_on_falsy(self):
        _p, out = self.run_node("ble.link", {"device": MAC}, payload=0)
        self.assertEqual(self.calls, ["disconnect"])
        self.assertFalse(out["meta"]["ble_connected"])

    def test_a_device_can_be_named_rather_than_addressed(self):
        _p, out = self.run_node("ble.link", {"device": "polar"})
        self.assertEqual(out["meta"]["ble_device"], MAC)

    def test_read_decodes(self):
        _p, out = self.run_node("ble.read", {"device": MAC, "char": "2a19",
                                             "format": "uint8"})
        self.assertEqual(out["payload"], 87)
        self.assertEqual(out["meta"]["hex"], "57")

    def test_read_while_disconnected_stops_and_says_so(self):
        self.connected = False
        _p, out = self.run_node("ble.read", {"device": MAC, "char": "2a19"})
        self.assertIsNone(out)
        self.assertTrue([m for lvl, m in self.said if "not connected" in m])

    def test_read_of_a_characteristic_it_does_not_have(self):
        _p, out = self.run_node("ble.read", {"device": MAC, "char": "ffff"})
        self.assertIsNone(out)
        self.assertTrue([m for lvl, m in self.said if "no characteristic" in m])

    def test_write_encodes_the_value(self):
        _p, out = self.run_node("ble.write", {"device": MAC, "char": "2a19",
                                              "format": "uint16-le",
                                              "value": "{{payload}}"}, payload=258)
        self.assertEqual(self.writes, [b"\x02\x01"])
        self.assertEqual(out["meta"]["written"], "0201")
        self.assertEqual(out["payload"], 258)

    def test_a_value_that_does_not_fit_is_never_written(self):
        _p, out = self.run_node("ble.write", {"device": MAC, "char": "2a19",
                                              "format": "uint8", "value": "300"})
        self.assertIsNone(out)
        self.assertEqual(self.writes, [])

    def test_notify_fires_the_decoded_value(self):
        fired = []
        self.e.fire = lambda key, msg: fired.append((key, msg))
        self.e._ble_notified(("f", "n"), {"char": "2a37", "format": "uint16-le"},
                             MAC, b"\x2a\x00")
        self.assertEqual(fired[0][1]["payload"], 42)
        self.assertEqual(fired[0][1]["meta"]["ble_device"], MAC)

    def test_notify_is_armed_as_a_subscription_and_torn_down(self):
        made = []

        class FakeSub:
            def __init__(self, mac, uuid, deliver, on_state=None):
                made.append((mac, uuid))
                self.stopped = False

            def start(self):
                pass

            def stop(self):
                self.stopped = True

        flows.gattmod.Subscription = FakeSub
        self.e._arm({"id": "f", "name": "f", "nodes": [
            {"id": "n", "type": "ble.notify",
             "config": {"device": MAC, "char": "2a37", "format": "hex"}}],
            "edges": []})
        self.assertEqual(made, [(MAC, "2a37")])
        sub = [w for w in self.e.watchers if isinstance(w, FakeSub)][0]
        self.e._teardown()
        self.assertTrue(sub.stopped)


class TestTheSubscriptionIsHeld(unittest.TestCase):
    def test_it_is_held_by_a_session_that_stays_running(self):
        """A one-off StartNotify ends when its caller exits: measured, the
        monitor saw Notifying go true and straight back to false."""
        with open(os.path.join(ROOT, "zero2w_console", "gatt.py")) as fh:
            src = fh.read()
        run = src[src.index("    def run(self):"):]
        run = run[:run.index("    def _follow")]
        self.assertIn("hold_notify(path)", run)
        self.assertNotIn("notify(path, True)", run)


class TestTheBluetoothTabHoldsItToo(unittest.TestCase):
    def test_the_route_keeps_the_session(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            src = fh.read()
        at = src.index('if verb == "notify":')
        block = src[at:at + 900]
        self.assertIn("gattmod.hold_notify(path)", block)
        self.assertIn("gattmod.release_notify(", block)
        self.assertNotIn("gattmod.notify(", block)
        self.assertIn("httpd.bt_holds = {}", src)


class TestTheEditor(unittest.TestCase):
    def test_the_device_field_lists_this_host_s_bluetooth(self):
        with open(os.path.join(ROOT, "zero2w_console", "static", "flows.js")) as fh:
            js = fh.read()
        self.assertIn("ble_devices: function", js)
        self.assertIn('api("/api/bt")', js)


if __name__ == "__main__":
    unittest.main()
