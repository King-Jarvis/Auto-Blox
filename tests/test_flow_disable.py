"""Switching a flow off has to switch it off on the board as well."""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod  # noqa: E402

AGENT_DIR = os.path.join(ROOT, "zero2w_console", "agent")


def source(name):
    with open(os.path.join(AGENT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def function(src, header):
    at = src.index(header)
    body = src[at:]
    cuts = [c for c in (body.find("\n    def "), body.find("\n\ndef ")) if c != -1]
    return body[:min(cuts)] if cuts else body


class TestTheDeviceReadsTheSwitch(unittest.TestCase):
    def test_start_flow_refuses_a_flow_that_is_switched_off(self):
        body = function(source("agent.py"), "def start_flow(self)")
        self.assertIn('"enabled"', body,
                      "start_flow never looks at enabled, so a flow switched "
                      "off in the studio goes on running from flash")

    def test_it_leaves_no_runner_behind(self):
        body = function(source("agent.py"), "def start_flow(self)")
        after = body[body.index('"enabled"'):]
        self.assertIn("self.runner = None", after)

    def test_reload_is_what_applies_it(self):
        """A reload carries the switch to the board and `try_flow` acts on it,
        so the same document twice is one restart (test_agent_reload_once)."""
        body = function(source("agent.py"), "def handle(self, cmd)")
        self.assertIn("self.try_flow()", body)
        applies = function(source("agent.py"), "def try_flow(self)")
        self.assertIn("self.runner.stop()", applies)
        self.assertIn("self.start_flow()", applies)

    def test_the_report_says_whether_it_is_actually_running(self):
        """Deployed-and-off and deployed-and-broken both report a flow with
        no nodes and no pins."""
        body = function(source("agent.py"), "def report(self)")
        self.assertIn('"running"', body)

    def test_stopping_drives_the_pwm_pins_low(self):
        """deinit leaves a pin wherever the waveform stopped, which on a
        motor driver is a wheel still turning after the flow was stopped."""
        body = function(source("flow.py"), "def stop(self)")
        self.assertIn("deinit", body)
        after = body[body.index("deinit"):]
        self.assertIn("value(0)", after,
                      "stop() releases the channel without driving the pin low")


class Devices:
    """Enough of a device store for reconcile()."""

    def __init__(self, rows):
        self.doc = {"devices": rows}

    def load(self):
        return self.doc

    def save(self, doc):
        self.doc = doc


class TestTheSwitchReachesTheDevice(unittest.TestCase):
    def setUp(self):
        self.fleet = fleetmod.Fleet(
            Devices([{"id": "dev_a", "name": "A", "flow": "f_heartbeat"},
                     {"id": "dev_b", "name": "B", "flow": "f_other"},
                     {"id": "dev_c", "name": "C", "flow": None}]),
            None, None)

    def doc(self, **enabled):
        return {"flows": [{"id": fid, "enabled": on}
                          for fid, on in enabled.items()]}

    def queued(self, device_id):
        return self.fleet.queues.get(device_id) or []

    def test_switching_one_off_tells_the_device_running_it(self):
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat", "enabled": True}},
            self.doc(f_heartbeat=False))
        self.assertEqual(told, ["dev_a"])
        self.assertEqual(self.queued("dev_a")[0]["op"], "reload")

    def test_switching_one_back_on_tells_it_too(self):
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat", "enabled": False}},
            self.doc(f_heartbeat=True))
        self.assertEqual(told, ["dev_a"])

    def test_a_flow_nobody_is_running_tells_nobody(self):
        told = self.fleet.reconcile(
            {"f_lonely": {"id": "f_lonely", "enabled": True}},
            {"flows": [{"id": "f_lonely", "enabled": False}]})
        self.assertEqual(told, [])

    def test_saving_without_touching_the_switch_tells_nobody(self):
        """Otherwise every edit restarts every board, which is a different
        feature and not this one."""
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat", "enabled": True,
                             "nodes": [1]}},
            {"flows": [{"id": "f_heartbeat", "enabled": True, "nodes": [1, 2]}]})
        self.assertEqual(told, [])

    def test_a_missing_enabled_counts_as_on(self):
        """The default everywhere else, so it has to be the default here."""
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat"}},
            self.doc(f_heartbeat=False))
        self.assertEqual(told, ["dev_a"])
        self.fleet.queues.clear()
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat"}},
            self.doc(f_heartbeat=True))
        self.assertEqual(told, [])

    def test_a_brand_new_flow_is_not_a_change(self):
        told = self.fleet.reconcile({}, self.doc(f_heartbeat=False))
        self.assertEqual(told, [])

    def test_two_flows_at_once_are_both_handled(self):
        told = self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat", "enabled": True},
             "f_other": {"id": "f_other", "enabled": True}},
            self.doc(f_heartbeat=False, f_other=False))
        self.assertEqual(sorted(told), ["dev_a", "dev_b"])

    def test_the_command_names_the_flow_the_device_has(self):
        self.fleet.reconcile(
            {"f_heartbeat": {"id": "f_heartbeat", "enabled": True}},
            self.doc(f_heartbeat=False))
        self.assertEqual(self.queued("dev_a")[0]["flow"], "f_heartbeat")


class TestTheSaveHandlerCallsIt(unittest.TestCase):
    """The wiring, which is easy to leave out and impossible to see."""

    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py"),
                  encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_flow_save_route_reconciles(self):
        at = self.src.index('if route == "/api/flows":')
        block = self.src[at:at + 3000]
        self.assertIn("reconcile", block,
                      "saving flows never tells the fleet, so the switch "
                      "does nothing until the board happens to resync")

    def test_it_reads_the_previous_document_before_saving(self):
        at = self.src.index('if route == "/api/flows":')
        block = self.src[at:at + 3000]
        load = block.index("store.load()")
        save = block.index("store.save(")
        self.assertLess(load, save,
                        "the comparison is made against the document it just "
                        "wrote, so nothing ever looks changed")

    def test_a_fleet_that_is_not_there_does_not_break_saving(self):
        """Tests and --no-iot-net builds construct an engine with no fleet."""
        at = self.src.index('if route == "/api/flows":')
        block = self.src[at:at + 3000]
        self.assertIn("if self.server.fleet:", block)

    def test_it_parses(self):
        ast.parse(self.src)


if __name__ == "__main__":
    unittest.main()


class TestTheRadioGetsItsMemoryFirst(unittest.TestCase):
    """A board that cannot reach the host cannot be told anything."""

    def setUp(self):
        self.src = source("agent.py")

    def test_there_is_a_radio_step_of_its_own(self):
        self.assertIn("def radio(self)", self.src)

    def test_run_brings_the_radio_up_before_anything_starts_the_flow(self):
        body = function(self.src, "def run(self)")
        self.assertIn("self.radio()", body)
        self.assertIn("self.try_flow()", body,
                      "nothing in the loop starts the deployed flow")
        self.assertLess(body.index("self.radio()"), body.index("self.try_flow()"),
                        "the flow is loaded before the radio exists, which is "
                        "how the board ran out of memory for wifi")

    def test_the_flow_does_not_start_before_the_network_is_tried(self):
        """The later half of the same lesson: associating allocates too, and
        the flow competes for a rail this fleet's motor board has little
        of."""
        body = function(self.src, "def run(self)")
        self.assertLess(body.index("self.connect()"), body.index("self.try_flow()"),
                        "the flow starts before the board has tried to join")

    def test_a_board_with_no_host_still_runs_its_flow(self):
        """Deferring must not become waiting: `FLOW_GRACE` is the bound."""
        body = function(self.src, "def run(self)")
        self.assertIn("self.flow_due", body)
        self.assertIn("FLOW_GRACE", self.src)

    def test_the_radio_step_activates_the_interface(self):
        body = function(self.src, "def radio(self)")
        self.assertIn("active(True)", body)

    def test_it_is_safe_to_call_twice(self):
        body = function(self.src, "def radio(self)")
        self.assertIn("if self.wlan is not None:", body)

    def test_a_radio_that_will_not_come_up_does_not_stop_the_board(self):
        """No wifi at all is a normal state for a field device."""
        body = function(self.src, "def radio(self)")
        self.assertIn("except Exception", body)

    def test_connect_still_works_if_the_radio_is_not_up_yet(self):
        body = function(self.src, "def connect(self, tries=20)")
        self.assertIn("self.radio()", body)
        self.assertIn("return False", body)

    def test_power_save_is_still_turned_off(self):
        """Left in place by the move: a sleeping station adds a beacon wait
        to every round trip."""
        self.assertIn("PM_NONE", function(self.src, "def radio(self)"))


class TestTheReportSurvivesTheWhitelist(unittest.TestCase):
    def test_running_is_kept(self):
        with open(os.path.join(ROOT, "zero2w_console", "fleet.py"),
                  encoding="utf-8") as fh:
            body = function(fh.read(), "def report_state(self, device, body)")
        self.assertIn('"running"', body,
                      "the device reports it and the whitelist drops it, so "
                      "the console still cannot tell off from broken")
