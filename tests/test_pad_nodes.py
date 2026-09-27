"""The controller nodes, and the two different ways they fail safe.

The asymmetry is the whole design and it is not obvious, so it is pinned here:

  - `pad.axis` emits **nothing** when the pad is stale. Not zero. A zero would
    look identical on the canvas and would command a real value, so a Watchdog
    fed from the chain would go on being fed by the thing that had failed.
    Silence starves it, and starving it is what drives every duty to zero.
  - `pad.button` on **held** emits **0** when the pad is stale, because an arm
    has to fall. Emitting nothing there would leave a latch set.

Two routes out of the same fault, and each one is wrong in the other's place.
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows                          # noqa: E402
from zero2w_console.agent.modules import pad as padmod     # noqa: E402

AXIS = {"axis": "right trigger", "invert": "no", "stale_ms": 250, "decimals": 3}
HELD = {"button": "a", "emit": "held", "stale_ms": 250}
MSG = {"payload": 1, "meta": {}}


def engine():
    """A bare engine: these three handlers touch nothing else on it."""
    e = flows.FlowEngine.__new__(flows.FlowEngine)
    e.pad = padmod.blank()
    e.pad_reader = None
    return e


def live(state, **values):
    state.update({"connected": True, "name": "Xbox", "at": time.monotonic()})
    state.update(values)
    return state


class TestTheRegistryContract(unittest.TestCase):
    def test_all_three_exist_and_run_on_a_device(self):
        for ntype in ("pad.link", "pad.axis", "pad.button"):
            with self.subTest(node=ntype):
                self.assertIn(ntype, flows.REGISTRY)
                self.assertTrue(flows.runs_on_device(ntype))

    def test_every_dropdown_value_is_one_the_shared_table_knows(self):
        """The registry and the scaling cannot disagree about what an axis is
        called, or a flow selects something that reads as centred."""
        axes = flows.REGISTRY["pad.axis"]["fields"][0]
        self.assertEqual(axes["key"], "axis")
        self.assertEqual(sorted(axes["options"]), sorted(padmod.AXES))
        buttons = flows.REGISTRY["pad.button"]["fields"][0]
        self.assertEqual(buttons["key"], "button")
        self.assertEqual(sorted(buttons["options"]), sorted(padmod.BUTTONS))

    def test_they_need_a_radio_so_a_board_without_one_is_not_offered_them(self):
        for ntype in ("pad.link", "pad.axis", "pad.button"):
            self.assertEqual(flows.NEEDS.get(ntype), "ble")
        deaf = {"pins": [], "peripherals": {"ble": False}, "buses": {}}
        palette = flows.registry_for(deaf)
        for ntype in ("pad.link", "pad.axis", "pad.button"):
            self.assertNotIn(ntype, palette)

    def test_an_esp32_is_offered_them(self):
        from zero2w_console import iot
        palette = flows.registry_for(iot.board_profile("esp32"))
        for ntype in ("pad.link", "pad.axis", "pad.button"):
            self.assertIn(ntype, palette)


class TestAnAxisIsANumberTheDriveBlocksAccept(unittest.TestCase):
    def test_a_stick_comes_out_as_a_bare_float(self):
        """Not a dict. A dict reaches `deadband` as 0.0 through `float()`
        failing, so every axis would read centred with every node still
        flashing green."""
        e = engine()
        live(e.pad, lx=-0.6)
        out = e._pad_axis(dict(AXIS, axis="left stick x"), MSG)
        self.assertIsInstance(out["payload"], float)
        self.assertAlmostEqual(out["payload"], -0.6, places=3)

    def test_a_trigger_never_goes_negative(self):
        e = engine()
        live(e.pad, rt=-5.0)
        self.assertEqual(e._pad_axis(AXIS, MSG)["payload"], 0.0)

    def test_a_stick_is_clamped_to_the_range_it_claims(self):
        e = engine()
        for raw, want in ((9.0, 1.0), (-9.0, -1.0)):
            live(e.pad, lx=raw)
            out = e._pad_axis(dict(AXIS, axis="left stick x"), MSG)
            self.assertEqual(out["payload"], want)

    def test_invert_flips_it(self):
        e = engine()
        live(e.pad, ly=0.5)
        cfg = dict(AXIS, axis="left stick y")
        self.assertAlmostEqual(e._pad_axis(cfg, MSG)["payload"], 0.5, places=3)
        cfg["invert"] = "yes"
        self.assertAlmostEqual(e._pad_axis(cfg, MSG)["payload"], -0.5, places=3)

    def test_it_feeds_the_real_dead_zone_and_split_untouched(self):
        """The point of the whole design: no new arithmetic in the chain."""
        e = engine()
        live(e.pad, lx=-0.6)
        axis = e._pad_axis(dict(AXIS, axis="left stick x"), MSG)
        dz = e._deadband({"threshold": 0.05, "full": 1, "rescale": "yes",
                          "resting": "0", "decimals": 3}, axis)
        back = e._split({"part": "back only", "full": 1, "scale": 100,
                         "floor": 25, "invert": "no", "decimals": 2}, dz)
        fwd = e._split({"part": "forward only", "full": 1, "scale": 100,
                        "floor": 25, "invert": "no", "decimals": 2}, dz)
        self.assertGreater(back["payload"], 25.0)
        self.assertEqual(fwd["payload"], 0.0,
                         "both halves of the bridge would be live")


class TestTheDeadman(unittest.TestCase):
    def test_no_pad_at_all_means_the_axis_says_nothing(self):
        self.assertIsNone(engine()._pad_axis(AXIS, MSG))

    def test_a_stale_pad_means_the_axis_says_nothing(self):
        e = engine()
        live(e.pad, rt=0.9)
        self.assertIsNotNone(e._pad_axis(AXIS, MSG))
        e.pad["at"] = time.monotonic() - 2.0
        self.assertIsNone(e._pad_axis(AXIS, MSG),
                          "a stale stick must starve the watchdog, not feed it")

    def test_it_does_not_emit_a_zero_instead(self):
        """The trap this is here to stop: 0.0 is a perfectly good command."""
        e = engine()
        live(e.pad, rt=0.9)
        e.pad["at"] = time.monotonic() - 2.0
        self.assertIsNone(e._pad_axis(AXIS, MSG))

    def test_a_held_button_falls_to_zero_when_the_pad_goes(self):
        e = engine()
        live(e.pad)
        padmod.press(e.pad, "a", True)
        self.assertEqual(e._pad_button(HELD, MSG)["payload"], 1)
        e.pad["at"] = time.monotonic() - 2.0
        self.assertEqual(e._pad_button(HELD, MSG)["payload"], 0,
                         "an arm built on held has to release by itself")

    def test_a_held_button_is_zero_before_any_pad_has_ever_reported(self):
        self.assertEqual(engine()._pad_button(HELD, MSG)["payload"], 0)

    def test_stale_ms_of_zero_turns_the_check_off(self):
        """A flow that would rather hold the last value says so explicitly."""
        e = engine()
        live(e.pad, rt=0.9)
        e.pad["at"] = time.monotonic() - 30.0
        self.assertIsNone(e._pad_axis(AXIS, MSG))
        self.assertIsNotNone(e._pad_axis(dict(AXIS, stale_ms=0), MSG))


class TestButtonEdges(unittest.TestCase):
    def test_pressed_fires_once_per_press(self):
        e = engine()
        live(e.pad)
        cfg = {"button": "b", "emit": "pressed", "stale_ms": 250}
        self.assertIsNone(e._pad_button(cfg, MSG), "nothing has happened yet")
        padmod.press(e.pad, "b", True)
        self.assertEqual(e._pad_button(cfg, MSG)["payload"], 1)
        self.assertIsNone(e._pad_button(cfg, MSG), "one press, one message")

    def test_released_fires_on_the_way_up(self):
        e = engine()
        live(e.pad)
        cfg = {"button": "b", "emit": "released", "stale_ms": 250}
        padmod.press(e.pad, "b", True)
        self.assertIsNone(e._pad_button(cfg, MSG))
        padmod.press(e.pad, "b", False)
        self.assertEqual(e._pad_button(cfg, MSG)["payload"], 1)

    def test_a_tap_between_two_reads_is_not_lost(self):
        """Polled at 100ms, a 30ms tap happens entirely between two passes."""
        e = engine()
        live(e.pad)
        padmod.press(e.pad, "b", True)
        padmod.press(e.pad, "b", False)
        cfg = {"button": "b", "emit": "released", "stale_ms": 250}
        self.assertEqual(e._pad_button(cfg, MSG)["payload"], 1)

    def test_an_edge_says_nothing_once_the_pad_is_gone(self):
        e = engine()
        live(e.pad)
        padmod.press(e.pad, "b", True)
        e.pad["at"] = time.monotonic() - 2.0
        cfg = {"button": "b", "emit": "pressed", "stale_ms": 250}
        self.assertIsNone(e._pad_button(cfg, MSG))


class TestTheLinkNode(unittest.TestCase):
    def test_it_passes_the_message_through_and_says_whether_there_is_a_pad(self):
        e = engine()
        out = e._pad_link("f", "n", {"name": ""}, {"payload": 0, "meta": {}})
        self.assertEqual(out["payload"], 0)
        self.assertIs(out["meta"]["pad"], False)

    def test_a_falsy_message_stops_the_reader(self):
        e = engine()
        live(e.pad, rt=0.7)
        e._pad_link("f", "n", {"name": ""}, {"payload": 0, "meta": {}})
        self.assertFalse(e.pad.get("connected"))
        self.assertIsNone(e.pad_reader)
        self.assertIsNone(e._pad_axis(AXIS, MSG),
                          "dropping the pad has to starve the chain too")


class TestTheVariables(unittest.TestCase):
    def test_every_axis_is_reachable_as_a_variable(self):
        names = {v["name"] for v in flows.VARIABLES if v["group"] == "Controller"}
        self.assertEqual(names, {"pad.lx", "pad.ly", "pad.rx", "pad.ry",
                                 "pad.lt", "pad.rt", "pad.connected"})

    def test_no_button_is_a_variable(self):
        """`{{pad.x}}` would be the X button to whoever wrote it and the X axis
        to whoever read it."""
        names = {v["name"] for v in flows.VARIABLES}
        for key in padmod.BUTTONS.values():
            self.assertNotIn("pad." + key, names)

    def test_a_formula_can_read_a_stick(self):
        state = live(padmod.blank(), lx=0.25)
        self.assertEqual(flows.render("{{pad.lx}}", {}, {"pad": state}), "0.25")

    def test_with_no_pad_it_renders_empty_rather_than_zero(self):
        """Empty is wrong-looking, which is the point: 0 would look correct."""
        self.assertEqual(flows.render("{{pad.lx}}", {}, {}), "")

    def test_an_unknown_pad_name_stays_verbatim(self):
        self.assertEqual(flows.render("{{pad.nope}}", {}, {}), "{{pad.nope}}")


class TestTheExample(unittest.TestCase):
    def setUp(self):
        from zero2w_console import examples
        self.flow = examples.by_id("ex_motor_pad")

    def test_it_ships_switched_off_and_bound_to_a_board(self):
        self.assertIs(self.flow["enabled"], False)
        self.assertEqual(self.flow["board"], "esp32")

    def test_every_node_in_it_can_run_on_that_board(self):
        self.assertEqual(self.flow["unsupported"], [])

    def test_the_controller_node_comes_before_anything_that_reads_it(self):
        """It takes the snapshot, so a reader upstream of it reads last tick."""
        order = {}
        for edge in self.flow["edges"]:
            order.setdefault(edge["from"], []).append(edge["to"])
        kinds = {n["id"]: n["type"] for n in self.flow["nodes"]}
        link = [nid for nid, t in kinds.items() if t == "pad.link"][0]
        reachable, stack = set(), list(order.get(link, []))
        while stack:
            nid = stack.pop()
            if nid in reachable:
                continue
            reachable.add(nid)
            stack.extend(order.get(nid, []))
        for nid, kind in kinds.items():
            if kind in ("pad.axis", "pad.button"):
                self.assertIn(nid, reachable,
                              "%s is not downstream of the Controller" % nid)

    def test_steering_reads_the_snapshot_rather_than_a_wire(self):
        formulas = [n["config"]["expr"] for n in self.flow["nodes"]
                    if n["type"] == "math.expr"]
        self.assertEqual(len(formulas), 2)
        for expr in formulas:
            self.assertIn("{{pad.lx}}", expr)

    def test_the_watchdog_resets_the_ramps_as_well_as_zeroing_the_pins(self):
        """So a pad coming back after a dropout cannot resume at the duty it
        left, whatever happens to feed the arm gate."""
        kinds = {n["id"]: n["type"] for n in self.flow["nodes"]}
        resets = [e for e in self.flow["edges"] if e["toPort"] == "reset"]
        sources = {kinds[e["from"]] for e in resets}
        self.assertIn("safety.watchdog", sources)
        self.assertIn("logic.if", sources)

    def test_it_needs_only_the_arm_tag_and_that_tag_is_shared(self):
        tags = {t["name"]: t for t in self.flow["needs_tags"]}
        self.assertEqual(set(tags), {"drive_armed"})
        self.assertTrue(tags["drive_armed"]["share"])

    def test_the_advice_is_clean(self):
        self.assertEqual(flows.advice(self.flow), [])


if __name__ == "__main__":
    unittest.main()
