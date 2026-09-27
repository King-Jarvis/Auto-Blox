"""The installer fills the packaging templates in for whoever installs it."""
import os
import pwd
import grp
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(ROOT, "packaging")
TEMPLATES = ("zero2w-console.service", "zero2w-iotnet.service",
             "zero2w-console.desktop", "zero2w-iot.sudoers")


class TestTheTemplates(unittest.TestCase):
    def test_no_template_names_one_machine(self):
        """A user, a group or a home path in here only works for its author."""
        for name in TEMPLATES + ("iot-netctl",):
            with open(os.path.join(PACKAGING, name)) as fh:
                text = fh.read()
            with self.subTest(file=name):
                self.assertNotIn("/home/", text)
                for key in ("User", "Group"):
                    for value in re.findall(r"^%s=(.*)$" % key, text, re.M):
                        self.assertEqual(value, "@%s@" % key.upper())
                self.assertNotRegex(text, r"ZERO2W_IOT_GROUP=(?!@GROUP@)\w")


class TestRendering(unittest.TestCase):
    def render(self, user):
        out = tempfile.mkdtemp()
        subprocess.run([os.path.join(ROOT, "scripts", "install.sh"),
                        "--render", out, "--user", user],
                       check=True, capture_output=True)
        docs = {}
        for name in TEMPLATES:
            with open(os.path.join(out, name)) as fh:
                docs[name] = fh.read()
        return docs

    def test_every_placeholder_is_filled(self):
        for name, text in self.render(pwd.getpwuid(os.getuid()).pw_name).items():
            with self.subTest(file=name):
                self.assertNotIn("@", text)

    def test_the_unit_runs_as_that_user_from_this_checkout(self):
        user = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(user.pw_gid).gr_name
        docs = self.render(user.pw_name)
        unit = docs["zero2w-console.service"]
        self.assertIn("User=%s\n" % user.pw_name, unit)
        self.assertIn("Group=%s\n" % group, unit)
        self.assertIn("WorkingDirectory=%s\n" % ROOT, unit)
        self.assertIn("ZERO2W_IOT_GROUP=%s\n" % group, docs["zero2w-iotnet.service"])
        self.assertIn("Exec=%s/scripts/open-console.sh" % ROOT,
                      docs["zero2w-console.desktop"])

    def test_another_user_gets_their_own(self):
        unit = self.render("nobody")["zero2w-console.service"]
        self.assertIn("User=nobody\n", unit)


class TestTheHelperHasNoDefaultGroup(unittest.TestCase):
    def test_it_refuses_to_serve_without_one(self):
        from tests.test_helper_socket import load_helper
        helper = load_helper()
        self.assertEqual(helper.SOCKET_GROUP, os.environ.get("ZERO2W_IOT_GROUP", ""))
        helper.SOCKET_GROUP = ""
        helper.SOCKET = os.path.join(tempfile.mkdtemp(), "run", "ctl.sock")
        with self.assertRaises(SystemExit) as caught:
            helper.cmd_serve()
        self.assertIn("ZERO2W_IOT_GROUP", str(caught.exception))
        self.assertFalse(os.path.exists(helper.SOCKET))


if __name__ == "__main__":
    unittest.main()
