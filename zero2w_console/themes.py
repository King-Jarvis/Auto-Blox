"""Themes: the two built in, the ones you import, and which one is showing.

A theme is the JSON `themecheck` defines — colours, two fonts, three radii and
a shadow depth — never CSS. This module is the only thing that turns one into
CSS, from values that `themecheck.check` has already held to #rrggbb, a font
from its list and a number, so an imported file cannot put anything on the
page but colours.

Imported themes live in ~/.config/auto-blox/themes/<slug>.json and the
one in use is named in theme.json beside them. The choice belongs to the
console rather than to a browser: the phone, the desktop and the sign-in page
all show the same one.
"""
import io
import json
import os
import threading
import zipfile

from . import paths
from . import themecheck

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS = os.path.join(os.path.dirname(HERE), "design", "tokens.json")
KIT = os.path.join(HERE, "theme_kit")
CONFIG_DIR = paths.CONFIG_DIR
BUILTIN = ("dark", "light")
MAX_THEMES = 40

# What each shadow depth means on each base. Written here, not taken from the
# theme, so an imported file never supplies a CSS value.
SHADOWS = {
    ("none", "dark"): ("none", "none"),
    ("none", "light"): ("none", "none"),
    ("soft", "dark"): ("0 1px 2px rgba(0,0,0,0.55)", "0 8px 24px rgba(0,0,0,0.70)"),
    ("soft", "light"): ("0 1px 2px rgba(18,24,32,0.16)", "0 8px 24px rgba(18,24,32,0.24)"),
    ("deep", "dark"): ("0 2px 6px rgba(0,0,0,0.70)", "0 14px 40px rgba(0,0,0,0.85)"),
    ("deep", "light"): ("0 2px 6px rgba(18,24,32,0.26)", "0 14px 40px rgba(18,24,32,0.36)"),
}


def _tokens():
    with open(TOKENS, encoding="utf-8") as fh:
        return json.load(fh)


def builtin(theme_id):
    """Dark or Light, read from the design tokens they are generated from."""
    d = _tokens()
    colors = {}
    for tok in d["color"]["tokens"]:
        v = tok["value"]
        colors[tok["name"]] = (v.get(theme_id) if isinstance(v, dict) else v).lower()
    return {"format": themecheck.FORMAT, "name": theme_id.capitalize(),
            "base": theme_id,
            "colors": {k: colors[k] for k in themecheck.COLORS},
            "fonts": {"ui": "IBM Plex Sans", "mono": "IBM Plex Mono"},
            "radius": dict(themecheck.RADIUS), "shadow": "soft"}


def _stack(role, name):
    family, fallback = themecheck.FONTS[role][name]
    return fallback if family is None else '"%s", %s' % (name, fallback)


def declarations(theme):
    """The custom properties for one theme, as CSS declarations."""
    rows = ["color-scheme: %s;" % theme["base"]]
    rows += ["--%s: %s;" % (k, theme["colors"][k]) for k in themecheck.COLORS]
    widget, floating = SHADOWS[(theme.get("shadow", "soft"), theme["base"])]
    rows += ["--shadow-widget: %s;" % widget, "--shadow-float: %s;" % floating]
    fonts = theme.get("fonts") or {}
    rows += ["--font-ui: %s;" % _stack("ui", fonts.get("ui", "IBM Plex Sans")),
             "--font-mono: %s;" % _stack("mono", fonts.get("mono", "IBM Plex Mono"))]
    for key, value in (theme.get("radius") or {}).items():
        rows.append("--radius-%s: %gpx;" % (key, value))
    return rows


def font_url(theme):
    """The Google Fonts stylesheet for a theme's fonts, or None when both are
    the default Plex (every page already loads those) or a system stack."""
    fonts = theme.get("fonts") or {}
    families = []
    for role, default in (("ui", "IBM Plex Sans"), ("mono", "IBM Plex Mono")):
        name = fonts.get(role, default)
        family = themecheck.FONTS[role][name][0]
        if family and name != default:
            families.append("family=" + family)
    if not families:
        return None
    return "https://fonts.googleapis.com/css2?%s&display=swap" % "&".join(families)


class ThemeStore:
    def __init__(self, config_dir=CONFIG_DIR):
        self.dir = os.path.join(config_dir, "themes")
        self.active_path = os.path.join(config_dir, "theme.json")
        self.lock = threading.Lock()

    # -- reading -----------------------------------------------------------
    def _path(self, theme_id):
        return os.path.join(self.dir, themecheck.slug(theme_id) + ".json")

    def get(self, theme_id):
        if theme_id in BUILTIN:
            return builtin(theme_id)
        try:
            with open(self._path(theme_id), encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return None
        # Its shape is checked again on the way out, since that is what keeps
        # CSS out of the page: a file edited by hand on the Pi gets no further
        # than one imported through it. Readability was held at import and is
        # not re-judged, so a rule tightened later never makes a theme vanish.
        clean, problems = themecheck.shape(raw)
        return clean if clean is not None and not problems else None

    def imported(self):
        out = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            theme_id = name[:-5]
            theme = self.get(theme_id)
            if theme:
                out.append((theme_id, theme))
        return out

    def active_id(self):
        try:
            with open(self.active_path, encoding="utf-8") as fh:
                theme_id = json.load(fh).get("active")
        except (OSError, ValueError, AttributeError):
            return "dark"
        if theme_id in BUILTIN or (isinstance(theme_id, str) and self.get(theme_id)):
            return theme_id
        return "dark"

    def active(self):
        theme_id = self.active_id()
        return theme_id, self.get(theme_id)

    def listing(self):
        rows = [{"id": t, "name": t.capitalize(), "base": t, "builtin": True}
                for t in BUILTIN]
        rows += [{"id": tid, "name": th["name"], "base": th["base"], "builtin": False}
                 for tid, th in self.imported()]
        return {"themes": rows, "active": self.active_id()}

    # -- writing -----------------------------------------------------------
    def _write(self, path, doc):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def add(self, theme):
        """Import one. Returns (id, problems); nothing is saved on a problem.
        Importing a theme with the name of one already here replaces it, so a
        design can go back to Claude and come back again."""
        clean, problems = themecheck.check(theme)
        if problems:
            return None, problems
        theme_id = themecheck.slug(clean["name"])
        if theme_id in BUILTIN:
            theme_id = "my-" + theme_id
        with self.lock:
            if not os.path.exists(self._path(theme_id)) and \
                    len(self.imported()) >= MAX_THEMES:
                return None, ["there are already %d themes; delete one first" % MAX_THEMES]
            self._write(self._path(theme_id), clean)
        return theme_id, []

    def set_active(self, theme_id):
        if not (theme_id in BUILTIN or self.get(theme_id)):
            return False
        with self.lock:
            self._write(self.active_path, {"active": theme_id})
        return True

    def delete(self, theme_id):
        if theme_id in BUILTIN:
            return False
        with self.lock:
            try:
                os.remove(self._path(theme_id))
            except OSError:
                return False
        return True

    # -- what the pages get -----------------------------------------------
    def page_theme(self):
        """The data-theme attribute every page is served with."""
        theme_id = self.active_id()
        return theme_id if theme_id in BUILTIN else "custom"

    def stylesheet(self, theme_id=None):
        """/theme.css: the imported theme in use, or nothing for Dark and Light,
        which tokens.css already holds. With an id, that theme instead, which
        is how a page previews one (?theme=) without changing the console's."""
        if theme_id:
            theme = self.get(theme_id)
        else:
            theme_id, theme = self.active()
        if theme_id in BUILTIN or not theme:
            return "/* %s: built in, in tokens.css */\n" % theme_id
        out = []
        url = font_url(theme)
        if url:
            out.append('@import url("%s");' % url)
        out.append("/* %s */" % theme["name"])
        out.append('[data-theme="custom"] {\n  %s\n}' % "\n  ".join(declarations(theme)))
        return "\n".join(out) + "\n"

    def inline(self):
        """The active theme as one :root block, for the sign-in page, which is
        shown before the token and so cannot load a stylesheet."""
        _theme_id, theme = self.active()
        return ":root {\n  %s\n}" % "\n  ".join(declarations(theme or builtin("dark")))

    def template(self):
        """The theme in use, as the starting point for a new one."""
        theme_id, theme = self.active()
        out = dict(theme or builtin("dark"))
        if theme_id in BUILTIN:
            out["name"] = "My %s theme" % theme_id
        return out

    def kit(self):
        """The theme kit: a Claude skill, zipped, with the template filled in
        from the theme in use."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(KIT)):
                path = os.path.join(KIT, name)
                if os.path.isfile(path) and not name.startswith("."):
                    z.write(path, "auto-blox-theme/" + name)
            with open(themecheck.__file__, encoding="utf-8") as fh:
                z.writestr("auto-blox-theme/check_theme.py", fh.read())
            z.writestr("auto-blox-theme/template.json",
                       json.dumps(self.template(), indent=2) + "\n")
            z.writestr("auto-blox-theme/TOKENS.md", tokens_reference())
        return buf.getvalue()


def tokens_reference():
    """Every colour a theme sets, what it is for and where it shows, from the
    design tokens' own usage notes, so the kit never drifts from them."""
    d = _tokens()
    usage = {t["name"]: t.get("usage", "") for t in d["color"]["tokens"]}
    dark, light = builtin("dark")["colors"], builtin("light")["colors"]
    lines = ["# The colours of an Auto-Blox theme", "",
             "Every one is required. The built-in Dark and Light values are "
             "shown for reference.", "",
             "| Name | Dark | Light | What it is for |", "| --- | --- | --- | --- |"]
    for k in themecheck.COLORS:
        lines.append("| `%s` | `%s` | `%s` | %s |" % (k, dark[k], light[k],
                                                    usage.get(k, "").replace("|", "/")))
    lines += ["", "## What must be readable on what", "",
              "`check_theme.py` holds these; the console refuses a theme that "
              "fails any of them.", ""]
    for fg, bg, need, what in themecheck.RULES:
        lines.append("- `%s` on `%s`: %.1f:1 (%s)" % (fg, bg, need, what))
    for a, b, need, what in themecheck.SEPARATIONS:
        lines.append("- `%s` against `%s`: at least %.2f:1 (%s)" % (a, b, need, what))
    lines += ["- No surface brighter than luminance %.2f (about #eeeeee)" % themecheck.BRIGHTEST,
              "- The four series at least %.0f degrees of hue apart" % themecheck.SERIES_APART,
              "- `scrim` dark in every theme", "",
              "## Fonts", "",
              "`fonts.ui`: " + ", ".join(themecheck.FONTS["ui"]),
              "", "`fonts.mono`: " + ", ".join(themecheck.FONTS["mono"]), "",
              "## Radius and shadow", "",
              "`radius.sm` (buttons, inputs, badges), `radius.md` (widgets, the "
              "dock) and `radius.lg` (menus, popovers): pixels, 0 to %d. "
              "`shadow`: none, soft or deep." % themecheck.RADIUS_MAX, ""]
    return "\n".join(lines)
