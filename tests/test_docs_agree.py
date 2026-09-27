"""The documents have to agree with the code, and with each other."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DOCS = ("README.md", "docs/ARCHITECTURE.md", "docs/CONTEXT.md",
        "docs/IOT-PLAN.md", "docs/NODE-LIBRARY.md", "docs/RADIO.md")


def text(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def every_doc():
    return {name: text(name) for name in DOCS}


class TestTheCountsAreReal(unittest.TestCase):
    """Node types, variables and examples are API surface: someone reads
    these numbers and expects the palette to match."""

    def setUp(self):
        from zero2w_console import examples, flows
        self.flows, self.examples = flows, examples
        self.docs = every_doc()

    def claimed(self, pattern):
        """Every (number, document) pair matching a claim, including one
        wrapped across a line break."""
        out = []
        for name, body in self.docs.items():
            for m in re.finditer(pattern, body):
                out.append((int(m.group(1).replace(",", "")), name))
        return out

    def test_node_type_counts_match_the_registry(self):
        real = len(self.flows.REGISTRY)
        for got, where in self.claimed(r"(\d+)\s+node\s+types"):
            with self.subTest(doc=where):
                self.assertEqual(got, real, "%s says %d node types, there are %d"
                                 % (where, got, real))

    def test_device_runnable_counts_match(self):
        real = len([t for t in self.flows.REGISTRY
                    if self.flows.runs_on_device(t)])
        for got, where in self.claimed(r"(\d+)(?:\s+of\s+them)?\s+runnable\s+on\s+a\s+device"):
            with self.subTest(doc=where):
                self.assertEqual(got, real)

    def test_variable_counts_match(self):
        real = len(self.flows.VARIABLES)
        for got, where in self.claimed(r"(\d+)\s+variables"):
            with self.subTest(doc=where):
                self.assertEqual(got, real)

    def test_example_counts_match(self):
        real = len(self.examples.catalogue())
        for got, where in self.claimed(r"(\d+)\s+example(?:s|\s+flows)\b"):
            with self.subTest(doc=where):
                self.assertEqual(got, real)

    def test_counts_are_written_as_digits(self):
        """"Ten example flows" sat in NODE-LIBRARY.md contradicting
        CONTEXT.md's 15 for weeks, because a spelled-out number matches
        no check."""
        words = ("one", "two", "three", "four", "five", "six", "seven", "eight",
                 "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
                 "fifteen", "twenty", "thirty", "forty")
        for name, body in self.docs.items():
            for word in words:
                for noun in ("example", "node type", "variable"):
                    claim = r"\b%s %ss?\b" % (word, noun)
                    with self.subTest(doc=name, claim=claim):
                        self.assertNotRegex(
                            body, claim,
                            "%s spells out a count (%s %ss); write the digits so "
                            "it can be checked" % (name, word, noun))

    def test_nothing_quotes_a_test_count(self):
        """It changes on almost every commit, so a document that states it
        is a document that is wrong."""
        for name, body in self.docs.items():
            with self.subTest(doc=name):
                self.assertNotRegex(body, r"[\d,]{3,6} tests\b",
                                    "%s quotes a test count" % name)


class TestTheDocumentsAreOneSet(unittest.TestCase):
    """Five documents drifted apart partly because nothing tied them
    together."""

    def test_every_document_points_at_the_front_door(self):
        for name in DOCS:
            if name == "docs/ARCHITECTURE.md":
                continue
            with self.subTest(doc=name):
                self.assertIn("ARCHITECTURE.md", text(name),
                              "%s stands alone; a reader landing there gets no "
                              "map" % name)

    def test_only_one_document_claims_to_be_read_first(self):
        claims = [n for n in DOCS
                  if n != "docs/ARCHITECTURE.md"        # the one that may
                  and re.search(r"(read|start)[^.\n]{0,30}first", text(n), re.I)
                  and "ARCHITECTURE.md" not in text(n)[:600]]
        self.assertEqual(claims, [],
                         "these send the reader somewhere else first: %r" % claims)

    def test_the_front_door_covers_what_it_promises(self):
        """It is the document a newcomer is sent to, so the five things the
        user asked it to answer have to be in it."""
        body = text("docs/ARCHITECTURE.md")
        for heading in ("What it is", "The shape", "How a flow runs",
                        "Tags", "Who owns which fact", "Integrating with it"):
            with self.subTest(heading=heading):
                self.assertIn(heading, body)


class TestTheVersionIsReal(unittest.TestCase):
    def test_the_current_agent_version_is_not_misstated(self):
        """Historical mentions are fine — "before agent 0.7.0" stays true."""
        src = text("zero2w_console/agent/agent.py")
        real = re.search(r'VERSION = "([^"]+)"', src).group(1)
        for name, body in every_doc().items():
            for m in re.finditer(r"agent \*\*?(\d+\.\d+\.\d+)\*\*?[,.]", body):
                with self.subTest(doc=name, claim=m.group(1)):
                    line_start = body.rfind("\n", 0, m.start()) + 1
                    line = body[line_start:body.find("\n", m.start())]
                    if "before" in line.lower() or "from" in line.lower():
                        continue          # a historical note
                    self.assertEqual(
                        m.group(1), real,
                        "%s presents agent %s as current; it is %s"
                        % (name, m.group(1), real))


class TestTheDocsAgreeWithEachOther(unittest.TestCase):
    def test_they_all_point_at_the_same_config_directory(self):
        """A reader following the wrong path finds nothing and concludes the
        feature does not work."""
        for name, body in every_doc().items():
            if "zero2w-console" not in body:
                continue
            with self.subTest(doc=name):
                self.assertNotIn("/etc/zero2w-console/flows.json", body,
                                 "%s puts flows under /etc; they live in "
                                 "~/.config/zero2w-console" % name)

    def test_the_freshness_number_is_stated_once(self):
        """`flows.DEVICE_FRESH` is the only definition."""
        from zero2w_console import flows
        for name, body in every_doc().items():
            for m in re.finditer(r"(\d+) seconds? (?:of silence|before .*offline)",
                                 body):
                with self.subTest(doc=name):
                    self.assertEqual(int(m.group(1)), flows.DEVICE_FRESH)

    def test_no_document_claims_the_flow_starts_during_boot(self):
        """It stopped being true, and it is the difference between a board
        that enrols and one that joins and is never heard from again."""
        for name, body in every_doc().items():
            with self.subTest(doc=name):
                self.assertNotIn("flow still starts before anything waits", body)
                self.assertNotIn("The deployed flow starts immediately", body)


class TestTheBoardsBeliefsAreStated(unittest.TestCase):
    """Two numbers a reader will otherwise measure wrong."""

    def test_the_bytes_per_character_figure_appears_with_both_readings(self):
        """2.2 was a cold board with the wifi stack allocating around it;
        1.00 is the steady state."""
        body = text("docs/CONTEXT.md")
        at = body.index("per character")
        near = body[max(0, at - 400):at + 600]
        self.assertIn("2.2", near, "the cold-boot figure is missing")
        self.assertIn("1.00", near,
                      "CONTEXT.md gives only the pessimistic bytes/char figure, "
                      "which sets a budget twice as tight as the measurement")


if __name__ == "__main__":
    unittest.main()
