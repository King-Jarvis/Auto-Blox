/* Auto-Blox — builds the main screen from the Zero2W design system and
   keeps it live off one Server-Sent Events stream. Metric widget bodies are
   rebuilt each sample; the log list and the terminal are append-only, so scroll
   position and typed input survive. */
(function () {
  "use strict";

  var Z = window.Zero2W;
  var HISTORY = 60;                  // samples kept for sparklines (~2 min at 2s)
  var LOG_CAP = 300;                 // rows retained in the log widget
  var TERM_CAP = 400;                // lines retained in the terminal

  var state = {
    snap: null,
    connected: false,
    logs: [],
    term: [{ kind: "dim", text: "Auto-Blox — type a command. Try: uname -a" }],
    cwd: null,
    busy: false,
    history: { cpu: [], zones: {}, net: {} },
    panels: {}, holdProcesses: false, heldRows: []
  };

  /* ---------- helpers --------------------------------------------------- */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function push(arr, v) {
    arr.push(v);
    if (arr.length > HISTORY) arr.shift();
    return arr;
  }
  function svgGlyph(d, size) {
    var NS = "http://www.w3.org/2000/svg";
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 16 16");
    s.setAttribute("width", size || 13);
    s.setAttribute("height", size || 13);
    s.setAttribute("aria-hidden", "true");
    var p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "square");
    s.appendChild(p);
    return s;
  }

  /* A Widget plus handles for updating it in place. */
  function panel(key, opts) {
    var node = Z.Widget(opts);
    node.id = "panel-" + key;
    var p = {
      el: node,
      body: node.querySelector(".z-widget-body"),
      tools: node.querySelector(".z-widget-tools"),
      /* The dot and any action buttons share one container, so emptying it to
         redraw the dot silently deleted the buttons. Replace the dot alone. */
      setState: function (st, label, pulse) {
        var old = this.tools.querySelector(".z-dot-row");
        if (!st) {
          if (old) this.tools.removeChild(old);
          return;
        }
        var fresh = Z.StatusDot({ state: st, label: label, pulse: !!pulse });
        if (old) this.tools.replaceChild(fresh, old);
        else this.tools.insertBefore(fresh, this.tools.firstChild);
      },
      fill: function (child) {
        this.body.innerHTML = "";
        if (child) this.body.appendChild(child);
      }
    };
    state.panels[key] = p;
    return p;
  }

  /* ---------- the screen skeleton, built once -------------------------- */
  function build() {
    var grid = document.getElementById("grid");

    var banner = el("div", "banner", "");
    banner.id = "banner";
    banner.appendChild(el("span", null, "Stream disconnected — values below are the last known readings."));
    document.querySelector(".page").insertBefore(banner, grid);

    var health = panel("health", {
      title: "Health", subtitle: "/proc/loadavg · /proc/meminfo · statvfs", state: "idle"
    });

    var thermal = panel("thermal", {
      title: "Thermal", subtitle: "/sys/class/thermal", state: "idle",
      footer: [document.createTextNode("passively cooled · thresholds read from trip points")]
    });

    var net = panel("network", { title: "Network", subtitle: "/proc/net/dev", state: "idle" });

    var logs = panel("logs", {
      title: "Live logs", subtitle: "journalctl -f", state: "idle", flush: true
    });

    var cameras = panel("cameras", {
      title: "Cameras", subtitle: "field devices with an eye", state: "idle", flush: true
    });
    var cores = panel("cores", { title: "Cores", subtitle: "Cortex-A53", state: "idle" });

    var storage = panel("storage", { title: "Storage", subtitle: "/proc/mounts", state: "idle" });

    var term = panel("terminal", {
      title: "Terminal", subtitle: "bash -lc · one command per line", state: "idle", flush: true
    });
    buildTerminal(term);

    // Full width: the command column is the whole point of this widget and it
    // is the first thing that gets clipped.
    var holdBtn = Z.Button({
      label: "Hold", variant: "ghost", size: "sm",
      onClick: function () {
        state.holdProcesses = !state.holdProcesses;
        holdBtn.lastChild.textContent = state.holdProcesses ? "Resume" : "Hold";
        holdBtn.className = "z-btn z-btn--sm " +
          (state.holdProcesses ? "z-btn--primary" : "z-btn--ghost");
        if (!state.holdProcesses && state.snap) renderProcesses(state.snap);
      }
    });
    var procs = panel("processes", {
      title: "Processes", subtitle: "/proc · sorted by CPU", state: "idle", flush: true,
      actions: holdBtn
    });

    // Layout declared in one place, in reading order. Each row must total 12,
    // or a widget ends up beside a gap.
    [
      [health, 12],                 // the glance row
      [thermal, 6], [net, 6],       // thermal first: no fan, degrades first
      [logs, 8], [term, 4],
      [procs, 12],                  // full width: the command column is the point
      [cameras, 12],                // the fleet's eyes, beside this board's own vitals
      [cores, 6], [storage, 6]
    ].forEach(function (row) {
      row[0].el.classList.add("span-" + row[1]);
      grid.appendChild(row[0].el);
    });

    buildTopbar();
    buildDock();
    startCameras(cameras);
  }

  function buildTopbar() {
    var bar = document.getElementById("topbar");
    var id = el("div", "topbar-id");
    id.appendChild(el("span", "topbar-host", "auto-blox"));
    id.appendChild(el("span", "topbar-model", "—"));
    bar.appendChild(id);
  }

  function buildDock() {
    /* The four screens come from nav.js and are identical on every page.
       These are the dashboard's own in-page jumps, appended after them. */
    var extra = [
      { label: "Thermal", target: "panel-thermal", glyph: svgGlyph("M8 2v7M8 12v2M4 12h8") },
      { label: "Network", target: "panel-network", glyph: svgGlyph("M2 12h12M5 9h6M7 6h2") },
      { label: "Logs", target: "panel-logs", glyph: svgGlyph("M3 4h10M3 8h10M3 12h6") },
      { label: "Processes", target: "panel-processes", glyph: svgGlyph("M3 3h10v10H3zM6 6h4v4H6z") },
      { label: "Terminal", target: "panel-terminal", glyph: svgGlyph("M3 5l3 3-3 3M9 11h4") }
    ];
    var nav = window.Zero2WNav.mount("dock", "/", extra);
    if (!nav) return;
    // The Dock renders buttons; wiring is ours. Scroll to the matching panel.
    nav.extras.forEach(function (pair) {
      var target = pair.item.target;
      if (!target) return;
      pair.button.addEventListener("click", function () {
        nav.items.forEach(function (o) {
          o.button.classList.remove("is-active");
          o.button.removeAttribute("aria-current");
        });
        pair.button.classList.add("is-active");
        pair.button.setAttribute("aria-current", "page");
        var t = document.getElementById(target);
        if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    state.nav = nav;
  }

  /* ---------- cameras ---------------------------------------------------- */
  /* The glance view. The whole of /cameras is the same feed with room to
     breathe; everything both of them need lives in cameras-core.js. */
  function startCameras(panelRef) {
    var C = window.Zero2WCam;
    var cams = { list: [], now: 0, tiles: {}, signature: "" };

    function refresh() {
      fetch("/api/iot/cameras").then(function (r) { return r.json(); })
        .then(function (d) {
          cams.list = d.cameras || [];
          cams.now = d.now || 0;
          // Rebuilding the tiles throws away the images in them, which is a
          // visible flash. Only redraw when the set has actually changed — and
          // "the set" includes a camera's name and frame size, or a rename never shows.
          var sig = cams.list.map(function (c) {
            return [c.id, c.online, c.ip, c.name, c.size].join(":");
          }).join("|");
          if (sig !== cams.signature) { cams.signature = sig; render(); }
          else { label(); }
        })
        .catch(function () { /* the IOT side may simply not be there */ });
    }

    function label() {
      var live = 0;
      cams.list.forEach(function (cam) {
        var t = cams.tiles[cam.id];
        var seconds = C.frameAge(t && t.view, cam, cams.now);
        if (C.isLive(seconds, cam)) live += 1;
        if (t && t.age) t.age.textContent = C.ageText(seconds);
      });
      panelRef.setState(cams.list.length ? (live ? "ok" : "warn") : "idle",
                        cams.list.length
                          ? live + " of " + cams.list.length + " live" : "none");
    }

    function render() {
      cams.tiles = {};
      if (!cams.list.length) {
        label();
        panelRef.fill(C.el("p", "cam-empty",
          "No device is configured with a camera. Tick Camera on a device config " +
          "on the IOT screen, and flash it with a camera-capable build."));
        return;
      }
      var wrap = C.el("div", "cam-grid");
      cams.list.forEach(function (cam) { wrap.appendChild(tile(cam)); });
      panelRef.fill(wrap);
      label();
      poke();
    }

    function tile(cam) {
      var box = C.el("figure", "cam-tile");
      var view = C.swapper("cam-frame");
      box.appendChild(view.el);

      var shade = C.el("div", "cam-shade");
      shade.appendChild(C.el("span", "cam-name", cam.name || cam.id));
      var badge = C.el("span", "cam-state" + (cam.online ? " is-live" : ""));
      badge.textContent = cam.online ? "live" : (cam.ip ? "not seen" : "no address");
      shade.appendChild(badge);
      box.appendChild(shade);

      var cap = C.el("figcaption", "cam-meta", C.metaLine(cam));
      var age = C.el("span", "cam-age", C.ageText(C.frameAge(view, cam, cams.now)));
      cap.appendChild(age);
      box.appendChild(cap);
      cams.tiles[cam.id] = { view: view, age: age };

      box.tabIndex = 0;
      box.setAttribute("role", "button");
      box.title = "Open " + (cam.name || cam.id);
      box.addEventListener("click", function () { C.openModal(cam); });
      box.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); C.openModal(cam); }
      });
      return box;
    }

    var pace = C.pacer();

    function poke() {
      cams.list.forEach(function (cam) {
        var t = cams.tiles[cam.id];
        if (t && pace.due(cam)) t.view.load(C.frameURL(cam), cam.name || cam.id, cam.screen_turn);
      });
    }

    refresh();
    C.ticker(refresh, C.MS.list);
    // One timer, each tile asked at whatever rate its flow set.
    C.ticker(poke, C.MS.beat);
    C.ticker(label, 1000);
  }

  /* ---------- renderers ------------------------------------------------- */
  function renderHealth(s) {
    var p = state.panels.health;
    var wrap = el("div", "strip");
    var load = s.load || {}, mem = s.memory || {}, up = s.uptime || {};
    var root = (s.filesystems || []).filter(function (f) { return f.mount === "/"; })[0] || {};
    push(state.history.cpu, (s.cpu && s.cpu.total) || 0);

    wrap.appendChild(Z.StatTile({
      label: "load 1m", value: (load.one != null ? load.one.toFixed(2) : "--"),
      state: load.state, caption: (load.ncpu || 4) + " cores",
      trend: state.history.cpu.slice(), series: "series-1", sparkWidth: 88
    }));
    wrap.appendChild(Z.StatTile({
      label: "uptime", value: up.human || "--", caption: up.since ? "since " + up.since : ""
    }));
    wrap.appendChild(Z.StatTile({
      label: "memory", value: (mem.pct != null ? mem.pct : "--"), unit: "%",
      state: mem.state, caption: (mem.used_h || "--") + " of " + (mem.total_h || "--")
    }));
    wrap.appendChild(Z.StatTile({
      label: "root fs", value: (root.pct != null ? root.pct : "--"), unit: "%",
      state: root.state, caption: (root.used_h || "--") + " of " + (root.total_h || "--")
    }));
    p.fill(wrap);
    p.setState(worst([load.state, mem.state, root.state]), "nominal", false);
  }

  function renderThermal(s) {
    var p = state.panels.thermal;
    var wrap = el("div", "gauges");
    var states = [];
    (s.thermal || []).forEach(function (z) {
      states.push(z.state);
      var note;
      if (z.temp == null) note = "no reading";
      else if (z.warn == null) note = "trip " + z.trip + " °C";
      else note = "warn " + z.warn + " °C";
      wrap.appendChild(Z.ThermalGauge({
        zone: z.zone, temp: z.temp, min: 20, max: 100,
        warn: z.warn, trip: z.trip, series: z.series, note: note
      }));
    });
    p.fill(wrap);
    var w = worst(states);
    p.setState(w, w === "ok" ? "nominal" : w, state.connected);
  }

  function renderNetwork(s) {
    var p = state.panels.network;
    var wrap = el("div", "rows");
    var states = [];
    (s.network || []).forEach(function (i) {
      states.push(i.state);
      var h = state.history.net[i.name] || (state.history.net[i.name] = { rx: [], tx: [] });
      push(h.rx, i.rx_kib || 0);
      push(h.tx, i.tx_kib || 0);

      var box = el("div", "iface");
      var head = el("div", "iface-head");
      head.appendChild(Z.StatusDot({ state: i.state, label: i.name, mono: true }));
      var right = el("div", "iface-rates");
      if (i.is_default) right.appendChild(Z.Badge({ text: "default", tone: "ok" }));
      right.appendChild(Z.Badge({ text: i.operstate, tone: i.state }));
      head.appendChild(right);
      box.appendChild(head);

      if (i.address) box.appendChild(el("div", "iface-addr", i.address));

      var rates = el("div", "iface-rates");
      rates.appendChild(rate("rx", i.rx_kib, h.rx, "series-1"));
      rates.appendChild(rate("tx", i.tx_kib, h.tx, "series-2"));
      box.appendChild(rates);
      wrap.appendChild(box);
    });
    p.fill(wrap);
    var def = (s.network || []).filter(function (i) { return i.is_default; })[0];
    p.setState(def ? def.state : worst(states), def ? def.name : "no default route", state.connected);
  }

  function rate(kind, value, hist, series) {
    var d = el("div", "iface-rate");
    d.appendChild(el("span", null, kind));
    var b = el("b", null, value != null ? value.toFixed(1) : "--");
    d.appendChild(b);
    d.appendChild(el("span", null, "KiB/s"));
    // Both rates share one scale so rx and tx are comparable at a glance.
    var top = Math.max(64, Math.max.apply(null, hist.concat([1])));
    d.appendChild(Z.Sparkline({
      data: hist.slice(), series: series, width: 96, height: 24,
      min: 0, max: top, area: kind === "rx",
      label: kind + " over the last " + hist.length * 2 + " seconds"
    }));
    return d;
  }

  function renderCores(s) {
    var p = state.panels.cores;
    var wrap = el("div", "stack");
    (s.cpu && s.cpu.cores || []).forEach(function (c) {
      wrap.appendChild(Z.MeterBar({
        label: c.core, mono: true, value: c.pct, display: c.pct.toFixed(0) + "%",
        series: "series-1"
      }));
    });
    p.fill(wrap);
    var tot = s.cpu && s.cpu.total;
    p.setState(tot == null ? "idle" : tot > 90 ? "warn" : "ok",
      (tot != null ? tot.toFixed(0) + "% · " : "") + ((s.cpu && s.cpu.mhz) || "--") + " MHz");
  }

  function renderStorage(s) {
    var p = state.panels.storage;
    var wrap = el("div", "stack");
    var states = [];
    (s.filesystems || []).forEach(function (f) {
      states.push(f.state);
      wrap.appendChild(Z.MeterBar({
        label: f.mount, mono: true, value: f.pct, state: f.state,
        display: f.used_h + " / " + f.total_h, caption: f.device + " · " + f.fstype
      }));
    });
    var mem = s.memory || {};
    if (mem.swap_total) {
      wrap.appendChild(Z.MeterBar({
        label: "swap", value: mem.swap_pct, series: "series-2",
        display: mem.swap_used_h + " / " + mem.swap_total_h
      }));
    }
    p.fill(wrap);
    p.setState(worst(states), "mounted");
  }

  function renderProcesses(s) {
    var p = state.panels.processes;
    // Hold still while it is being read: a table that re-sorts under the
    // cursor every 2s cannot be scrolled across or read.
    if (state.holdProcesses) {
      p.setState("idle", "held · " + state.heldRows.length + " rows");
      return;
    }
    var rows = s.processes || [];
    state.heldRows = rows;
    var wrap = p.body.querySelector(".z-table-wrap");
    var left = wrap ? wrap.scrollLeft : 0;
    var top = wrap ? wrap.scrollTop : 0;
    p.fill(Z.ProcessTable({ rows: rows, sort: { key: "cpu", dir: "desc" }, hot: 50 }));
    var fresh = p.body.querySelector(".z-table-wrap");
    if (fresh) { fresh.scrollLeft = left; fresh.scrollTop = top; }
    var blocked = rows.filter(function (r) { return r.state === "critical"; }).length;
    p.setState(blocked ? "warn" : "ok", blocked ? blocked + " blocked" : "running");
  }

  function renderLogs() {
    if (document.hidden) return;
    var p = state.panels.logs;
    p.fill(Z.LogStream({ entries: state.logs, rows: 14 }));
    var list = p.body.querySelector(".z-logs-list");
    if (list) list.scrollTop = list.scrollHeight;   // open on the newest line
    updateLogState();
  }

  /* New journal lines are APPENDED. Rebuilding the list threw away the reader's
     scroll position on every incoming line. */
  function appendLogs(rows) {
    if (document.hidden) return;
    var p = state.panels.logs;
    var list = p.body.querySelector(".z-logs-list");
    if (!list) { renderLogs(); return; }
    Z.LogAppend(list, rows, LOG_CAP);
    updateLogState();
  }

  function updateLogState() {
    var p = state.panels.logs;
    var errs = state.logs.filter(function (e) { return e.level === "critical"; }).length;
    p.setState(errs ? "critical" : "ok", errs ? errs + " errors" : "streaming", state.connected);
  }

  function worst(states) {
    var order = ["critical", "serious", "warn", "ok", "idle"];
    for (var i = 0; i < order.length; i++) {
      if (states.indexOf(order[i]) !== -1) return order[i];
    }
    return "idle";
  }

  /* ---------- terminal -------------------------------------------------- */
  function buildTerminal(p) {
    var out = el("div");
    out.id = "term-out";
    p.body.appendChild(out);

    var form = el("form", "term-form");
    var prompt = el("span", "term-form-prompt", "$");
    prompt.id = "term-prompt";
    var input = el("input");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "run a command";
    input.setAttribute("aria-label", "terminal command");
    input.id = "term-input";
    form.appendChild(prompt);
    form.appendChild(input);
    p.body.appendChild(form);

    var hist = [], histIdx = -1;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var cmd = input.value.trim();
      if (!cmd || state.busy) return;
      hist.push(cmd);
      histIdx = hist.length;
      input.value = "";
      exec(cmd);
    });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowUp" && histIdx > 0) {
        histIdx--; input.value = hist[histIdx]; ev.preventDefault();
      } else if (ev.key === "ArrowDown") {
        if (histIdx < hist.length - 1) { histIdx++; input.value = hist[histIdx]; }
        else { histIdx = hist.length; input.value = ""; }
        ev.preventDefault();
      }
    });
    renderTerm();
    p.setState("ok", "ready");
  }

  function renderTerm() {
    var out = document.getElementById("term-out");
    if (!out) return;
    out.innerHTML = "";
    var who = (state.snap && state.snap.user) || "user";
    var where = (state.snap && state.snap.host) || "auto-blox";
    out.appendChild(Z.Terminal({
      lines: state.term, cursor: false, rows: 10,
      user: who, host: where, cwd: state.cwd || "~"
    }));
    var body = out.querySelector(".z-term-body");
    if (body) body.scrollTop = body.scrollHeight;
    var prompt = document.getElementById("term-prompt");
    if (prompt) prompt.textContent = who + "@" + where + ":" + (state.cwd || "~") + "$";
  }

  function addTermLines(lines) {
    state.term = state.term.concat(lines);
    if (state.term.length > TERM_CAP) state.term = state.term.slice(-TERM_CAP);
    renderTerm();
  }

  function exec(cmd) {
    state.busy = true;
    var p = state.panels.terminal;
    p.setState("warn", "running", true);
    addTermLines([{ kind: "in", text: cmd }]);
    fetch("/api/exec", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd: cmd, cwd: state.cwd })
    }).then(function (r) {
      if (r.status === 403) return { lines: [{ kind: "err", text: "shell disabled on the server (--no-exec)" }] };
      if (!r.ok) return { lines: [{ kind: "err", text: "HTTP " + r.status }] };
      return r.json();
    }).then(function (res) {
      if (res.cwd) state.cwd = res.cwd;
      var lines = (res.lines || []).slice();
      if (res.code) lines.push({ kind: "dim", text: "exit " + res.code + " · " + (res.ms || 0) + "ms" });
      addTermLines(lines);
      p.setState("ok", "ready");
    }).catch(function (err) {
      addTermLines([{ kind: "err", text: String(err) }]);
      p.setState("critical", "error");
    }).then(function () {
      state.busy = false;
    });
  }

  /* ---------- stream ---------------------------------------------------- */
  function applySnapshot(s) {
    state.snap = s;
    // A hidden tab still receives the stream; rebuilding into it is pure cost.
    if (document.hidden) return;
    renderHealth(s);
    renderThermal(s);
    renderNetwork(s);
    renderCores(s);
    renderStorage(s);
    renderProcesses(s);
    updateChrome(s);
  }

  function updateChrome(s) {
    var host = document.querySelector(".topbar-host");
    var model = document.querySelector(".topbar-model");
    if (host && s.host) host.textContent = s.host;
    if (model && s.model) model.textContent = s.model;
    // The prompt names who the shell runs as, which only the server knows.
    if (s.user && state.termUser !== s.user) {
      state.termUser = s.user;
      renderTerm();
    }
    if (state.nav) {
      state.nav.setClock(s.at);
      state.nav.setHost(s.host);
      state.nav.setState(state.connected ? "ok" : "critical",
                         state.connected ? "live" : "offline", state.connected);
    }
  }

  function setConnected(on) {
    state.connected = on;
    var b = document.getElementById("banner");
    if (b) b.classList.toggle("is-shown", !on);
    if (state.snap) updateChrome(state.snap);
  }

  function connect() {
    var es = new EventSource("/api/stream");
    es.addEventListener("open", function () { setConnected(true); });
    es.addEventListener("metrics", function (ev) {
      setConnected(true);
      try { applySnapshot(JSON.parse(ev.data)); } catch (e) { /* skip a bad frame */ }
    });
    es.addEventListener("log", function (ev) {
      try {
        var row = JSON.parse(ev.data);
        state.logs.push(row);
        if (state.logs.length > LOG_CAP) state.logs = state.logs.slice(-LOG_CAP);
        appendLogs(row);
      } catch (e) { /* skip */ }
    });
    es.addEventListener("error", function () {
      setConnected(false);
      // EventSource reconnects on its own; nothing to do but show the banner.
    });
  }

  /* ---------- boot ------------------------------------------------------ */
  document.addEventListener("DOMContentLoaded", function () {
    build();
    renderLogs();
    fetch("/api/snapshot").then(function (r) { return r.json(); })
      .then(applySnapshot).catch(function () { /* the stream will fill it in */ });
    // ?snapshot=1 renders one sample and opens no stream, so the page reaches a
    // settled load state. Used for screenshots and for embedding it elsewhere.
    if (!/[?&]snapshot=1\b/.test(location.search)) {
      connect();
    } else {
      fetch("/api/logs").then(function (r) { return r.ok ? r.json() : []; })
        .then(function (rows) {
          if (rows && rows.length) { state.logs = rows; renderLogs(); }
        }).catch(function () { /* logs are optional in snapshot mode */ });
      setConnected(true);
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && state.snap) { applySnapshot(state.snap); renderLogs(); }
    });
    document.addEventListener("keydown", function (ev) {
      // A console should focus its terminal from the keyboard.
      if (ev.key === "`" && (ev.ctrlKey || ev.metaKey)) {
        var i = document.getElementById("term-input");
        if (i) { i.focus(); ev.preventDefault(); }
      }
    });
  });
})();
