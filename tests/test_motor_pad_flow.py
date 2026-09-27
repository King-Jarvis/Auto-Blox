"""The shipped `Motor drive — from a controller` example, on a stub board.

`tests/test_motor_drive_flow.py` asks the tag-driven flow the questions that
matter about a machine with wheels. This asks the same ones of the pad-driven
one, plus the one it exists to answer: **when the controller stops reporting,
does everything stop?**

The pad state is written directly rather than through a radio. That is the whole
point of `agent/modules/pad.py` holding the state shape — a fake transport and a
real one are the same thing to every node downstream.
"""
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples, fleet                        # noqa: E402
from zero2w_console.agent.modules import blocks, drive, expr      # noqa: E402
from zero2w_console.agent.modules import pad as padmod            # noqa: E402
from tests.test_agent_pwm import FakePWM, FakePin, load_runner    # noqa: E402

RIGHT_FWD, RIGHT_REV = 27, 25
LEFT_FWD, LEFT_REV = 33, 32
ALL_PINS = (RIGHT_FWD, RIGHT_REV, LEFT_FWD, LEFT_REV)
CAP, FLOOR = 100.0, 25.0
DOG_MS = 400


class PadCase(unittest.TestCase):
    def setUp(self):
        FakePin.log = []
        FakePWM.made = {}
        self.flowmod = load_runner()
        # Exactly what `fleet.agent_files` says this flow makes a board pull,
        # and nothing else: `modules.blepad` is deliberately absent, which is
        # what a board looks like before the BLE transport is deployed.
        pkg = type(sys)("modules")
        pkg.__path__ = []
        sys.modules["modules"] = pkg
        for name, mod in (("drive", drive), ("blocks", blocks),
                          ("expr", expr), ("pad", padmod)):
            setattr(pkg, name, mod)
            sys.modules["modules." + name] = mod
        self.flow = examples.by_id("ex_motor_pad")

        test = self

        class Agent:
            # `tag.set` records what it wrote so the next report carries it, the
            # same as a real agent. Without it the arm latch raises and every
            # test below fails for the wrong reason.
            tags = {"drive_armed": 0}
            tags_written = {}

            def log(self, level, message, **kw):
                test.said.append((level, message))

        self.said = []
        Agent.tags = {"drive_armed": 0}
        Agent.tags_written = {}
        self.agent = Agent()
        self.runner = self.flowmod.Runner(self.flow, self.agent)
        self.flowmod._RUNNER = self.runner
        self.runner.start()
        # A pad that is connected and reporting, until a test says otherwise.
        self.runner.pad.update(padmod.blank())
        self.pad(rt=0.0)

    def tearDown(self):
        self.flowmod._RUNNER = None
        for name in ("machine", "modules", "modules.drive", "modules.blocks",
                     "modules.expr", "modules.pad"):
            sys.modules.pop(name, None)

    # -- driving it ---------------------------------------------------------
    def pad(self, **values):
        """A report landing, now."""
        self.runner.pad.update({"connected": True, "name": "Xbox",
                                "at": self.flowmod.time.ticks_ms()})
        self.runner.pad.update(values)

    def button(self, key, down):
        padmod.press(self.runner.pad, key, down)
        self.pad()

    def silence(self):
        """The pad walks out of range: connected, but nothing arriving."""
        self.runner.pad["at"] = self.flowmod.time.ticks_ms() - 5000

    def run_for(self, seconds, feeding=None):
        deadline = _real.monotonic() + seconds
        while _real.monotonic() < deadline:
            if feeding:
                self.pad(**feeding)
            self.runner.tick()
            _real.sleep(0.005)
        while self.runner.queue:
            self.runner.tick()

    def arm(self):
        """One press of A, which the Toggle latches into `drive_armed`."""
        self.button("a", True)
        self.run_for(0.25)
        self.button("a", False)
        self.run_for(0.15)

    def disarm(self):
        """And one press of B, which is the other half of the same latch."""
        self.button("b", True)
        self.run_for(0.25)
        self.button("b", False)
        self.run_for(0.15)

    def duty(self, gpio):
        pwm = FakePWM.made.get(gpio)
        return None if pwm is None else pwm.percent

    def duties(self):
        return {g: self.duty(g) for g in ALL_PINS}

    def moving(self):
        return [g for g in ALL_PINS if (self.duty(g) or 0) > 0]

    def critical(self):
        return [m for lvl, m in self.said if lvl == "critical"]


class TestItIsTheShippedFlow(PadCase):
    def test_it_is_for_the_motor_board_and_ships_off(self):
        self.assertEqual(self.flow["board"], "esp32")
        self.assertIs(self.flow["enabled"], False,
                      "a flow that drives motors must ship switched off")

    def test_every_node_in_it_can_run_on_a_device(self):
        self.assertEqual(self.flow["unsupported"], [])

    def test_the_harness_provides_what_a_real_device_would_pull(self):
        wanted = {n.split("/")[-1][:-3] for n in
                  fleet.Fleet(None, None, None).agent_files(
                      self.flow, {"board": "esp32"}) if n.startswith("modules/")}
        self.assertLessEqual(wanted, {"drive", "blocks", "expr", "pwm", "pad"},
                             "this flow pulls a module the harness does not have")

    def test_nothing_logs_a_missing_handler(self):
        self.arm()
        self.run_for(0.4, feeding={"rt": 0.5})
        self.assertEqual(self.critical(), [])


class TestNothingDrivesItUnarmed(PadCase):
    def test_a_full_trigger_with_no_arm_drives_nothing(self):
        self.run_for(0.5, feeding={"rt": 1.0})
        self.assertEqual(self.moving(), [], self.duties())

    def test_a_hard_over_stick_with_no_arm_drives_nothing(self):
        for steer in (-1.0, -0.5, 0.5, 1.0):
            with self.subTest(steer=steer):
                self.run_for(0.3, feeding={"rt": 0.8, "lx": steer})
                self.assertEqual(self.moving(), [], self.duties())

    def test_pressing_b_disarms_again(self):
        self.arm()
        self.run_for(0.3, feeding={"rt": 0.6})
        self.assertNotEqual(self.moving(), [], "it never armed at all")
        self.disarm()
        self.run_for(DOG_MS / 1000.0 + 0.3, feeding={"rt": 0.6})
        self.assertEqual(self.moving(), [], self.duties())


class TestTheTriggerDrives(PadCase):
    def test_a_trigger_turns_the_wheels_forward(self):
        self.arm()
        self.run_for(1.2, feeding={"rt": 0.8})
        self.assertIn(RIGHT_FWD, self.moving(), self.duties())
        self.assertIn(LEFT_FWD, self.moving(), self.duties())
        self.assertEqual(self.duty(RIGHT_REV), 0.0)
        self.assertEqual(self.duty(LEFT_REV), 0.0)

    def test_every_duty_is_zero_or_inside_the_band(self):
        self.arm()
        self.run_for(1.2, feeding={"rt": 0.7})
        for gpio, value in self.duties().items():
            with self.subTest(gpio=gpio):
                self.assertTrue(value == 0.0 or FLOOR <= value <= CAP,
                                "%s is at %s" % (gpio, value))

    def test_a_stick_makes_one_wheel_outrun_the_other(self):
        self.arm()
        self.run_for(1.4, feeding={"rt": 0.7, "lx": 0.5})
        right, left = self.duty(RIGHT_FWD) or 0, self.duty(LEFT_FWD) or 0
        self.assertGreater(left, right,
                           "a positive stick has to turn it right: %s"
                           % self.duties())

    def test_never_both_halves_of_a_bridge(self):
        self.arm()
        for rt, lx in ((0.8, 0.0), (0.8, 1.0), (0.8, -1.0), (0.0, 1.0),
                       (0.3, -0.7), (1.0, 0.4)):
            with self.subTest(rt=rt, lx=lx):
                self.run_for(0.6, feeding={"rt": rt, "lx": lx})
                self.assertFalse((self.duty(RIGHT_FWD) or 0) > 0
                                 and (self.duty(RIGHT_REV) or 0) > 0,
                                 "right bridge shorted: %s" % self.duties())
                self.assertFalse((self.duty(LEFT_FWD) or 0) > 0
                                 and (self.duty(LEFT_REV) or 0) > 0,
                                 "left bridge shorted: %s" % self.duties())


class TestTheDeadman(PadCase):
    """The reason this example exists. `ex_motor_drive` cannot do this."""

    def test_a_pad_that_stops_reporting_stops_the_wheels(self):
        self.arm()
        self.run_for(1.2, feeding={"rt": 0.8})
        self.assertNotEqual(self.moving(), [], "it never drove")
        self.silence()
        self.run_for(DOG_MS / 1000.0 + 0.4)
        self.assertEqual(self.moving(), [],
                         "the controller went quiet and the wheels did not "
                         "stop: %s" % self.duties())

    def test_it_stops_without_the_host_doing_anything(self):
        """Nothing here writes a tag or takes a command: the board does it."""
        self.arm()
        self.run_for(1.0, feeding={"rt": 0.7})
        before = list(self.agent.tags.items())
        self.silence()
        self.run_for(DOG_MS / 1000.0 + 0.4)
        self.assertEqual(self.moving(), [])
        self.assertEqual(list(self.agent.tags.items()), before,
                         "the stop should not have needed a tag to change")

    def test_the_watchdog_says_so(self):
        self.arm()
        self.run_for(1.0, feeding={"rt": 0.7})
        self.silence()
        self.run_for(DOG_MS / 1000.0 + 0.4)
        warned = [m for lvl, m in self.said if "fed nothing" in m]
        self.assertTrue(warned, "it stopped silently: %s" % self.said)

    def test_a_pad_that_comes_back_does_not_resume_at_the_duty_it_left(self):
        """The ramp holds its state across a gap, and `elapsed` is the real
        wall-clock gap — so without the Watchdog resetting the ramps, the first
        message after a dropout would be granted a full-range step."""
        self.arm()
        self.run_for(1.2, feeding={"rt": 0.9})
        left_at = self.duty(RIGHT_FWD) or 0
        self.assertGreater(left_at, FLOOR)
        self.silence()
        self.run_for(DOG_MS / 1000.0 + 0.4)
        self.assertEqual(self.moving(), [])
        # Back, trigger still held down hard.
        self.pad(rt=0.9)
        self.runner.tick()
        while self.runner.queue:
            self.runner.tick()
        came_back = self.duty(RIGHT_FWD) or 0
        self.assertLess(came_back, left_at,
                        "it resumed at %s%% having left at %s%%"
                        % (came_back, left_at))

    def test_stopping_the_runner_drives_every_pin_low(self):
        """Checked on the pin, not the duty: `deinit` deliberately leaves the
        waveform where it stopped — a wheel still turning — which is exactly
        why `stop()` follows it by driving the pin low.
        """
        self.arm()
        self.run_for(1.0, feeding={"rt": 0.8})
        self.assertTrue(self.moving(), "it never drove")
        FakePin.log = []
        self.runner.stop()
        driven_low = {gpio for gpio, level in FakePin.log if level == 0}
        for gpio in ALL_PINS:
            with self.subTest(gpio=gpio):
                self.assertIn(gpio, driven_low,
                              "GPIO%d was left floating by stop()" % gpio)


class TestSteeringReadsTheSnapshot(PadCase):
    def test_the_formula_reaches_the_pad_at_all(self):
        """The regression guard: `{{pad.lx}}` resolving to nothing on a device
        would leave the mixers subtracting an empty string, which reads as
        straight ahead for ever."""
        self.arm()
        self.run_for(1.2, feeding={"rt": 0.6, "lx": 0.0})
        straight = abs((self.duty(RIGHT_FWD) or 0) - (self.duty(LEFT_FWD) or 0))
        self.run_for(1.2, feeding={"rt": 0.6, "lx": 0.45})
        turning = abs((self.duty(RIGHT_FWD) or 0) - (self.duty(LEFT_FWD) or 0))
        self.assertLess(straight, 2.0, "it was not going straight to begin with")
        self.assertGreater(turning, 5.0,
                           "the stick never reached the formula: %s"
                           % self.duties())

    def test_steering_on_the_spot_turns_opposite_wheels(self):
        self.arm()
        self.run_for(1.4, feeding={"rt": 0.0, "lx": 0.6})
        moving = set(self.moving())
        self.assertIn(RIGHT_REV, moving, self.duties())
        self.assertIn(LEFT_FWD, moving, self.duties())
        self.assertNotIn(RIGHT_FWD, moving, self.duties())
        self.assertNotIn(LEFT_REV, moving, self.duties())


if __name__ == "__main__":
    unittest.main()
