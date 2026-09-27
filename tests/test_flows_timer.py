"""Timers, and the scheduler underneath them."""
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.rang = threading.Event()

        def deliver(key, msg):
            self.got.append((key, msg))
            self.rang.set()

        self.clock = flows._Scheduler(deliver)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()

    def test_something_scheduled_arrives(self):
        self.clock.at(0.02, ("f", "n"), {"payload": 1})
        self.assertTrue(self.rang.wait(2), "nothing was delivered")
        self.assertEqual(self.got[0][0], ("f", "n"))

    def test_it_does_not_arrive_early(self):
        self.clock.at(5, ("f", "n"), {"payload": 1})
        self.assertFalse(self.rang.wait(0.2))
        self.assertEqual(self.got, [])

    def test_cancelling_stops_it(self):
        self.clock.at(0.05, ("f", "n"), {"payload": 1}, tag=("f", "n"))
        self.assertEqual(self.clock.cancel(("f", "n")), 1)
        self.assertFalse(self.rang.wait(0.4))
        self.assertEqual(self.got, [])

    def test_cancelling_leaves_other_nodes_alone(self):
        self.clock.at(0.05, ("f", "a"), {"payload": 1}, tag=("f", "a"))
        self.clock.at(0.05, ("f", "b"), {"payload": 2}, tag=("f", "b"))
        self.clock.cancel(("f", "a"))
        self.assertTrue(self.rang.wait(2))
        time.sleep(0.1)
        self.assertEqual([k for k, _ in self.got], [("f", "b")])

    def test_the_soonest_goes_first_whatever_order_they_were_added(self):
        self.clock.at(0.20, ("f", "late"), {})
        self.clock.at(0.02, ("f", "soon"), {})
        deadline = time.monotonic() + 3
        while len(self.got) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([k[1] for k, _ in self.got], ["soon", "late"])

    def test_one_that_throws_does_not_stop_the_thread(self):
        boom = flows._Scheduler(lambda k, m: (_ for _ in ()).throw(RuntimeError()))
        boom.start()
        try:
            boom.at(0.01, ("f", "n"), {})
            time.sleep(0.15)
            self.assertTrue(boom.is_alive())
        finally:
            boom.stop()

    def test_clearing_drops_everything(self):
        self.clock.at(0.05, ("f", "a"), {}, tag=("f", "a"))
        self.clock.at(0.05, ("f", "b"), {}, tag=("f", "b"))
        self.clock.clear()
        self.assertFalse(self.rang.wait(0.4))


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.flow = {"id": "f", "name": "f"}
        # The engine's own workers eat anything that reaches the job queue, so
        # what the scheduler delivered is caught here instead — which is the
        # seam that matters: the right message, to the right node, later.
        self.delivered = []
        self.rang = threading.Event()

        def catch(key, msg):
            self.delivered.append((key, msg))
            self.rang.set()

        self.e.clock.deliver = catch

    def tearDown(self):
        self.e.clock.stop()

    def send(self, ntype, cfg, payload, nid="n"):
        node = {"id": nid, "type": ntype, "config": cfg}
        _p, out = self.e._execute(self.flow, node, {"payload": payload, "meta": {}})
        return out

    def queued(self):
        """Whatever the engine has parked, waiting to be delivered."""
        with self.e.clock.cond:
            return list(self.e.clock.items)

    def arrived(self, timeout=3):
        self.rang.wait(timeout)
        return self.delivered


class TestDelayNoLongerBlocks(EngineCase):
    def test_a_delay_returns_at_once_and_parks_the_message(self):
        """The engine schedules a delay rather than sleeping through it."""
        started = time.monotonic()
        out = self.send("logic.delay", {"ms": 5000}, 1)
        self.assertLess(time.monotonic() - started, 0.5,
                        "the engine sat waiting instead of scheduling")
        self.assertIsNone(out, "the branch continues later, not now")
        self.assertEqual(len(self.queued()), 1)

    def test_and_the_message_comes_back_to_that_node(self):
        """Delivered to the delay's own key, which continues downstream."""
        self.send("logic.delay", {"ms": 10}, "hello")
        self.e.clock.start()
        got = self.arrived()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], ("f", "n"))
        self.assertEqual(got[0][1]["payload"], "hello")


class TestWaitUntilTrueFor(EngineCase):
    cfg = {"mode": "wait until true for", "ms": 4000}

    def test_a_true_message_is_held_rather_than_passed_on(self):
        self.assertIsNone(self.send("logic.timer", self.cfg, 1))
        self.assertEqual(len(self.queued()), 1)

    def test_going_false_before_the_time_is_up_cancels_it(self):
        """A flickering sensor never reaches whatever is downstream."""
        self.send("logic.timer", self.cfg, 1)
        self.send("logic.timer", self.cfg, 0)
        self.assertEqual(self.queued(), [])

    def test_staying_true_does_not_restart_the_wait(self):
        """Otherwise a sensor reporting every second would never get through."""
        self.send("logic.timer", self.cfg, 1)
        first = self.queued()[0][0]
        self.send("logic.timer", self.cfg, 1)
        self.send("logic.timer", self.cfg, 1)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued()[0][0], first)

    def test_it_says_so_when_a_wait_is_abandoned(self):
        self.send("logic.timer", self.cfg, 1)
        self.send("logic.timer", self.cfg, 0)
        self.assertIn("went false", self.e.recent()[-1]["message"])

    def test_a_wait_that_is_not_interrupted_arrives(self):
        self.e.clock.start()
        self.send("logic.timer", {"mode": "wait until true for", "ms": 20}, 1)
        got = self.arrived()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1]["meta"]["held_ms"], 20)

    def test_false_on_its_own_does_nothing(self):
        self.assertIsNone(self.send("logic.timer", self.cfg, 0))
        self.assertEqual(self.queued(), [])


class TestKeepItTrueFor(EngineCase):
    cfg = {"mode": "keep it true for", "ms": 4000}

    def test_true_goes_straight_through(self):
        out = self.send("logic.timer", self.cfg, 1)
        self.assertIsNotNone(out)
        self.assertEqual(out["meta"]["held_ms"], 0)

    def test_staying_true_says_nothing_more(self):
        self.send("logic.timer", self.cfg, 1)
        self.assertIsNone(self.send("logic.timer", self.cfg, 1))

    def test_going_false_is_what_waits(self):
        self.send("logic.timer", self.cfg, 1)
        self.assertIsNone(self.send("logic.timer", self.cfg, 0))
        self.assertEqual(len(self.queued()), 1)

    def test_coming_back_true_during_the_wait_cancels_the_off(self):
        """A light that stays on while movement keeps happening."""
        self.send("logic.timer", self.cfg, 1)
        self.send("logic.timer", self.cfg, 0)
        self.send("logic.timer", self.cfg, 1)
        self.assertEqual(self.queued(), [])

    def test_false_before_anything_was_on_does_nothing(self):
        self.assertIsNone(self.send("logic.timer", self.cfg, 0))
        self.assertEqual(self.queued(), [])


class TestOneAndThenNothingFor(EngineCase):
    cfg = {"mode": "one and then nothing for", "ms": 4000}

    def test_the_first_one_goes_through_immediately(self):
        out = self.send("logic.timer", self.cfg, "first")
        self.assertEqual(out["payload"], "first")

    def test_and_the_rest_of_the_burst_does_not(self):
        self.send("logic.timer", self.cfg, "first")
        self.assertIsNone(self.send("logic.timer", self.cfg, "second"))
        self.assertIsNone(self.send("logic.timer", self.cfg, "third"))

    def test_after_the_window_another_gets_through(self):
        self.send("logic.timer", {"mode": "one and then nothing for", "ms": 1}, "a")
        time.sleep(0.05)
        self.assertIsNotNone(
            self.send("logic.timer", {"mode": "one and then nothing for", "ms": 1}, "b"))

    def test_it_does_not_care_whether_the_payload_is_true(self):
        """Unlike the other two, this one is about arrival, not level."""
        self.assertIsNotNone(self.send("logic.timer", self.cfg, 0))


class TestReArmingClearsWhatWasPending(EngineCase):
    def test_a_reload_drops_messages_waiting_on_the_old_graph(self):
        self.send("logic.timer", {"mode": "wait until true for", "ms": 9000}, 1)
        self.assertEqual(len(self.queued()), 1)
        self.e._teardown()
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.e.timers, {})


if __name__ == "__main__":
    unittest.main()
