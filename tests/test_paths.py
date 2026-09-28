"""The settings folder's move from ~/.config/zero2w-console to auto-blox."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import paths  # noqa: E402


class TestTheMove(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="test-paths-")
        self.old = os.path.join(self.base, "zero2w-console")
        self.new = os.path.join(self.base, "auto-blox")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def make_old(self):
        os.mkdir(self.old, 0o700)                # as load_or_create_token makes it
        os.mkdir(os.path.join(self.old, "tls"), 0o700)
        for name, text in (("token", "abc\n"), ("flows.json", "{}"), ("tls/key.pem", "k")):
            with open(os.path.join(self.old, name), "w") as fh:
                fh.write(text)
        os.chmod(os.path.join(self.old, "token"), 0o600)

    def test_everything_moves_and_the_old_name_still_works(self):
        self.make_old()
        said = paths.migrate(self.new, self.old)
        self.assertIn("moved", said)
        for name in ("token", "flows.json", "tls/key.pem"):
            self.assertTrue(os.path.isfile(os.path.join(self.new, name)), name)
        self.assertEqual(os.stat(os.path.join(self.new, "token")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.new).st_mode & 0o777, 0o700)
        self.assertTrue(os.path.islink(self.old))
        with open(os.path.join(self.old, "token")) as fh:
            self.assertEqual(fh.read(), "abc\n")

    def test_it_happens_once(self):
        self.make_old()
        paths.migrate(self.new, self.old)
        self.assertIsNone(paths.migrate(self.new, self.old))

    def test_a_fresh_install_has_nothing_to_move(self):
        self.assertIsNone(paths.migrate(self.new, self.old))
        self.assertFalse(os.path.exists(self.new))

    def test_when_both_exist_neither_is_touched(self):
        self.make_old()
        os.makedirs(self.new)
        said = paths.migrate(self.new, self.old)
        self.assertIn("both", said)
        self.assertFalse(os.path.islink(self.old))
        self.assertTrue(os.path.isfile(os.path.join(self.old, "token")))
        self.assertEqual(os.listdir(self.new), [])

    def test_every_store_uses_the_one_folder(self):
        from zero2w_console import flows, iot, server, tags, themes
        for mod in (flows, iot, server, tags, themes):
            self.assertEqual(mod.CONFIG_DIR, paths.CONFIG_DIR, mod.__name__)
        self.assertTrue(paths.CONFIG_DIR.endswith(os.path.join(".config", "auto-blox")))


if __name__ == "__main__":
    unittest.main()
