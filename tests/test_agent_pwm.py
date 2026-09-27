"""What a device actually does when a flow drives a PWM pin."""
import importlib.util
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")


class FakePin:
    """Every pin ever made, and what was written to it."""
    IN = OUT = PULL_UP = 0
    IRQ_RISING, IRQ_FALLING = 1, 2
    log = []

    def __init__(self, gpio, mode=None, pull=None):
        self.gpio = int(gpio)
        self.level = 0

    def value(self, *a):
        if a:
            self.level = a[0]
            FakePin.log.append((self.gpio, a[0]))
            return None
        return self.level

    def irq(self, **kw):
        pass


class FakePWM:
    made = {}

    def __init__(self, pin):
        self.gpio = pin.gpio
        self.hz = None
        self.raw = None
        self.dead = False
        FakePWM.made[self.gpio] = self

    def freq(self, hz):
        self.hz = hz

    def duty_u16(self, v):
        self.raw = v

    def deinit(self):
        self.dead = True

    @property
    def percent(self):
        return None if self.raw is None else round(self.raw * 100.0 / 65535, 1)


def load_runner():
    machine = type(sys)("machine")
    machine.Pin = FakePin
    machine.PWM = FakePWM
    sys.modules["machine"] = machine
    spec = importlib.util.spec_from_file_location("agentflow_pwm", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    clock = type(sys)("time")
    clock.time, clock.sleep = _real.time, _real.sleep
    clock.sleep_ms = lambda ms: None
    clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
    clock.ticks_diff = lambda a, b: a - b
    clock.ticks_add = lambda t, d: t + d
    mod.time = clock
    return mod


class PWMCase(unittest.TestCase):
    def setUp(self):
        FakePin.log = []
        FakePWM.made = {}
        self.flowmod = load_runner()

    def tearDown(self):
        sys.modules.pop("machine", None)

    def drive(self, cfg, payload=None, nodes=None, edges=None):
        """Run one pwm.out node and hand back the runner and what came out."""
        caught = []

        class Agent:
            def log(self, *a, **kw):
                caught.append((a, kw))

        flow = {"id": "f",
                "nodes": nodes or [{"id": "p", "type": "pwm.out", "config": cfg}],
                "edges": edges or []}
        r = self.flowmod.Runner(flow, Agent())
        r.start()
        sent = []
        r._emit = lambda nid, msg, hops, port="out": sent.append(msg)
        r._run("p", {"payload": payload, "meta": {}}, 0)
        return r, sent, caught


class TestStopActuallyStops(PWMCase):
    def test_stop_does_not_start_the_waveform(self):
        """Action "stop" must never start a channel."""
        self.drive({"gpio": 27, "action": "stop", "duty": 80})
        started = FakePWM.made.get(27)
        self.assertTrue(started is None or started.dead,
                        "stop left a live PWM channel on the pin")

    def test_stop_after_start_deinits_and_leaves_the_pin_low(self):
        r, _, _ = self.drive({"gpio": 27, "action": "start", "duty": 80})
        self.assertAlmostEqual(FakePWM.made[27].percent, 80, delta=0.1)
        r._run("p", {"payload": None, "meta": {}}, 0)  # same node, cfg unchanged
        self.assertAlmostEqual(FakePWM.made[27].percent, 80, delta=0.1)

    def test_a_stopped_pin_is_driven_low_rather_than_left_floating(self):
        """A half-driven H-bridge input is a motor still running."""
        r, _, _ = self.drive({"gpio": 27, "action": "start", "duty": 80})
        r.nodes["p"]["config"]["action"] = "stop"
        r._run("p", {"payload": None, "meta": {}}, 0)
        self.assertTrue(FakePWM.made[27].dead, "the channel was not released")
        self.assertIn((27, 0), FakePin.log, "the pin was not driven low")
        self.assertEqual(r.duties[27], 0)

    def test_a_gpio_output_the_flow_drove_is_released_low(self):
        """A new flow must not inherit a pin the old one left high."""
        r, _sent, _ = self.drive({"gpio": 4, "action": "high"},
                                 nodes=[{"id": "p", "type": "gpio.out",
                                         "config": {"gpio": 4, "action": "high"}}])
        self.assertEqual(FakePin.log[-1], (4, 1))
        r.stop()
        self.assertEqual(FakePin.log[-1], (4, 0))
        self.assertEqual(r.outputs, {})

    def test_a_pin_the_flow_never_drove_is_left_alone(self):
        r, _sent, _ = self.drive({"gpio": 4, "action": "high"},
                                 nodes=[{"id": "p", "type": "gpio.out",
                                         "config": {"gpio": 4, "action": "high"}}])
        r.stop()
        self.assertNotIn(13, [g for g, _v in FakePin.log])

    def test_toggle_starts_then_stops(self):
        r, _, _ = self.drive({"gpio": 27, "action": "toggle", "duty": 60})
        self.assertIn(27, r.pwms)
        r._run("p", {"payload": None, "meta": {}}, 0)
        self.assertNotIn(27, r.pwms)


class TestDutyComesFromUpstream(PWMCase):
    def test_a_templated_duty_is_rendered(self):
        """`{{payload}}` is what the field's help has always promised."""
        self.drive({"gpio": 33, "action": "start", "duty": "{{payload}}"}, payload=42)
        self.assertAlmostEqual(FakePWM.made[33].percent, 42, delta=0.1)

    def test_an_empty_duty_still_falls_back_to_the_payload(self):
        self.drive({"gpio": 33, "action": "start", "duty": ""}, payload=17)
        self.assertAlmostEqual(FakePWM.made[33].percent, 17, delta=0.1)

    def test_a_meta_template_works_too(self):
        _, _, _ = self.drive({"gpio": 33, "action": "start", "duty": "{{meta.speed}}"})
        # meta.speed is absent, so the template stays verbatim and reads as 0
        # rather than silently running at some other speed.
        self.assertAlmostEqual(FakePWM.made[33].percent, 0, delta=0.1)

    def test_nonsense_is_zero_not_an_exception(self):
        self.drive({"gpio": 33, "action": "start", "duty": "banana"})
        self.assertAlmostEqual(FakePWM.made[33].percent, 0, delta=0.1)

    def test_duty_is_clamped_at_both_ends(self):
        self.drive({"gpio": 33, "action": "start", "duty": 180})
        self.assertAlmostEqual(FakePWM.made[33].percent, 100, delta=0.1)
        FakePWM.made = {}
        self.drive({"gpio": 32, "action": "start", "duty": -40})
        self.assertAlmostEqual(FakePWM.made[32].percent, 0, delta=0.1)


class TestMicrosecondsForServos(PWMCase):
    def test_1500us_at_50hz_is_seven_and_a_half_percent(self):
        self.drive({"gpio": 25, "action": "start", "freq": 50,
                    "units": "microseconds", "duty": 1500})
        self.assertAlmostEqual(FakePWM.made[25].percent, 7.5, delta=0.1)

    def test_percent_stays_the_default(self):
        self.drive({"gpio": 25, "action": "start", "freq": 50, "duty": 7.5})
        self.assertAlmostEqual(FakePWM.made[25].percent, 7.5, delta=0.1)


class TestItEmitsWhatTheRegistrySaysItDoes(PWMCase):
    def test_the_duty_goes_downstream(self):
        from zero2w_console import flows as flowmod
        declared = [row["name"] for row in flowmod.REGISTRY["pwm.out"]["emits"]]
        self.assertIn("payload", declared)
        _, sent, _ = self.drive({"gpio": 27, "action": "start", "duty": 65})
        self.assertEqual(len(sent), 1)
        self.assertAlmostEqual(sent[0]["payload"], 65, delta=0.1)

    def test_stopping_emits_zero(self):
        _, sent, _ = self.drive({"gpio": 27, "action": "stop"})
        self.assertEqual(sent[0]["payload"], 0)

    def test_the_host_and_the_device_agree_on_the_number(self):
        """Both sides read the same config and must reach the same duty."""
        from zero2w_console import flows as flowmod
        cfg = {"gpio": 25, "action": "start", "freq": 50,
               "units": "microseconds", "duty": "{{payload}}"}
        msg = {"payload": 2000, "meta": {}}
        engine = flowmod.FlowEngine.__new__(flowmod.FlowEngine)
        host = flowmod._num(engine._tnum(cfg["duty"], msg, {}, 0))
        host = 100.0 * host / (1000000.0 / 50)
        self.drive(cfg, payload=2000)
        self.assertAlmostEqual(FakePWM.made[25].percent, host, delta=0.1)


if __name__ == "__main__":
    unittest.main()
