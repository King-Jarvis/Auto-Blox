"""Pairing from the console: parsing, sequencing and refusals.

`bluetoothctl` and D-Bus are stubbed throughout; nothing here touches the radio.
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import bluetooth as bt                   # noqa: E402
from zero2w_console import gatt                              # noqa: E402
from tests.test_gatt import DUALSHOCK, STRAP                 # noqa: E402

DEVICES = """Device 44:16:22:11:00:AA Xbox Wireless Controller
Device 00:1A:7D:DA:71:13 BT Speaker
Device 11:22:33:44:55:66 11-22-33-44-55-66
"""


def stub(mapping, calls=None):
    """Replace `_run` with a table keyed on the first argument."""
    def fake(args, timeout=8):
        if calls is not None:
            calls.append(list(args))
        key = args[0]
        if key == "--timeout":
            key = args[2]
        if key == "devices" and len(args) > 1:
            key = "devices " + args[1]
        out = mapping.get(key, "")
        return out(args) if callable(out) else out
    return fake


class BtCase(unittest.TestCase):
    def setUp(self):
        self.old = bt._run
        self.addCleanup(setattr, bt, "_run", self.old)
        bt.forget_cache()
        self.addCleanup(bt.forget_cache)


class TestReadingTheAdapter(BtCase):
    """Status comes off BlueZ's object tree, stubbed at `gatt._gdbus` so the
    real parsing, classification and cache run."""

    def stub_tree(self, tree):
        calls = []

        def fake(path, method, *args, **kw):
            calls.append([path, method])
            return (tree,)

        self.addCleanup(setattr, gatt, "_gdbus", gatt._gdbus)
        gatt._gdbus = fake
        bt._run = stub({})          # nothing may reach bluetoothctl
        return calls

    def test_it_reads_the_adapter(self):
        self.stub_tree(STRAP)
        s = bt.status()
        self.assertEqual(s["adapter"]["address"], "C4:5F:AC:00:00:1F")
        self.assertTrue(s["adapter"]["powered"])
        self.assertTrue(s["adapter"]["present"])

    def test_it_says_whether_this_host_could_be_a_peripheral(self):
        """The other direction — something connecting *to* the host — needs
        these, and whether they are there is read, not assumed."""
        self.stub_tree(STRAP)
        s = bt.status()
        self.assertTrue(s["adapter"]["can_advertise"])
        self.assertTrue(s["adapter"]["can_serve_gatt"])

    def test_a_device_carries_what_a_screen_needs(self):
        self.stub_tree(STRAP)
        d = bt.status()["devices"][0]
        self.assertEqual(d["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(d["name"], "Polar H10")
        self.assertTrue(d["paired"])
        self.assertTrue(d["le"])
        self.assertTrue(d["gatt"], "it has services and the screen should offer them")

    def test_bluez_classifies_the_pad_rather_than_the_name_matching_it(self):
        self.stub_tree(DUALSHOCK)
        d = bt.status()["devices"][0]
        self.assertTrue(d["pad"], "Icon: input-gaming is the honest signal")
        self.assertTrue(d["classic_hid"])
        self.assertFalse(d["gatt"],
                         "Classic HID has no GATT, so nothing should offer a browse")

    def test_no_adapter_says_so_rather_than_looking_empty(self):
        self.stub_tree({})
        s = bt.status()
        self.assertFalse(s["adapter"]["present"])
        self.assertIn("missing", s)
        self.assertEqual(s["devices"], [])

    def test_the_whole_tree_is_one_call(self):
        """One `GetManagedObjects`, however many devices and attributes."""
        calls = self.stub_tree(STRAP)
        bt.status()
        self.assertEqual(len(calls), 1, [c[-1] for c in calls])
        self.assertTrue(calls[0][1].endswith("GetManagedObjects"), calls[0])

    def test_a_second_read_inside_the_window_forks_nothing(self):
        calls = self.stub_tree(STRAP)
        bt.status()
        bt.status()
        self.assertEqual(len(calls), 1, "the cache did not hold")

    def test_forcing_it_reads_again(self):
        calls = self.stub_tree(STRAP)
        bt.status()
        bt.status(force=True)
        self.assertEqual(len(calls), 2)

    def test_a_tool_that_is_not_there_is_not_an_exception(self):
        import subprocess
        old = subprocess.run
        subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(OSError("nope"))
        try:
            self.assertEqual(bt._run(["show"]), "")
            self.assertFalse(bt.installed())
            self.assertFalse(bt.status(force=True)["adapter"]["present"])
        finally:
            subprocess.run = old


MAC = "44:16:22:11:00:AA"


class FakeCtl:
    """A bluetoothctl that answers a scripted conversation.

    `replies` maps a command to the lines it produces. Anything not mentioned
    produces silence, as a real one does when the device is not there.
    """

    def __init__(self, replies, advertises=True):
        self.replies = replies
        self.advertises = advertises
        self.sent = []
        self.lines = []
        self.stdin = self
        self.stdout = self
        self.killed = False

    # -- stdin ----------------------------------------------------------
    def write(self, text):
        cmd = text.strip()
        self.sent.append(cmd)
        if cmd == "scan on" and self.advertises:
            self.lines.append("[NEW] Device %s Xbox Wireless Controller" % MAC)
        for key, out in self.replies.items():
            if cmd.startswith(key):
                self.lines.extend(out)
        return len(text)

    def flush(self):
        pass

    # -- stdout, as an iterator that ends when the process is told to quit --
    def __iter__(self):
        at = 0
        while True:
            if at < len(self.lines):
                yield self.lines[at] + "\n"
                at += 1
                continue
            if "quit" in self.sent:
                return
            time.sleep(0.01)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class TestPairingHappensInsideALiveScan(BtCase):
    """BlueZ forgets an unpaired device when discovery ends, so a `pair` after
    a scan answers `not available`. Pairing has to happen inside the scan."""

    def fake(self, replies, advertises=True):
        made = []

        def popen(args):
            ctl = FakeCtl(replies, advertises)
            made.append(ctl)
            return ctl

        bt._popen = popen
        self.addCleanup(setattr, bt, "_popen", bt._popen)
        return made

    def test_it_holds_discovery_open_across_the_pair(self):
        made = self.fake({"pair": ["Pairing successful"],
                          "trust": ["Changing %s trust succeeded" % MAC],
                          "connect": ["Connection successful"]})
        out = bt.pair(MAC, find_seconds=2)
        self.assertTrue(out["ok"], out)
        sent = made[0].sent
        self.assertIn("scan on", sent)
        self.assertLess(sent.index("scan on"), sent.index("pair " + MAC),
                        "it paired before it started looking")
        self.assertLess(sent.index("pair " + MAC), sent.index("scan off"),
                        "discovery ended before the pair, which is the bug")

    def test_it_registers_an_agent_because_nobody_can_answer_a_prompt(self):
        made = self.fake({"pair": ["Pairing successful"],
                          "connect": ["Connection successful"]})
        bt.pair(MAC, find_seconds=2)
        self.assertIn("agent on", made[0].sent)
        self.assertIn("default-agent", made[0].sent)

    def test_it_trusts_before_connecting(self):
        made = self.fake({"pair": ["Pairing successful"],
                          "trust": ["Changing %s trust succeeded" % MAC],
                          "connect": ["Connection successful"]})
        bt.pair(MAC, find_seconds=2)
        sent = made[0].sent
        self.assertLess(sent.index("trust " + MAC), sent.index("connect " + MAC))

    def test_a_pad_that_never_advertises_is_told_to_go_into_pairing_mode(self):
        """Not "failed": there is one thing to do about it and it should say so."""
        self.fake({}, advertises=False)
        out = bt.pair(MAC, find_seconds=1)
        self.assertFalse(out["ok"])
        self.assertIn("pairing mode", out["detail"])

    def test_it_does_not_trust_something_that_would_not_pair(self):
        made = self.fake({"pair": ["Failed to pair: org.bluez.Error.Failed"]})
        out = bt.pair(MAC, find_seconds=2)
        self.assertFalse(out["ok"])
        self.assertNotIn("trust " + MAC, made[0].sent)
        self.assertIn("Failed to pair", out["detail"])

    def test_bluez_own_words_come_back(self):
        """"AuthenticationCanceled" tells someone to hold the pair button; a
        tidied "could not pair" does not."""
        self.fake({"pair": ["Failed to pair: org.bluez.Error.AuthenticationCanceled"]})
        out = bt.pair(MAC, find_seconds=2)
        self.assertIn("AuthenticationCanceled", out["detail"])

    def test_an_already_paired_device_still_gets_connected(self):
        made = self.fake({"pair": ["Failed to pair: org.bluez.Error.AlreadyExists"],
                          "trust": ["trust succeeded"],
                          "connect": ["Connection successful"]})
        out = bt.pair(MAC, find_seconds=2)
        self.assertTrue(out["ok"], out)
        self.assertIn("connect " + MAC, made[0].sent)

    def test_a_pair_that_will_not_connect_says_so_rather_than_claiming_success(self):
        self.fake({"pair": ["Pairing successful"],
                   "trust": ["trust succeeded"],
                   "connect": ["Failed to connect: org.bluez.Error.Failed"]})
        out = bt.pair(MAC, find_seconds=2)
        self.assertFalse(out["ok"])
        self.assertIn("Failed to connect", out["detail"])

    def test_the_session_always_closes(self):
        made = self.fake({}, advertises=False)
        bt.pair(MAC, find_seconds=1)
        self.assertIn("scan off", made[0].sent)
        self.assertIn("quit", made[0].sent)

    def test_a_bad_address_never_starts_a_session(self):
        made = self.fake({})
        self.assertFalse(bt.pair("nonsense")["ok"])
        self.assertEqual(made, [])

    def test_the_escapes_bluetoothctl_paints_are_not_part_of_the_answer(self):
        self.assertEqual(bt._clean("\x1b[0;94m[bluetooth]\x1b[0m# Pairing successful\r"),
                         "[bluetooth]# Pairing successful")


class TestTheScan(BtCase):
    def setUp(self):
        super().setUp()
        # Classifying what a scan found reads the tree; nothing here may touch a
        # real radio.
        self.addCleanup(setattr, gatt, "_gdbus", gatt._gdbus)
        gatt._gdbus = lambda path, method, *a, **kw: ({},)

    def test_it_is_bounded_by_timeout_rather_than_a_scan_off(self):
        """A scan left running is the state that would contend with the AP for
        as long as the console was up."""
        calls = []
        bt._run = stub({"scan": "", "devices": DEVICES}, calls)
        bt.scan(5)
        for _ in range(200):
            if not bt.scan_state()["running"]:
                break
            import time
            time.sleep(0.01)
        args = [c for c in calls if "scan" in c]
        self.assertTrue(args, calls)
        self.assertIn("--timeout", args[0])
        self.assertNotIn("off", args[0])

    def test_the_length_is_clamped(self):
        self.addCleanup(setattr, gatt, "_gdbus", gatt._gdbus)
        gatt._gdbus = lambda path, method, *a, **kw: ({},)
        for asked, want in ((0, 3), (1, 3), (999, bt.SCAN_MAX), (None, bt.SCAN_SECONDS)):
            bt._run = stub({"scan": "", "devices": ""})
            state = bt.scan(asked)
            with self.subTest(asked=asked):
                self.assertLessEqual(state["seconds_left"], want)
            for _ in range(200):
                if not bt.scan_state()["running"]:
                    break
                import time
                time.sleep(0.01)

    def test_a_second_scan_does_not_start_while_one_runs(self):
        calls = []
        bt._run = stub({"scan": lambda a: __import__("time").sleep(0.2) or "",
                        "devices": ""}, calls)
        bt.scan(4)
        bt.scan(4)
        bt.scan(4)
        for _ in range(300):
            if not bt.scan_state()["running"]:
                break
            import time
            time.sleep(0.01)
        self.assertEqual(len([c for c in calls if "scan" in c]), 1)

    def test_what_it_found_says_which_are_new(self):
        import time
        bt._run = stub({"scan": "", "devices": DEVICES})
        bt.scan(3)
        for _ in range(300):
            if not bt.scan_state()["running"]:
                break
            time.sleep(0.01)
        found = bt.scan_state()["found"]
        self.assertEqual(len(found), 3)
        self.assertFalse(any(f["new"] for f in found),
                         "everything was already known before the scan")


class TestTheRoutesAreWired(unittest.TestCase):
    """The wiring, which is easy to leave out and impossible to see."""

    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py"),
                  encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_status_route_exists(self):
        self.assertIn('if path == "/api/bt":', self.src)
        at = self.src.index('if path == "/api/bt":')
        self.assertIn("btmod.status()", self.src[at:at + 400])

    def bt_post_block(self):
        """The whole handler, bounded by the next route rather than a length."""
        at = self.src.index('if route.startswith("/api/bt/"):')
        end = self.src.index('\n        if route == ', at)
        return self.src[at:end]

    def test_every_action_has_a_route(self):
        block = self.bt_post_block()
        for verb in ("scan", "pair", "connect", "disconnect", "forget",
                     "read", "write", "notify"):
            with self.subTest(verb=verb):
                self.assertIn('"%s"' % verb, block)
        self.assertIn("no such bluetooth action", block,
                      "an unknown verb must 404 rather than fall through")

    def test_a_write_refuses_anything_that_is_not_hex(self):
        """A characteristic takes bytes; a flow or a form gives text. Guessing
        at what someone meant is how you write the wrong value to a device."""
        block = self.bt_post_block()
        self.assertIn("bytes.fromhex", block)
        self.assertIn("ValueError", block)

    def test_browsing_is_a_get_and_validates_the_address(self):
        at = self.src.index('if path == "/api/bt/gatt":')
        block = self.src[at:at + 900]
        self.assertIn("is_address", block)
        self.assertIn("gattmod.gatt", block)

    def test_a_device_token_cannot_reach_any_of_it(self):
        """A board holds a token. The console's Bluetooth is not a board's
        business, and the device whitelist is an explicit list for exactly this
        reason."""
        at = self.src.index("A device's own token reaches exactly these routes")
        block = self.src[at:at + 400]
        self.assertNotIn("/api/bt", block)

    def test_the_devices_route_carries_each_boards_pad(self):
        at = self.src.index('if path == "/api/iot/devices":')
        block = self.src[at:at + 600]
        self.assertIn("pad_state", block,
                      "the screen reads d.pad and nothing sends it")


class TestWhatTheScreenIsToldAboutTheRisk(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "static", "iot.js"),
                  encoding="utf-8") as fh:
            self.js = fh.read()

    def test_the_screen_says_the_scan_shares_the_wifi_chip(self):
        """The person pressing Scan is the person who would lose the fleet, so
        it is said on the screen rather than only in docs/RADIO.md."""
        at = self.js.index("function btAdapterWidget")
        block = self.js[at:at + 4000]
        self.assertIn("access point", block)
        self.assertIn("wlan0", block)

    def test_the_scan_is_a_button_and_not_part_of_the_poll(self):
        at = self.js.index("function poll(")
        block = self.js[at:self.js.index("\n  }", at)]
        self.assertIn("/api/bt", block, "the status is polled")
        self.assertNotIn("/api/bt/scan", block, "a scan must never be polled")

    def test_the_bluetooth_tab_exists_beside_the_others(self):
        self.assertIn('{ id: "bluetooth", label: "Bluetooth" }', self.js)
        self.assertIn('S.tab === "bluetooth" ? bluetoothTab()', self.js)

    def test_the_setup_screen_keeps_the_radio_first_and_adds_a_branch(self):
        radio = self.js.index("// 1 — the radio")
        controller = self.js.index("// 6 — a controller")
        self.assertLess(radio, controller)
        block = self.js[controller:controller + 1800]
        self.assertIn("stepRow(6", block)
        self.assertIn("optional", block,
                      "nothing above it needs Bluetooth and it should say so")

    def test_a_field_devices_controller_is_shown_as_its_own_claim(self):
        at = self.js.index("function btFieldWidget")
        block = self.js[at:at + 2500]
        self.assertIn("d.pad", block)
        self.assertIn("own", block,
                      "this host cannot see a device's BLE links and must not "
                      "imply that it can")


if __name__ == "__main__":
    unittest.main()
