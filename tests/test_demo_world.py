"""The demo world says only what it was told to, and nothing about this machine.

Each check runs scripts/demo.py's patches in a child process — they replace
module functions for good — and hands back what the console would put on
screen. The parent, which still has the real PATH, then looks for this
machine's own identifiers in it with the same check screenshots.py uses.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import demo  # noqa: E402
import screenshots  # noqa: E402

CHILD = r"""
import json, os, sys
sys.path.insert(0, %(scripts)r)
import demo
demo.prepare(%(base)r)
ctx = demo.patch_console()
from zero2w_console import server, iot, bluetooth, gatt
c = server.Collector()
out = {
    "path": os.environ["PATH"],
    "host": c.hostname, "user": c.user,
    "network": c.network(), "processes": c.processes(limit=50),
    "filesystems": c.filesystems(),
    "radio": iot.radio(), "ap": iot.ap_state(),
    "links": iot.link_candidates(), "scan": iot.scan(),
    "caps": iot.capabilities(),
    "bt": bluetooth.status(force=True),
    "gatt": gatt.gatt(demo.DEMO["pads"][0]["address"]),
    "shell": server.run_command("uptime", "/"),
    "shell_other": server.run_command("cat /etc/passwd", "/"),
    "logs": server.recent_logs(),
}
print(json.dumps(out))
"""

BOARD = r"""
import json, os, sys
sys.path.insert(0, %(scripts)r)
import demo
spec = dict(demo.DEMO["boards"][0], photo=None)
demo.install_micropython(spec)
sys.path.insert(0, %(agent)r)
import agent
agent.load_json = lambda path, default=None: {"device": "dev_x", "host": "http://x"}
a = agent.Agent()
out = {"probe": a.probe(), "ip": a.radio() and a.ip(), "rssi": a.rssi()}
try:
    import machine
    machine.reset()
    out["reset"] = "returned"
except BaseException as exc:
    out["reset"] = type(exc).__name__
print(json.dumps(out))
"""


def run(code):
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=120, cwd=ROOT)
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestTheConsoleSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="test-demo-")
        cls.got = run(CHILD % {"scripts": SCRIPTS, "base": cls.base})

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_the_host_is_the_demo_host(self):
        self.assertEqual(self.got["host"], demo.DEMO["host"])
        self.assertEqual(self.got["user"], demo.DEMO["user"])

    def test_only_the_demo_tools_are_on_the_path(self):
        self.assertEqual(self.got["path"], os.path.join(self.base, "bin"))
        for name in os.listdir(self.got["path"]):
            self.assertIn(name, demo.ALLOWED_TOOLS + ("hostapd",))
        with open(os.path.join(self.got["path"], "hostapd")) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\nexit 1\n")

    def test_the_network_is_the_demo_network(self):
        names = {i["name"]: i["address"] for i in self.got["network"]}
        self.assertEqual(names, {demo.DEMO["uplink"]["name"]: demo.DEMO["uplink"]["address"],
                                 "wlan0": demo.DEMO["gateway"] + "/24"})
        self.assertEqual([l["interface"] for l in self.got["links"]],
                         [demo.DEMO["uplink"]["name"], "wlan0"])

    def test_the_access_point_is_the_demo_one(self):
        ap = self.got["ap"]
        self.assertTrue(ap["up"])
        self.assertEqual(ap["ssid"], demo.DEMO["ssid"])
        self.assertEqual(ap["missing"], [])
        self.assertEqual(sorted(l["mac"] for l in ap["leases"]),
                         sorted(b["mac"] for b in demo.DEMO["boards"]))
        wlan = [i for i in self.got["radio"]["interfaces"] if i["name"] == "wlan0"][0]
        self.assertEqual(wlan["addr"], demo.DEMO["wlan_mac"])

    def test_the_usb_scan_is_read_by_the_real_parser(self):
        port, = self.got["scan"]["ports"]
        self.assertEqual(port["chip"], "ESP32")
        self.assertEqual(port["board"], "esp32")
        self.assertEqual(port["flash"], "4MB")

    def test_bluetooth_is_the_demo_adapter_and_devices(self):
        bt = self.got["bt"]
        self.assertEqual(bt["adapter"]["address"], demo.DEMO["bt"]["address"])
        self.assertEqual(sorted(d["name"] for d in bt["devices"]),
                         sorted(p["name"] for p in demo.DEMO["pads"]))
        self.assertIn("1812", [s["uuid"][4:8] for s in self.got["gatt"]])

    def test_the_terminal_never_reaches_a_shell(self):
        self.assertIn("load average", self.got["shell"]["lines"][0]["text"])
        self.assertEqual(self.got["shell"]["cwd"], demo.DEMO["home"])
        self.assertNotIn("root:", json.dumps(self.got["shell_other"]))

    def test_nothing_on_screen_belongs_to_this_machine(self):
        d = screenshots.Denylist(screenshots.real_identifiers(),
                                 allow_words=screenshots.demo_values(),
                                 allow_text=screenshots.shipped_text())
        text = json.dumps(self.got)
        hits = d.search(text)
        self.assertEqual(hits, [], "the demo showed %s" % sorted({k for k, _ in hits}))
        # And the demo's own working directory is not on screen either.
        shown = dict(self.got)
        del shown["path"]
        self.assertNotIn(self.base, json.dumps(shown))


class TestABoard(unittest.TestCase):
    def test_the_agent_believes_it_is_on_an_esp32(self):
        got = run(BOARD % {"scripts": SCRIPTS,
                           "agent": os.path.join(ROOT, "zero2w_console", "agent")})
        spec = demo.DEMO["boards"][0]
        self.assertEqual(got["probe"]["firmware"], "micropython 1.24.1")
        self.assertEqual(got["probe"]["chip"], "ESP32")
        self.assertEqual(got["probe"]["unique_id"], spec["mac"].replace(":", ""))
        self.assertEqual(got["ip"], spec["ip"])
        self.assertLess(abs(got["rssi"] - spec["rssi"]), 4)
        # A reboot must get past the agent's own `except Exception`.
        self.assertEqual(got["reset"], "_Reset")


class TestCheckPagesKnowsEveryTab(unittest.TestCase):
    def test_the_iot_tabs_match_the_page(self):
        import importlib.util
        import re
        spec = importlib.util.spec_from_file_location(
            "check_pages", os.path.join(SCRIPTS, "check-pages.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with open(os.path.join(ROOT, "zero2w_console", "static", "iot.js")) as fh:
            js = fh.read()
        block = js[js.index("var TABS = ["):]
        block = block[:block.index("];")]
        self.assertEqual(mod.IOT_HASHES, re.findall(r'id: "([a-z]+)"', block))


if __name__ == "__main__":
    unittest.main()
