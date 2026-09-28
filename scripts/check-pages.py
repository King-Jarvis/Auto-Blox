#!/usr/bin/env python3
"""Load every page in a real browser and report what the console says."""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jsc  # noqa: E402

# Every tab on /iot, in the order iot.js lists them (TABS).
IOT_HASHES = ["setup", "devices", "bluetooth", "enrollment", "flashing", "boards"]
IOT_TABS = len(IOT_HASHES)
PAGES = ["/", "/flows", "/iot"] + ["/iot#" + h for h in IOT_HASHES] + ["/cameras"]
PHONE = ["/", "/flows", "/iot", "/cameras"]
PHONE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

PROBE = """
  (function () {
    var dock = document.querySelector('.z-dock');
    var doc = document.documentElement;
    return {
      nav: dock ? Array.prototype.map.call(
            dock.querySelectorAll('.z-dock-label'),
            function (n) { return n.textContent; }).join('|') : null,
      active: dock && dock.querySelector('.z-dock-item.is-active')
        ? dock.querySelector('.z-dock-item.is-active').textContent : null,
      tabs: document.querySelectorAll('.iot-tab').length,
      tab: (document.querySelector('.iot-tab.is-active') || {}).textContent || null,
      widgets: document.querySelectorAll('.z-widget').length,
      overflow: doc.scrollWidth > doc.clientWidth + 1,
      // A container that clips hides a card running off the edge from the
      // check above, so cards and widgets are measured themselves.
      cut: Array.prototype.filter.call(
        document.querySelectorAll('.flow-card, .z-widget, [class$="-card"], [class*="-card "]'),
        function (n) {
          var r = n.getBoundingClientRect();
          return r.width > 0 && r.right > doc.clientWidth + 1;
        }).map(function (n) { return n.className.split(' ')[0]; }).slice(0, 5),
      width: doc.scrollWidth + '/' + doc.clientWidth
    };
  })()
"""
# Every screen carries these four, in this order, on every page.
SCREENS = "Overview|Flows|IOT|Cameras"

# Every visible piece of text against what is actually behind it, in whatever
# theme the page is wearing. This is what catches a colour a theme cannot
# reach: text that passes in Dark because the page happened to be dark, and
# vanishes in Light. Text over a picture is skipped (its background is the
# picture), and so is anything disabled, which WCAG exempts.
CONTRAST = r"""
  (function () {
    function rgba(s) {
      var m = /rgba?\(([^)]+)\)/.exec(s);
      if (m) {
        var p = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat);
        return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
      }
      m = /color\(srgb ([^)]+)\)/.exec(s);
      if (m) {
        var q = m[1].split(/[ \/]+/).filter(Boolean).map(parseFloat);
        return [q[0] * 255, q[1] * 255, q[2] * 255, q.length > 3 ? q[3] : 1];
      }
      return null;
    }
    function lum(c) {
      var v = [c[0], c[1], c[2]].map(function (x) {
        x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
    }
    function over(top, under) {
      var a = top[3];
      return [top[0] * a + under[0] * (1 - a), top[1] * a + under[1] * (1 - a),
              top[2] * a + under[2] * (1 - a), 1];
    }
    // The colour behind an element: the first opaque background up the tree,
    // with the translucent ones on the way blended over it.
    function behind(el) {
      var layers = [];
      for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
        var cs = getComputedStyle(n);
        if (cs.backgroundImage && cs.backgroundImage !== "none" &&
            !/radial-gradient/.test(cs.backgroundImage)) return null;
        if (n.tagName === "IMG" || n.tagName === "VIDEO" || n.tagName === "CANVAS") return null;
        var c = rgba(cs.backgroundColor);
        if (c && c[3] > 0) {
          layers.push(c);
          if (c[3] >= 1) break;
        }
      }
      var base = [255, 255, 255, 1];
      if (layers.length && layers[layers.length - 1][3] >= 1) base = layers.pop();
      for (var i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
      return base;
    }
    var seen = {}, bad = [];
    var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      var t = walk.currentNode, el = t.parentElement;
      var text = t.textContent.trim();
      if (!text || !el || seen[el.__cid]) continue;
      var r = el.getBoundingClientRect();
      if (!r.width || !r.height || el.closest("[disabled], [aria-disabled=true], .is-disabled")) continue;
      var cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) continue;
      var faded = false;
      for (var n = el; n; n = n.parentElement) {
        if (parseFloat(getComputedStyle(n).opacity) < 1) { faded = true; break; }
      }
      if (faded) continue;
      var fg = rgba(cs.color), bg = behind(el);
      if (!fg || !bg) continue;
      fg = over(fg, bg);
      var a = lum(fg), b = lum(bg);
      var ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      var size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight, 10) >= 700;
      var need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
      if (ratio + 0.005 < need) {
        var key = (el.className && el.className.baseVal === undefined ? el.className : el.tagName) + ":" + ratio.toFixed(2);
        if (seen[key]) continue;
        seen[key] = 1;
        bad.push(ratio.toFixed(2) + ":1 " + text.slice(0, 30).replace(/\s+/g, " ") +
                 " (" + el.tagName.toLowerCase() + "." + String(el.className).split(" ")[0] + ")");
      }
    }
    return bad.slice(0, 8);
  })()
"""

# A garish theme that passes every rule, used by --themes: anything that stays
# dark or grey in it is a colour no theme can reach.
LOUD = {
    "format": "auto-blox-theme/1", "name": "Check pages loud", "base": "dark",
    "colors": {
        "surface-000": "#1a0f24", "surface-100": "#24163a", "surface-200": "#2f1d4a",
        "surface-300": "#120a1a", "hairline": "#4d3769", "hairline-strong": "#9277b8",
        "ink": "#f5edfd", "ink-muted": "#cbb9e2", "ink-faint": "#b09bcb",
        "signal": "#3ee07a", "signal-wash": "#173a28", "on-signal": "#07210f",
        "link": "#ffd24a", "ok": "#5fe0b0", "warn": "#ffc14a", "serious": "#ff9a5c",
        "critical": "#ff7b96", "ok-wash": "#12372c", "warn-wash": "#3a2e10",
        "critical-wash": "#43172a", "series-1": "#58a6ff", "series-2": "#20d0b0",
        "series-3": "#f0a020", "series-4": "#ff70d0", "grid": "#3f2d58",
        "focus-ring": "#ffd24a", "scrim": "#050308"},
    "fonts": {"ui": "System", "mono": "System"},
    "radius": {"sm": 0, "md": 12, "lg": 16}, "shadow": "deep"}


def browser():
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no chromium on this machine")


def themed(path, theme):
    """The page, shown in a theme for this load only (?theme=, see nav.js)."""
    if not theme:
        return path
    page, _, tab = path.partition("#")
    return "%s?theme=%s%s" % (page, theme, "#" + tab if tab else "")


def run(base, phone, shots, theme=None):
    b = jsc.Browser(browser(), phone=phone, width=390 if phone else 1400,
                    height=844 if phone else 1000,
                    user_agent=PHONE_UA if phone else None)
    bad = 0
    try:
        b.call("Runtime.enable")
        b.call("Log.enable")
        b.call("Page.enable")
        # As a phone on the IoT network sees it: no internet, so no webfont,
        # and the fallback font is wider. Layout bugs hide behind the webfont.
        b.call("Network.enable")
        b.call("Network.setBlockedURLs", urls=["*fonts.googleapis.com*",
                                               "*fonts.gstatic.com*"])
        if phone:
            b.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                   deviceScaleFactor=2, mobile=True)

        for path in (PHONE if phone else PAGES):
            b.call("Page.navigate", url=base + themed(path, theme))
            problems = []
            for ev in b.settle(7.0 if path == "/cameras" else 4.0):
                if ev["method"] == "Runtime.exceptionThrown":
                    det = ev["params"]["exceptionDetails"]
                    problems.append("exception: " + (
                        det.get("exception", {}).get("description") or det.get("text")))
                elif (ev["method"] == "Runtime.consoleAPICalled"
                      and ev["params"]["type"] == "error"):
                    problems.append("console.error " + json.dumps(
                        [a.get("value") for a in ev["params"]["args"]]))
                elif (ev["method"] == "Log.entryAdded"
                      and ev["params"]["entry"]["level"] == "error"):
                    entry = ev["params"]["entry"]
                    if "favicon" in (entry.get("url") or "") or "fonts.g" in (entry.get("url") or ""):
                        continue
                    problems.append("%s %s" % (entry["text"], entry.get("url") or ""))

            p = b.evaluate(PROBE) or {}
            if not (p.get("nav") or "").startswith(SCREENS):
                problems.append("the nav is missing or reordered: %r" % p.get("nav"))
            if p.get("overflow"):
                problems.append("scrolls sideways (%s)" % p.get("width"))
            if p.get("cut"):
                problems.append("runs off the right edge: %s" % ", ".join(p["cut"]))
            for line in b.evaluate(CONTRAST) or []:
                problems.append("hard to read: " + line)
            if path.startswith("/iot") and p.get("tabs") != IOT_TABS:
                problems.append("expected %d tabs, found %s" % (IOT_TABS, p.get("tabs")))
            want = path.partition("#")[2]
            if want and (p.get("tab") or "").lower()[:len(want)] != want:
                problems.append("#%s did not select its tab (on %r)" % (want, p.get("tab")))

            if shots:
                name = path.replace("/", "_").replace("#", "-") or "_root"
                b.screenshot(os.path.join(shots, "%s%s%s.png" % (
                    "phone" if phone else "page", "-" + theme if theme else "", name)), full=True)

            print("%s %-8s %-6s %-18s nav=%s active=%s tab=%s widgets=%s"
                  % ("FAIL" if problems else "ok  ",
                     "phone" if phone else "desktop", (theme or "")[:6], path,
                     p.get("nav"), p.get("active"), p.get("tab"), p.get("widgets")))
            for line in problems:
                bad += 1
                print("      " + line[:300])
    finally:
        b.close()
    return bad


def post(base, path, body):
    import urllib.request
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    token = os.environ.get("CONSOLE_TOKEN")
    if token:
        req.add_header("X-Console-Token", token)
    with urllib.request.urlopen(req, timeout=10) as fh:
        return json.loads(fh.read())


def import_loud(base):
    """LOUD, imported but not switched to; main() deletes it afterwards."""
    return post(base, "/api/themes", LOUD)["id"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = args[0] if args else "http://127.0.0.1:8787"
    shots = None
    for a in sys.argv[1:]:
        if a.startswith("--shots="):
            shots = a.split("=", 1)[1]
            os.makedirs(shots, exist_ok=True)
    # --themes checks every page in Dark, Light and LOUD. It imports LOUD into
    # the console it is pointed at (without switching to it), so point it at
    # the demo world: scripts/demo.py --serve.
    themes, loud = [None], None
    for a in sys.argv[1:]:
        if a.startswith("--theme="):
            themes = [a.split("=", 1)[1]]
        elif a == "--themes":
            loud = import_loud(base)
            themes = ["dark", "light", loud]
    bad = 0
    try:
        for theme in themes:
            bad += run(base, False, shots, theme) + run(base, True, shots, theme)
    finally:
        if loud:
            post(base, "/api/themes/delete", {"id": loud})
    print("\n%d problem%s" % (bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
