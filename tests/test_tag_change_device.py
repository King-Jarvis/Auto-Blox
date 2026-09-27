"""Tag change on a device: the board's own copy of the tags, polled per tick."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_pwm import load_runner                  # noqa: E402
from zero2w_console import flows                              # noqa: E402
from zero2w_console.agent.modules import tagwatch             # noqa: E402


class Agent:
    def __init__(self, tags=None):
        self.tags = dict(tags or {})
        self.tags_written = {}
        self.said = []

    def log(self, level, message, **kw):
        self.said.append((level, message))


class DeviceCase(unittest.TestCase):
    def setUp(self):
        self.flowmod = load_runner()
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.tagwatch = tagwatch
        sys.modules["modules"] = pkg
        sys.modules["modules.tagwatch"] = tagwatch

    def tearDown(self):
        for name in ("machine", "modules", "modules.tagwatch"):
            sys.modules.pop(name, None)

    def runner(self, cfg, tags=None, extra_nodes=(), extra_edges=()):
        """A Tag change wired to a Log, with every message the Log gets kept."""
        agent = Agent(tags)
        nodes = [{"id": "t", "type": "tag.change", "config": cfg},
                 {"id": "out", "type": "log.write", "config": {"message": "x"}}]
        edges = [{"id": "e", "from": "t", "fromPort": "out", "to": "out",
                  "toPort": "in"}]
        r = self.flowmod.Runner({"id": "f", "nodes": nodes + list(extra_nodes),
                                 "edges": edges + list(extra_edges)}, agent)
        got = []
        r._do_log_write = lambda node_id, c, msg, hops: got.append(msg)
        r.start()
        return r, agent, got

    def settle(self, r, times=3):
        for _ in range(times):
            r.tick()


class TestItFiresOnAChange(DeviceCase):
    def test_a_pushed_value_fires_it(self):
        r, agent, got = self.runner({"tag": "level"}, {"level": 1})
        self.settle(r)
        agent.tags["level"] = 5
        self.settle(r)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["payload"], 5)
        self.assertEqual(got[0]["meta"], {"tag": "level", "previous": 1})

    def test_the_same_value_again_is_not_a_change(self):
        r, agent, got = self.runner({"tag": "level"}, {"level": 1})
        self.settle(r)
        agent.tags["level"] = 1
        self.settle(r)
        self.assertEqual(got, [])

    def test_the_value_a_sync_delivers_is_a_baseline(self):
        """A board boots with no tags; the manifest's values are where it
        starts, not a change."""
        r, agent, got = self.runner({"tag": "level"}, {})
        self.settle(r)
        agent.tags["level"] = 7
        self.settle(r)
        self.assertEqual(got, [])
        agent.tags["level"] = 8
        self.settle(r)
        self.assertEqual([m["payload"] for m in got], [8])

    def test_another_tag_does_not_fire_it(self):
        r, agent, got = self.runner({"tag": "level"}, {"level": 1, "other": 1})
        self.settle(r)
        agent.tags["other"] = 2
        self.settle(r)
        self.assertEqual(got, [])

    def test_no_tag_named_watches_them_all(self):
        r, agent, got = self.runner({"tag": ""}, {"a": 1, "b": 1})
        self.settle(r)
        agent.tags["a"] = 2
        agent.tags["b"] = 3
        self.settle(r)
        self.assertEqual(sorted(m["meta"]["tag"] for m in got), ["a", "b"])

    def test_the_flow_s_own_write_fires_it(self):
        """Tag change sees a Write tag in the same flow, as the host does."""
        r, agent, got = self.runner(
            {"tag": "level"}, {"level": 0},
            extra_nodes=[{"id": "go", "type": "manual.fire", "config": {}},
                         {"id": "w", "type": "tag.set",
                          "config": {"tag": "level", "value": "4"}}],
            extra_edges=[{"id": "e2", "from": "go", "fromPort": "out",
                          "to": "w", "toPort": "in"}])
        self.settle(r)
        r.fire("go")
        self.settle(r, 5)
        self.assertEqual([m["meta"]["previous"] for m in got], [0])


class TestWhen(DeviceCase):
    def moves(self, when, start, *values):
        r, agent, got = self.runner({"tag": "x", "when": when}, {"x": start})
        self.settle(r)
        for v in values:
            agent.tags["x"] = v
            self.settle(r)
        return [m["payload"] for m in got]

    def test_becomes_true_means_false_before(self):
        self.assertEqual(self.moves("becomes true", 0, 1, 2, 0, 1), [1, 1])

    def test_becomes_false(self):
        self.assertEqual(self.moves("becomes false", 1, 2, 0, 0, 1, 0), [0, 0])

    def test_rises_and_falls(self):
        self.assertEqual(self.moves("rises", 5, 6, 4, 9), [6, 9])
        self.assertEqual(self.moves("falls", 5, 6, 4, 9), [4])

    def test_text_never_rises(self):
        self.assertEqual(self.moves("rises", "a", "b"), [])


class TestTheTwoSidesAgree(unittest.TestCase):
    """One rule, `tagwatch.tag_wants`, on both runtimes."""

    CASES = [("changes", 1, 0), ("becomes true", 1, 0), ("becomes true", 2, 1),
             ("becomes false", 0, 1), ("becomes false", 0, 0),
             ("rises", 3, 2), ("rises", 2, 3), ("falls", 2, 3),
             ("rises", "b", "a"), ("becomes true", "on", "off")]

    def test_the_host_fires_exactly_when_the_rule_says(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        for when, value, previous in self.CASES:
            with self.subTest(when=when, value=value, previous=previous):
                e.tag_triggers = [(("f", "n"), {"tag": "x", "when": when})]
                want = tagwatch.tag_wants(when, value, previous, flows.truthy)
                self.assertEqual(e.on_tag_changed("x", value, previous),
                                 1 if want else 0)

    def test_the_host_no_longer_calls_1_to_2_becoming_true(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        e.tag_triggers = [(("f", "n"), {"tag": "x", "when": "becomes true"})]
        self.assertEqual(e.on_tag_changed("x", 2, 1), 0)


class TestItCostsNothingWhenUnused(DeviceCase):
    def test_a_flow_without_one_never_polls(self):
        r = self.flowmod.Runner({"id": "f", "nodes": [], "edges": []}, Agent())
        r.start()
        self.assertEqual(r.polls, [])


if __name__ == "__main__":
    unittest.main()
