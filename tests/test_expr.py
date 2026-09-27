"""The formula node's evaluator."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402
from zero2w_console.agent.modules import expr  # noqa: E402


def ev(text):
    return expr.evaluate(text)


class TestArithmetic(unittest.TestCase):
    def test_the_basics(self):
        self.assertEqual(ev("1 + 2"), 3)
        self.assertEqual(ev("9 - 4"), 5)
        self.assertEqual(ev("6 * 7"), 42)
        self.assertEqual(ev("9 / 2"), 4.5)

    def test_multiplication_binds_tighter_than_addition(self):
        self.assertEqual(ev("2 + 3 * 4"), 14)

    def test_brackets_win(self):
        self.assertEqual(ev("(2 + 3) * 4"), 20)

    def test_subtraction_goes_left_to_right(self):
        """Right-associative would make this 8, which is the classic bug."""
        self.assertEqual(ev("10 - 3 - 2"), 5)

    def test_division_goes_left_to_right_too(self):
        self.assertEqual(ev("100 / 10 / 2"), 5)

    def test_powers_go_right_to_left(self):
        """2 ** 3 ** 2 is 2 ** 9, not 8 ** 2."""
        self.assertEqual(ev("2 ** 3 ** 2"), 512)

    def test_a_power_binds_tighter_than_a_minus_in_front(self):
        self.assertEqual(ev("-3 ** 2"), -9)

    def test_floor_division_and_remainder(self):
        self.assertEqual(ev("7 // 2"), 3)
        self.assertEqual(ev("7 % 2"), 1)

    def test_a_minus_in_front(self):
        self.assertEqual(ev("-5"), -5)
        self.assertEqual(ev("--5"), 5)
        self.assertEqual(ev("3 * -2"), -6)

    def test_decimals_and_exponents(self):
        self.assertEqual(ev("0.5 + 0.25"), 0.75)
        self.assertEqual(ev("1.5e2"), 150.0)
        self.assertEqual(ev(".5 * 4"), 2.0)

    def test_the_real_world_one(self):
        self.assertAlmostEqual(ev("(71 - 32) * 5 / 9"), 21.667, places=3)


class TestComparisons(unittest.TestCase):
    """They give 1 or 0, so the answer can drive an If without translation."""

    def test_they_answer_one_or_zero(self):
        self.assertEqual(ev("10 > 3"), 1)
        self.assertEqual(ev("3 > 10"), 0)
        self.assertEqual(ev("5 == 5"), 1)
        self.assertEqual(ev("5 != 5"), 0)
        self.assertEqual(ev("5 <= 5"), 1)
        self.assertEqual(ev("4 >= 5"), 0)

    def test_the_sum_happens_before_the_comparison(self):
        self.assertEqual(ev("2 + 3 > 4"), 1)

    def test_a_comparison_can_be_used_as_a_number(self):
        self.assertEqual(ev("(10 > 3) * 5"), 5)


class TestFunctions(unittest.TestCase):
    def test_the_ones_worth_having(self):
        self.assertEqual(ev("abs(-5)"), 5)
        self.assertEqual(ev("min(3, 1, 2)"), 1)
        self.assertEqual(ev("max(3, 1, 2)"), 3)
        self.assertEqual(ev("int(3.9)"), 3)
        self.assertEqual(ev("floor(3.9)"), 3)
        self.assertEqual(ev("ceil(3.1)"), 4)
        self.assertEqual(ev("round(3.14159, 2)"), 3.14)
        self.assertEqual(ev("sqrt(144)"), 12)

    def test_floor_and_ceil_go_the_right_way_for_negatives(self):
        self.assertEqual(ev("floor(-3.1)"), -4)
        self.assertEqual(ev("ceil(-3.9)"), -3)

    def test_clamp_holds_a_value_inside_a_range(self):
        self.assertEqual(ev("clamp(150, 0, 100)"), 100)
        self.assertEqual(ev("clamp(-5, 0, 100)"), 0)
        self.assertEqual(ev("clamp(42, 0, 100)"), 42)

    def test_the_constants(self):
        self.assertAlmostEqual(ev("pi"), 3.14159, places=4)
        self.assertAlmostEqual(ev("e"), 2.71828, places=4)

    def test_functions_nest(self):
        self.assertEqual(ev("max(abs(-7), min(3, 4))"), 7)

    def test_the_wrong_number_of_arguments_is_refused(self):
        with self.assertRaises(expr.ExprError):
            ev("abs(1, 2)")
        with self.assertRaises(expr.ExprError):
            ev("clamp(1, 2)")


class TestItIsArithmeticAndNothingElse(unittest.TestCase):
    """Values arrive already substituted, so by the time this runs there
    should be nothing left but numbers and operators."""

    def test_a_bare_name_is_refused_and_says_what_to_do(self):
        with self.assertRaises(expr.ExprError) as caught:
            ev("payload * 2")
        self.assertIn("variable", str(caught.exception))

    def test_an_attribute_is_refused(self):
        with self.assertRaises(expr.ExprError):
            ev("os.system(1)")

    def test_calling_something_that_is_not_in_the_list_is_refused(self):
        for text in ("open(1)", "exec(1)", "eval(1)", "__import__(1)"):
            with self.subTest(text=text):
                with self.assertRaises(expr.ExprError):
                    ev(text)

    def test_a_string_is_refused(self):
        with self.assertRaises(expr.ExprError):
            ev("'hello'")

    def test_indexing_is_refused(self):
        with self.assertRaises(expr.ExprError):
            ev("[1, 2][0]")


class TestItFailsCleanly(unittest.TestCase):
    """Never anything but ExprError: the node turns that into a warning and
    stops the branch, and an unexpected exception would kill the worker."""

    def test_dividing_by_zero_says_so(self):
        for text in ("1 / 0", "1 // 0", "1 % 0"):
            with self.subTest(text=text):
                with self.assertRaises(expr.ExprError):
                    ev(text)

    def test_an_unfinished_formula_says_so(self):
        for text in ("2 +", "(1 + 2", "", "   ", "*"):
            with self.subTest(text=text):
                with self.assertRaises(expr.ExprError):
                    ev(text)

    def test_leftovers_are_refused_rather_than_ignored(self):
        with self.assertRaises(expr.ExprError):
            ev("1 2")

    def test_nothing_at_all_is_refused(self):
        with self.assertRaises(expr.ExprError):
            ev(None)

    def test_a_huge_power_is_refused_rather_than_hanging_the_board(self):
        with self.assertRaises(expr.ExprError):
            ev("9 ** 999999")

    def test_a_stray_character_names_itself(self):
        with self.assertRaises(expr.ExprError) as caught:
            ev("1 $ 2")
        self.assertIn("$", str(caught.exception))


class TestTidy(unittest.TestCase):
    def test_a_whole_float_loses_its_point(self):
        """72.0 in a payload reads as a rounding artefact."""
        self.assertEqual(repr(expr.tidy(72.0)), "72")

    def test_a_real_fraction_keeps_it(self):
        self.assertEqual(expr.tidy(0.5), 0.5)

    def test_rounding_when_asked(self):
        self.assertEqual(expr.tidy(3.14159, 2), 3.14)


class TestTheNode(unittest.TestCase):
    def setUp(self):
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)

    def tearDown(self):
        self.e.clock.stop()

    def run_node(self, cfg, payload=1, meta=None):
        node = {"id": "n", "type": "math.expr", "config": cfg}
        _p, out = self.e._execute({"id": "f", "name": "f"}, node,
                                  {"payload": payload, "meta": meta or {}})
        return out

    def test_the_payload_goes_into_the_formula(self):
        out = self.run_node({"expr": "{{payload}} * 2"}, 21)
        self.assertEqual(out["payload"], 42)

    def test_the_famous_one(self):
        out = self.run_node({"expr": "({{payload}} - 32) * 5 / 9",
                             "decimals": 1}, 71)
        self.assertEqual(out["payload"], 21.7)

    def test_it_keeps_the_sum_it_actually_worked_out(self):
        """With the variables filled in, which is what you need to see when
        the answer is not what you expected."""
        out = self.run_node({"expr": "{{payload}} + 1"}, 5)
        self.assertEqual(out["meta"]["formula"], "5 + 1")

    def test_a_formula_that_cannot_be_worked_out_stops_the_branch(self):
        """Passing a wrong number on is worse than passing nothing."""
        self.assertIsNone(self.run_node({"expr": "{{payload}} / 0"}, 1))
        self.assertIn("divides by zero", self.e.recent()[-1]["message"])

    def test_and_says_what_it_was_trying_to_work_out(self):
        self.run_node({"expr": "{{payload}} +"}, 3)
        self.assertIn("3 +", self.e.recent()[-1]["message"])

    def test_an_unknown_variable_is_left_verbatim_and_then_refused(self):
        """The usual convention: a typo stays visible rather than becoming
        an empty string that silently evaluates to something."""
        self.run_node({"expr": "{{payloud}} * 2"}, 5)
        self.assertIn("payloud", self.e.recent()[-1]["message"])

    def test_no_decimals_keeps_the_whole_answer(self):
        out = self.run_node({"expr": "1 / 3"})
        self.assertAlmostEqual(out["payload"], 0.3333, places=3)


class TestBothSidesUseTheSameFile(unittest.TestCase):
    def test_the_device_gets_the_module_when_a_flow_needs_it(self):
        from zero2w_console import fleet as fleetmod
        flow = {"nodes": [{"id": "n", "type": "math.expr", "config": {}}]}
        names = fleetmod.Fleet(None, None, None).agent_files(flow, {})
        self.assertIn("modules/expr.py", names)

    def test_and_not_when_it_does_not(self):
        from zero2w_console import fleet as fleetmod
        flow = {"nodes": [{"id": "n", "type": "logic.set", "config": {}}]}
        names = fleetmod.Fleet(None, None, None).agent_files(flow, {})
        self.assertNotIn("modules/expr.py", names)

    def test_the_module_needs_nothing_a_device_lacks(self):
        """It is imported by the host and pulled by a device, so it cannot
        import anything: MicroPython has no ast, and its re is not worth
        it."""
        path = os.path.join(ROOT, "zero2w_console", "agent", "modules", "expr.py")
        with open(path) as fh:
            body = fh.read()
        for line in body.splitlines():
            stripped = line.strip()
            with self.subTest(line=stripped):
                self.assertFalse(stripped.startswith("import "), stripped)
                self.assertFalse(stripped.startswith("from "), stripped)

    def test_it_never_reaches_for_eval(self):
        path = os.path.join(ROOT, "zero2w_console", "agent", "modules", "expr.py")
        with open(path) as fh:
            body = fh.read()
        self.assertNotIn("eval(", body)
        self.assertNotIn("exec(", body)


if __name__ == "__main__":
    unittest.main()
