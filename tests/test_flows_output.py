"""What a node last produced, kept so the inspector can show it."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class Bus:
    """Just enough of the SSE bus to see what was published."""

    def __init__(self):
        self.sent = []

    def publish(self, kind, row):
        self.sent.append((kind, row))


def engine(bus=None):
    # No driver, no inventory, no threads: nothing here touches hardware.
    return flows.FlowEngine(flows.FlowStore(os.devnull), None, None, bus)


class TestClip(unittest.TestCase):
    def test_numbers_and_none_pass_through_untouched(self):
        for v in (None, 0, 1, -2.5, True, False):
            self.assertIs(flows._clip(v), v)

    def test_short_text_is_left_alone(self):
        self.assertEqual(flows._clip("hello"), "hello")

    def test_long_text_is_cut_and_says_how_much_is_missing(self):
        out = flows._clip("x" * 450)
        self.assertTrue(out.startswith("x" * 400))
        self.assertIn("50 more", out)
        self.assertLess(len(out), 450)

    def test_structures_become_json_first(self):
        self.assertEqual(flows._clip({"a": 1}), '{"a": 1}')


class TestRecordOutput(unittest.TestCase):
    def test_payload_and_meta_are_kept(self):
        e = engine()
        e._record_output("f", "n", {"payload": "hi", "meta": {"edge": "rising"}})
        row = e.outputs()["f/n"]
        self.assertEqual(row["payload"], "hi")
        self.assertEqual(row["meta"], {"edge": "rising"})
        self.assertNotIn("stopped", row)

    def test_a_node_that_passed_nothing_on_is_marked_stopped(self):
        """An If that did not match is not the same as a node with no value."""
        e = engine()
        e._record_output("f", "n", None, "out")
        row = e.outputs()["f/n"]
        self.assertTrue(row["stopped"])
        self.assertNotIn("payload", row)

    def test_the_port_is_kept_so_a_branch_can_be_told_apart(self):
        e = engine()
        e._record_output("f", "n", {"payload": 1}, "false")
        self.assertEqual(e.outputs()["f/n"]["port"], "false")

    def test_only_the_latest_is_kept_per_node(self):
        e = engine()
        e._record_output("f", "n", {"payload": "first"})
        e._record_output("f", "n", {"payload": "second"})
        self.assertEqual(len(e.outputs()), 1)
        self.assertEqual(e.outputs()["f/n"]["payload"], "second")

    def test_nodes_in_different_flows_do_not_collide(self):
        e = engine()
        e._record_output("one", "n", {"payload": "a"})
        e._record_output("two", "n", {"payload": "b"})
        self.assertEqual(e.outputs()["one/n"]["payload"], "a")
        self.assertEqual(e.outputs()["two/n"]["payload"], "b")

    def test_a_big_payload_is_clipped_before_it_reaches_the_bus(self):
        bus = Bus()
        e = engine(bus)
        e._record_output("f", "n", {"payload": "y" * 5000})
        kind, row = bus.sent[-1]
        self.assertEqual(kind, "flow")
        self.assertEqual(row["kind"], "output")
        self.assertLess(len(row["payload"]), 500)

    def test_meta_is_capped_so_one_node_cannot_flood_the_panel(self):
        e = engine()
        wide = {"k%d" % i: i for i in range(40)}
        e._record_output("f", "n", {"payload": 1, "meta": wide})
        self.assertEqual(len(e.outputs()["f/n"]["meta"]), 8)

    def test_meta_that_is_not_a_dict_is_ignored_rather_than_crashing(self):
        e = engine()
        e._record_output("f", "n", {"payload": 1, "meta": "not a dict"})
        self.assertIsNone(e.outputs()["f/n"]["meta"])

    def test_outputs_are_keyed_as_strings_so_they_survive_json(self):
        import json
        e = engine()
        e._record_output("f", "n", {"payload": 1})
        self.assertEqual(json.loads(json.dumps(e.outputs()))["f/n"]["payload"], 1)


if __name__ == "__main__":
    unittest.main()
