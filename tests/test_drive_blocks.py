"""The drive blocks: dead zone, sign split, ramp, latch, watchdog."""
import importlib.util
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402
from zero2w_console.agent.modules import blocks  # noqa: E402
from zero2w_console.agent.modules import drive  # noqa: E402
# The recording hardware stubs, so there is one of them rather than two that
# drift apart.
from tests.test_agent_pwm import FakePin, FakePWM, load_runner  # noqa: E402

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")


# ---------------------------------------------------------------- the maths
class TestDeadband(unittest.TestCase):
    def test_a_resting_stick_reads_as_nothing(self):
        for raw in (0.0, 0.05, -0.05, 0.1, -0.1):
            value, inside = drive.deadband(raw, {"threshold": 0.1, "full": 1})
            self.assertTrue(inside, "%r should be inside the dead zone" % raw)
            self.assertEqual(value, 0.0)

    def test_past_the_threshold_it_starts_from_zero_again(self):
        """Without rescaling the output jumps; with it, it starts."""
        value, inside = drive.deadband(0.1001, {"threshold": 0.1, "full": 1})
        self.assertFalse(inside)
        self.assertAlmostEqual(value, 0.0, places=3)

    def test_and_still_reaches_full(self):
        value, _ = drive.deadband(1.0, {"threshold": 0.1, "full": 1})
        self.assertAlmostEqual(value, 1.0, places=6)
        value, _ = drive.deadband(-1.0, {"threshold": 0.1, "full": 1})
        self.assertAlmostEqual(value, -1.0, places=6)

    def test_halfway_past_the_threshold_is_halfway_out(self):
        value, _ = drive.deadband(0.55, {"threshold": 0.1, "full": 1})
        self.assertAlmostEqual(value, 0.5, places=6)

    def test_without_rescaling_the_value_passes_through_as_it_was(self):
        value, _ = drive.deadband(0.2, {"threshold": 0.1, "full": 1,
                                        "rescale": "no"})
        self.assertAlmostEqual(value, 0.2, places=6)

    def test_it_is_symmetric(self):
        cfg = {"threshold": 0.15, "full": 1}
        up, _ = drive.deadband(0.6, cfg)
        down, _ = drive.deadband(-0.6, cfg)
        self.assertAlmostEqual(up, -down, places=9)

    def test_beyond_full_scale_is_clamped_not_amplified(self):
        value, _ = drive.deadband(3.0, {"threshold": 0.1, "full": 1})
        self.assertAlmostEqual(value, 1.0, places=6)

    def test_a_threshold_at_or_past_full_scale_does_not_divide_by_zero(self):
        value, inside = drive.deadband(0.9, {"threshold": 1, "full": 1})
        self.assertTrue(inside)
        value, inside = drive.deadband(1.5, {"threshold": 1, "full": 1})
        self.assertFalse(inside)
        self.assertAlmostEqual(value, 1.0, places=6)


class TestSplit(unittest.TestCase):
    def test_size_is_a_duty(self):
        value, _ = drive.split_drive(-0.5, {"part": "size", "full": 1, "scale": 100})
        self.assertAlmostEqual(value, 50.0, places=6)

    def test_direction_is_a_pin_level(self):
        self.assertEqual(drive.split_drive(0.5, {"part": "direction"})[0], 1)
        self.assertEqual(drive.split_drive(-0.5, {"part": "direction"})[0], 0)
        self.assertEqual(drive.split_drive(0.0, {"part": "direction"})[0], 1)

    def test_the_two_halves_of_an_h_bridge_never_fight(self):
        """The property that matters."""
        cfg_f = {"part": "forward only", "full": 1, "scale": 100}
        cfg_b = {"part": "back only", "full": 1, "scale": 100}
        for raw in (-1, -0.6, -0.01, 0, 0.01, 0.6, 1):
            fwd, _ = drive.split_drive(raw, cfg_f)
            back, _ = drive.split_drive(raw, cfg_b)
            with self.subTest(raw=raw):
                self.assertFalse(fwd > 0 and back > 0,
                                 "both H-bridge inputs driven at %r" % raw)

    def test_forward_only_carries_the_size_forward_and_nothing_back(self):
        cfg = {"part": "forward only", "full": 1, "scale": 100}
        self.assertAlmostEqual(drive.split_drive(0.75, cfg)[0], 75.0, places=6)
        self.assertEqual(drive.split_drive(-0.75, cfg)[0], 0.0)

    def test_back_only_is_the_mirror(self):
        cfg = {"part": "back only", "full": 1, "scale": 100}
        self.assertAlmostEqual(drive.split_drive(-0.75, cfg)[0], 75.0, places=6)
        self.assertEqual(drive.split_drive(0.75, cfg)[0], 0.0)

    def test_invert_swaps_which_way_is_forward(self):
        plain = {"part": "forward only", "full": 1, "scale": 100}
        flipped = dict(plain, invert="yes")
        self.assertEqual(drive.split_drive(0.8, flipped)[0], 0.0)
        self.assertAlmostEqual(drive.split_drive(-0.8, flipped)[0], 80.0, places=6)
        self.assertEqual(drive.split_drive(0.8, plain)[1], True)
        self.assertEqual(drive.split_drive(0.8, flipped)[1], False)

    def test_beyond_full_scale_is_clamped(self):
        cfg = {"part": "size", "full": 1, "scale": 100}
        self.assertAlmostEqual(drive.split_drive(9.0, cfg)[0], 100.0, places=6)

    def test_a_zero_full_scale_does_not_divide_by_zero(self):
        value, _ = drive.split_drive(0.5, {"part": "size", "full": 0, "scale": 100})
        self.assertAlmostEqual(value, 50.0, places=6)

    def test_an_upstream_that_already_works_in_percent(self):
        cfg = {"part": "size", "full": 100, "scale": 100}
        self.assertAlmostEqual(drive.split_drive(-40, cfg)[0], 40.0, places=6)


class TestTheFloorThatMakesItTurn(unittest.TestCase):
    """`floor`: the duty under which a geared motor does not move."""

    cfg = {"part": "size", "full": 1, "scale": 100, "floor": 25}

    def test_nought_stays_nought(self):
        """The one that must not be got wrong."""
        self.assertEqual(drive.split_drive(0.0, self.cfg)[0], 0.0)

    def test_the_smallest_thing_asked_for_comes_out_at_the_floor(self):
        value, _ = drive.split_drive(0.000001, self.cfg)
        self.assertAlmostEqual(value, 25.0, places=3)

    def test_full_scale_still_reaches_full_scale(self):
        value, _ = drive.split_drive(1.0, self.cfg)
        self.assertAlmostEqual(value, 100.0, places=6)

    def test_the_middle_is_rescaled_rather_than_clamped(self):
        """Clamping would make the bottom quarter of the travel one value:
        the wheel would break away and then sit there while the command
        climbed."""
        value, _ = drive.split_drive(0.5, self.cfg)
        self.assertAlmostEqual(value, 62.5, places=6)   # 25 + 0.5 x 75

    def test_it_is_monotonic_and_inside_the_band(self):
        last = -1.0
        for i in range(1, 201):
            value, _ = drive.split_drive(i / 200.0, self.cfg)
            self.assertGreaterEqual(value, 25.0 - 1e-9)
            self.assertLessEqual(value, 100.0 + 1e-9)
            self.assertGreater(value, last)
            last = value

    def test_no_floor_behaves_exactly_as_before(self):
        """Most things do something useful at low duty — a servo, a lamp —
        so the default has to be the old arithmetic to the last decimal."""
        plain = {"part": "size", "full": 1, "scale": 100}
        for raw in (0.0, 0.01, 0.25, 0.5, 0.75, 1.0, -0.4, 9.0):
            self.assertAlmostEqual(drive.split_drive(raw, plain)[0],
                                   drive.split_drive(raw, dict(plain, floor=0))[0],
                                   places=9)

    def test_the_two_halves_still_never_fight_with_a_floor_on(self):
        fwd = dict(self.cfg, part="forward only")
        back = dict(self.cfg, part="back only")
        for i in range(-100, 101):
            raw = i / 100.0
            a, b = drive.split_drive(raw, fwd)[0], drive.split_drive(raw, back)[0]
            self.assertFalse(a > 0 and b > 0, "both driven at %r" % raw)

    def test_a_floor_above_the_ceiling_is_just_the_ceiling(self):
        silly = dict(self.cfg, floor=250)
        for raw in (0.1, 0.5, 1.0):
            self.assertAlmostEqual(drive.split_drive(raw, silly)[0], 100.0,
                                   places=6)
        self.assertEqual(drive.split_drive(0.0, silly)[0], 0.0)

    def test_a_negative_floor_is_read_as_its_size(self):
        self.assertAlmostEqual(drive.split_drive(0.5, dict(self.cfg, floor=-25))[0],
                               62.5, places=6)

    def test_a_direction_pin_is_untouched_by_it(self):
        pin = {"part": "direction", "floor": 25}
        self.assertEqual(drive.split_drive(0.5, pin)[0], 1)
        self.assertEqual(drive.split_drive(-0.5, pin)[0], 0)


class TestTheDeadStep(unittest.TestCase):
    """A reversal stops at zero for one step."""

    cfg = {"rate_up": 1.2, "rate_down": 4}

    def walk(self, start, target, step=0.1, limit=60):
        value, seen = start, []
        for _ in range(limit):
            value, arrived = drive.ramp_toward(value, target, step, self.cfg)
            seen.append(value)
            if arrived:
                break
        return seen

    def test_falling_through_zero_lands_on_it(self):
        self.assertIn(0.0, self.walk(0.3, -0.8))

    def test_rising_through_zero_lands_on_it(self):
        self.assertIn(0.0, self.walk(-0.3, 0.8))

    def test_it_is_one_step_and_not_a_trap(self):
        seen = self.walk(0.3, -0.8)
        self.assertEqual(seen.count(0.0), 1, seen)
        self.assertAlmostEqual(seen[-1], -0.8, places=6)

    def test_it_still_arrives_from_a_standstill(self):
        """Starting at zero is not a crossing, so nothing should be inserted."""
        seen = self.walk(0.0, -0.8)
        self.assertNotEqual(seen[0], 0.0, seen)
        self.assertAlmostEqual(seen[-1], -0.8, places=6)

    def test_moving_within_one_sign_is_unaffected(self):
        seen = self.walk(0.2, 0.9)
        self.assertNotIn(0.0, seen)
        seen = self.walk(-0.2, -0.9)
        self.assertNotIn(0.0, seen)

    def test_stopping_at_zero_is_not_turned_into_two_steps(self):
        """Ramping down to rest is not a reversal and must not gain a step."""
        seen = self.walk(0.8, 0.0)
        self.assertEqual(seen.count(0.0), 1, seen)

    def test_a_single_huge_step_still_pauses(self):
        """An elapsed long enough to cross the whole range in one go is
        exactly when a dead step matters most."""
        value, arrived = drive.ramp_toward(0.9, -0.9, 10.0, self.cfg)
        self.assertEqual(value, 0.0)
        self.assertFalse(arrived)


class TestRamp(unittest.TestCase):
    cfg = {"rate_up": 1, "rate_down": 2, "max_gap_ms": 0}

    def test_it_moves_at_the_rate_and_no_faster(self):
        value, arrived = drive.ramp_toward(0, 1, 0.5, self.cfg)
        self.assertAlmostEqual(value, 0.5, places=6)
        self.assertFalse(arrived)

    def test_it_stops_when_it_gets_there(self):
        value, arrived = drive.ramp_toward(0, 1, 5, self.cfg)
        self.assertAlmostEqual(value, 1.0, places=6)
        self.assertTrue(arrived)

    def test_falling_uses_the_other_rate(self):
        value, _ = drive.ramp_toward(1, 0, 0.25, self.cfg)
        self.assertAlmostEqual(value, 0.5, places=6)

    def test_most_things_should_stop_faster_than_they_start(self):
        up, _ = drive.ramp_toward(0, 1, 0.3, self.cfg)
        down, _ = drive.ramp_toward(1, 0, 0.3, self.cfg)
        self.assertGreater(1 - down, up, "falling was not the quicker move")

    def test_a_rate_of_zero_means_no_limit(self):
        value, arrived = drive.ramp_toward(0, 1, 0.001, {"rate_up": 0})
        self.assertEqual(value, 1)
        self.assertTrue(arrived)

    def test_reversing_through_zero_is_a_fall_then_a_rise(self):
        """Full forward to full back passes through nothing, and slowing
        down is not the same move as speeding up."""
        value, _ = drive.ramp_toward(1.0, -1.0, 0.1, self.cfg)
        self.assertAlmostEqual(value, 0.8, places=6)   # rate_down, 2/s
        value, _ = drive.ramp_toward(-0.1, -1.0, 0.1, self.cfg)
        self.assertAlmostEqual(value, -0.2, places=6)  # rate_up, 1/s

    def test_it_never_overshoots(self):
        value, _ = drive.ramp_toward(0.9, 1.0, 10, self.cfg)
        self.assertAlmostEqual(value, 1.0, places=6)
        value, _ = drive.ramp_toward(-0.9, -1.0, 10, self.cfg)
        self.assertAlmostEqual(value, -1.0, places=6)

    def test_no_elapsed_time_means_no_movement(self):
        value, _ = drive.ramp_toward(0.3, 1.0, 0, self.cfg)
        self.assertAlmostEqual(value, 0.3, places=6)

    def test_a_clock_that_went_backwards_does_not_move_it(self):
        value, _ = drive.ramp_toward(0.3, 1.0, -5, self.cfg)
        self.assertAlmostEqual(value, 0.3, places=6)


class TestTheLongestStep(unittest.TestCase):
    """A stall between messages must not turn into a jump."""

    cfg = {"rate_up": 1.2, "rate_down": 4}

    def test_a_long_gap_counts_as_the_default_quarter_second(self):
        value, arrived = drive.ramp_toward(0, 1, 5.0, self.cfg)
        self.assertAlmostEqual(value, 0.3, places=6)     # 1.2/s for 0.25s
        self.assertFalse(arrived)

    def test_a_gap_under_the_cap_counts_in_full(self):
        value, _ = drive.ramp_toward(0, 1, 0.1, self.cfg)
        self.assertAlmostEqual(value, 0.12, places=6)

    def test_the_cap_is_a_setting(self):
        value, _ = drive.ramp_toward(0, 1, 5.0, dict(self.cfg, max_gap_ms=500))
        self.assertAlmostEqual(value, 0.6, places=6)

    def test_zero_counts_the_whole_gap(self):
        value, arrived = drive.ramp_toward(0, 1, 5.0, dict(self.cfg, max_gap_ms=0))
        self.assertEqual((value, arrived), (1, True))

    def test_the_shipped_motor_ramps_keep_the_default(self):
        from zero2w_console import examples
        for fid in ("ex_motor_drive", "ex_motor_pad"):
            for node in examples.by_id(fid)["nodes"]:
                if node["type"] == "math.ramp":
                    with self.subTest(flow=fid, node=node["id"]):
                        cap = node["config"].get("max_gap_ms", 250)
                        self.assertTrue(0 < float(cap) <= 250)


# --------------------------------------------------------------- the engine
class Block(unittest.TestCase):
    ntype = None

    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)

    def tearDown(self):
        try:
            self.e.stop()
        except Exception:
            pass

    def send(self, cfg, payload, port=None):
        node = {"id": "n", "type": self.ntype, "config": cfg}
        _p, out = self.e._execute({"id": "f", "name": "f"}, node,
                                  {"payload": payload, "meta": {}}, port)
        return out


class TestDeadbandNode(Block):
    ntype = "math.deadband"

    def test_the_resting_value_is_configurable(self):
        out = self.send({"threshold": 0.1, "full": 1, "resting": "off"}, 0.02)
        self.assertEqual(out["payload"], "off")
        self.assertTrue(out["meta"]["inside"])

    def test_it_reports_what_arrived(self):
        out = self.send({"threshold": 0.1, "full": 1}, 0.6)
        self.assertAlmostEqual(out["meta"]["raw"], 0.6, places=6)
        self.assertFalse(out["meta"]["inside"])


class TestSplitNode(Block):
    ntype = "math.split"

    def test_a_direction_pin_gets_a_whole_number(self):
        out = self.send({"part": "direction"}, -0.4)
        self.assertEqual(out["payload"], 0)
        self.assertIs(out["meta"]["forward"], False)


class TestRampNode(Block):
    ntype = "math.ramp"

    def test_the_first_message_starts_from_the_configured_value(self):
        out = self.send({"rate_up": 1000, "start": 0}, 50)
        self.assertEqual(out["meta"]["target"], 50)

    def test_reset_forgets_where_it_was_and_says_nothing(self):
        """A reset restores state; it must not issue a command."""
        cfg = {"rate_up": 1000, "start": 7}
        self.send(cfg, 90)
        self.assertIsNone(self.send(cfg, 0, port="reset"),
                          "a reset emitted a message")

    def test_and_the_next_real_message_starts_from_the_configured_value(self):
        """Forgetting has to actually work, or a reset would be cosmetic."""
        cfg = {"rate_up": 0.001, "start": 7}
        self.send(cfg, 100)
        self.send(cfg, 0, port="reset")
        out = self.send(cfg, 100)
        self.assertAlmostEqual(out["payload"], 7, delta=0.5)

    def test_a_slow_ramp_does_not_arrive_at_once(self):
        cfg = {"rate_up": 0.001, "start": 0}
        out = self.send(cfg, 100)
        self.assertLess(out["payload"], 100)
        self.assertFalse(out["meta"]["arrived"])


class TestLatchNode(Block):
    ntype = "logic.latch"

    def test_set_holds_on_through_later_messages(self):
        cfg = {"emit": "every message"}
        self.assertEqual(self.send(cfg, 1, port="in")["payload"], "1")
        self.assertEqual(self.send(cfg, 0, port="in")["payload"], "1")
        self.assertEqual(self.send(cfg, "anything", port="in")["payload"], "1")

    def test_reset_clears_it(self):
        cfg = {"emit": "every message"}
        self.send(cfg, 1, port="in")
        self.assertEqual(self.send(cfg, 1, port="reset")["payload"], "0")

    def test_an_edge_with_no_port_sets_rather_than_landing_nowhere(self):
        """A saved edge carries toPort "in" — and the device reads it the
        same way, so this must not be a place the two sides differ."""
        out = self.send({"emit": "every message"}, 1, port=None)
        self.assertEqual(out["payload"], "1")

    def test_on_change_stays_quiet_while_nothing_moves(self):
        cfg = {}
        self.assertIsNotNone(self.send(cfg, 1, port="in"))
        self.assertIsNone(self.send(cfg, 1, port="in"))
        self.assertIsNotNone(self.send(cfg, 1, port="reset"))
        self.assertIsNone(self.send(cfg, 1, port="reset"))

    def test_it_can_start_latched(self):
        self.assertIsNone(self.send({"start": "on"}, 1, port="in"))


class TestWatchdogNode(Block):
    ntype = "safety.watchdog"

    def catch(self):
        """The scheduler is what does the firing, and a bare engine has not
        started it — engine.start() does, and that wants real hardware."""
        fired = []
        self.e.clock.deliver = lambda key, msg: fired.append(msg)
        if not self.e.clock.is_alive():
            self.e.clock.start()
        return fired

    def test_being_fed_says_nothing(self):
        self.assertIsNone(self.send({"timeout": 5000}, 1))

    def test_it_fires_when_the_feed_stops(self):
        fired = self.catch()
        self.send({"timeout": 20, "value": "0"}, 1)
        deadline = _real.monotonic() + 3
        while not fired and _real.monotonic() < deadline:
            _real.sleep(0.01)
        self.assertTrue(fired, "the watchdog never tripped")
        self.assertEqual(fired[0]["payload"], "0")
        self.assertTrue(fired[0]["meta"]["starved"])

    def test_a_steady_feed_never_trips_it(self):
        fired = self.catch()
        for _ in range(12):
            self.send({"timeout": 200, "value": "0"}, 1)
            _real.sleep(0.02)
        self.assertEqual(fired, [], "it tripped while it was still being fed")

    def test_a_flow_that_is_never_fed_never_trips(self):
        """Nothing is armed until a message arrives."""
        fired = self.catch()
        _real.sleep(0.1)
        self.assertEqual(fired, [])

    def test_recovery_is_reported_only_when_asked(self):
        fired = self.catch()
        cfg = {"timeout": 20, "value": "0", "recovery": "yes"}
        self.send(cfg, 1)
        deadline = _real.monotonic() + 3
        while not fired and _real.monotonic() < deadline:
            _real.sleep(0.01)
        back = self.send(cfg, 55)
        self.assertIsNotNone(back, "the feed coming back was not reported")
        self.assertFalse(back["meta"]["starved"])
        self.assertEqual(back["payload"], 55)


# ------------------------------------------------------- host against device
class TestBothSidesAgree(unittest.TestCase):
    """The test that `pwm.out` needed and did not have."""

    def setUp(self):
        machine = type(sys)("machine")

        class Pin:
            IN = OUT = PULL_UP = 0
            IRQ_RISING, IRQ_FALLING = 1, 2

            def __init__(self, *a, **kw):
                pass

            def value(self, *a):
                return 0

            def irq(self, **kw):
                pass

        machine.Pin = machine.PWM = Pin
        sys.modules["machine"] = machine
        # On a board these sit under modules/ and the runner reaches them with
        # __import__("modules.x"). Stand that package up here so the device
        # path under test is the real one.
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.drive, pkg.blocks = drive, blocks
        sys.modules["modules"] = pkg
        sys.modules["modules.drive"] = drive
        sys.modules["modules.blocks"] = blocks
        spec = importlib.util.spec_from_file_location("agentflow_drive", AGENT)
        self.dev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dev)
        clock = type(sys)("time")
        clock.time, clock.sleep = _real.time, _real.sleep
        clock.sleep_ms = lambda ms: None
        clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
        clock.ticks_diff = lambda a, b: a - b
        clock.ticks_add = lambda t, d: t + d
        self.dev.time = clock
        self.host = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)

    def tearDown(self):
        for name in ("machine", "modules", "modules.drive",
                     "modules.blocks"):
            sys.modules.pop(name, None)
        try:
            self.host.stop()
        except Exception:
            pass

    def on_device(self, ntype, cfg, payload):
        class Agent:
            tags = {}

            def log(self, *a, **kw):
                pass

        flow = {"id": "f", "nodes": [{"id": "n", "type": ntype, "config": cfg}],
                "edges": []}
        r = self.dev.Runner(flow, Agent())
        sent = []
        r._emit = lambda nid, msg, hops, port="out": sent.append(msg)
        r._run("n", {"payload": payload, "meta": {}}, 0)
        return sent[0] if sent else None

    def on_host(self, ntype, cfg, payload):
        _p, out = self.host._execute({"id": "f", "name": "f"},
                                     {"id": "n", "type": ntype, "config": cfg},
                                     {"payload": payload, "meta": {}}, None)
        return out

    def compare(self, ntype, cfg, payloads):
        for payload in payloads:
            with self.subTest(node=ntype, payload=payload):
                host = self.on_host(ntype, cfg, payload)
                dev = self.on_device(ntype, cfg, payload)
                self.assertIsNotNone(dev, "the device produced nothing")
                self.assertAlmostEqual(float(host["payload"]),
                                       float(dev["payload"]), places=4)

    SWEEP = (-1.0, -0.6, -0.11, -0.09, 0.0, 0.09, 0.11, 0.6, 1.0, 2.5)

    def test_the_dead_zone_agrees(self):
        self.compare("math.deadband",
                     {"threshold": 0.1, "full": 1, "decimals": 4}, self.SWEEP)

    def test_every_part_of_the_split_agrees(self):
        for part in ("size", "direction", "forward only", "back only"):
            self.compare("math.split",
                         {"part": part, "full": 1, "scale": 100}, self.SWEEP)

    def test_the_split_agrees_when_inverted_too(self):
        self.compare("math.split",
                     {"part": "forward only", "full": 1, "scale": 100,
                      "invert": "yes"}, self.SWEEP)

    def branch(self, op, value, payload):
        """Which port each engine sends an If out of, for the same inputs."""
        cfg = {"op": op, "value": value}
        host_port, _out = self.host._execute(
            {"id": "f", "name": "f"},
            {"id": "n", "type": "logic.if", "config": cfg},
            {"payload": payload, "meta": {}})

        class Agent:
            tags = {}

            def log(self, *a, **kw):
                pass

        r = self.dev.Runner({"id": "f", "nodes": [{"id": "n", "type": "logic.if",
                                                   "config": cfg}],
                             "edges": []}, Agent())
        seen = []
        r._emit = lambda nid, msg, hops, port="out": seen.append(port)
        r._run("n", {"payload": payload, "meta": {}}, 0)
        return host_port, (seen[0] if seen else None)

    def test_the_if_agrees_about_ordering_something_that_is_not_a_number(self):
        """Both engines had the alphabetical fallback, so both had to lose
        it."""
        for payload in ("off", "on", "", None, "abc"):
            for op in (">", ">=", "<", "<="):
                with self.subTest(payload=payload, op=op):
                    host, dev = self.branch(op, "0.5", payload)
                    self.assertEqual(host, "false", "host let %r past" % payload)
                    self.assertEqual(dev, "false", "device let %r past" % payload)

    def test_the_if_still_agrees_about_real_numbers(self):
        for payload, want in ((1, "true"), (0, "false"), (0.5, "true"),
                              ("0.9", "true"), (True, "true"), (False, "false")):
            with self.subTest(payload=payload):
                host, dev = self.branch(">=", "0.5", payload)
                self.assertEqual(host, want)
                self.assertEqual(dev, want)

    def test_the_split_agrees_with_a_floor_on(self):
        """The field the motor flow depends on."""
        for part in ("size", "forward only", "back only"):
            self.compare("math.split",
                         {"part": part, "full": 1, "scale": 100, "floor": 25},
                         self.SWEEP)

    def test_neither_engine_has_its_own_copy_of_the_ramp(self):
        """The ramp cannot be compared by running it twice — it depends on
        how much time passed between the two calls, which is why the
        sweeps above stop at the stateless blocks."""
        for path in ("zero2w_console/flows.py",
                     "zero2w_console/agent/flow.py"):
            with self.subTest(path=path):
                with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertIn("ramp_toward(", src)
        # And the rates are read in one place only. `flows.py` names them once
        # more, to declare the fields; the device runner should not know them at
        # all, because knowing them is how a second implementation starts.
        with open(os.path.join(ROOT, "zero2w_console/agent/flow.py"),
                  encoding="utf-8") as fh:
            device = fh.read()
        for field in ("rate_up", "rate_down"):
            self.assertNotIn(field, device,
                             "%s is read in the device runner as well as in "
                             "drive.py" % field)

    def test_the_latch_agrees(self):
        for port in (None, "in", "reset"):
            with self.subTest(port=port):
                host = self.host._execute(
                    {"id": "f", "name": "f"},
                    {"id": "n", "type": "logic.latch",
                     "config": {"emit": "every message"}},
                    {"payload": 1, "meta": {}}, port)[1]

                class Agent:
                    tags = {}

                    def log(self, *a, **kw):
                        pass

                r = self.dev.Runner(
                    {"id": "f", "nodes": [{"id": "n", "type": "logic.latch",
                                           "config": {"emit": "every message"}}],
                     "edges": []}, Agent())
                sent = []
                r._emit = lambda nid, msg, hops, p="out": sent.append(msg)
                r._run("n", {"payload": 1, "meta": {}}, 0, port or "in")
                self.assertEqual(host["payload"], sent[0]["payload"])


class TestTheBenchFlowActuallyDrives(unittest.TestCase):
    """The shipped example, executed on a stub board."""

    def setUp(self):
        FakePin.log = []
        FakePWM.made = {}
        self.flowmod = load_runner()
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.drive, pkg.blocks = drive, blocks
        sys.modules["modules"] = pkg
        sys.modules["modules.drive"] = drive
        sys.modules["modules.blocks"] = blocks
        from zero2w_console import examples
        self.flow = examples.wired("ex_motor_bench")

        class Agent:
            tags = {}
            said = []

            def log(self, level, message, **kw):
                Agent.said.append((level, message))

        Agent.said = []
        self.agent = Agent()
        self.runner = self.flowmod.Runner(self.flow, self.agent)
        self.runner.start()

    def tearDown(self):
        for name in ("machine", "modules", "modules.drive",
                     "modules.blocks"):
            sys.modules.pop(name, None)

    def pump(self, seconds=0.0):
        """Let the runner work through whatever it has queued."""
        deadline = _real.monotonic() + seconds
        while True:
            self.runner.tick()
            if _real.monotonic() >= deadline and not self.runner.queue:
                return
            _real.sleep(0.005)

    def test_the_example_is_the_one_being_tested(self):
        """If the example is renamed or rewired, this should stop pretending
        to cover it."""
        self.assertIsNotNone(self.flow)
        self.assertEqual(self.flow["board"], "esp32")
        types = {n["type"] for n in self.flow["nodes"]}
        self.assertLessEqual({"math.deadband", "math.ramp", "math.split",
                              "safety.watchdog", "pwm.out"}, types)

    def feed(self, times=20, gap=0.02):
        """Fire it the way something actually driving it would."""
        for _ in range(times):
            self.runner.fire("go")
            self.pump(gap)

    def test_the_very_first_message_comes_out_at_the_starting_value(self):
        """Worth stating because it surprises: the ramp has no clock of its
        own, so the first message is what starts it rather than something
        it can already have moved in response to."""
        self.runner.fire("go")
        self.pump(0.05)
        self.assertEqual(FakePWM.made[27].percent, 0.0)

    def test_firing_it_drives_the_forward_pin_and_not_the_reverse_one(self):
        self.feed()
        self.assertIn(27, FakePWM.made, "the forward pin was never driven")
        self.assertGreater(FakePWM.made[27].percent, 0,
                           "the forward pin got no duty")
        reverse = FakePWM.made.get(25)
        self.assertTrue(reverse is None or reverse.percent == 0,
                        "both halves of the bridge were driven at once")

    def test_the_ramp_means_it_does_not_arrive_at_full_speed_at_once(self):
        self.runner.fire("go")
        self.pump(0.02)
        self.runner.fire("go")
        self.pump(0.02)
        early = FakePWM.made[27].percent
        self.feed()
        self.assertGreater(FakePWM.made[27].percent, early,
                           "the ramp never advanced past its first step")
        # Not 35, and three things account for the gap. The dead zone: 0.35
        # with a 0.08 threshold and rescaling on is (0.35 - 0.08) / (1 - 0.08)
        # = 0.293. Then the split's band: this example scales to 60 rather than
        # 100, because nothing in it can be armed and the number is the only
        # protection there is — and it floors at 25, because under about a
        # quarter duty a geared motor buzzes and warms without turning. So the
        # magnitude lands at 25 + 0.293 x (60 - 25) = 35.3%.
        # Worth asserting the real figure rather than the one somebody would
        # expect from reading the Set node, because that gap is exactly what
        # confuses people at the bench.
        self.assertAlmostEqual(FakePWM.made[27].percent, 35.3, delta=0.6)

    def test_it_stops_on_its_own_when_nothing_feeds_it(self):
        """The whole point of the watchdog, and the reason it runs here
        rather than on the host."""
        self.feed()
        self.assertGreater(FakePWM.made[27].percent, 0)
        timeout = 0.4 + 0.3          # the example's 400ms, plus slack
        deadline = _real.monotonic() + timeout
        while _real.monotonic() < deadline:
            self.runner.tick()
            _real.sleep(0.01)
        self.assertTrue(FakePWM.made[27].dead,
                        "the watchdog never released the forward channel")
        self.assertIn((27, 0), FakePin.log,
                      "the forward pin was not driven low after the trip")

    def test_a_steady_feed_keeps_it_running(self):
        self.feed(times=18, gap=0.03)
        self.assertFalse(FakePWM.made[27].dead,
                         "it tripped while it was still being fed")
        self.assertGreater(FakePWM.made[27].percent, 0)

    def test_nothing_moves_until_it_is_fired(self):
        """No node acts by existing — including on a board that just booted
        with this flow in flash."""
        self.pump(0.5)
        self.assertEqual(FakePWM.made, {},
                         "a pin was driven before anything fired")


if __name__ == "__main__":
    unittest.main()


class TestAMissingHandlerFailsClosed(unittest.TestCase):
    """A node that cannot do its job must not pass the message on."""

    def setUp(self):
        FakePin.log = []
        FakePWM.made = {}
        self.flowmod = load_runner()
        # Deliberately no `modules` package: this is the missing-module case.
        for name in ("modules", "modules.blocks", "modules.drive"):
            sys.modules.pop(name, None)

    def tearDown(self):
        sys.modules.pop("machine", None)

    def runner(self, nodes, edges):
        said = []

        class Agent:
            tags = {}

            def log(self, level, message, **kw):
                said.append((level, message))

        r = self.flowmod.Runner({"id": "f", "nodes": nodes, "edges": edges},
                                Agent())
        r.start()
        return r, said

    def test_a_watchdog_with_no_module_does_not_become_a_wire(self):
        r, said = self.runner(
            [{"id": "t", "type": "manual.fire", "config": {}},
             {"id": "dog", "type": "safety.watchdog",
              "config": {"timeout": 500, "value": "0"}},
             {"id": "stop", "type": "pwm.out",
              "config": {"gpio": 27, "action": "stop"}}],
            [{"id": "e1", "from": "t", "fromPort": "out", "to": "dog", "toPort": "in"},
             {"id": "e2", "from": "dog", "fromPort": "out", "to": "stop", "toPort": "in"}])
        r.fire("t")
        for _ in range(5):
            r.tick()
        self.assertEqual(FakePin.log, [],
                         "the message went straight through a watchdog that "
                         "has no handler, and drove the pin")
        self.assertTrue([m for lvl, m in said if "no handler" in m],
                        "it failed silently: %r" % (said,))

    def test_it_says_which_type_is_missing(self):
        r, said = self.runner(
            [{"id": "t", "type": "manual.fire", "config": {}},
             {"id": "s", "type": "math.scale", "config": {}}],
            [{"id": "e", "from": "t", "fromPort": "out", "to": "s", "toPort": "in"}])
        r.fire("t")
        for _ in range(3):
            r.tick()
        self.assertTrue([m for lvl, m in said if "math.scale" in m],
                        "the message does not name the node type: %r" % (said,))

    def test_the_level_is_loud(self):
        """This is a flow that cannot work, not a note."""
        r, said = self.runner(
            [{"id": "t", "type": "manual.fire", "config": {}},
             {"id": "s", "type": "math.scale", "config": {}}],
            [{"id": "e", "from": "t", "fromPort": "out", "to": "s", "toPort": "in"}])
        r.fire("t")
        for _ in range(3):
            r.tick()
        levels = [lvl for lvl, m in said if "no handler" in m]
        self.assertIn("critical", levels)

    def test_a_handler_that_is_present_is_unaffected(self):
        """The canary: this whole class would pass if nothing ever ran."""
        r, _said = self.runner(
            [{"id": "t", "type": "manual.fire", "config": {}},
             {"id": "p", "type": "pwm.out",
              "config": {"gpio": 33, "action": "start", "duty": 40}}],
            [{"id": "e", "from": "t", "fromPort": "out", "to": "p", "toPort": "in"}])
        r.fire("t")
        for _ in range(3):
            r.tick()
        self.assertIn(33, FakePWM.made)
