#!/usr/bin/env python3
"""Check an Auto-Blox theme file: its shape, and whether it can be read.

One file, standard library only, so the same code runs in three places: the
console's import (`POST /api/themes`), the tests that hold the built-in dark
and light themes to it, and the theme kit Claude runs while designing one.

    python3 check_theme.py my-theme.json

prints every problem, or "ok", and exits 1 on a problem. A theme is JSON:

    {"format": "auto-blox-theme/1", "name": "Harbour", "base": "dark",
     "colors": {"surface-000": "#0b1418", ...every name in COLORS...},
     "fonts": {"ui": "IBM Plex Sans", "mono": "IBM Plex Mono"},
     "radius": {"sm": 3, "md": 6, "lg": 10}, "shadow": "soft"}

Nothing in a theme reaches the page as written: colours must be #rrggbb,
fonts come from FONTS, radii are small numbers and shadow is a word. That is
what lets the console accept a file from anywhere without it carrying CSS.
"""
import json
import re
import sys

FORMAT = "auto-blox-theme/1"

# Every colour a theme sets, in the order the template lists them.
COLORS = (
    "surface-000", "surface-100", "surface-200", "surface-300",
    "hairline", "hairline-strong",
    "ink", "ink-muted", "ink-faint",
    "signal", "signal-wash", "on-signal", "link",
    "ok", "warn", "serious", "critical",
    "ok-wash", "warn-wash", "critical-wash",
    "series-1", "series-2", "series-3", "series-4",
    "grid", "focus-ring", "scrim",
)

SURFACES = ("surface-000", "surface-100", "surface-200", "surface-300")
PANELS = ("surface-100", "surface-200")
STATUS = ("ok", "warn", "serious", "critical")

# What must be readable on what. Text is held to WCAG's 4.5:1; marks (chart
# lines, borders that mean something, the focus ring) to 3:1.
TEXT, MARK = 4.5, 3.0
RULES = (
    [(fg, bg, TEXT, "text") for fg in ("ink", "ink-muted", "ink-faint", "signal", "link")
     for bg in SURFACES]
    + [("ink", w, TEXT, "text") for w in ("signal-wash", "ok-wash", "warn-wash", "critical-wash")]
    + [("on-signal", "signal", TEXT, "text on a primary button")]
    + [(s, bg, TEXT, "status text") for s in STATUS for bg in PANELS + ("surface-300",)]
    + [("ok", "ok-wash", TEXT, "status text"), ("warn", "warn-wash", TEXT, "status text"),
       ("critical", "critical-wash", TEXT, "status text")]
    + [(s, bg, MARK, "chart line") for s in ("series-1", "series-2", "series-3", "series-4")
       for bg in PANELS]
    + [("hairline-strong", bg, MARK, "control outline") for bg in PANELS]
    + [("focus-ring", bg, MARK, "focus ring") for bg in SURFACES]
)

# Structure: what makes a screen read as panels on a ground rather than one
# sheet. These are what the first light theme lacked — every text pair passed
# and it still read as a blinding white page. Contrast, not WCAG: separations
# between surfaces are meant to be quiet, only not invisible.
SEPARATIONS = (
    ("surface-100", "surface-000", 1.05, "a widget against the page behind it"),
    ("hairline", "surface-000", 1.3, "a widget's outline against the page"),
    ("surface-200", "surface-100", 1.08, "a widget's header band against its body"),
    ("hairline", "surface-100", 1.3, "a divider inside a widget"),
    ("grid", "surface-000", 1.15, "the dots of the flow canvas"),
    ("grid", "surface-100", 1.1, "chart gridlines"),
)

# A page of pure white glares, whatever sits on it. The brightest surface stays
# at or under about #eeeeee, so a light theme is light rather than a lamp.
BRIGHTEST = 0.86

# The series are told apart by hue: a theme may move them, but not onto one
# another.
SERIES_APART = 20.0        # degrees of hue between any two series

FONTS = {
    # name: (Google Fonts family or None for a system stack, fallback stack)
    "ui": {
        "IBM Plex Sans": ("IBM+Plex+Sans:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Inter": ("Inter:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Roboto": ("Roboto:wght@400;500;700", "ui-sans-serif, system-ui, sans-serif"),
        "Source Sans 3": ("Source+Sans+3:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Work Sans": ("Work+Sans:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "DM Sans": ("DM+Sans:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Manrope": ("Manrope:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Rubik": ("Rubik:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Lexend": ("Lexend:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Space Grotesk": ("Space+Grotesk:wght@400;500;600", "ui-sans-serif, system-ui, sans-serif"),
        "Atkinson Hyperlegible": ("Atkinson+Hyperlegible:wght@400;700", "ui-sans-serif, system-ui, sans-serif"),
        "Nunito Sans": ("Nunito+Sans:wght@400;600;700", "ui-sans-serif, system-ui, sans-serif"),
        "System": (None, "ui-sans-serif, system-ui, sans-serif"),
    },
    "mono": {
        "IBM Plex Mono": ("IBM+Plex+Mono:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "JetBrains Mono": ("JetBrains+Mono:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "Fira Code": ("Fira+Code:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "Source Code Pro": ("Source+Code+Pro:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "Roboto Mono": ("Roboto+Mono:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "DM Mono": ("DM+Mono:wght@400;500", 'ui-monospace, "SFMono-Regular", monospace'),
        "Space Mono": ("Space+Mono:wght@400;700", 'ui-monospace, "SFMono-Regular", monospace'),
        "Ubuntu Mono": ("Ubuntu+Mono:wght@400;700", 'ui-monospace, "SFMono-Regular", monospace'),
        "System": (None, 'ui-monospace, "SFMono-Regular", monospace'),
    },
}
RADIUS = {"sm": 3, "md": 6, "lg": 10}
RADIUS_MAX = 16
SHADOWS = ("none", "soft", "deep")
NAME_MAX = 40
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _channel(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hue(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    hi, lo = max(r, g, b), min(r, g, b)
    if hi == lo:
        return None
    d = hi - lo
    if hi == r:
        x = ((g - b) / d) % 6
    elif hi == g:
        x = (b - r) / d + 2
    else:
        x = (r - g) / d + 4
    return x * 60.0


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:32] or "theme"


def shape(theme):
    """Problems with the file itself, and the theme cleaned to exactly the
    keys the console uses. Unknown keys are dropped, never passed on."""
    problems = []
    if not isinstance(theme, dict):
        return None, ["a theme is a JSON object"]
    if theme.get("format") != FORMAT:
        problems.append('"format" must be "%s"' % FORMAT)
    name = theme.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append('"name" is missing')
        name = ""
    elif len(name) > NAME_MAX or re.search(r"[<>\"'`\\\x00-\x1f]", name):
        problems.append('"name" must be under %d characters, with no quotes or '
                        'angle brackets' % NAME_MAX)
    base = theme.get("base")
    if base not in ("dark", "light"):
        problems.append('"base" must be "dark" or "light": it tells the browser '
                        'which way its own scrollbars and form controls go')
    colors = theme.get("colors")
    clean = {}
    if not isinstance(colors, dict):
        problems.append('"colors" is missing')
        colors = {}
    for key in COLORS:
        v = colors.get(key)
        if not isinstance(v, str) or not HEX.match(v):
            problems.append('colors."%s" must be a #rrggbb colour%s'
                            % (key, "" if key in colors else " (it is missing)"))
        else:
            clean[key] = v.lower()
    extra = sorted(set(colors) - set(COLORS))
    if extra:
        problems.append("colors has names the console does not use: %s"
                        % ", ".join(extra))
    fonts = theme.get("fonts") or {}
    got_fonts = {}
    for role, allowed in FONTS.items():
        v = fonts.get(role, "IBM Plex Sans" if role == "ui" else "IBM Plex Mono")
        if v not in allowed:
            problems.append('fonts.%s "%s" is not one the console can load; '
                            'choose from: %s' % (role, v, ", ".join(allowed)))
        else:
            got_fonts[role] = v
    radius = theme.get("radius") or {}
    got_radius = {}
    for key, default in RADIUS.items():
        v = radius.get(key, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= RADIUS_MAX:
            problems.append("radius.%s must be a number from 0 to %d" % (key, RADIUS_MAX))
        else:
            got_radius[key] = round(float(v), 1)
    shadow = theme.get("shadow", "soft")
    if shadow not in SHADOWS:
        problems.append('"shadow" must be one of %s' % ", ".join(SHADOWS))
    out = {"format": FORMAT, "name": name.strip(), "base": base,
           "colors": clean, "fonts": got_fonts, "radius": got_radius,
           "shadow": shadow}
    return out, problems


def readability(colors):
    """Every pair in RULES and SEPARATIONS that falls short, worst first."""
    found = []
    for fg, bg, need, what in RULES:
        if fg in colors and bg in colors:
            got = contrast(colors[fg], colors[bg])
            if got < need:
                found.append((need - got, "%s on %s is %.2f:1; %s needs %.1f:1"
                              % (fg, bg, got, what, need)))
    for a, b, need, what in SEPARATIONS:
        if a in colors and b in colors:
            got = contrast(colors[a], colors[b])
            if got < need:
                found.append((need - got, "%s against %s is %.2f:1; %s needs at "
                              "least %.2f:1 to be seen" % (a, b, got, what, need)))
    hues = [(k, hue(colors[k])) for k in ("series-1", "series-2", "series-3", "series-4")
            if k in colors]
    for i, (ka, ha) in enumerate(hues):
        for kb, hb in hues[i + 1:]:
            if ha is None or hb is None:
                found.append((1, "%s and %s must both have a hue; a grey series "
                              "is lost among the gridlines" % (ka, kb)))
                continue
            gap = min(abs(ha - hb), 360 - abs(ha - hb))
            if gap < SERIES_APART:
                found.append((1, "%s and %s are only %.0f degrees of hue apart; "
                              "charts tell them apart by hue, so keep them %.0f "
                              "or more" % (ka, kb, gap, SERIES_APART)))
    for k in SURFACES:
        if k in colors and luminance(colors[k]) > BRIGHTEST:
            found.append((1, "%s %s is too bright to sit behind a whole screen; "
                          "keep surfaces at or under about #eeeeee"
                          % (k, colors[k])))
    if "scrim" in colors and luminance(colors["scrim"]) > 0.05:
        found.append((1, "scrim must be dark (it dims the page behind a drawer "
                      "or a picture), whatever the theme's base"))
    found.sort(key=lambda p: -p[0])
    return [msg for _, msg in found]


def check(theme):
    """(cleaned theme, problems). No problems means the console will take it."""
    clean, problems = shape(theme)
    if clean is None:
        return None, problems
    return clean, problems + readability(clean["colors"])


def main(argv):
    if len(argv) != 2:
        print("usage: check_theme.py THEME.json")
        return 2
    try:
        with open(argv[1], encoding="utf-8") as fh:
            theme = json.load(fh)
    except (OSError, ValueError) as exc:
        print("cannot read %s: %s" % (argv[1], exc))
        return 1
    _clean, problems = check(theme)
    for p in problems:
        print("- " + p)
    print("ok" if not problems else "%d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
