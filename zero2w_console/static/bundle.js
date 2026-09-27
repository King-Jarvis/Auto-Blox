/* @ds-bundle: {"format":4,"namespace":"Zero2W","components":[{"name":"Widget"},{"name":"Dock"},{"name":"StatTile"},{"name":"MeterBar"},{"name":"ThermalGauge"},{"name":"Sparkline"},{"name":"LogStream"},{"name":"Terminal"},{"name":"ProcessTable"},{"name":"StatusDot"},{"name":"Badge"},{"name":"Button"}]} */
(function (root) {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var STATES = ["ok", "warn", "serious", "critical", "idle"];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, String(attrs[k]));
    return n;
  }
  function add(parent, child) { if (child) parent.appendChild(child); return parent; }
  /* A glyph is a NODE, not text: el()'s third argument would stringify it. */
  function wrap(cls, node) {
    var n = el("span", cls);
    if (node) n.appendChild(node);
    return n;
  }
  function state(s) { return STATES.indexOf(s) === -1 ? "idle" : s; }
  function num(v, digits) {
    if (v == null || v !== v) return "--";
    return digits == null ? String(v) : Number(v).toFixed(digits);
  }
  /* Children may be a node, a string, or an array of either. */
  function fill(parent, children) {
    if (children == null) return parent;
    var list = Array.isArray(children) ? children : [children];
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (c == null) continue;
      parent.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return parent;
  }

  /* ---- StatusDot ------------------------------------------------------- */
  function StatusDot(o) {
    o = o || {};
    var wrap = el("span", "z-dot-row");
    var dot = el("span", "z-dot z-dot--" + state(o.state) + (o.pulse ? " is-pulsing" : ""));
    dot.setAttribute("aria-hidden", "true");
    wrap.appendChild(dot);
    if (o.label != null) {
      var lb = el("span", "z-dot-label" + (o.mono ? " z-key" : ""), o.label);
      wrap.appendChild(lb);
    }
    wrap.setAttribute("role", "status");
    wrap.setAttribute("aria-label", (o.label != null ? o.label + " " : "") + state(o.state));
    return wrap;
  }

  /* ---- Badge ----------------------------------------------------------- */
  function Badge(o) {
    o = o || {};
    var b = el("span", "z-badge z-badge--" + state(o.tone), o.text || "");
    if (o.glyph) b.insertBefore(wrap("z-badge-glyph", o.glyph), b.firstChild);
    return b;
  }

  /* ---- Button ---------------------------------------------------------- */
  function Button(o) {
    o = o || {};
    var variant = ["primary", "secondary", "ghost", "danger"].indexOf(o.variant) === -1
      ? "secondary" : o.variant;
    var b = el("button", "z-btn z-btn--" + variant + (o.size === "sm" ? " z-btn--sm" : ""));
    b.type = o.type || "button";
    if (o.glyph) b.appendChild(wrap("z-btn-glyph", o.glyph));
    b.appendChild(el("span", null, o.label || ""));
    if (o.disabled) { b.disabled = true; b.setAttribute("aria-disabled", "true"); }
    if (typeof o.onClick === "function") b.addEventListener("click", o.onClick);
    return b;
  }

  /* ---- Widget ---------------------------------------------------------- */
  function Widget(o) {
    o = o || {};
    var w = el("section", "z-widget" + (o.focused ? " is-focused" : ""));
    if (o.span) w.style.gridColumn = "span " + o.span;
    var head = el("header", "z-widget-head");
    var titles = el("div", "z-widget-titles");
    var h = el("h2", "z-widget-title", o.title || "");
    titles.appendChild(h);
    if (o.subtitle) titles.appendChild(el("p", "z-widget-sub z-key", o.subtitle));
    head.appendChild(titles);
    var right = el("div", "z-widget-tools");
    if (o.state) right.appendChild(StatusDot({ state: o.state, label: o.stateLabel, pulse: o.pulse }));
    fill(right, o.actions);
    head.appendChild(right);
    w.appendChild(head);
    var body = el("div", "z-widget-body" + (o.flush ? " is-flush" : ""));
    fill(body, o.children);
    w.appendChild(body);
    if (o.footer) {
      var f = el("footer", "z-widget-foot");
      fill(f, o.footer);
      w.appendChild(f);
    }
    return w;
  }

  /* ---- Sparkline ------------------------------------------------------- */
  function Sparkline(o) {
    o = o || {};
    var data = (o.data || []).map(Number).filter(function (n) { return n === n; });
    var w = o.width || 120, h = o.height || 28, pad = 2;
    var s = svg("svg", { class: "z-spark", viewBox: "0 0 " + w + " " + h, width: w, height: h,
      preserveAspectRatio: "none", role: "img", "aria-label": o.label || "trend" });
    if (data.length < 2) return s;
    var lo = o.min != null ? o.min : Math.min.apply(null, data);
    var hi = o.max != null ? o.max : Math.max.apply(null, data);
    if (hi === lo) hi = lo + 1;
    var stepX = (w - pad * 2) / (data.length - 1);
    var pts = data.map(function (v, i) {
      var x = pad + i * stepX;
      var y = h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);
      return [x, y];
    });
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + num(p[0], 2) + " " + num(p[1], 2); }).join(" ");
    if (o.area) {
      var af = svg("path", { class: "z-spark-area", d: d + " L" + num(pts[pts.length - 1][0], 2) + " " + (h - pad) + " L" + pad + " " + (h - pad) + " Z" });
      af.style.fill = "var(--" + (o.series || "series-1") + ")";
      s.appendChild(af);
    }
    var line = svg("path", { class: "z-spark-line", d: d });
    line.style.stroke = "var(--" + (o.series || "series-1") + ")";
    s.appendChild(line);
    if (o.marker !== false) {
      var last = pts[pts.length - 1];
      var c = svg("circle", { class: "z-spark-dot", cx: num(last[0], 2), cy: num(last[1], 2), r: 2.5 });
      c.style.fill = "var(--" + (o.series || "series-1") + ")";
      s.appendChild(c);
    }
    return s;
  }

  /* ---- StatTile -------------------------------------------------------- */
  function StatTile(o) {
    o = o || {};
    var t = el("div", "z-stat" + (o.hero ? " is-hero" : ""));
    t.appendChild(el("p", "z-stat-label", o.label || ""));
    var row = el("div", "z-stat-row");
    var v = el("span", o.hero ? "z-readout-xl" : "z-readout", o.value == null ? "--" : o.value);
    if (o.state) v.classList.add("z-ink--" + state(o.state));
    row.appendChild(v);
    if (o.unit) row.appendChild(el("span", "z-stat-unit", o.unit));
    t.appendChild(row);
    var meta = el("div", "z-stat-meta");
    if (o.caption) meta.appendChild(el("span", "z-stat-caption", o.caption));
    if (o.trend && o.trend.length) {
      meta.appendChild(Sparkline({ data: o.trend, series: o.series, width: o.sparkWidth || 96,
        height: 24, label: (o.label || "value") + " trend", area: o.area !== false }));
    }
    if (meta.childNodes.length) t.appendChild(meta);
    if (o.stale) {
      t.appendChild(el("p", "z-stat-stale", "last seen " + o.stale));
    }
    return t;
  }

  /* ---- MeterBar -------------------------------------------------------- */
  function MeterBar(o) {
    o = o || {};
    var max = o.max == null ? 100 : Number(o.max);
    var val = Math.max(0, Math.min(max, Number(o.value) || 0));
    var pct = max === 0 ? 0 : (val / max) * 100;
    var m = el("div", "z-meter");
    var head = el("div", "z-meter-head");
    head.appendChild(el("span", "z-meter-label" + (o.mono ? " z-key" : ""), o.label || ""));
    var readout = el("span", "z-meter-value", o.display != null ? o.display : num(pct, 0) + "%");
    if (o.state) readout.classList.add("z-ink--" + state(o.state));
    head.appendChild(readout);
    m.appendChild(head);
    var track = el("div", "z-meter-track");
    track.setAttribute("role", "meter");
    track.setAttribute("aria-valuenow", String(val));
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(max));
    track.setAttribute("aria-label", o.label || "utilisation");
    var f = el("div", "z-meter-fill");
    f.style.width = num(pct, 2) + "%";
    f.style.background = "var(--" + (o.state ? state(o.state) : (o.series || "series-1")) + ")";
    track.appendChild(f);
    m.appendChild(track);
    if (o.caption) m.appendChild(el("p", "z-meter-caption", o.caption));
    return m;
  }

  /* ---- ThermalGauge ---------------------------------------------------- */
  /* A 240-degree arc. Warn and trip bands sit IN the track so a reading is
     judged against its thresholds without arithmetic. */
  function ThermalGauge(o) {
    o = o || {};
    var lo = o.min == null ? 20 : Number(o.min);
    var hi = o.max == null ? 100 : Number(o.max);
    var warn = o.warn == null ? null : Number(o.warn);
    var trip = o.trip == null ? null : Number(o.trip);
    var temp = o.temp == null ? null : Number(o.temp);
    var R = 46, CX = 56, CY = 52, START = 150, SWEEP = 240;

    function clamp(v) { return Math.max(lo, Math.min(hi, v)); }
    function ang(v) { return START + ((clamp(v) - lo) / (hi - lo)) * SWEEP; }
    function pt(a, r) {
      var rad = (a * Math.PI) / 180;
      return [CX + r * Math.cos(rad), CY + r * Math.sin(rad)];
    }
    function arc(a0, a1, r) {
      var p0 = pt(a0, r), p1 = pt(a1, r);
      var large = Math.abs(a1 - a0) > 180 ? 1 : 0;
      return "M" + num(p0[0], 2) + " " + num(p0[1], 2) + " A" + r + " " + r + " 0 " + large + " 1 " + num(p1[0], 2) + " " + num(p1[1], 2);
    }

    var g = el("figure", "z-gauge");
    var s = svg("svg", { viewBox: "0 0 112 92", class: "z-gauge-svg", role: "img",
      "aria-label": (o.zone || "zone") + " " + (temp == null ? "no reading" : num(temp, 1) + " degrees celsius") });

    s.appendChild(svg("path", { class: "z-gauge-track", d: arc(START, START + SWEEP, R) }));
    if (warn != null) {
      var wb = svg("path", { class: "z-gauge-band", d: arc(ang(warn), ang(trip != null ? trip : hi), R) });
      wb.style.stroke = "var(--warn-wash)";
      s.appendChild(wb);
    }
    if (trip != null) {
      var tb = svg("path", { class: "z-gauge-band", d: arc(ang(trip), START + SWEEP, R) });
      tb.style.stroke = "var(--critical-wash)";
      s.appendChild(tb);
    }
    if (temp != null) {
      var vs = svg("path", { class: "z-gauge-value", d: arc(START, ang(temp), R) });
      var tone = o.series || "series-1";
      if (trip != null && temp >= trip) tone = "critical";
      else if (warn != null && temp >= warn) tone = "warn";
      vs.style.stroke = "var(--" + tone + ")";
      s.appendChild(vs);
      var cap = pt(ang(temp), R);
      var dot = svg("circle", { class: "z-gauge-cap", cx: num(cap[0], 2), cy: num(cap[1], 2), r: 3.5 });
      dot.style.fill = "var(--" + tone + ")";
      s.appendChild(dot);
    }
    g.appendChild(s);
    var read = el("div", "z-gauge-read");
    var big = el("span", "z-gauge-temp", temp == null ? "--" : num(temp, 1));
    read.appendChild(big);
    read.appendChild(el("span", "z-gauge-unit", " °C"));
    g.appendChild(read);
    var cap2 = el("figcaption", "z-gauge-zone z-key", o.zone || "");
    g.appendChild(cap2);
    if (o.note) g.appendChild(el("p", "z-gauge-note", o.note));
    return g;
  }

  /* ---- LogStream ------------------------------------------------------- */
  /* One row. Exported so a live log can APPEND rather than rebuild: rebuilding
     the list throws away the reader's scroll position on every line. */
  function LogRow(r) {
    r = r || {};
    var li = el("li", "z-log z-log--" + state(r.level));
    li.appendChild(el("time", "z-log-time", r.time || ""));
    li.appendChild(el("span", "z-log-level", (r.level || "idle").slice(0, 4)));
    if (r.unit) li.appendChild(el("span", "z-log-unit z-key", r.unit));
    li.appendChild(el("span", "z-log-msg", r.message || ""));
    return li;
  }

  /* Appends entries to a list a LogStream already built, trims to `cap`, and
     keeps the newest row in view unless the reader has scrolled up. */
  function LogAppend(listEl, entries, cap) {
    if (!listEl) return;
    var stick = listEl.scrollTop + listEl.clientHeight >= listEl.scrollHeight - 24;
    var list = Array.isArray(entries) ? entries : [entries];
    for (var i = 0; i < list.length; i++) listEl.appendChild(LogRow(list[i]));
    if (cap) {
      while (listEl.childNodes.length > cap) listEl.removeChild(listEl.firstChild);
    }
    if (stick) listEl.scrollTop = listEl.scrollHeight;
  }

  function LogStream(o) {
    o = o || {};
    var rows = o.entries || [];
    var wrap = el("div", "z-logs");
    var list = el("ol", "z-logs-list");
    list.setAttribute("role", "log");
    list.setAttribute("aria-live", o.live === false ? "off" : "polite");
    if (o.rows) list.style.maxHeight = (o.rows * 17 + 16) + "px";
    for (var i = 0; i < rows.length; i++) list.appendChild(LogRow(rows[i]));
    wrap.appendChild(list);
    if (rows.length === 0) wrap.appendChild(el("p", "z-empty", o.emptyText || "no entries"));
    return wrap;
  }

  /* ---- Terminal -------------------------------------------------------- */
  function Terminal(o) {
    o = o || {};
    var t = el("div", "z-term");
    var lines = o.lines || [];
    var pre = el("pre", "z-term-body");
    pre.setAttribute("tabindex", "0");
    pre.setAttribute("role", "log");
    pre.setAttribute("aria-label", "terminal output");
    if (o.rows) pre.style.maxHeight = (o.rows * 18 + 24) + "px";
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i] || {};
      var row = el("div", "z-term-line z-term-line--" + (l.kind || "out"));
      if (l.kind === "in") {
        row.appendChild(el("span", "z-term-prompt", (o.prompt || (o.user || "user") + "@" + (o.host || "auto-blox") + ":" + (o.cwd || "~") + "$")));
        row.appendChild(document.createTextNode(" "));
      }
      row.appendChild(el("span", "z-term-text", l.text || ""));
      pre.appendChild(row);
    }
    if (o.cursor !== false) {
      var live = el("div", "z-term-line z-term-line--in");
      live.appendChild(el("span", "z-term-prompt", (o.prompt || (o.user || "user") + "@" + (o.host || "auto-blox") + ":" + (o.cwd || "~") + "$")));
      live.appendChild(document.createTextNode(" "));
      live.appendChild(el("span", "z-term-cursor", "█"));
      pre.appendChild(live);
    }
    t.appendChild(pre);
    return t;
  }

  /* ---- ProcessTable ---------------------------------------------------- */
  function ProcessTable(o) {
    o = o || {};
    var rows = o.rows || [];
    var cols = o.columns || [
      { key: "pid", label: "PID", align: "right", mono: true },
      { key: "user", label: "USER" },
      { key: "cpu", label: "CPU%", align: "right", mono: true },
      { key: "mem", label: "MEM%", align: "right", mono: true },
      { key: "cmd", label: "COMMAND", mono: true, grow: true }
    ];
    var table = el("table", "z-table");
    var thead = el("thead");
    var tr = el("tr");
    for (var c = 0; c < cols.length; c++) {
      var col = cols[c];
      var th = el("th", "z-th" + (col.align === "right" ? " is-right" : "") + (col.grow ? " is-grow" : ""));
      var sortable = o.sort && o.sort.key === col.key;
      if (sortable) {
        th.classList.add("is-sorted");
        th.setAttribute("aria-sort", o.sort.dir === "asc" ? "ascending" : "descending");
        th.appendChild(el("span", null, col.label));
        th.appendChild(el("span", "z-th-caret", o.sort.dir === "asc" ? "▴" : "▾"));
      } else {
        th.textContent = col.label;
      }
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    table.appendChild(thead);
    var tb = el("tbody");
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i] || {};
      var row = el("tr", "z-tr" + (r.state ? " z-tr--" + state(r.state) : ""));
      for (var j = 0; j < cols.length; j++) {
        var cc = cols[j];
        var td = el("td", "z-td" + (cc.align === "right" ? " is-right" : "") + (cc.mono ? " z-key" : ""));
        var raw = r[cc.key];
        td.textContent = raw == null ? "--" : String(raw);
        if (cc.key === "cpu" && Number(raw) >= (o.hot == null ? 50 : o.hot)) td.classList.add("z-ink--warn");
        row.appendChild(td);
      }
      tb.appendChild(row);
    }
    table.appendChild(tb);
    if (rows.length === 0) {
      var w = el("div", "z-table-wrap");
      w.appendChild(table);
      w.appendChild(el("p", "z-empty", "no processes"));
      return w;
    }
    var wrap2 = el("div", "z-table-wrap");
    wrap2.appendChild(table);
    return wrap2;
  }

  /* ---- Dock ------------------------------------------------------------ */
  function Dock(o) {
    o = o || {};
    var items = o.items || [];
    var d = el("nav", "z-dock");
    d.setAttribute("aria-label", "console sections");
    var brand = el("div", "z-dock-brand");
    brand.appendChild(el("span", "z-dock-host z-key", o.host || "auto-blox"));
    if (o.address) brand.appendChild(el("span", "z-dock-addr z-key", o.address));
    d.appendChild(brand);
    var list = el("ul", "z-dock-list");
    for (var i = 0; i < items.length; i++) {
      var it = items[i] || {};
      var li = el("li");
      var b = el("button", "z-dock-item" + (it.active ? " is-active" : ""));
      b.type = "button";
      if (it.active) b.setAttribute("aria-current", "page");
      if (it.glyph) b.appendChild(wrap("z-dock-glyph", it.glyph));
      b.appendChild(el("span", "z-dock-label", it.label || ""));
      if (it.badge) b.appendChild(Badge({ text: it.badge, tone: it.badgeTone || "critical" }));
      if (it.disabled) { b.disabled = true; b.setAttribute("aria-disabled", "true"); }
      li.appendChild(b);
      list.appendChild(li);
    }
    d.appendChild(list);
    var tail = el("div", "z-dock-tail");
    if (o.state) tail.appendChild(StatusDot({ state: o.state, label: o.stateLabel, pulse: o.pulse }));
    if (o.clock) tail.appendChild(el("span", "z-dock-clock z-key", o.clock));
    d.appendChild(tail);
    return d;
  }


  /* ---- Facts, written one way ------------------------------------------
     A number means the same thing on every screen, so it has to read the same way
     on every screen. These lived twice and disagreed. Not here: how long a device
     may be silent before it counts as offline — the server owns that
     (`flows.DEVICE_FRESH`) and sends the answer as `device.online`, so the
     browser has no opinion about it at all. */

  function bytes(n) {
    if (n == null) return null;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  /* An absolute unix time, as "how long ago". Absolute because most callers have
     a timestamp rather than an age, and a function that accepted either would be
     handed the wrong one. */
  function ago(epochSeconds) {
    if (epochSeconds == null) return null;
    var d = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
    if (d < 2) return "just now";
    if (d < 90) return d + "s ago";
    if (d < 5400) return Math.round(d / 60) + "m ago";
    if (d < 172800) return Math.round(d / 3600) + "h ago";
    return Math.round(d / 86400) + "d ago";
  }

  /* A span of seconds, as a length of time. Not `ago()` with the "ago" cut off:
     an uptime is not a moment in the past. */
  function duration(sec) {
    if (sec == null) return null;
    sec = Math.round(sec);
    if (sec < 90) return sec + "s";
    if (sec < 5400) return Math.round(sec / 60) + "m";
    if (sec < 172800) return Math.round(sec / 3600) + "h";
    return Math.round(sec / 86400) + "d";
  }

  root.Zero2W = {
    Widget: Widget, Dock: Dock, StatTile: StatTile, MeterBar: MeterBar,
    ThermalGauge: ThermalGauge, Sparkline: Sparkline, LogStream: LogStream,
    LogRow: LogRow, LogAppend: LogAppend,
    Terminal: Terminal, ProcessTable: ProcessTable, StatusDot: StatusDot,
    Badge: Badge, Button: Button,
    bytes: bytes, ago: ago, duration: duration
  };
})(typeof window !== "undefined" ? window : this);
