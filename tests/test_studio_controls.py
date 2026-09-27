"""The same control has to mean the same thing wherever it appears."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS_JS = os.path.join(ROOT, "zero2w_console", "static", "flows.js")


def source():
    with open(FLOWS_JS, encoding="utf-8") as fh:
        return fh.read()


def toggles(src):
    """Every enable/disable button, as (line number, its handler text)."""
    found = []
    for m in re.finditer(r'\?\s*"Disable"\s*:\s*"Enable"', src):
        line = src.count("\n", 0, m.start()) + 1
        # The Z.Button object literal this label belongs to: from the label to
        # the end of the onClick function that follows it.
        tail = src[m.start():m.start() + 1200]
        end = tail.find("}));")
        found.append((line, tail[:end if end != -1 else len(tail)]))
    return found


class TestBothEnableButtons(unittest.TestCase):
    def setUp(self):
        self.src = source()
        self.found = toggles(self.src)

    def test_there_are_exactly_two_of_them(self):
        """If a third appears, it has to be held to the same two rules — and
        if one vanishes, these tests would otherwise pass by checking
        nothing."""
        self.assertEqual(len(self.found), 2,
                         "expected the studio bar and the flow card, found %d"
                         % len(self.found))

    def test_each_one_saves_when_pressed(self):
        for line, body in self.found:
            with self.subTest(line=line):
                self.assertRegex(body, r"\bsave\(",
                                 "the enable toggle at line %d only stages the "
                                 "change, so pressing it does nothing until "
                                 "Save — and it is what pushes the reload to "
                                 "the board" % line)

    def test_neither_is_labelled_with_the_state(self):
        """A button reading "Enabled" is pressed by someone who wants it
        enabled."""
        for bad in ('"Enabled" : "Disabled"', '"Disabled" : "Enabled"'):
            self.assertNotIn(bad, self.src,
                             "a toggle is labelled with the state, not the verb")

    def test_each_one_records_an_undo_step_first(self):
        """Switching a flow off is undoable in the studio like anything else."""
        for line, body in self.found:
            with self.subTest(line=line):
                self.assertIn("snapshot()", body,
                              "the toggle at line %d cannot be undone" % line)


if __name__ == "__main__":
    unittest.main()
