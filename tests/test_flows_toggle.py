"""Toggle's two inputs, and what a flip does."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class TestWhichActionAPortMeans(unittest.TestCase):
    def test_an_edge_with_no_port_at_all_flips(self):
        """Edges predate toPort; those must land on the left input."""
        self.assertEqual(flows.toggle_action({}, None), "toggle")

    def test_the_old_port_name_flips(self):
        self.assertEqual(flows.toggle_action({}, "in"), "toggle")

    def test_the_new_port_turns_it_off_by_default(self):
        self.assertEqual(flows.toggle_action({}, "in2"), "off")

    def test_each_input_can_be_set_to_anything(self):
        cfg = {"in_action": "on", "in2_action": "toggle"}
        self.assertEqual(flows.toggle_action(cfg, "in"), "on")
        self.assertEqual(flows.toggle_action(cfg, "in2"), "toggle")

    def test_nonsense_falls_back_to_the_default_for_that_input(self):
        self.assertEqual(flows.toggle_action({"in_action": "explode"}, "in"),
                         "toggle")
        self.assertEqual(flows.toggle_action({"in2_action": ""}, "in2"), "off")

    def test_an_unknown_port_is_treated_as_the_left_one(self):
        self.assertEqual(flows.toggle_action({}, "nonsense"), "toggle")


class TestToggling(unittest.TestCase):
    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.flow = {"id": "f", "name": "f"}

    def send(self, cfg, port=None):
        node = {"id": "n", "type": "logic.toggle", "config": cfg}
        _port, out = self.e._execute(self.flow, node, {"payload": 1, "meta": {}}, port)
        return out["payload"]

    def test_the_first_flip_moves_it_off_the_start(self):
        """One press of a button wired to a flip turns it on."""
        self.assertEqual(self.send({}), "1")

    def test_and_then_it_alternates(self):
        cfg = {}
        self.assertEqual(self.send(cfg), "1")
        self.assertEqual(self.send(cfg), "0")
        self.assertEqual(self.send(cfg), "1")

    def test_starting_on_flips_off_first(self):
        self.assertEqual(self.send({"start": "on"}), "0")

    def test_the_configured_values_are_used(self):
        cfg = {"start": "on", "on_value": "open", "off_value": "shut"}
        self.assertEqual(self.send(cfg), "shut")
        self.assertEqual(self.send(cfg), "open")

    def test_an_on_input_forces_on_and_stays_there(self):
        cfg = {"in_action": "on"}
        self.assertEqual(self.send(cfg, "in"), "1")
        self.assertEqual(self.send(cfg, "in"), "1")

    def test_an_off_input_forces_off_even_from_on(self):
        cfg = {"start": "on", "in_action": "on", "in2_action": "off"}
        self.assertEqual(self.send(cfg, "in"), "1")
        self.assertEqual(self.send(cfg, "in2"), "0")
        self.assertEqual(self.send(cfg, "in2"), "0")

    def test_two_inputs_make_a_latch(self):
        """One trigger starts it, a different one stops it."""
        cfg = {"in_action": "on", "in2_action": "off"}
        self.assertEqual(self.send(cfg, "in"), "1")
        self.assertEqual(self.send(cfg, "in2"), "0")
        self.assertEqual(self.send(cfg, "in"), "1")

    def test_a_legacy_edge_still_flips(self):
        """An edge saved before there were two inputs lands on the flip."""
        cfg = {}
        self.assertEqual(self.send(cfg, None), "1")
        self.assertEqual(self.send(cfg, None), "0")


class TestTheDeviceAgrees(unittest.TestCase):
    """The same presses through the board's runner give the same answers."""

    def presses(self, cfg, ports):
        from tests.test_agent_pwm import load_runner

        class Agent:
            tags = {}

            def log(self, *a, **kw):
                pass

        flowmod = load_runner()
        try:
            r = flowmod.Runner({"id": "f", "nodes": [], "edges": []}, Agent())
            out = []
            r._emit = lambda node_id, msg, hops: out.append(msg["payload"])
            for port in ports:
                r.arrived = port
                r._do_logic_toggle("k", cfg, {"payload": 1}, 0)
            return out
        finally:
            sys.modules.pop("machine", None)

    def test_the_first_flip_moves_it_off_the_start(self):
        self.assertEqual(self.presses({}, ["in", "in", "in"]), ["1", "0", "1"])

    def test_starting_on_flips_off_first(self):
        self.assertEqual(self.presses({"start": "on"}, ["in"]), ["0"])

    def test_on_and_off_inputs(self):
        cfg = {"in_action": "on", "in2_action": "off"}
        self.assertEqual(self.presses(cfg, ["in", "in", "in2"]), ["1", "1", "0"])


class TestPortsAreDeclaredProperly(unittest.TestCase):
    def test_the_toggle_has_two_inputs_on_their_own_sides(self):
        ports = flows.REGISTRY["logic.toggle"]["inputs"]
        self.assertEqual([p["name"] for p in ports], ["in", "in2"])
        self.assertEqual([p["side"] for p in ports], ["left", "top"])

    def test_the_first_one_keeps_the_name_every_saved_edge_uses(self):
        self.assertEqual(flows.REGISTRY["logic.toggle"]["inputs"][0]["name"], "in")

    def test_the_heavy_form_is_only_used_where_a_side_is_meant(self):
        """A plain name is the normal case — a port only names a side when
        the graph should show which input is which."""
        heavy = {t for t, s in flows.REGISTRY.items()
                 if any(not isinstance(p, str) for p in (s.get("inputs") or []))}
        self.assertEqual(heavy, {"logic.toggle", "logic.count", "math.smooth",
                                 "math.ramp", "logic.latch", "logic.step"})

    def test_every_declared_port_is_well_formed(self):
        for ntype, spec in flows.REGISTRY.items():
            ports = list(spec.get("inputs") or []) + list(spec.get("outputs") or [])
            for port in ports:
                with self.subTest(node=ntype, port=port):
                    if isinstance(port, str):
                        self.assertTrue(port)
                        continue
                    self.assertTrue(port.get("name"))
                    self.assertIn(port.get("side"),
                                  ("left", "top", "right", "bottom"))

    def test_a_node_with_two_inputs_keeps_in_as_one_of_them(self):
        """Every saved edge names "in"; a second input must be the new one."""
        for ntype, spec in flows.REGISTRY.items():
            ports = spec.get("inputs") or []
            if len(ports) < 2:
                continue
            names = [p if isinstance(p, str) else p["name"] for p in ports]
            with self.subTest(node=ntype):
                self.assertIn("in", names)

    def test_a_port_that_names_a_config_key_for_its_label_names_a_real_one(self):
        spec = flows.REGISTRY["logic.toggle"]
        keys = {f["key"] for f in spec["fields"]}
        for port in spec["inputs"]:
            if "label_from" in port:
                self.assertIn(port["label_from"], keys)


if __name__ == "__main__":
    unittest.main()
