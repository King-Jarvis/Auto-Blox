"""Where a flow runs, and what it may contain when it runs there."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows, iot  # noqa: E402


class TestRunsField(unittest.TestCase):
    def test_every_node_type_says_where_it_runs(self):
        for ntype, spec in flows.REGISTRY.items():
            with self.subTest(node=ntype):
                self.assertIn(spec.get("runs"), ("host", "device", "both"))

    def test_host_only_nodes_are_the_ones_that_need_this_machine(self):
        host_only = sorted(t for t, d in flows.REGISTRY.items() if d["runs"] == "host")
        self.assertEqual(host_only, [
            "device.command",   # this host's queue to the fleet
            "flow.call",        # other flows live here
            "host.event",       # devices report to this host, not to each other
            "http.webhook",     # inbound HTTP, on this host's port
            "metric.threshold", # this host's metrics
            "mqtt.publish", "mqtt.subscribe",   # this host's broker connection
            "shell.run",        # this host's shell
            "spi.transfer",     # untested on a device
        ])

    def test_a_device_can_at_least_read_a_pin_drive_one_and_branch(self):
        for ntype in ("gpio.in", "gpio.out", "logic.if", "logic.delay", "timer.interval"):
            self.assertTrue(flows.runs_on_device(ntype), ntype)

    def test_an_unknown_type_is_not_assumed_to_run_anywhere(self):
        self.assertFalse(flows.runs_on_device("no.such.node"))


class TestRegistryForBoard(unittest.TestCase):
    def test_without_a_board_the_palette_is_everything(self):
        self.assertIs(flows.registry_for(None), flows.REGISTRY)

    def test_with_a_board_the_palette_is_only_what_it_can_run(self):
        profile = iot.board_profile("esp32s3")
        palette = flows.registry_for(profile)
        self.assertLess(len(palette), len(flows.REGISTRY))
        for ntype in palette:
            self.assertTrue(flows.runs_on_device(ntype), ntype)
        for ntype in ("shell.run", "metric.threshold", "http.webhook"):
            self.assertNotIn(ntype, palette)

    def test_a_board_without_a_peripheral_does_not_offer_it(self):
        """The filter is driven by the profile, not by a list of exceptions."""
        profile = dict(iot.board_profile("esp32c3"))
        profile["buses"] = {"uart0": profile["buses"]["uart0"]}   # no I2C
        palette = flows.registry_for(profile)
        for ntype in ("i2c.read", "i2c.write", "i2c.scan"):
            self.assertNotIn(ntype, palette)
        self.assertIn("gpio.out", palette)

    def test_every_real_profile_can_run_something(self):
        for board, profile in iot.board_profiles().items():
            with self.subTest(board=board):
                self.assertTrue(flows.registry_for(profile))


class TestWhereAFlowRuns(unittest.TestCase):
    def test_a_plain_flow_runs_here(self):
        self.assertTrue(flows.runs_here({"id": "f1", "nodes": []}))
        self.assertTrue(flows.runs_here({"id": "f1", "board": None}))

    def test_a_board_flow_does_not(self):
        self.assertFalse(flows.runs_here({"id": "f1", "board": "esp32s3"}))

    def test_nothing_at_all_runs_here_rather_than_raising(self):
        self.assertTrue(flows.runs_here(None))


if __name__ == "__main__":
    unittest.main()
