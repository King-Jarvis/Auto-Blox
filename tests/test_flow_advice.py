"""Mistakes a flow is allowed to contain, and ought to be told about."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples                 # noqa: E402
from zero2w_console import flows as flowmod         # noqa: E402


def flow(every, timeout, chain=True):
    """A timer feeding a watchdog, through a couple of nodes when `chain`."""
    nodes = [{"id": "t", "type": "timer.interval", "config": {"every": every}},
             {"id": "d", "type": "safety.watchdog",
              "config": {"timeout": timeout, "value": "0"}}]
    edges = []
    if chain:
        nodes.insert(1, {"id": "m", "type": "math.deadband",
                         "config": {"threshold": 0.05}})
        edges = [{"from": "t", "to": "m"}, {"from": "m", "to": "d"}]
    else:
        edges = [{"from": "t", "to": "d"}]
    return {"id": "f", "name": "F", "nodes": nodes, "edges": edges}


def levels(doc):
    return [a["level"] for a in flowmod.advice(doc)]


class TestAWatchdogFasterThanItsFeeder(unittest.TestCase):
    def test_the_case_that_cost_the_day(self):
        notes = flowmod.advice(flow(1000, 400))
        self.assertEqual([n["level"] for n in notes], ["critical"])
        self.assertIn("400", notes[0]["message"])
        self.assertIn("1000", notes[0]["message"])
        self.assertEqual(notes[0]["node"], "d")

    def test_it_reaches_through_the_whole_chain_not_just_one_edge(self):
        """The timer is four nodes upstream in the real flow."""
        doc = flow(1000, 400)
        doc["nodes"].insert(1, {"id": "a", "type": "logic.if",
                                "config": {"op": ">=", "value": "0.5"}})
        doc["nodes"].insert(2, {"id": "b", "type": "tag.read",
                                "config": {"tag": "x"}})
        doc["edges"] = [{"from": "t", "to": "a"}, {"from": "a", "to": "b"},
                        {"from": "b", "to": "m"}, {"from": "m", "to": "d"}]
        self.assertEqual(levels(doc), ["critical"])

    def test_equal_is_still_wrong(self):
        """Fed exactly as often as it gives up is a coin toss every cycle."""
        self.assertEqual(levels(flow(400, 400)), ["critical"])

    def test_a_thin_margin_is_a_warning_not_a_refusal(self):
        self.assertEqual(levels(flow(400, 600)), ["warn"])

    def test_a_real_margin_says_nothing(self):
        self.assertEqual(flowmod.advice(flow(100, 400)), [])

    def test_the_shipped_motor_example_is_clean(self):
        """If this ever fails, the example is teaching the bug."""
        for fid in ("ex_motor_drive", "ex_motor_bench", "ex_motor_arm"):
            with self.subTest(flow=fid):
                self.assertEqual(flowmod.advice(examples.by_id(fid)), [])

    def test_every_shipped_example_is_clean(self):
        for ex in examples.catalogue():
            with self.subTest(flow=ex["id"]):
                self.assertEqual(flowmod.advice(ex), [])


class TestItDoesNotInventProblems(unittest.TestCase):
    def test_a_watchdog_fed_by_a_pin_says_nothing(self):
        """No timer upstream means no knowable cadence."""
        doc = {"id": "f", "nodes": [
            {"id": "g", "type": "gpio.in", "config": {"gpio": 4}},
            {"id": "d", "type": "safety.watchdog", "config": {"timeout": 50}}],
            "edges": [{"from": "g", "to": "d"}]}
        self.assertEqual(flowmod.advice(doc), [])

    def test_a_watchdog_nothing_feeds_says_nothing(self):
        doc = {"id": "f", "nodes": [
            {"id": "d", "type": "safety.watchdog", "config": {"timeout": 50}}],
            "edges": []}
        self.assertEqual(flowmod.advice(doc), [])

    def test_a_flow_with_no_watchdog_says_nothing(self):
        self.assertEqual(flowmod.advice(examples.by_id("ex_blink")), [])

    def test_the_fastest_feeder_is_the_one_that_counts(self):
        """Two timers into one watchdog: it is fed whenever either fires, so
        the quick one decides whether it ever starves."""
        doc = flow(1000, 400, chain=False)
        doc["nodes"].append({"id": "t2", "type": "timer.interval",
                             "config": {"every": 50}})
        doc["edges"].append({"from": "t2", "to": "d"})
        self.assertEqual(flowmod.advice(doc), [])


class TestItSurvivesRubbish(unittest.TestCase):
    def test_nothing_at_all(self):
        for doc in (None, {}, {"nodes": []}, {"nodes": [], "edges": []}):
            with self.subTest(doc=doc):
                self.assertEqual(flowmod.advice(doc), [])

    def test_missing_and_silly_numbers(self):
        for every, timeout in ((None, 400), (1000, None), ("x", "y"),
                               (0, 0), (-5, -5), ("1000", "400")):
            with self.subTest(every=every, timeout=timeout):
                flowmod.advice(flow(every, timeout))      # must not raise

    def test_a_cycle_in_the_graph_terminates(self):
        doc = flow(1000, 400)
        doc["edges"].append({"from": "d", "to": "m"})     # back round
        self.assertEqual(levels(doc), ["critical"])

    def test_a_node_with_no_id_does_not_break_it(self):
        doc = flow(1000, 400)
        doc["nodes"].append({"type": "math.scale"})
        self.assertEqual(levels(doc), ["critical"])


if __name__ == "__main__":
    unittest.main()
