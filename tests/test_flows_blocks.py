"""The standard blocks: detect, count, stabilise, scale, smooth."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class Block(unittest.TestCase):
    """Sends messages through one node and collects what comes out."""

    ntype = None

    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)

    def send(self, cfg, payload, port=None, meta=None):
        node = {"id": "n", "type": self.ntype, "config": cfg}
        _p, out = self.e._execute({"id": "f", "name": "f"}, node,
                                  {"payload": payload, "meta": meta or {}}, port)
        return out

    def run_all(self, cfg, values, port=None):
        """Every payload in turn; None where the branch stopped."""
        return [self.send(cfg, v, port) for v in values]

    def payloads(self, cfg, values):
        return [None if o is None else o["payload"] for o in self.run_all(cfg, values)]


class TestTruthiness(unittest.TestCase):
    """One rule, because a payload arrives as whatever produced it."""

    def test_the_obvious_things_are_true(self):
        for v in (1, 2, -1, 0.5, True, "yes", "on", "true", "anything"):
            self.assertTrue(flows.truthy(v), v)

    def test_the_obvious_things_are_false(self):
        for v in (0, 0.0, False, None, "", "0", "off", "false", "no", "none"):
            self.assertFalse(flows.truthy(v), v)

    def test_the_string_false_is_false_not_merely_non_empty(self):
        """A webhook posting JSON sends the word, not the boolean."""
        self.assertFalse(flows.truthy("false"))
        self.assertFalse(flows.truthy("False"))
        self.assertFalse(flows.truthy(" OFF "))


class TestOnChange(Block):
    ntype = "logic.edge"

    def test_a_steady_value_is_silence(self):
        """The whole point: fifty reads of 1 is one event, not fifty."""
        self.assertEqual(self.payloads({}, [1, 1, 1, 1]), [None, None, None, None])

    def test_the_rise_gets_through(self):
        self.assertEqual(self.payloads({}, [0, 0, 1, 1]), [None, None, 1, None])

    def test_falling_can_be_chosen_instead(self):
        cfg = {"edges": "falling"}
        self.assertEqual(self.payloads(cfg, [1, 0, 1, 0]), [None, 0, None, 0])

    def test_any_passes_both_ways(self):
        cfg = {"edges": "any"}
        self.assertEqual(self.payloads(cfg, [0, 1, 0, 1]), [None, 1, 0, 1])

    def test_the_first_message_is_never_an_edge(self):
        """Nothing is known about what came before it."""
        self.assertIsNone(self.send({"edges": "any"}, 1))

    def test_it_says_which_way_it_went(self):
        self.send({"edges": "any"}, 0)
        self.assertEqual(self.send({"edges": "any"}, 1)["meta"]["edge"], "rising")

    def test_a_string_payload_counts_the_same_as_a_number(self):
        cfg = {"edges": "any"}
        self.assertEqual(self.payloads(cfg, ["off", "on"]), [None, "on"])


class TestCounter(Block):
    ntype = "logic.count"

    def test_it_counts(self):
        self.assertEqual(self.payloads({}, [1, 1, 1]), [1, 2, 3])

    def test_it_can_count_by_something_other_than_one(self):
        self.assertEqual(self.payloads({"step": 5}, [1, 1]), [5, 10])

    def test_it_can_count_down(self):
        self.assertEqual(self.payloads({"step": -1, "start": 3}, [1, 1]), [2, 1])

    def test_with_a_target_it_stays_quiet_until_it_gets_there(self):
        cfg = {"target": 3}
        self.assertEqual(self.payloads(cfg, [1, 1, 1, 1]), [None, None, 3, None])

    def test_and_starts_again_afterwards(self):
        cfg = {"target": 2}
        self.assertEqual(self.payloads(cfg, [1, 1, 1, 1]), [None, 2, None, 2])

    def test_or_keeps_going_if_told_to(self):
        cfg = {"target": 2, "auto_reset": "keep counting"}
        self.assertEqual(self.payloads(cfg, [1, 1, 1, 1]), [None, 2, 3, 4])

    def test_the_message_that_hits_the_target_says_so(self):
        cfg = {"target": 2}
        self.send(cfg, 1)
        self.assertTrue(self.send(cfg, 1)["meta"]["hit"])

    def test_the_reset_input_puts_it_back_and_counts_nothing(self):
        cfg = {"start": 0}
        self.send(cfg, 1)
        self.send(cfg, 1)
        self.assertIsNone(self.send(cfg, "anything", "reset"))
        self.assertEqual(self.send(cfg, 1)["payload"], 1)

    def test_reset_goes_back_to_the_starting_number_not_to_zero(self):
        cfg = {"start": 10}
        self.send(cfg, 1)
        self.send(cfg, "x", "reset")
        self.assertEqual(self.send(cfg, 1)["payload"], 11)


class TestHysteresis(Block):
    ntype = "logic.hysteresis"

    cfg = {"on_above": 30, "off_below": 20, "on_value": "on", "off_value": "off"}

    def test_it_switches_on_above_the_top_number(self):
        self.assertEqual(self.send(self.cfg, 35)["payload"], "on")

    def test_it_does_not_switch_off_until_below_the_bottom_one(self):
        """The gap is the whole feature: 25 is below the on point and the
        output stays on, which is what stops a thermostat chattering."""
        self.send(self.cfg, 35)
        self.assertIsNone(self.send(self.cfg, 25))
        self.assertEqual(self.send(self.cfg, 19)["payload"], "off")

    def test_and_does_not_come_back_on_in_the_gap_either(self):
        self.send(self.cfg, 35)
        self.send(self.cfg, 19)
        self.assertIsNone(self.send(self.cfg, 25))
        self.assertEqual(self.send(self.cfg, 31)["payload"], "on")

    def test_a_reading_sitting_on_the_line_does_not_chatter(self):
        """One threshold would make this alternate on every message."""
        out = self.payloads(self.cfg, [30, 30, 30, 30])
        self.assertEqual(out, ["on", None, None, None])

    def test_it_can_report_every_message_instead(self):
        cfg = dict(self.cfg, emit="every message")
        self.assertEqual(self.payloads(cfg, [35, 35]), ["on", "on"])

    def test_it_says_which_state_and_what_decided_it(self):
        out = self.send(self.cfg, 35)
        self.assertEqual(out["meta"]["state"], "on")
        self.assertEqual(out["meta"]["input"], 35)


class TestScale(Block):
    ntype = "math.scale"

    adc = {"in_min": 0, "in_max": 4095, "out_min": 0, "out_max": 100,
           "decimals": 1}

    def test_the_middle_of_one_range_is_the_middle_of_the_other(self):
        self.assertEqual(self.send(self.adc, 2047.5)["payload"], 50.0)

    def test_the_ends_line_up(self):
        self.assertEqual(self.send(self.adc, 0)["payload"], 0.0)
        self.assertEqual(self.send(self.adc, 4095)["payload"], 100.0)

    def test_a_reading_past_the_end_is_held_there(self):
        out = self.send(self.adc, 5000)
        self.assertEqual(out["payload"], 100.0)
        self.assertTrue(out["meta"]["clamped"])

    def test_unless_clamping_is_turned_off(self):
        cfg = dict(self.adc, clamp="no")
        self.assertGreater(self.send(cfg, 5000)["payload"], 100)

    def test_a_backwards_range_inverts_the_reading(self):
        cfg = dict(self.adc, out_min=100, out_max=0)
        self.assertEqual(self.send(cfg, 0)["payload"], 100.0)
        self.assertEqual(self.send(cfg, 4095)["payload"], 0.0)

    def test_it_keeps_the_raw_reading(self):
        self.assertEqual(self.send(self.adc, 1000)["meta"]["raw"], 1000)

    def test_a_range_of_zero_width_says_so_rather_than_dividing_by_it(self):
        out = self.send({"in_min": 5, "in_max": 5}, 5)
        self.assertIsNone(out)
        self.assertIn("zero wide", self.e.recent()[-1]["message"])


class TestSmooth(Block):
    ntype = "math.smooth"

    def test_a_running_average_settles_towards_the_readings(self):
        cfg = {"window": 4, "decimals": 2}
        self.assertEqual(self.payloads(cfg, [10, 20]), [10.0, 15.0])

    def test_it_only_keeps_the_window(self):
        """Once full, an old reading stops counting."""
        cfg = {"window": 2, "decimals": 2}
        self.assertEqual(self.payloads(cfg, [0, 0, 10, 10]), [0.0, 0.0, 5.0, 10.0])

    def test_one_wild_reading_moves_an_average_a_little(self):
        cfg = {"window": 5, "decimals": 1}
        for _ in range(5):
            self.send(cfg, 20)
        self.assertLess(self.send(cfg, 100)["payload"], 40)

    def test_weighted_needs_no_window_and_still_settles(self):
        cfg = {"mode": "weighted", "weight": 0.5, "decimals": 2}
        self.assertEqual(self.payloads(cfg, [10, 20, 20]), [10.0, 15.0, 17.5])

    def test_weighted_reports_holding_one_sample(self):
        cfg = {"mode": "weighted", "weight": 0.5}
        self.send(cfg, 10)
        self.assertEqual(self.send(cfg, 20)["meta"]["samples"], 1)

    def test_lowest_and_highest_catch_the_spike_an_average_hides(self):
        low = {"mode": "lowest", "window": 5, "decimals": 1}
        self.assertEqual(self.payloads(low, [20, 3, 20]), [20.0, 3.0, 3.0])
        self.e.windows.clear()
        high = {"mode": "highest", "window": 5, "decimals": 1}
        self.assertEqual(self.payloads(high, [20, 99, 20]), [20.0, 99.0, 99.0])

    def test_the_raw_reading_survives_alongside_the_smoothed_one(self):
        cfg = {"window": 3}
        self.send(cfg, 10)
        out = self.send(cfg, 20)
        self.assertEqual(out["meta"]["raw"], 20)
        self.assertEqual(out["payload"], 15.0)

    def test_reset_forgets_the_samples(self):
        cfg = {"window": 4, "decimals": 2}
        self.send(cfg, 100)
        self.send(cfg, "x", "reset")
        self.assertEqual(self.send(cfg, 10)["payload"], 10.0)


class TestTheyComposeIntoSomethingUseful(Block):
    """The point of the set: a raw ADC reading turned into a stable
    decision."""

    def test_a_noisy_sensor_becomes_a_steady_on_off(self):
        e = self.e
        flow = {"id": "f", "name": "f"}
        scale = {"id": "s", "type": "math.scale",
                 "config": {"in_min": 0, "in_max": 4095, "out_min": 0,
                            "out_max": 100, "decimals": 2}}
        smooth = {"id": "m", "type": "math.smooth",
                  "config": {"window": 3, "decimals": 2}}
        hyst = {"id": "h", "type": "logic.hysteresis",
                "config": {"on_above": 60, "off_below": 40,
                           "on_value": "pump on", "off_value": "pump off"}}

        def feed(raw):
            _p, a = e._execute(flow, scale, {"payload": raw, "meta": {}})
            _p, b = e._execute(flow, smooth, a)
            _p, c = e._execute(flow, hyst, b)
            return None if c is None else c["payload"]

        # Readings wobbling around the top of the range: on once, then quiet.
        self.assertEqual(feed(3000), "pump on")
        self.assertIsNone(feed(2900))
        self.assertIsNone(feed(3100))
        # Falling through the gap changes nothing.
        self.assertIsNone(feed(2200))
        # One low reading is not enough — the average still sits in the gap,
        # which is smoothing doing its job rather than the sensor's last word
        # being taken as gospel.
        self.assertIsNone(feed(800))
        # Sustained, it wins.
        self.assertEqual(feed(800), "pump off")


if __name__ == "__main__":
    unittest.main()


class TestOrderingSomethingThatIsNotANumber(unittest.TestCase):
    """`>=` against text must not compare alphabetically and say yes."""

    ORDERING = (">", ">=", "<", "<=")
    NOT_NUMBERS = ("off", "on", "", "yes", "none", "None", None, "abc")

    def host(self, op, value, payload):
        return flows.FlowEngine._if({"op": op, "value": value},
                                    {"payload": payload, "meta": {}})

    def test_no_text_passes_an_ordering_test(self):
        for op in self.ORDERING:
            for payload in self.NOT_NUMBERS:
                with self.subTest(op=op, payload=payload):
                    self.assertEqual(self.host(op, "0.5", payload), "false",
                                     "%r %s 0.5 was allowed" % (payload, op))

    def test_numbers_still_compare_as_numbers(self):
        self.assertEqual(self.host(">=", "0.5", 1), "true")
        self.assertEqual(self.host(">=", "0.5", 0), "false")
        self.assertEqual(self.host(">=", "0.5", 0.5), "true")
        self.assertEqual(self.host("<", "10", 9.5), "true")
        # Numeric text is still a number.
        self.assertEqual(self.host(">=", "0.5", "0.9"), "true")

    def test_a_bool_is_still_a_number(self):
        """A tag declared as a bool has to keep working as an arm."""
        self.assertEqual(self.host(">=", "0.5", True), "true")
        self.assertEqual(self.host(">=", "0.5", False), "false")

    def test_equality_still_compares_as_words(self):
        self.assertEqual(self.host("==", "closed", "closed"), "true")
        self.assertEqual(self.host("==", "closed", "open"), "false")
        self.assertEqual(self.host("!=", "closed", "open"), "true")

    def test_truthy_is_untouched(self):
        self.assertEqual(self.host("truthy", "", "anything"), "true")
        self.assertEqual(self.host("truthy", "", ""), "false")
