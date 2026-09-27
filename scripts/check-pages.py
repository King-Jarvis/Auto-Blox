#!/usr/bin/env python3
"""Load every page in a real browser and report what the console says."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jsc  # noqa: E402

PAGES = ["/", "/flows", "/iot", "/iot#devices", "/iot#enrollment",
         "/iot#flashing", "/iot#boards", "/cameras"]
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
    profile = tempfile.mkdtemp(prefix="check-pages-")
    port = jsc._free_port()
    argv = [browser(), "--headless=new", "--remote-debugging-port=%d" % port,
            "--no-first-run", "--disable-gpu", "--hide-scrollbars",
            "--user-data-dir=" + profile, "about:blank"]
    if phone:
        argv[4:4] = ["--window-size=390,844", "--force-device-scale-factor=2",
                     "--user-agent=" + PHONE_UA]
    else:
        argv[4:4] = ["--window-size=1400,1000"]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    bad = 0
    try:
        ws = jsc._WS(jsc._wait_for_target(port))
        seq = [0]

        def call(method, **params):
            seq[0] += 1
            ws.send({"id": seq[0], "method": method, "params": params})
            while True:
                msg = ws.recv()
                if msg.get("id") == seq[0]:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result", {})

        def settle(seconds):
            """Collect events for a while, then go back to blocking."""
            out = []
            end = time.time() + seconds
            ws.sock.settimeout(0.4)
            while time.time() < end:
                try:
                    msg = ws.recv()
                except Exception:
                    continue
                if "method" in msg:
                    out.append(msg)
            ws.sock.settimeout(None)
            return out

        call("Runtime.enable")
        call("Log.enable")
        call("Page.enable")
        if phone:
            call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                 deviceScaleFactor=2, mobile=True)

        for path in (PHONE if phone else PAGES):
            call("Page.navigate", url=base + path)
            problems = []
            for ev in settle(7.0 if path == "/cameras" else 4.0):
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
                    if "favicon" in (entry.get("url") or ""):
                        continue
                    problems.append("%s %s" % (entry["text"], entry.get("url") or ""))

            p = call("Runtime.evaluate", returnByValue=True,
                     expression=PROBE)["result"].get("value") or {}
            if not (p.get("nav") or "").startswith(SCREENS):
                problems.append("the nav is missing or reordered: %r" % p.get("nav"))
            if p.get("overflow"):
                problems.append("scrolls sideways (%s)" % p.get("width"))
            if path.startswith("/iot") and p.get("tabs") != 5:
                problems.append("expected 5 tabs, found %s" % p.get("tabs"))
            want = path.partition("#")[2]
            if want and (p.get("tab") or "").lower()[:len(want)] != want:
                problems.append("#%s did not select its tab (on %r)" % (want, p.get("tab")))

            if shots:
                shot = call("Page.captureScreenshot", format="png",
                            captureBeyondViewport=True)
                name = path.replace("/", "_").replace("#", "-") or "_root"
                with open(os.path.join(shots, "%s%s.png" % (
                        "phone" if phone else "page", name)), "wb") as fh:
                    fh.write(base64.b64decode(shot["data"]))

            print("%s %-8s %-18s nav=%s active=%s tab=%s widgets=%s"
                  % ("FAIL" if problems else "ok  ",
                     "phone" if phone else "desktop", path,
                     p.get("nav"), p.get("active"), p.get("tab"), p.get("widgets")))
            for line in problems:
                bad += 1
                print("      " + line[:300])
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
            proc.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
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
