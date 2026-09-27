/* Cameras — the fleet's eyes, with room to look at them.
 *
 * The dashboard carries the same feed as a glance widget. This is the screen
 * you leave open. Everything about fetching and swapping frames is in
 * cameras-core.js; this file is layout.
 */
(function () {
  "use strict";
  var Z = window.Zero2W;
  var C = window.Zero2WCam;

  var S = { list: [], now: 0, tiles: {}, signature: "", error: null,
            nav: null, tools: null, pace: C.pacer() };

  function el(tag, cls, text) { return C.el(tag, cls, text); }

  function tile(cam) {
    var box = el("figure", "cam-tile");
    var view = C.swapper("cam-frame");
    box.appendChild(view.el);

    var shade = el("div", "cam-shade");
    shade.appendChild(el("span", "cam-name", cam.name || cam.id));
    var seconds = C.frameAge(view, cam, S.now);
    var live = C.isLive(seconds, cam);
    var badge = el("span", "cam-state" + (live ? " is-live" : ""));
    badge.textContent = live ? "live" : (cam.online ? "stalled" : "offline");
    shade.appendChild(badge);
    box.appendChild(shade);

    var cap = el("figcaption", "cam-meta");
    cap.appendChild(el("span", null, cam.ip || "no address"));
    if (cam.size) cap.appendChild(el("span", null, cam.size));
    var age = el("span", "cam-age", C.ageText(seconds));
    cap.appendChild(age);
    box.appendChild(cap);

    /* Which flow is putting this on the screen, and the node inside it. That
       is the thing you change when the picture is wrong. */
    var src = el("div", "cam-source");
    src.appendChild(el("span", null, cam.flow || "no flow"));
    if (cam.board) src.appendChild(el("span", "cam-board", cam.board));
    box.appendChild(src);

    S.tiles[cam.id] = { view: view, age: age, badge: badge };

    box.tabIndex = 0;
    box.setAttribute("role", "button");
    box.title = "Open " + (cam.name || cam.id);
    box.addEventListener("click", function () { C.openModal(cam); });
    box.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); C.openModal(cam); }
    });
    return box;
  }

  /* The labels change every second; the tiles must not, or the images in them are
     thrown away and the wall flashes. The widget's own dot is counted from the same
     numbers, or the header says "0 of 1 live" over a tile that says "live". */
  function relabel() {
    var live = 0;
    S.list.forEach(function (cam) {
      var t = S.tiles[cam.id];
      if (!t) return;
      var seconds = C.frameAge(t.view, cam, S.now);
      var ok = C.isLive(seconds, cam);
      if (ok) live += 1;
      t.age.textContent = C.ageText(seconds);
      t.badge.textContent = ok ? "live" : (cam.online ? "stalled" : "offline");
      t.badge.classList.toggle("is-live", ok);
    });
    if (!S.tools) return;
    var dot = S.tools.querySelector(".z-dot-row");
    if (!dot) return;
    S.tools.replaceChild(Z.StatusDot({
      state: S.error ? "critical" : (S.list.length ? (live ? "ok" : "warn") : "idle"),
      label: S.error ? "error"
        : (S.list.length ? live + " of " + S.list.length + " live" : "none"),
      pulse: live > 0
    }), dot);
  }

  function render() {
    var grid = document.getElementById("grid");
    grid.textContent = "";
    S.tiles = {};

    var body = [];
    if (S.error) {
      body.push(el("p", "cam-empty", S.error));
    } else if (!S.list.length) {
      var empty = el("div", "cam-empty");
      empty.appendChild(el("strong", null, "No camera is on the screen yet."));
      empty.appendChild(el("span", null,
        "A device shows up here when its deployed flow has a node set to display " +
        "on the main screen — having a lens is not enough. Tick Camera on a " +
        "device config in IOT, flash a camera-capable build, and wire a camera " +
        "node with “show on screen” set."));
      body.push(empty);
    } else {
      var wall = el("div", "cam-grid cam-wall");
      S.list.forEach(function (cam) { wall.appendChild(tile(cam)); });
      body.push(wall);
    }

    var w = Z.Widget({
      title: "Cameras",
      subtitle: "field devices with an eye · /api/iot/cameras",
      // relabel() fills this in from the tiles a moment later.
      state: S.error ? "critical" : (S.list.length ? "warn" : "idle"),
      stateLabel: S.error ? "error"
        : (S.list.length ? "waiting for a frame" : "none"),
      flush: true,
      actions: [Z.Button({
        label: "Devices", size: "sm",
        onClick: function () { location.href = "/iot#devices"; }
      })],
      children: body
    });
    w.classList.add("span-12");
    S.tools = w.querySelector(".z-widget-tools");
    grid.appendChild(w);
    poke();
    relabel();
  }

  function refresh() {
    fetch("/api/iot/cameras").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
      .then(function (d) {
        S.list = d.cameras || [];
        S.now = d.now || 0;
        S.error = null;
        chrome(true);
        var sig = S.list.map(function (c) {
          return [c.id, c.online, c.ip, c.name, c.size, c.flow].join(":");
        }).join("|");
        if (sig !== S.signature) { S.signature = sig; render(); }
        else { relabel(); }
      })
      .catch(function (e) {
        S.error = e.message === "HTTP 401"
          ? "Not signed in. Open this page with ?t=<token> once."
          : "Could not read /api/iot/cameras: " + e.message;
        S.signature = "";
        chrome(false);
        render();
      });
  }

  function poke() {
    S.list.forEach(function (cam) {
      var t = S.tiles[cam.id];
      if (t && S.pace.due(cam)) t.view.load(C.frameURL(cam), cam.name || cam.id, cam.screen_turn);
    });
  }

  /* The dock says whether this page is still hearing from the board. */
  function chrome(live) {
    if (!S.nav) return;
    S.nav.setClock(new Date().toTimeString().slice(0, 8));
    S.nav.setState(live ? "ok" : "critical", live ? "live" : "offline", live);
  }

  function buildTopbar() {
    var bar = document.getElementById("topbar");
    if (!bar) return;
    bar.textContent = "";
    var id = el("div", "topbar-id");
    id.appendChild(el("span", "topbar-host", "cameras"));
    id.appendChild(el("span", "topbar-model", location.host));
    bar.appendChild(id);
  }

  function start() {
    buildTopbar();
    S.nav = window.Zero2WNav.mount("dock", "/cameras");
    render();
    refresh();
    C.ticker(refresh, C.MS.list);
    // One timer for every tile: each is asked at its own rate, and a tick
    // where nothing is due costs a couple of comparisons.
    C.ticker(poke, C.MS.beat);
    // A second tick for the ages, so "4s ago" is not 15 seconds stale.
    C.ticker(relabel, 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
