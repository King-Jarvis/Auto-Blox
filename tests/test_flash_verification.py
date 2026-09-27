"""The flasher's last step has to run on the smallest board it flashes."""
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRIPT = os.path.join(ROOT, "scripts", "iot-flash.py")


def flasher():
    """The flasher as a module, without running `main()`."""
    spec = importlib.util.spec_from_file_location("iot_flash_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNothingReadsAWholeFileOnTheBoard(unittest.TestCase):
    def test_the_verification_reads_in_blocks(self):
        code = flasher().VERIFY
        self.assertIn("read(512)", code)
        self.assertIn("h.update(", code)

    def test_it_never_reads_a_file_whole(self):
        """The shape that failed. `.read()` with no size is the whole file."""
        code = flasher().VERIFY
        self.assertNotIn(".read())", code)
        self.assertNotIn("read()", code,
                         "an unbounded read is what asked an idle ESP32 for "
                         "32,000 contiguous bytes and did not get them")

    def test_the_block_is_smaller_than_the_allocation_that_failed(self):
        """32,000 bytes was refused."""
        code = flasher().VERIFY
        size = int(code.split("read(")[1].split(")")[0])
        self.assertLessEqual(size, 1024)


class TestItStillComputesTheRightDigest(unittest.TestCase):
    """The snippet is source sent to another interpreter, so nothing in this
    repo type-checks it."""

    def setUp(self):
        # The snippet imports MicroPython's name for binascii, so stand one up.
        import binascii
        shim = type(sys)("ubinascii")
        shim.hexlify = binascii.hexlify
        sys.modules["ubinascii"] = shim
        self.addCleanup(sys.modules.pop, "ubinascii", None)

    def run_it(self, data):
        mod = flasher()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "payload.bin")
            with open(path, "wb") as fh:
                fh.write(data)
            printed = []
            env = {"print": printed.append}
            exec(mod.VERIFY % path, env)       # noqa: S102 - that is the point
            return printed[-1]

    def test_the_digest_matches_for_every_awkward_length(self):
        for size in (0, 1, 511, 512, 513, 1024, 38667):
            data = bytes((i * 7 + 3) % 256 for i in range(size))
            with self.subTest(size=size):
                self.assertEqual(self.run_it(data),
                                 hashlib.sha256(data).hexdigest())

    def test_every_real_device_file_hashes_correctly(self):
        """The synthetic sizes above carry the past-32,000 case; this covers
        the real files, whatever size they are."""
        agent = os.path.join(ROOT, "zero2w_console", "agent")
        names = []
        for root, _dirs, files in os.walk(agent):
            names += [os.path.join(root, f) for f in files if f.endswith(".py")]
        self.assertTrue(names, "no device files to hash")
        biggest = 0
        for path in sorted(names):
            with open(path, "rb") as fh:
                body = fh.read()
            biggest = max(biggest, len(body))
            with self.subTest(file=os.path.basename(path)):
                self.assertEqual(self.run_it(body),
                                 hashlib.sha256(body).hexdigest())
        self.assertGreater(biggest, 4096,
                           "these do not look like real device files")


if __name__ == "__main__":
    unittest.main()
