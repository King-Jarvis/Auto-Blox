"""The Output panel for a flow that runs on a board: the runner remembers what
each node sent, the report carries it, and the console hands it on."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_pwm import load_runner                    # noqa: E402
from zero2w_console import fleet as fleetmod                     # noqa: E402


class Agent:
    tags, tags_written = {}, {}

    def log(self, *a, **kw):
        pass


class TestTheRunnerRemembers(unittest.TestCase):
    def setUp(self):
        self.flowmod = load_runner()

    def tearDown(self):
        sys.modules.pop("machine", None)

    def test_the_last_message_per_node_and_port(self):
        r = self.flowmod.Runner({"id": "f", "nodes": [], "edges": []}, Agent())
        r._emit("a", {"payload": 1, "meta": {}}, 0)
        r._emit("a", {"payload": 2, "meta": {"edge": "rising"}}, 0)
        r._emit("gate", {"payload": 0.4, "meta": {}}, 0, "true")
        out = r.take_outputs()
        self.assertEqual(out["a"], ["out", 2, {"edge": "rising"}])
        self.assertEqual(out["gate"][0], "true")
        self.assertEqual(r.take_outputs(), {}, "asking again repeats nothing")

    def test_it_is_clipped_to_ride_a_report(self):
        r = self.flowmod.Runner({"id": "f", "nodes": [], "edges": []}, Agent())
        meta = dict(("k%d" % i, "v" * 100) for i in range(10))
        r._emit("a", {"payload": "x" * 500, "meta": meta}, 0)
        port, payload, kept = r.take_outputs()["a"]
        self.assertEqual(len(payload), 40)
        self.assertLessEqual(len(kept), 6)
        self.assertTrue(all(len(v) == 40 for v in kept.values()))

    def test_numbers_stay_numbers(self):
        self.assertEqual(self.flowmod._small(0.25), 0.25)
        self.assertIs(self.flowmod._small(True), True)
        self.assertIsNone(self.flowmod._small(None))


class Bus:
    def __init__(self):
        self.rows = []

    def publish(self, channel, row):
        self.rows.append(row)


class TestTheConsoleHandsItOn(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.f = fleetmod.Fleet(None, None, self.bus)
        self.dev = {"id": "dev_m", "flow": "flow_drive"}

    def test_stored_by_flow_and_node_and_marked(self):
        self.f._note_outputs(self.dev, {"dz": ["out", 0.3, {"raw": 0.4}]})
        row = self.f.outputs_snapshot()["flow_drive/dz"]
        self.assertEqual((row["payload"], row["meta"], row["port"]), (0.3, {"raw": 0.4}, "out"))
        self.assertTrue(row["from_report"])
        self.assertEqual(self.bus.rows[-1], row)

    def test_nonsense_is_ignored(self):
        self.f._note_outputs(self.dev, {"a": "nope", "b": [1, 2]})
        self.f._note_outputs(self.dev, "nope")
        self.f._note_outputs({"id": "d"}, {"a": ["out", 1, {}]})    # no flow
        self.assertEqual(self.f.outputs_snapshot(), {})

    def test_the_wiring(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            agent = fh.read()
        self.assertIn('"outputs": self.runner.take_outputs()', agent)
        self.assertIn('if hasattr(self.runner, "take_outputs") else {}', agent,
                      "a board whose flow runner is older than its agent must "
                      "still report")
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            self.assertIn("outputs.update(self.server.fleet.outputs_snapshot())", fh.read())
        with open(os.path.join(ROOT, "zero2w_console", "static", "flows.js")) as fh:
            self.assertIn('r.from_report === true', fh.read())
        with open(os.path.join(ROOT, "zero2w_console", "fleet.py")) as fh:
            src = fh.read()
        at = src.index("def record_event") if "def record_event" in src else 0
        self.assertNotIn("from_report", src[at:at + 2000] if at else "",
                         "a device's own event must not be able to set it")


if __name__ == "__main__":
    unittest.main()
