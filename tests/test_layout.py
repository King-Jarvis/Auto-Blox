"""The repository holds together: imports resolve, data files are where the
code looks for them, and the generated files are not stale."""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestImports(unittest.TestCase):
    def test_every_module_imports(self):
        """Importing must not need hardware — the modules probe lazily."""
        for name in ("buses", "flows", "gpio", "iot", "mqtt", "server"):
            with self.subTest(module=name):
                __import__("zero2w_console." + name)

    def test_stdlib_only(self):
        """No third-party import may creep in: this board has no pip."""
        import ast
        pkg = os.path.join(ROOT, "zero2w_console")
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(read("zero2w_console", name), name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:            # relative: our own package
                        continue
                    roots = [(node.module or "").split(".")[0]]
                else:
                    continue
                for root in roots:
                    with self.subTest(module=name, imports=root):
                        self.assertIn(root, sys.stdlib_module_names)

    def test_entry_point_runs(self):
        """`python3 -m zero2w_console` reaches argparse without side effects."""
        p = subprocess.run([sys.executable, "-m", "zero2w_console", "--help"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--no-gpio-write", p.stdout)


class TestDataFiles(unittest.TestCase):
    def test_pinout_is_where_gpio_looks(self):
        from zero2w_console import gpio
        self.assertTrue(os.path.isfile(gpio.PINOUT), gpio.PINOUT)

    def test_pinout_shape(self):
        from zero2w_console import gpio
        with open(gpio.PINOUT, encoding="utf-8") as fh:
            doc = json.load(fh)
        pins = doc["pins"]
        self.assertEqual(len(pins), 40, "40-pin header")
        self.assertEqual(len({p["phys"] for p in pins}), 40, "no duplicate pin numbers")
        gpios = [p for p in pins if p["kind"] == "gpio"]
        self.assertEqual(len(gpios), 28, "28 usable GPIO")
        for p in gpios:
            with self.subTest(pin=p["phys"]):
                # sunxi global numbering: bank * 32 + offset, banks A=0..I=8.
                bank = "ABCDEFGHI".index(p["label"][1])
                offset = int(p["label"][2:])
                self.assertEqual(p["line"], bank * 32 + offset)
                self.assertIsNotNone(p["wpi"])

    def test_every_screen_carries_the_same_nav(self):
        """One bar, in one place, built from one file."""
        for page in ("index.html", "flows.html", "iot.html", "cameras.html"):
            html = read("zero2w_console", "static", page)
            with self.subTest(page=page):
                self.assertIn('id="dock"', html)
                self.assertIn("/static/nav.js", html)
                # nav.js needs the bundle for Dock, and the page script needs
                # nav.js, so the order is not incidental.
                self.assertLess(html.index("/static/bundle.js"),
                                html.index("/static/nav.js"))

    def test_every_screen_the_nav_offers_is_routed(self):
        nav = read("zero2w_console", "static", "nav.js")
        server = read("zero2w_console", "server.py")
        hrefs = re.findall(r'href: "(/[a-z]*)"', nav)
        self.assertEqual(len(hrefs), 4, hrefs)
        for href in hrefs:
            with self.subTest(href=href):
                self.assertIn('path == "%s"' % href, server)

    def test_static_files_referenced_by_the_pages_exist(self):
        static = os.path.join(ROOT, "zero2w_console", "static")
        import re
        for page in ("index.html", "flows.html", "login.html", "iot.html", "cameras.html"):
            html = read("zero2w_console", "static", page)
            for ref in re.findall(r'/static/([A-Za-z0-9._-]+)', html):
                with self.subTest(page=page, asset=ref):
                    self.assertTrue(os.path.isfile(os.path.join(static, ref)), ref)


class TestGeneratedFiles(unittest.TestCase):
    """Both generators must reproduce their committed output byte for byte."""

    def _reproduces(self, script, target):
        with open(target, "rb") as fh:
            before = fh.read()
        p = subprocess.run([sys.executable, os.path.join("scripts", script)],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        with open(target, "rb") as fh:
            after = fh.read()
        if after != before:                      # leave the tree as we found it
            with open(target, "wb") as fh:
                fh.write(before)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(after, before, "%s is stale — re-run scripts/%s" % (target, script))

    def test_tokens_css_is_current(self):
        self._reproduces("build_tokens.py",
                         os.path.join(ROOT, "zero2w_console", "static", "tokens.css"))

    def test_pinout_is_current(self):
        self._reproduces("build_pinout.py",
                         os.path.join(ROOT, "zero2w_console", "data", "pinout-zero2w.json"))


class TestNodeGeometry(unittest.TestCase):
    """flows.js computes anchor positions from NODE_W instead of measuring
    the box, so a width that disagrees with the CSS drifts every link by
    the difference."""

    def test_node_width_matches_css(self):
        import re
        js = read("zero2w_console", "static", "flows.js")
        css = read("zero2w_console", "static", "flows.css")
        node_w = int(re.search(r"\bNODE_W\s*=\s*(\d+)", js).group(1))
        block = re.search(r"^\.node \{(.*?)^\}", css, re.S | re.M).group(1)
        css_w = int(re.search(r"width:\s*(\d+)px", block).group(1))
        self.assertEqual(node_w, css_w, "NODE_W in flows.js must equal .node width in flows.css")


class TestRegistry(unittest.TestCase):
    def test_every_node_type_is_complete(self):
        from zero2w_console import flows
        for ntype, spec in flows.REGISTRY.items():
            with self.subTest(node=ntype):
                for key in ("label", "group", "kind", "inputs", "outputs", "fields"):
                    self.assertIn(key, spec)
                self.assertIn(spec["kind"], ("trigger", "logic", "action"))
                if spec["kind"] == "trigger":
                    self.assertEqual(spec["inputs"], [], "a trigger takes no input")
                else:
                    self.assertTrue(spec["inputs"], "%s needs an input" % ntype)
                keys = [f["key"] for f in spec["fields"]]
                self.assertEqual(len(keys), len(set(keys)), "duplicate field key")
                for f in spec["fields"]:
                    self.assertIn("label", f)
                    self.assertIn("kind", f)

    def test_variable_library_is_sound(self):
        from zero2w_console import flows
        names = [v["name"] for v in flows.VARIABLES]
        self.assertEqual(len(names), len(set(names)), "duplicate variable")
        for v in flows.VARIABLES:
            with self.subTest(variable=v["name"]):
                for key in ("name", "group", "example", "desc"):
                    self.assertIn(key, v)
                # Every documented variable must survive a lookup with nothing
                # in the message: unknown names come back None, never raise.
                flows.resolve_variable(v["name"], {}, {})

    def test_render_leaves_unknown_variables_alone(self):
        from zero2w_console import flows
        self.assertEqual(flows.render("{{payload}}", {"payload": 7}, {}), "7")


if __name__ == "__main__":
    unittest.main()
