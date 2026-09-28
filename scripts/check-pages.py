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


def browser():
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no chromium on this machine")


def run(base, phone, shots):
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
            b.call("Page.navigate", url=base + path)
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
            if path.startswith("/iot") and p.get("tabs") != IOT_TABS:
                problems.append("expected %d tabs, found %s" % (IOT_TABS, p.get("tabs")))
            want = path.partition("#")[2]
            if want and (p.get("tab") or "").lower()[:len(want)] != want:
                problems.append("#%s did not select its tab (on %r)" % (want, p.get("tab")))

            if shots:
                name = path.replace("/", "_").replace("#", "-") or "_root"
                b.screenshot(os.path.join(shots, "%s%s.png" % (
                    "phone" if phone else "page", name)), full=True)

            print("%s %-8s %-18s nav=%s active=%s tab=%s widgets=%s"
                  % ("FAIL" if problems else "ok  ",
                     "phone" if phone else "desktop", path,
                     p.get("nav"), p.get("active"), p.get("tab"), p.get("widgets")))
            for line in problems:
                bad += 1
                print("      " + line[:300])
    finally:
        b.close()
    return bad


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = args[0] if args else "http://127.0.0.1:8787"
    shots = None
    for a in sys.argv[1:]:
        if a.startswith("--shots="):
            shots = a.split("=", 1)[1]
            os.makedirs(shots, exist_ok=True)
    bad = run(base, False, shots) + run(base, True, shots)
    print("\n%d problem%s" % (bad, "" if bad == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
