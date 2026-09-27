"""The shipped `Motor drive` example, executed on a stub board."""
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples, fleet                      # noqa: E402
from zero2w_console.agent.modules import blocks, drive, expr     # noqa: E402
from tests.test_agent_pwm import FakePWM, FakePin, load_runner   # noqa: E402

RIGHT_FWD, RIGHT_REV = 27, 25
LEFT_FWD, LEFT_REV = 33, 32
ALL_PINS = (RIGHT_FWD, RIGHT_REV, LEFT_FWD, LEFT_REV)
CAP = 100.0           # `scale` on the four Sign splits
FLOOR = 25.0          # and `floor`: the duty below which a wheel does not turn


class DriveCase(unittest.TestCase):
    def setUp(self):
        FakePin.log = []
        FakePWM.made = {}
        self.flowmod = load_runner()
        # Exactly the modules `fleet.agent_files` says this flow makes a device
        # pull, so the harness cannot be more generous than a real board. The
        # first run of this had no `expr`, and the whole chain silently did
        # nothing — which is how the flow would behave if the fetch failed.
        pkg = type(sys)("modules")
        pkg.__path__ = []
        sys.modules["modules"] = pkg
        for name, mod in (("drive", drive), ("blocks", blocks), ("expr", expr)):
            setattr(pkg, name, mod)
            sys.modules["modules." + name] = mod
        self.flow = examples.by_id("ex_motor_drive")

        test = self

        class Agent:
            tags = {"drive_armed": 0, "drive_speed": 0, "drive_steer": 0}

            def log(self, level, message, **kw):
                test.said.append((level, message))

        self.said = []
        self.agent = Agent()
        self.runner = self.flowmod.Runner(self.flow, self.agent)
        self.flowmod._RUNNER = self.runner
        self.runner.start()

    def tearDown(self):
        self.flowmod._RUNNER = None
        for name in ("machine", "modules", "modules.drive", "modules.blocks",
                     "modules.expr"):
            sys.modules.pop(name, None)

    # -- driving it --------------------------------------------------------
    def set(self, **tags):
        self.agent.tags.update(tags)

    def run_for(self, seconds):
        """Tick the way the agent's serve() slice does."""
        deadline = _real.monotonic() + seconds
        while _real.monotonic() < deadline:
            self.runner.tick()
            _real.sleep(0.005)
        while self.runner.queue:
            self.runner.tick()

    def duty(self, gpio):
        pwm = FakePWM.made.get(gpio)
        return None if pwm is None else pwm.percent

    def duties(self):
        return {g: self.duty(g) for g in ALL_PINS}

    def moving(self):
        return [g for g in ALL_PINS if (self.duty(g) or 0) > 0]

    def critical(self):
        return [m for lvl, m in self.said if lvl == "critical"]


class TestItIsTheShippedFlow(DriveCase):
    def test_the_example_exists_and_is_for_the_motor_board(self):
        self.assertIsNotNone(self.flow)
        self.assertEqual(self.flow["board"], "esp32")
        self.assertIs(self.flow["enabled"], False,
                      "a flow that drives motors must ship switched off")

    def test_every_node_in_it_can_run_on_a_device(self):
        """The point of the whole exercise."""
        self.assertEqual(examples.by_id("ex_motor_drive")["unsupported"], [])

    def test_it_names_the_tags_it_needs(self):
        names = {t["name"] for t in self.flow["needs_tags"]}
        self.assertEqual(names, {"drive_armed", "drive_speed", "drive_steer"})
        for tag in self.flow["needs_tags"]:
            self.assertTrue(tag["share"],
                            "%s has to be shared or the board never sees it"
                            % tag["name"])

    def test_the_harness_provides_what_a_real_device_would_pull(self):
        """If this flow gains a node needing a module the harness does not
        stand up, everything below would pass by doing nothing."""
        wanted = {n.split("/")[-1][:-3]
                  for n in fleet.Fleet(None, None, None).agent_files(
                      self.flow, {"board": "esp32"}) if n.startswith("modules/")}
        self.assertLessEqual(wanted, {"drive", "blocks", "expr", "pwm"})
        for name in wanted - {"pwm"}:        # pwm is stubbed by the fake board
            self.assertIn("modules." + name, sys.modules, name)

    def test_no_handler_is_missing_on_a_device(self):
        """Fail-closed dispatch logs `critical` and stops the branch."""
        self.set(drive_armed=1, drive_speed=0.5)
        self.run_for(0.4)
        self.assertEqual(self.critical(), [])


class TestTheThreeMotorFlowsAgree(unittest.TestCase):
    """Bench, Arm and Drive are meant to be usable together."""

    def flow(self, fid):
        return examples.by_id(fid)

    def test_arm_writes_the_tag_drive_reads(self):
        arm = self.flow("ex_motor_arm")
        drive = self.flow("ex_motor_drive")
        written = {n["config"]["tag"] for n in arm["nodes"]
                   if n["type"] == "tag.set"}
        read = {n["config"]["tag"] for n in drive["nodes"]
                if n["type"] == "tag.read"}
        self.assertIn("drive_armed", written, "Arm writes %s" % written)
        self.assertIn("drive_armed", read, "Drive reads %s" % read)

    def test_they_define_that_tag_identically(self):
        """Two examples may both declare a tag; importing the second must
        not find a different idea of what it is."""
        def defs(fid):
            return {t["name"]: t for t in self.flow(fid).get("needs_tags", [])}
        arm, drive = defs("ex_motor_arm"), defs("ex_motor_drive")
        shared = set(arm) & set(drive)
        self.assertEqual(shared, {"drive_armed"})
        for name in shared:
            self.assertEqual(arm[name], drive[name], name)

    def test_arm_runs_on_the_host_so_it_can_sit_beside_the_drive_flow(self):
        """A device holds one deployed flow."""
        arm = self.flow("ex_motor_arm")
        self.assertIsNone(arm.get("board"))
        self.assertEqual(arm["runs_on"], "this board")

    def test_arm_does_not_drive_a_pin_wired_to_nothing(self):
        """D14 is a spare opto output connected to nothing on this chassis."""
        for node in self.flow("ex_motor_arm")["nodes"]:
            self.assertNotEqual(node["type"], "gpio.out",
                                "the arm gate is a pin again")

    def test_arm_can_be_dropped_by_hand_and_not_only_by_the_watchdog(self):
        resets = [e for e in self.flow("ex_motor_arm")["edges"]
                  if e["toPort"] == "reset"]
        sources = {e["from"] for e in resets}
        kinds = {n["id"]: n["type"] for n in self.flow("ex_motor_arm")["nodes"]}
        self.assertIn("manual.fire", {kinds[s] for s in sources},
                      "nothing disarms it except waiting for the watchdog")
        self.assertIn("safety.watchdog", {kinds[s] for s in sources})

    def test_the_bench_flow_is_capped_lower_than_the_drive_flow(self):
        """Bench cannot be armed, so the number is the only protection it has."""
        def field(fid, key):
            return {n["config"][key] for n in self.flow(fid)["nodes"]
                    if n["type"] == "math.split"}
        bench, drive = field("ex_motor_bench", "scale"), field("ex_motor_drive", "scale")
        self.assertEqual(len(bench), 1, "the bench splits disagree: %s" % bench)
        self.assertEqual(len(drive), 1, "the drive splits disagree: %s" % drive)
        self.assertLess(max(bench), max(drive))

    def test_both_motor_flows_clear_breakaway(self):
        """Neither is any use with a floor of nothing: that was the bug."""
        for fid in ("ex_motor_bench", "ex_motor_drive", "ex_motor_pad"):
            floors = {n["config"].get("floor") for n in self.flow(fid)["nodes"]
                      if n["type"] == "math.split"}
            with self.subTest(flow=fid):
                self.assertEqual(len(floors), 1, "splits disagree: %s" % floors)
                self.assertGreaterEqual(max(floors), 20,
                                        "%s will buzz rather than turn" % fid)

    def test_every_motor_example_ships_switched_off(self):
        for fid in ("ex_motor_bench", "ex_motor_drive", "ex_motor_pad",
                    "ex_motor_arm"):
            with self.subTest(flow=fid):
                self.assertIs(self.flow(fid)["enabled"], False)


class TestNothingDrivesItWithoutTheArm(DriveCase):
    """Reported from the bench: it drove as soon as a steering value was
    set, with nothing armed."""

    def test_no_combination_of_speed_and_steer_drives_it_disarmed(self):
        for speed in (0, 0.5, -0.5, 1, -1):
            for steer in (0, 0.5, -0.5, 1, -1):
                with self.subTest(speed=speed, steer=steer):
                    self.setUp()
                    self.set(drive_armed=0, drive_speed=speed, drive_steer=steer)
                    self.run_for(1.2)
                    self.assertEqual(self.moving(), [],
                                     "drove with no arm at speed=%s steer=%s: %s"
                                     % (speed, steer, self.duties()))

    def test_it_does_not_drive_on_the_way_down_either(self):
        """Disarming while moving is when the reset actually fires."""
        self.set(drive_armed=1, drive_speed=0.0, drive_steer=0.6)
        self.run_for(2.0)
        self.assertTrue(self.moving(), "it never started, so this proves nothing")
        self.set(drive_armed=0)
        self.run_for(1.5)
        self.assertEqual(self.moving(), [], self.duties())

    def test_an_arm_that_is_not_a_number_is_not_an_arm(self):
        """A tag is a value someone can type, and anything that is not
        clearly on has to read as off."""
        for value in ("", None, "off", "no", 0, 0.4):
            with self.subTest(value=value):
                self.setUp()
                self.set(drive_armed=value, drive_speed=0.8, drive_steer=0.3)
                self.run_for(1.2)
                self.assertEqual(self.moving(), [],
                                 "armed by %r: %s" % (value, self.duties()))


class TestTheWatchdogIsNotFasterThanTheAgent(unittest.TestCase):
    """Reported from the bench: it drove for a second or so, stopped for
    about half a second, and drove again, in a loop."""

    def agent_source(self):
        path = os.path.join(ROOT, "zero2w_console", "agent", "agent.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def constant(self, name):
        for line in self.agent_source().splitlines():
            if line.startswith(name + " ="):
                return float(line.split("=", 1)[1].split("#")[0].strip())
        self.fail("no %s in agent.py" % name)

    def test_the_agent_does_not_park_on_the_command_poll(self):
        self.assertEqual(self.constant("COMMAND_WAIT"), 0,
                         "an agent parked on the poll is an agent not ticking")

    def test_the_host_honours_a_wait_of_zero(self):
        """No `max(1, ...)` clamp adding a second to every poll."""
        from zero2w_console import iot as iotmod
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            devices = iotmod.DeviceStore(os.path.join(tmp, "iot.json"))
            f = fleet.Fleet(devices, None, None)
            began = _real.monotonic()
            self.assertEqual(f.take_commands({"id": "dev_x"}, wait=0), [])
            self.assertLess(_real.monotonic() - began, 0.3,
                            "a zero wait still parked")
            began = _real.monotonic()
            f.take_commands({"id": "dev_y"}, wait=1)
            self.assertGreater(_real.monotonic() - began, 0.8,
                               "a device that asks for a second lost it")

    def test_the_flows_watchdog_outlasts_what_the_agent_blocks_for(self):
        """The invariant."""
        timeout = min(n["config"]["timeout"]
                      for n in examples.by_id("ex_motor_drive")["nodes"]
                      if n["type"] == "safety.watchdog")
        gap_ms = self.constant("COMMAND_WAIT") * 1000
        self.assertGreater(timeout, gap_ms + 200,
                           "the watchdog (%dms) does not clear the agent's "
                           "blocking gap (%dms)" % (timeout, gap_ms))


class TestDisarmedMeansStill(DriveCase):
    def test_a_speed_with_no_arm_drives_nothing(self):
        self.set(drive_speed=1.0)
        self.run_for(0.5)
        self.assertEqual(self.moving(), [],
                         "it drove with drive_armed at 0: %s" % self.duties())

    def test_arming_then_disarming_stops_it_inside_the_watchdog_window(self):
        self.set(drive_armed=1, drive_speed=0.8)
        self.run_for(1.2)
        self.assertTrue(self.moving(), "it never started, so this proves nothing")
        self.set(drive_armed=0)
        self.run_for(0.4 + 0.4)          # the example's 400ms, plus slack
        self.assertEqual(self.moving(), [],
                         "still driving after disarm: %s" % self.duties())

    def test_re_arming_starts_from_a_standstill(self):
        """Disarming resets the Ramp, so the drive does not come back at the
        speed it left off at."""
        self.set(drive_armed=1, drive_speed=0.9)
        self.run_for(1.5)
        fast = self.duty(RIGHT_FWD)
        self.assertGreater(fast, 0)
        self.set(drive_armed=0)
        self.run_for(0.8)
        self.set(drive_armed=1)
        self.run_for(0.12)               # one or two ticks only
        self.assertLess(self.duty(RIGHT_FWD), fast,
                        "it came straight back to speed on re-arm")


class TestReversingThroughZero(DriveCase):
    """The one case the steady-state checks cannot see."""

    def record(self):
        """Every duty write in order, with both bridges' live state at the time."""
        writes = []
        original = FakePWM.duty_u16

        def spy(pwm, value):
            original(pwm, value)
            writes.append((pwm.gpio, {g: (p.raw or 0)
                                      for g, p in FakePWM.made.items()}))

        FakePWM.duty_u16 = spy
        self.addCleanup(setattr, FakePWM, "duty_u16", original)
        return writes

    def both_live(self, writes):
        out = []
        for gpio, live in writes:
            for fwd, rev, side in ((RIGHT_FWD, RIGHT_REV, "right"),
                                   (LEFT_FWD, LEFT_REV, "left")):
                if (live.get(fwd) or 0) > 0 and (live.get(rev) or 0) > 0:
                    out.append((side, gpio, live))
        return out

    def test_a_reversal_never_has_both_halves_live(self):
        self.set(drive_armed=1, drive_speed=0.8)
        self.run_for(2.0)
        self.assertGreater(self.duty(RIGHT_FWD), 0, "it never went forward")
        writes = self.record()
        self.set(drive_speed=-0.8)
        self.run_for(2.5)
        self.assertGreater(self.duty(RIGHT_REV), 0, "it never reversed")
        self.assertTrue(writes, "nothing was written during the reversal")
        self.assertEqual(self.both_live(writes), [],
                         "both halves of a bridge were live mid-reversal")

    def test_a_steer_crossing_never_has_both_halves_live(self):
        """Steering past centre reverses one wheel while the other keeps
        going."""
        self.set(drive_armed=1, drive_speed=0.1, drive_steer=-0.9)
        self.run_for(2.5)
        writes = self.record()
        self.set(drive_steer=0.9)
        self.run_for(3.0)
        self.assertTrue(writes, "nothing was written during the crossing")
        self.assertEqual(self.both_live(writes), [])

    def test_it_does_not_depend_on_the_order_the_pins_are_written(self):
        """The strong form."""
        order = {"e_rampr_rf": 1, "e_rampr_rb": 0,
                 "e_rampl_lf": 1, "e_rampl_lb": 0}
        self.flow["edges"].sort(key=lambda e: order.get(e["id"], 2))
        self.runner = self.flowmod.Runner(self.flow, self.agent)
        self.flowmod._RUNNER = self.runner
        self.runner.start()
        self.set(drive_armed=1, drive_speed=0.8)
        self.run_for(3.0)
        writes = self.record()
        self.set(drive_speed=-0.8)
        self.run_for(3.0)
        self.assertEqual(self.both_live(writes), [])

    def test_a_steer_step_is_rate_limited_at_all(self):
        """The reason the ramp is per wheel rather than on the throttle."""
        self.set(drive_armed=1, drive_speed=0.1, drive_steer=-0.9)
        self.run_for(2.0)
        before = self.duty(LEFT_REV) or 0
        self.assertGreater(before, 0, "the left wheel was not reversing")
        self.set(drive_steer=0.9)
        self.run_for(0.12)                 # one or two ticks
        self.assertLess(self.duty(LEFT_FWD) or 0, before,
                        "a steer step crossed straight to the other direction "
                        "at full duty")

    def test_the_ramp_stops_at_zero_on_the_way_past_it(self):
        """What makes all of the above true, asserted on the arithmetic
        rather than inferred from the pins."""
        cfg = {"rate_up": 1.2, "rate_down": 4, "start": 0}
        for start, target in ((0.3, -0.8), (-0.3, 0.8)):
            with self.subTest(start=start, target=target):
                value, seen_zero = start, False
                for _ in range(40):
                    value = drive.ramp_toward(value, target, 0.1, cfg)[0]
                    if value == 0.0:
                        seen_zero = True
                    if value == target:
                        break
                self.assertTrue(seen_zero,
                                "a reversal stepped over zero instead of "
                                "stopping on it")
                self.assertEqual(value, target, "it never got there")


class TestTheBridgesAreNeverDrivenBothWays(DriveCase):
    def test_forwards_drives_one_input_of_each_bridge(self):
        self.set(drive_armed=1, drive_speed=0.6)
        self.run_for(1.2)
        self.assertGreater(self.duty(RIGHT_FWD), 0)
        self.assertGreater(self.duty(LEFT_FWD), 0)
        self.assertFalse(self.duty(RIGHT_REV), self.duties())
        self.assertFalse(self.duty(LEFT_REV), self.duties())

    def test_backwards_drives_the_other_one(self):
        self.set(drive_armed=1, drive_speed=-0.6)
        self.run_for(1.2)
        self.assertGreater(self.duty(RIGHT_REV), 0)
        self.assertGreater(self.duty(LEFT_REV), 0)
        self.assertFalse(self.duty(RIGHT_FWD), self.duties())
        self.assertFalse(self.duty(LEFT_FWD), self.duties())

    def test_the_extremes_of_speed_and_steer_do_not_drive_both_halves(self):
        """End to end, on the corners and on either side of the dead zone."""
        for speed, steer in ((1, 1), (1, -1), (-1, 1), (-1, -1),
                             (0, 1), (0.06, 1), (-0.06, -1), (0, 0)):
            with self.subTest(speed=speed, steer=steer):
                self.setUp()
                self.set(drive_armed=1, drive_speed=speed, drive_steer=steer)
                self.run_for(0.5)
                for fwd, rev, side in ((RIGHT_FWD, RIGHT_REV, "right"),
                                       (LEFT_FWD, LEFT_REV, "left")):
                    both = (self.duty(fwd) or 0) > 0 and (self.duty(rev) or 0) > 0
                    self.assertFalse(both, "%s bridge driven both ways at "
                                           "speed=%s steer=%s: %s"
                                     % (side, speed, steer, self.duties()))


class TestTheMixCanNeverDriveBothWays(unittest.TestCase):
    """The same claim over the whole range, in arithmetic rather than in
    time."""

    def splits(self):
        nodes = {n["id"]: n["config"] for n in
                 examples.by_id("ex_motor_drive")["nodes"]}
        return nodes["rf"], nodes["rb"], nodes["lf"], nodes["lb"]

    def test_no_mix_at_all_drives_both_inputs_of_a_bridge(self):
        rf, rb, lf, lb = self.splits()
        steps = [i / 20.0 for i in range(-20, 21)]
        for speed in steps:
            for steer in steps:
                right = speed + steer
                left = speed - steer
                for mix, fwd_cfg, rev_cfg, side in ((right, rf, rb, "right"),
                                                    (left, lf, lb, "left")):
                    fwd = drive.split_drive(mix, fwd_cfg)[0]
                    rev = drive.split_drive(mix, rev_cfg)[0]
                    if fwd > 0 and rev > 0:
                        self.fail("%s bridge: mix %.2f gave fwd=%s rev=%s"
                                  % (side, mix, fwd, rev))

    def test_nothing_in_that_range_leaves_the_band(self):
        """Every value is either nought or between the floor and the cap."""
        for cfg in self.splits():
            for mix in [i / 20.0 for i in range(-40, 41)]:
                value = drive.split_drive(mix, cfg)[0]
                self.assertLessEqual(value, CAP + 1e-9,
                                     "mix %.2f gave %s" % (mix, value))
                self.assertGreaterEqual(value, 0.0)
                if value > 0:
                    self.assertGreaterEqual(value, FLOOR - 1e-9,
                                            "mix %.2f gave %s, under the floor"
                                            % (mix, value))


class TestTheRangeIsUsableAtBothEnds(DriveCase):
    """The bug that made the flow look broken on a real chassis."""

    def test_full_throttle_reaches_full_scale(self):
        self.set(drive_armed=1, drive_speed=1.0)
        self.run_for(3.0)
        for gpio in (RIGHT_FWD, LEFT_FWD):
            with self.subTest(gpio=gpio):
                self.assertAlmostEqual(self.duty(gpio) or 0, CAP, delta=1.0)

    def test_the_smallest_command_that_moves_at_all_clears_breakaway(self):
        """Just past the dead zone. This is the end that was unusable."""
        self.set(drive_armed=1, drive_speed=0.06)
        self.run_for(2.0)
        duty = self.duty(RIGHT_FWD) or 0
        self.assertGreaterEqual(duty, FLOOR - 0.5,
                                "a command just off the stop came out at %.1f%%, "
                                "under the %.0f%% a wheel needs to turn"
                                % (duty, FLOOR))

    def test_inside_the_dead_zone_is_still_nothing(self):
        """The floor must not lift a stopped wheel."""
        self.set(drive_armed=1, drive_speed=0.04)
        self.run_for(1.5)
        self.assertEqual(self.moving(), [], self.duties())

    def test_the_middle_of_the_range_is_proportional(self):
        """Rescaled into floor..scale rather than clamped up to the floor,
        so the bottom third of the travel is not all one duty."""
        seen = []
        for command in (0.1, 0.4, 0.7, 1.0):
            self.setUp()
            self.set(drive_armed=1, drive_speed=command)
            self.run_for(3.0)
            seen.append(self.duty(RIGHT_FWD) or 0)
        for earlier, later in zip(seen, seen[1:]):
            self.assertGreater(later, earlier + 2.0,
                               "not proportional across the range: %s" % seen)

    def test_a_command_beyond_full_scale_is_clamped(self):
        """A tag is a number someone can type. 5 must not mean five times."""
        self.set(drive_armed=1, drive_speed=5.0)
        self.run_for(3.0)
        self.assertLessEqual(self.duty(RIGHT_FWD) or 0, CAP + 0.5,
                             self.duties())

    def test_the_mix_overflowing_is_clamped_rather_than_wrapping(self):
        self.set(drive_armed=1, drive_speed=1.0, drive_steer=1.0)
        self.run_for(3.0)
        self.assertLessEqual(self.duty(RIGHT_FWD) or 0, CAP + 0.5,
                             self.duties())


class TestSteering(DriveCase):
    """Which way a positive steer actually turns the thing."""

    def test_a_positive_steer_gives_the_left_wheel_more(self):
        self.set(drive_armed=1, drive_speed=0.5, drive_steer=0.3)
        self.run_for(2.0)
        right, left = self.duty(RIGHT_FWD), self.duty(LEFT_FWD)
        self.assertGreater(left, right,
                           "a positive steer must slow the right wheel: "
                           "right=%s left=%s" % (right, left))

    def test_a_negative_steer_is_the_mirror_of_it(self):
        self.set(drive_armed=1, drive_speed=0.5, drive_steer=-0.3)
        self.run_for(2.0)
        self.assertGreater(self.duty(RIGHT_FWD), self.duty(LEFT_FWD))

    def test_the_turn_is_the_same_way_in_reverse(self):
        """Yaw comes from the steer alone, so backing up while steering
        right keeps turning right rather than inverting."""
        def yaw():
            right = (self.duty(RIGHT_FWD) or 0) - (self.duty(RIGHT_REV) or 0)
            left = (self.duty(LEFT_FWD) or 0) - (self.duty(LEFT_REV) or 0)
            return right - left

        self.set(drive_armed=1, drive_speed=0.5, drive_steer=0.3)
        self.run_for(2.0)
        forward = yaw()
        self.setUp()
        self.set(drive_armed=1, drive_speed=-0.5, drive_steer=0.3)
        self.run_for(2.5)
        backward = yaw()
        self.assertLess(forward, 0, "a positive steer should yaw one way")
        self.assertLess(backward, 0,
                        "the turn inverted in reverse: forward %.1f, back %.1f"
                        % (forward, backward))

    def test_the_tag_reaches_the_formula_at_all(self):
        """The bug this flow was written against: the device could not
        resolve `{{tag.x}}`, so the steer term rendered as text, became a
        nought, and both wheels always matched."""
        self.set(drive_armed=1, drive_speed=0.5, drive_steer=0.0)
        self.run_for(2.0)
        straight = abs((self.duty(RIGHT_FWD) or 0) - (self.duty(LEFT_FWD) or 0))
        self.setUp()
        self.set(drive_armed=1, drive_speed=0.5, drive_steer=0.4)
        self.run_for(2.0)
        turning = abs((self.duty(RIGHT_FWD) or 0) - (self.duty(LEFT_FWD) or 0))
        self.assertLess(straight, 0.5)
        self.assertGreater(turning, 1.0)

    def test_steering_on_the_spot_turns_the_wheels_opposite_ways(self):
        """No throttle at all: the right wheel goes back, the left goes
        forward, and it pivots right where it stands."""
        self.set(drive_armed=1, drive_speed=0.0, drive_steer=0.6)
        self.run_for(2.0)
        self.assertGreater(self.duty(RIGHT_REV) or 0, 0, self.duties())
        self.assertGreater(self.duty(LEFT_FWD) or 0, 0, self.duties())
        self.assertFalse(self.duty(RIGHT_FWD), self.duties())
        self.assertFalse(self.duty(LEFT_REV), self.duties())


class TestItStopsOnItsOwn(DriveCase):
    def test_the_watchdog_takes_every_pin_to_zero(self):
        """All four, not just the one that happened to be driving."""
        self.set(drive_armed=1, drive_speed=0.7, drive_steer=0.2)
        self.run_for(1.5)
        self.assertTrue(self.moving())
        # Stop the chain the way a stopped flow would, and let only the
        # watchdog's own starvation loop run.
        self.runner.timers = []
        deadline = _real.monotonic() + 0.4 + 0.5
        while _real.monotonic() < deadline:
            self.runner.tick()
            _real.sleep(0.005)
        self.assertEqual(self.moving(), [],
                         "the watchdog did not clear every pin: %s"
                         % self.duties())

    def test_stopping_the_runner_leaves_all_four_pins_low(self):
        """`Runner.stop()` deinits the channels and drives the pins low."""
        self.set(drive_armed=1, drive_speed=0.7)
        self.run_for(1.2)
        self.assertTrue(self.moving())
        FakePin.log = []
        self.runner.stop()
        driven_low = {gpio for gpio, level in FakePin.log if level == 0}
        for gpio in ALL_PINS:
            with self.subTest(gpio=gpio):
                self.assertIn(gpio, driven_low,
                              "GPIO%d was left floating by stop()" % gpio)


if __name__ == "__main__":
    unittest.main()
