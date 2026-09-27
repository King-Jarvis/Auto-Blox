"""What the core costs a board, as a number a test can defend."""
import io
import os
import sys
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT_DIR = os.path.join(ROOT, "zero2w_console", "agent")

# Measured on the motor board: RAM per character of source. docs/CONTEXT.md §5.
BYTES_PER_CHAR = 1.00

# What a plain ESP32 in this fleet reports free at enrolment, radio up, no flow.
BOARD_FREE = 120640

# Raising this is a decision about a board's heap, not about a file.
AGENT_BUDGET = 32000


def size(name):
    """Bytes, not characters."""
    return os.path.getsize(os.path.join(AGENT_DIR, name))


def source(name):
    with open(os.path.join(AGENT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def code_only(src):
    """Source with comments removed — what is left still has to be compiled,
    but this is the part that carries meaning to the interpreter."""
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        kept.append(tok.string)
    return len("".join(kept))


class TestTheCoreFitsWithRoomToWork(unittest.TestCase):
    def test_agent_py_is_within_its_budget(self):
        bytes_ = size("agent.py")
        self.assertLessEqual(bytes_, AGENT_BUDGET,
                             "agent.py is %d bytes, %d over budget. On a board "
                             "that compiles its own source this is heap, not "
                             "style: move the prose to docs/CONTEXT.md or a test "
                             "docstring, or raise the budget deliberately and "
                             "say why." % (bytes_, bytes_ - AGENT_BUDGET))

    def test_the_core_leaves_a_working_board_room_for_a_flow(self):
        """agent.py and flow.py are on every board, whatever the flow is."""
        core = size("agent.py") + size("flow.py")
        left = BOARD_FREE - core * BYTES_PER_CHAR
        self.assertGreater(left, 40000,
                           "the core is %d bytes, leaving about %d of %d for "
                           "the flow, the runner, the sockets and every message"
                           % (core, left, BOARD_FREE))

    def test_most_of_the_core_is_code_rather_than_prose(self):
        """On a board that compiles its own source, prose is heap."""
        for name in ("agent.py", "flow.py"):
            src = source(name)
            prose = len(src) - code_only(src)
            with self.subTest(file=name):
                self.assertLess(prose, len(src) * 0.45,
                                "%s is %d characters of which %d is comment"
                                % (name, len(src), prose))


class TestTheModulesAFlowPullsAreCounted(unittest.TestCase):
    def test_deploy_cost_and_this_budget_agree_on_what_a_flow_adds(self):
        """The console already tells you what a deploy will *transfer*."""
        from zero2w_console import examples, fleet as fleetmod
        f = fleetmod.Fleet(None, None, None)
        for fid in ("ex_motor_drive", "ex_motor_pad"):
            flow = examples.by_id(fid)
            names = f.agent_files(flow, {"board": "esp32"})
            self.assertIn("agent.py", names)
            self.assertIn("flow.py", names)
            # bytes, not characters: it reads the files binary.
            held = sum(len(f.module_source(n)) for n in names)
            with self.subTest(flow=fid):
                self.assertLess(held * BYTES_PER_CHAR, BOARD_FREE,
                                "%s does not fit on a plain ESP32 at all: %d "
                                "bytes against %d free" % (fid, held, BOARD_FREE))

    def test_the_controller_flow_leaves_room_for_the_radio_it_has_not_got_yet(self):
        """A floor, not a guarantee: NimBLE's 33KB comes out of internal RAM,
        which a running board reports as idf_free (docs/CONTEXT.md §6,
        item 19). This counts source bytes only."""
        from zero2w_console import examples, fleet as fleetmod
        f = fleetmod.Fleet(None, None, None)
        flow = examples.by_id("ex_motor_pad")
        held = sum(len(f.module_source(n))
                   for n in f.agent_files(flow, {"board": "esp32"}))
        left = BOARD_FREE - held * BYTES_PER_CHAR
        self.assertGreater(left, 20000,
                           "the controller flow holds %d of %d, leaving %d for "
                           "a BLE stack, the runner's state and every message"
                           % (held, BOARD_FREE, left))


if __name__ == "__main__":
    unittest.main()
