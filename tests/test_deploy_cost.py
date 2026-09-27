"""What a deploy would cost over the air, and whether the wire is better."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples                # noqa: E402
from zero2w_console import fleet as fleetmod       # noqa: E402
from zero2w_console import flows as flowmod        # noqa: E402
from zero2w_console import iot as iotmod           # noqa: E402


class CostCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.devices = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.flows = flowmod.FlowStore(os.path.join(self.dir.name, "flows.json"))
        self.fleet = fleetmod.Fleet(self.devices, self.flows, None)
        self.device = iotmod.create_device(self.devices, {
            "name": "ESP32 Motor", "board": "esp32", "mac": "70:4b:ca:00:00:b8"})
        self.motor = examples.by_id("ex_motor_drive")
        self.blink = examples.by_id("ex_blink")

    def record(self):
        return iotmod.get_device(self.devices, self.device["id"])

    def cost(self, flow):
        return self.fleet.deploy_cost(flow, self.record())


class TestABoardThatHoldsNothing(CostCase):
    def test_everything_counts_and_the_wire_is_recommended(self):
        cost = self.cost(self.motor)
        self.assertEqual(cost["bytes"], cost["total"],
                         "a board with nothing must be charged for everything")
        self.assertGreater(cost["bytes"], 60 * 1024)
        self.assertTrue(cost["wire"])

    def test_even_a_two_node_flow_costs_the_runner(self):
        """`agent.py` and `flow.py` are most of it, and a fresh board needs
        both whatever the flow is."""
        cost = self.cost(self.blink)
        self.assertTrue(cost["wire"])
        names = [f["name"] for f in cost["files"]]
        self.assertIn("agent.py", names)
        self.assertIn("flow.py", names)
        self.assertIn("flow.json", names)


class TestABoardThatIsAlreadySynced(CostCase):
    def setUp(self):
        super().setUp()
        # Exactly what the module route records as it serves each file.
        self.fleet.note_modules(self.device["id"],
                                self.fleet._wrote(self.motor, self.record()))

    def test_only_the_flow_document_is_left_to_fetch(self):
        cost = self.cost(self.motor)
        self.assertFalse(cost["wire"], "it wanted the wire for a flow.json")
        self.assertLess(cost["bytes"], 16 * 1024)
        needed = [f["name"] for f in cost["files"] if f["needed"]]
        self.assertEqual(needed, ["flow.json"])

    def test_a_smaller_flow_is_cheaper_still(self):
        cost = self.cost(self.blink)
        self.assertFalse(cost["wire"])
        self.assertLess(cost["bytes"], self.cost(self.motor)["bytes"])

    def test_the_total_still_says_what_the_flow_needs(self):
        """Both numbers are useful: one is the wait, the other is the size."""
        cost = self.cost(self.motor)
        self.assertGreater(cost["total"], cost["bytes"])

    def test_a_changed_module_is_charged_for_again(self):
        """The record is by sha, not by name, so editing a module makes the
        board fetch it — which is the case that turns a cheap deploy into
        a slow one without anything else changing."""
        held = dict(self.record().get("modules") or {})
        held["flow.py"] = "0" * 16
        self.fleet.note_modules(self.device["id"], held)
        needed = [f["name"] for f in self.cost(self.motor)["files"] if f["needed"]]
        self.assertIn("flow.py", needed)


class TestTheRecordIsOnlyAHint(CostCase):
    def test_an_unknown_file_is_assumed_missing(self):
        """Wrong in the safe direction."""
        self.fleet.note_modules(self.device["id"], {"modules/drive.py": "deadbeefdeadbeef"})
        needed = [f["name"] for f in self.cost(self.motor)["files"] if f["needed"]]
        self.assertIn("agent.py", needed)
        self.assertIn("flow.py", needed)

    def test_noting_nothing_changes_nothing(self):
        before = json.dumps(self.record().get("modules"))
        self.fleet.note_modules(self.device["id"], {})
        self.assertEqual(json.dumps(self.record().get("modules")), before)

    def test_notes_accumulate_rather_than_replace(self):
        self.fleet.note_modules(self.device["id"], {"a.py": "1" * 16})
        self.fleet.note_modules(self.device["id"], {"b.py": "2" * 16})
        held = self.record()["modules"]
        self.assertEqual(sorted(held), ["a.py", "b.py"])

    def test_a_device_that_does_not_exist_is_not_an_error(self):
        self.fleet.note_modules("dev_nothing", {"a.py": "1" * 16})

    def test_a_flash_with_no_flow_records_only_the_agent(self):
        """It writes `agent.py`, `config.json` and `main.py` and nothing
        else, so the runner and the modules are still to come."""
        self.assertEqual(sorted(self.fleet._wrote(None, self.record())),
                         ["agent.py"])

    def test_a_flash_with_a_flow_records_everything_it_wrote(self):
        held = self.fleet._wrote(self.motor, self.record())
        self.assertIn("agent.py", held)
        self.assertIn("flow.py", held)
        self.assertTrue(any(n.startswith("modules/") for n in held))
        for name, digest in held.items():
            with self.subTest(name=name):
                self.assertEqual(digest,
                                 fleetmod.sha(self.fleet.module_source(name)))


class TestNoFlowAtAll(CostCase):
    def test_undeploying_costs_no_flow_document(self):
        cost = self.fleet.deploy_cost(None, self.record())
        self.assertFalse(any(f["name"] == "flow.json" for f in cost["files"]))


class TestFlashingWithAFlow(CostCase):
    """The wire path the cost is meant to send you to."""

    def setUp(self):
        super().setUp()
        self.flows.save({"flows": [
            dict(self.motor, id="f_motor", device=self.device["id"]),
            {"id": "f_host", "name": "Host only", "enabled": False,
             "nodes": [{"id": "s", "type": "shell.run",
                        "config": {"command": "id"}}], "edges": []},
            dict(self.blink, id="f_cam", board="esp32cam")]})
        self.ran = []
        self.fleet.flash = self.fleet.flash          # keep the real one

    def attempt(self, flow_id):
        """Everything `flash` validates, without starting a subprocess."""
        import threading
        started = []
        real_thread = threading.Thread

        class Fake:
            def __init__(self, **kw):
                started.append(kw)

            def start(self):
                pass

            def is_alive(self):
                return False

        threading.Thread = Fake
        try:
            return self.fleet.flash(self.device["id"], "/dev/ttyUSB0",
                                    "example-wifi", "x" * 12, flow=flow_id)
        finally:
            threading.Thread = real_thread

    def test_a_real_flow_for_this_board_is_accepted(self):
        self.assertTrue(self.attempt("f_motor")["ok"])

    def test_a_flow_that_does_not_exist_is_refused(self):
        with self.assertRaises(iotmod.NetError):
            self.attempt("f_nope")

    def test_a_flow_for_another_board_is_refused(self):
        with self.assertRaises(iotmod.NetError):
            self.attempt("f_cam")

    def test_a_flow_the_board_cannot_run_is_refused(self):
        with self.assertRaises(iotmod.NetError):
            self.attempt("f_host")

    def test_an_id_is_never_taken_on_trust(self):
        """It ends up on a command line, so it is looked up rather than
        passed through."""
        for nasty in ("--firmware=/etc/shadow", "; rm -rf /", "../../etc/passwd"):
            with self.subTest(nasty=nasty):
                with self.assertRaises(iotmod.NetError):
                    self.attempt(nasty)


if __name__ == "__main__":
    unittest.main()
