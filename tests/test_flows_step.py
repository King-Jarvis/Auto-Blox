"""Step: a sequence stage, active from go until done — on both runtimes."""
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_pwm import load_runner                  # noqa: E402
from zero2w_console import fleet as fleetmod, flows           # noqa: E402
from zero2w_console.agent.modules import steps                # noqa: E402


def edge(src, dst, from_port="out", to_port="in"):
    return {"id": "%s-%s-%s" % (src, dst, to_port), "from": src,
            "fromPort": from_port, "to": dst, "toPort": to_port}


# go -> fill; a separate trigger into fill's done; fill.next -> heat (which
# leaves by itself); a Log on every output worth watching.
FLOW = {
    "id": "f", "name": "seq",
    "nodes": [
        {"id": "go", "type": "manual.fire", "config": {}},
        {"id": "full", "type": "manual.fire", "config": {}},
        {"id": "stop", "type": "manual.fire", "config": {}},
        {"id": "fill", "type": "logic.step", "config": {"name": "fill"}},
        {"id": "heat", "type": "logic.step",
         "config": {"name": "heat", "leave_after": 60}},
        {"id": "pump_on", "type": "log.write", "config": {"message": "on"}},
        {"id": "pump_off", "type": "log.write", "config": {"message": "off"}},
        {"id": "heater_on", "type": "log.write", "config": {"message": "heat"}},
        {"id": "heater_off", "type": "log.write", "config": {"message": "cool"}},
    ],
    "edges": [
        edge("go", "fill"), edge("full", "fill", to_port="done"),
        edge("stop", "fill", to_port="reset"),
        edge("fill", "pump_on", "entered"), edge("fill", "pump_off", "left"),
        edge("fill", "heat", "next"),
        edge("heat", "heater_on", "entered"), edge("heat", "heater_off", "left"),
    ],
}


class Bus:
    def __init__(self):
        self.rows = []

    def publish(self, channel, row):
        self.rows.append(row)


# --------------------------------------------------------------- the host
class HostCase(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, self.bus)
        self.e.doc = {"flows": [FLOW]}

    def press(self, node_id):
        self.e._run_from(("f", node_id), {"payload": 1, "meta": {}})

    def ran(self, node_id):
        return ("f", node_id) in self.e.last_output

    def said(self):
        return [(r["node"], r["payload"]["active"]) for r in self.bus.rows
                if r.get("kind") == "step"]


class TestTheHostStep(HostCase):
    def test_go_makes_it_active_and_says_entered(self):
        self.press("go")
        self.assertTrue(self.ran("pump_on"))
        self.assertEqual(self.e.last_output[("f", "pump_on")]["meta"]["step"], "fill")
        self.assertEqual(self.e.active_steps(), ["f/fill"])

    def test_done_while_idle_does_nothing(self):
        """The whole point: a condition during some other step is ignored."""
        self.press("full")
        self.assertFalse(self.ran("pump_off"))
        self.assertFalse(self.ran("heater_on"))
        self.assertEqual(self.e.active_steps(), [])

    def test_done_while_active_leaves_and_moves_on(self):
        self.press("go")
        self.press("full")
        self.assertTrue(self.ran("pump_off"))
        self.assertTrue(self.ran("heater_on"))
        self.assertEqual(self.e.active_steps(), ["f/heat"])
        self.assertFalse(self.e.last_output[("f", "pump_off")]["meta"]["timed_out"])

    def test_a_second_go_is_not_a_second_entry(self):
        self.press("go")
        self.press("go")
        self.assertEqual(self.said(), [("fill", True)])

    def test_reset_goes_idle_and_says_nothing_downstream(self):
        self.press("go")
        self.press("stop")
        self.assertFalse(self.ran("pump_off"))
        self.assertEqual(self.e.active_steps(), [])
        self.press("full")
        self.assertFalse(self.ran("heater_on"))

    def test_leave_after_is_scheduled_and_moves_it_on(self):
        self.press("go")
        self.press("full")
        key = ("f", "heat")
        self.assertEqual(self.e.clock.pending(("step", key)), 1)
        self.e._run_from(key, {"payload": 1, "meta": {},
                               "_step_timeout": self.e.steps[key]})
        self.assertTrue(self.ran("heater_off"))
        self.assertTrue(self.e.last_output[("f", "heater_off")]["meta"]["timed_out"])
        self.assertEqual(self.e.active_steps(), [])

    def test_a_timeout_from_an_earlier_visit_is_ignored(self):
        self.press("go")
        self.press("full")
        key = ("f", "heat")
        self.e._run_from(key, {"payload": 1, "meta": {},
                               "_step_timeout": self.e.steps[key] - 1})
        self.assertFalse(self.ran("heater_off"))
        self.assertEqual(self.e.active_steps(), ["f/heat"])

    def test_the_canvas_is_told_both_ways(self):
        self.press("go")
        self.press("full")
        self.assertEqual(self.said(),
                         [("fill", True), ("fill", False), ("heat", True)])

    def test_a_reload_leaves_every_step_idle(self):
        self.press("go")
        self.e._teardown()
        self.assertEqual(self.e.active_steps(), [])


# --------------------------------------------------------------- the device
class Agent:
    def __init__(self):
        self.tags = {}
        self.tags_written = {}
        self.said = []

    def log(self, level, message, **kw):
        self.said.append((level, message, kw))


class DeviceCase(unittest.TestCase):
    def setUp(self):
        self.flowmod = load_runner()
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.steps = steps
        sys.modules["modules"] = pkg
        sys.modules["modules.steps"] = steps
        self.agent = Agent()
        self.r = self.flowmod.Runner(FLOW, self.agent)
        self.logged = []
        self.r._do_log_write = lambda node_id, cfg, msg, hops: \
            self.logged.append((node_id, msg))
        self.r.start()

    def tearDown(self):
        for name in ("machine", "modules", "modules.steps"):
            sys.modules.pop(name, None)

    def press(self, node_id):
        self.r.fire(node_id)
        for _ in range(6):
            self.r.tick()

    def ran(self):
        return [n for n, _m in self.logged]

    def said(self):
        return [(kw["node"], kw["payload"]["active"]) for _l, _t, kw in self.agent.said
                if kw.get("kind") == "step"]


class TestTheDeviceStep(DeviceCase):
    def test_go_then_done_runs_the_stages_in_order(self):
        self.press("go")
        self.press("full")
        self.assertEqual(self.ran(), ["pump_on", "pump_off", "heater_on"])

    def test_done_while_idle_does_nothing(self):
        self.press("full")
        self.assertEqual(self.ran(), [])

    def test_a_second_go_is_not_a_second_entry(self):
        self.press("go")
        self.press("go")
        self.assertEqual(self.ran(), ["pump_on"])

    def test_reset(self):
        self.press("go")
        self.press("stop")
        self.press("full")
        self.assertEqual(self.ran(), ["pump_on"])
        self.assertEqual(self.said(), [("fill", True), ("fill", False)])

    def test_leave_after_moves_on_by_itself(self):
        self.press("go")
        self.press("full")
        deadline = _real.monotonic() + 2
        while "heater_off" not in self.ran() and _real.monotonic() < deadline:
            self.r.tick()
            _real.sleep(0.01)
        self.assertIn("heater_off", self.ran())
        left = [m for n, m in self.logged if n == "heater_off"][0]
        self.assertTrue(left["meta"]["timed_out"])

    def test_the_host_is_told_both_ways(self):
        self.press("go")
        self.press("full")
        self.assertEqual(self.said(),
                         [("fill", True), ("fill", False), ("heat", True)])

    def test_a_flow_without_a_step_polls_nothing(self):
        r = self.flowmod.Runner({"id": "x", "nodes": [], "edges": []}, Agent())
        r.start()
        self.assertEqual(r.polls, [])


# --------------------------------------------------------------- the fleet
class TestTheConsoleRemembersABoardsSteps(unittest.TestCase):
    def setUp(self):
        self.fleet = fleetmod.Fleet(None, None, None)
        self.dev = {"id": "dev_a", "flow": "f"}

    def tell(self, node, active):
        self.fleet._emit(self.dev, "ok", "x", node=node, kind="step",
                         payload={"active": active})

    def test_an_active_step_is_listed_for_the_canvas(self):
        self.tell("fill", True)
        self.assertEqual(self.fleet.active_steps(), ["f/fill"])
        self.tell("fill", False)
        self.assertEqual(self.fleet.active_steps(), [])

    def test_a_boot_leaves_every_step_idle(self):
        self.tell("fill", True)
        self.fleet.booted(self.dev)
        self.assertEqual(self.fleet.active_steps(), [])

    def test_the_editor_is_handed_them(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            src = fh.read()
        at = src.index('if path == "/api/flows/outputs":')
        self.assertIn("active_steps()", src[at:at + 400])


if __name__ == "__main__":
    unittest.main()
