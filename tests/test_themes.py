"""Themes: the checker, the store, the routes, and that nothing escapes them.

A theme file comes from anywhere, typically a chat with Claude, so what it may
carry is the point: colours, named fonts, small numbers. These tests hold the
checker to refusing everything else, the built-in themes to passing it, and
the pages to drawing every colour through a token the theme can reach.
"""
import copy
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import server, themecheck, themes  # noqa: E402

STATIC = os.path.join(ROOT, "zero2w_console", "static")


def a_theme(**changes):
    t = themes.builtin("dark")
    t["name"] = "Harbour"
    for k, v in changes.items():
        t[k] = v
    return t


class TestTheBuiltInThemesPass(unittest.TestCase):
    def test_dark_and_light_hold_every_rule(self):
        for theme_id in themes.BUILTIN:
            with self.subTest(theme=theme_id):
                self.assertEqual(themecheck.check(themes.builtin(theme_id))[1], [])

    def test_the_old_light_theme_would_not(self):
        """What it replaced: every text pair passed and it still read as one
        white sheet, with the canvas grid gone. The checker has to say so."""
        old = themes.builtin("light")
        old["colors"].update({"surface-000": "#e7eaee", "surface-100": "#ffffff",
                              "surface-200": "#f3f5f8", "surface-300": "#eaeef2",
                              "hairline": "#d3d9e0", "grid": "#e6eaee"})
        problems = " ".join(themecheck.check(old)[1])
        self.assertIn("too bright", problems)
        self.assertIn("flow canvas", problems)

    def test_the_template_the_kit_starts_from_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(themecheck.check(themes.ThemeStore(d).template())[1], [])


class TestTheCheckerRefuses(unittest.TestCase):
    def problems(self, theme):
        return themecheck.check(theme)[1]

    def test_anything_but_a_six_digit_colour(self):
        for bad in ("red", "#fff", "rgb(0,0,0)", "#12345g", "#123456; }", 7, None):
            t = a_theme()
            t["colors"]["ink"] = bad
            with self.subTest(value=bad):
                self.assertTrue(any('"ink"' in p for p in self.problems(t)))

    def test_a_missing_colour_is_named(self):
        t = a_theme()
        del t["colors"]["scrim"]
        self.assertTrue(any("scrim" in p and "missing" in p for p in self.problems(t)))

    def test_a_name_that_could_break_out_of_the_page(self):
        for bad in ('x</style><script>', "a\"b", "", "x" * 60):
            with self.subTest(name=bad):
                self.assertTrue(self.problems(a_theme(name=bad)))

    def test_a_font_that_is_not_on_the_list(self):
        t = a_theme(fonts={"ui": "Comic Sans'; }", "mono": "IBM Plex Mono"})
        self.assertTrue(any("fonts.ui" in p for p in self.problems(t)))

    def test_radius_and_shadow_are_values_not_css(self):
        self.assertTrue(self.problems(a_theme(radius={"sm": "3px", "md": 6, "lg": 10})))
        self.assertTrue(self.problems(a_theme(radius={"sm": 99, "md": 6, "lg": 10})))
        self.assertTrue(self.problems(a_theme(shadow="0 0 9px red")))

    def test_the_wrong_format_or_base(self):
        self.assertTrue(self.problems(a_theme(format="something/2")))
        self.assertTrue(self.problems(a_theme(base="dim")))

    def test_unknown_keys_never_come_through(self):
        t = a_theme(css="body{display:none}")
        t["colors"]["surface-999"] = "#000000"
        clean, problems = themecheck.check(t)
        self.assertNotIn("css", clean)
        self.assertNotIn("surface-999", clean["colors"])
        self.assertTrue(any("surface-999" in p for p in problems))

    def test_unreadable_text_is_measured(self):
        t = a_theme()
        t["colors"]["ink-faint"] = t["colors"]["surface-100"]
        self.assertTrue(any(p.startswith("ink-faint on surface-100 is 1.00:1")
                            for p in self.problems(t)))

    def test_series_on_top_of_each_other(self):
        t = a_theme()
        t["colors"]["series-2"] = t["colors"]["series-1"]
        self.assertTrue(any("series-1 and series-2" in p for p in self.problems(t)))

    def test_a_light_scrim(self):
        t = a_theme()
        t["colors"]["scrim"] = "#f0f0f0"
        self.assertTrue(any("scrim" in p for p in self.problems(t)))

    def test_the_command_line_says_ok_or_lists(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad = os.path.join(d, "g.json"), os.path.join(d, "b.json")
            with open(good, "w") as fh:
                json.dump(a_theme(), fh)
            with open(bad, "w") as fh:
                json.dump(a_theme(base="dim"), fh)
            out = io.StringIO()
            old, sys.stdout = sys.stdout, out
            try:
                self.assertEqual(themecheck.main(["x", good]), 0)
                self.assertEqual(themecheck.main(["x", bad]), 1)
            finally:
                sys.stdout = old
            self.assertIn("ok", out.getvalue())


class TestTheStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test-themes-")
        self.store = themes.ThemeStore(self.dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dark_until_told_otherwise(self):
        self.assertEqual(self.store.active_id(), "dark")
        self.assertEqual(self.store.page_theme(), "dark")
        self.assertEqual([t["id"] for t in self.store.listing()["themes"]], ["dark", "light"])

    def test_an_import_is_saved_private_and_listed(self):
        theme_id, problems = self.store.add(a_theme())
        self.assertEqual((theme_id, problems), ("harbour", []))
        path = os.path.join(self.dir, "themes", "harbour.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertIn("harbour", [t["id"] for t in self.store.listing()["themes"]])

    def test_a_refused_import_saves_nothing(self):
        theme_id, problems = self.store.add(a_theme(base="dim"))
        self.assertIsNone(theme_id)
        self.assertTrue(problems)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "themes")))

    def test_the_same_name_again_replaces_it(self):
        self.store.add(a_theme())
        again = a_theme()
        again["colors"]["signal"] = "#e8963a"
        self.store.add(again)
        self.assertEqual(len(self.store.imported()), 1)
        self.assertEqual(self.store.get("harbour")["colors"]["signal"], "#e8963a")

    def test_a_theme_cannot_take_a_built_in_name(self):
        theme_id, _ = self.store.add(a_theme(name="Light"))
        self.assertEqual(theme_id, "my-light")
        self.assertEqual(themes.builtin("light")["colors"],
                         self.store.get("light")["colors"])

    def test_using_one_and_deleting_it(self):
        self.store.add(a_theme())
        self.assertTrue(self.store.set_active("harbour"))
        self.assertEqual(self.store.page_theme(), "custom")
        self.assertIn('[data-theme="custom"]', self.store.stylesheet())
        self.assertTrue(self.store.delete("harbour"))
        self.assertEqual(self.store.active_id(), "dark")
        self.assertFalse(self.store.delete("dark"))
        self.assertFalse(self.store.set_active("nothing-here"))

    def test_a_file_edited_by_hand_is_held_to_the_same_shape(self):
        os.makedirs(os.path.join(self.dir, "themes"))
        t = a_theme()
        t["colors"]["ink"] = "red; } body { display: none"
        with open(os.path.join(self.dir, "themes", "evil.json"), "w") as fh:
            json.dump(t, fh)
        self.assertIsNone(self.store.get("evil"))
        self.assertFalse(self.store.set_active("evil"))

    def test_the_stylesheet_is_only_declarations(self):
        self.store.add(a_theme(fonts={"ui": "Inter", "mono": "JetBrains Mono"},
                               radius={"sm": 0, "md": 2, "lg": 4}, shadow="deep"))
        self.store.set_active("harbour")
        css = self.store.stylesheet()
        lines = css.strip().splitlines()
        self.assertTrue(lines[0].startswith('@import url("https://fonts.googleapis.com/css2?'))
        self.assertIn("family=Inter", lines[0])
        body = css[css.index("{") + 1:css.rindex("}")]
        for decl in filter(None, (d.strip() for d in body.split(";"))):
            self.assertRegex(decl, r'^(color-scheme|--[a-z0-9-]+): [\w#"(),.\- ]+$')
        self.assertIn("--radius-sm: 0px", css)

    def test_the_built_in_default_fonts_need_no_import(self):
        self.store.add(a_theme())
        self.store.set_active("harbour")
        self.assertNotIn("@import", self.store.stylesheet())

    def test_the_kit_is_a_skill_with_this_theme_in_it(self):
        self.store.add(a_theme())
        self.store.set_active("harbour")
        z = zipfile.ZipFile(io.BytesIO(self.store.kit()))
        names = set(z.namelist())
        for need in ("SKILL.md", "template.json", "check_theme.py", "TOKENS.md", "preview.html"):
            self.assertIn("auto-blox-theme/" + need, names)
        skill = z.read("auto-blox-theme/SKILL.md").decode()
        self.assertTrue(skill.startswith("---\nname: auto-blox-theme\ndescription: "))
        self.assertEqual(json.loads(z.read("auto-blox-theme/template.json"))["name"], "Harbour")
        with open(themecheck.__file__, "rb") as fh:
            self.assertEqual(z.read("auto-blox-theme/check_theme.py"), fh.read())
        tokens = z.read("auto-blox-theme/TOKENS.md").decode()
        for name in themecheck.COLORS:
            self.assertIn("`%s`" % name, tokens)

    def test_the_kit_example_and_the_page_check_theme_pass(self):
        import importlib.util
        with open(os.path.join(themes.KIT, "example.json")) as fh:
            self.assertEqual(themecheck.check(json.load(fh))[1], [])
        spec = importlib.util.spec_from_file_location(
            "check_pages", os.path.join(ROOT, "scripts", "check-pages.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(themecheck.check(mod.LOUD)[1], [])

    def test_the_kit_preview_starts_as_dark(self):
        with open(os.path.join(themes.KIT, "preview.html")) as fh:
            page = fh.read()
        raw = re.search(r'<script id="theme" type="application/json">(.*?)</script>',
                        page, re.S).group(1)
        self.assertEqual(json.loads(raw), themes.builtin("dark"))


class TestTheRoutes(unittest.TestCase):
    """The real handler, on a port, with only what the theme routes touch."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="test-theme-routes-")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.cfg = {"token": "sekrit", "auth": True}
        cls.httpd.themes = themes.ThemeStore(cls.dir)
        cls.httpd.verbose = False
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def call(self, method, path, body=None, token="sekrit", accept=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Console-Token"] = token
        if accept:
            headers["Accept"] = accept
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), data

    def test_every_theme_route_wants_the_token(self):
        for method, path in (("GET", "/api/themes"), ("GET", "/theme.css"),
                             ("GET", "/api/themes/kit.zip"), ("POST", "/api/themes"),
                             ("POST", "/api/themes/active")):
            with self.subTest(route=path):
                self.assertEqual(self.call(method, path, {} if method == "POST" else None,
                                           token=None)[0], 401)

    def test_import_use_and_serve(self):
        status, _, data = self.call("POST", "/api/themes", a_theme(name="Route test"))
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["id"], "route-test")
        self.assertEqual(self.call("POST", "/api/themes/active", {"id": "route-test"})[0], 200)
        status, ctype, css = self.call("GET", "/theme.css")
        self.assertEqual((status, ctype), (200, "text/css; charset=utf-8"))
        self.assertIn(b'[data-theme="custom"]', css)
        _, _, page = self.call("GET", "/flows")
        self.assertIn(b'<html lang="en" data-theme="custom">', page)
        self.assertIn(b'href="/theme.css"', page)
        # The sign-in page wears it too, inline, before any token.
        status, _, login = self.call("GET", "/", token=None, accept="text/html")
        self.assertEqual(status, 401)
        self.assertNotIn(b"/*THEME*/", login)
        self.assertIn(b"--surface-000: ", login)
        self.assertNotIn(b"/static/", login)
        self.assertEqual(self.call("POST", "/api/themes/active", {"id": "dark"})[0], 200)

    def test_a_refused_import_says_why(self):
        status, _, data = self.call("POST", "/api/themes", a_theme(base="dim"))
        self.assertEqual(status, 400)
        self.assertTrue(json.loads(data)["problems"])

    def test_the_kit_downloads(self):
        status, ctype, data = self.call("GET", "/api/themes/kit.zip")
        self.assertEqual((status, ctype), (200, "application/zip"))
        self.assertIn("auto-blox-theme/SKILL.md", zipfile.ZipFile(io.BytesIO(data)).namelist())

    def test_the_built_ins_cannot_be_deleted(self):
        self.assertEqual(self.call("POST", "/api/themes/delete", {"id": "light"})[0], 400)


class TestEveryColourGoesThroughATheme(unittest.TestCase):
    """A colour written straight into a page is one a theme cannot change.
    tokens.css is generated from the tokens; the only exceptions are text on a
    photograph, marked in place as not themed."""

    COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|:\s*(white|black)\b")

    def test_no_page_writes_a_colour_of_its_own(self):
        found = []
        for name in sorted(os.listdir(STATIC)):
            if name == "tokens.css" or not name.endswith((".css", ".js", ".html")):
                continue
            with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if "not themed" in line:
                        continue
                    code = re.sub(r"/\*.*?\*/|//.*$", "", line)
                    for m in self.COLOUR.finditer(code):
                        # An HTML entity or an id fragment is not a colour.
                        if code[max(0, m.start() - 1)] in "&" or re.match(r"#[0-9a-f]{3,8}[\w-]", code[m.start():]):
                            continue
                        found.append("%s:%d %s" % (name, n, line.strip()[:80]))
        self.assertEqual(found, [])

    def test_every_token_a_page_uses_is_defined(self):
        used = set()
        for name in os.listdir(STATIC):
            if name.endswith((".css", ".js")):
                with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
                    used |= set(re.findall(r"var\(--([a-z0-9-]+(?:\\\.[0-9]+)?)", fh.read()))
        with open(os.path.join(STATIC, "tokens.css")) as fh:
            defined = set(re.findall(r"^\s*--([a-z0-9.\\-]+):", fh.read(), re.M))
        self.assertEqual(used - defined, set())

    def test_a_theme_sets_every_colour_the_tokens_have(self):
        with open(themes.TOKENS) as fh:
            names = [t["name"] for t in json.load(fh)["color"]["tokens"]]
        self.assertEqual(tuple(names), themecheck.COLORS)


if __name__ == "__main__":
    unittest.main()
