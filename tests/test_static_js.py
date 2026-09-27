"""Every static script must compile in the browser that will run it."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

STATIC = os.path.join(ROOT, "zero2w_console", "static")


def browser():
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class TestStaticJS(unittest.TestCase):
    @unittest.skipUnless(browser(), "no chromium on this machine")
    def test_every_script_compiles(self):
        import io
        from contextlib import redirect_stdout
        import jsc
        profile = tempfile.mkdtemp(prefix="test-jsc-")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = jsc.compile_all(browser(), STATIC, profile)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        self.assertEqual(code, 0, "\n" + buf.getvalue())
        self.assertIn("iot.js", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
