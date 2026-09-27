"""get.sh, the one-command installer, against local downloads and a fake sudo."""
import os
import subprocess
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET = os.path.join(ROOT, "get.sh")


class GetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.dir = os.path.join(self.home, "auto-blox")
        # A sudo that records what it was asked to run, and runs nothing.
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.sudo_log = os.path.join(self.tmp.name, "sudo.log")
        with open(os.path.join(self.bin, "sudo"), "w") as fh:
            fh.write('#!/bin/sh\necho "$@" >> %s\n' % self.sudo_log)
        os.chmod(os.path.join(self.bin, "sudo"), 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def tarball(self, version, installer=True):
        """A download shaped like GitHub's: one top directory."""
        src = os.path.join(self.tmp.name, "src-" + version)
        top = os.path.join(src, "auto-blox-main")
        os.makedirs(os.path.join(top, "scripts"))
        os.makedirs(os.path.join(top, "backups"))
        with open(os.path.join(top, "VERSION"), "w") as fh:
            fh.write(version)
        if installer:
            path = os.path.join(top, "scripts", "install.sh")
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        out = os.path.join(self.tmp.name, version + ".tar.gz")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(top, arcname="auto-blox-main")
        return out

    def run_get(self, *args, tarball=None):
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin",
               "AUTOBLOX_DIR": self.dir}
        if tarball:
            env["AUTOBLOX_TARBALL"] = tarball
        return subprocess.run(["bash", GET] + list(args), env=env,
                              capture_output=True, text=True, timeout=60)

    def version(self):
        with open(os.path.join(self.dir, "VERSION")) as fh:
            return fh.read()


class TestADownloadedCopy(GetCase):
    def test_a_fresh_download_lands_in_place(self):
        out = self.run_get("--download-only", tarball=self.tarball("1"))
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertEqual(self.version(), "1")
        self.assertTrue(os.access(os.path.join(self.dir, "scripts", "install.sh"), os.X_OK))
        self.assertFalse(os.path.exists(self.sudo_log))

    def test_an_update_keeps_the_flow_backups_and_the_previous_copy(self):
        self.run_get("--download-only", tarball=self.tarball("1"))
        with open(os.path.join(self.dir, "backups", "mine.json"), "w") as fh:
            fh.write("{}")
        out = self.run_get("--download-only", tarball=self.tarball("2"))
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertEqual(self.version(), "2")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "backups", "mine.json")))
        with open(os.path.join(self.dir + ".previous", "VERSION")) as fh:
            self.assertEqual(fh.read(), "1")

    def test_a_download_that_is_not_auto_blox_changes_nothing(self):
        self.run_get("--download-only", tarball=self.tarball("1"))
        out = self.run_get("--download-only", tarball=self.tarball("bad", installer=False))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.version(), "1")
        self.assertFalse(os.path.exists(self.dir + ".new"))

    def test_the_options_reach_the_installer_through_sudo(self):
        out = self.run_get("--iot", "--mpy", tarball=self.tarball("1"))
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        with open(self.sudo_log) as fh:
            self.assertEqual(fh.read().split(),
                             [os.path.join(self.dir, "scripts", "install.sh"), "--iot", "--mpy"])

    def test_no_options_is_a_plain_install(self):
        out = self.run_get(tarball=self.tarball("1"))
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        with open(self.sudo_log) as fh:
            self.assertEqual(fh.read().split(),
                             [os.path.join(self.dir, "scripts", "install.sh")])

    def test_a_cut_off_script_runs_nothing(self):
        """Piped from curl, half a script must not start installing."""
        with open(GET) as fh:
            text = fh.read()
        half = text[:text.rindex('main "$@"')]
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin",
               "AUTOBLOX_DIR": self.dir, "AUTOBLOX_TARBALL": self.tarball("1")}
        out = subprocess.run(["bash"], input=half, env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.dir))


class TestAGitCheckout(GetCase):
    def git(self, *args, cwd=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=True,
                       capture_output=True)

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp.name, "origin")
        os.makedirs(os.path.join(self.origin, "scripts"))
        with open(os.path.join(self.origin, "VERSION"), "w") as fh:
            fh.write("1")
        self.git("init", "-q", "-b", "main", cwd=self.origin)
        self.git("add", "-A", cwd=self.origin)
        self.git("commit", "-qm", "one", cwd=self.origin)
        self.git("clone", "-q", self.origin, self.dir)

    def test_a_checkout_is_pulled_not_replaced(self):
        with open(os.path.join(self.origin, "VERSION"), "w") as fh:
            fh.write("2")
        self.git("commit", "-qam", "two", cwd=self.origin)
        out = self.run_get("--download-only")
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertEqual(self.version(), "2")
        self.assertFalse(os.path.exists(self.dir + ".previous"))

    def test_local_changes_stop_it(self):
        with open(os.path.join(self.dir, "VERSION"), "w") as fh:
            fh.write("mine")
        out = self.run_get("--download-only")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("local changes", out.stdout)
        self.assertEqual(self.version(), "mine")


if __name__ == "__main__":
    unittest.main()
