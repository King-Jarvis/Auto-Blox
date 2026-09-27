/* IOT — the wireless side of this board, and the devices on it.
 *
 * It reports what the radio can do, whether an access point could come up, and
 * which devices have enrolled. Anything privileged is shown as the command to
 * run, never attempted. Same idiom as app.js: plain DOM factories, no framework.
 */
(function () {
  "use strict";
  var Z = window.Zero2W;
  var POLL_MS = 5000;

  var S = {
    status: null, devices: [], error: null, timer: null,
    config: { iot_net: true },
    boards: [],        // generated profiles, for the board picker
    scan: null,        // last USB scan, only ever from an explicit click
    scanning: false,
    draft: null,       // the config being created, keyed to a scan row
    flows: [],         // flows, for deploying one to a device
    flash: { running: false, log: [] },
    flashFor: null,    // the device whose flash form is open
    picked: {},        // device id -> the flow chosen in its dropdown, not yet used
    costs: {},         // "deviceid|flowid" -> what deploying it would fetch
    form: null,        // the network being edited, null until the form opens
    busy: false, notice: null,   // {scope, tone, text}

    tab: "setup",      // which section is on screen
    mounted: null,     // which section's DOM is actually in the grid
    selected: null,    // the device whose detail view is open
    pins: null,        // /api/iot/devices/<id>/pins for that device
    said: null,        // what that device said, as this host kept it; [] = none
    steps: {},         // setup steps the reader has opened by hand
    bt: null,          // /api/bt: the adapter, and what is paired to it
    btGatt: null,      // one device's GATT tree, when somebody is looking at it
    nav: null,
    // The log is built once and appended to. Rebuilding it throws away the
    // reader's scroll position, which is the whole point of a log.
    log: { list: null, count: 0, synced: false },
    stream: null
  };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function rows(pairs) {
    var dl = el("dl", "iot-row");
    pairs.forEach(function (p) {
      if (p[1] == null || p[1] === "") return;
      dl.appendChild(el("dt", null, p[0]));
      dl.appendChild(el("dd", p[2] === "prose" ? "is-prose" : null, String(p[1])));
    });
    return dl;
  }
  /* A hint is an instruction. Anything after "run:" is a command, so it is
     rendered as one rather than buried in a sentence. */
  function hintNode(text) {
    var box = el("div", "iot-hint");
    var m = /^run:\s*(.+)$/.exec(text);
    if (m) {
      box.appendChild(el("span", null, "Run"));
      box.appendChild(el("code", null, m[1]));
    } else {
      box.appendChild(el("span", null, text));
    }
    return box;
  }
  function notice(scope, tone, text) { return { scope: scope, tone: tone, text: text }; }
  function noticeFor(scope) {
    if (!S.notice || S.notice.scope !== scope) return null;
    var box = el("div", "iot-hint" + (S.notice.tone === "critical" ? " is-critical" : " is-ok"));
    box.appendChild(el("span", null, S.notice.text));
    return box;
  }
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var err = new Error(b.error || ("HTTP " + r.status));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    });
  }
  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }
  function field(label, key, opts) {
    opts = opts || {};
    var wrap = el("label", "iot-field");
    wrap.appendChild(el("span", "iot-field-label", label));
    var input;
    if (opts.options) {
      input = el("select");
      opts.options.forEach(function (o) {
        var opt = el("option", null, String(o));
        opt.value = String(o);
        if (String(S.form[key]) === String(o)) opt.selected = true;
        input.appendChild(opt);
      });
    } else {
      input = el("input");
      input.type = opts.type || "text";
      input.value = S.form[key] == null ? "" : S.form[key];
      if (opts.placeholder) input.placeholder = opts.placeholder;
      if (opts.maxLength) input.maxLength = opts.maxLength;
    }
    input.addEventListener("change", function () {
      S.form[key] = opts.number ? Number(input.value) : input.value;
      // Choosing a wire takes the SSID and channel away, and choosing a radio
      // brings them back — the form has to redraw for that to show.
      if (key === "interface") render();
    });
    input.addEventListener("input", function () {
      S.form[key] = opts.number ? Number(input.value) : input.value;
    });
    wrap.appendChild(input);
    if (opts.help) wrap.appendChild(el("span", "iot-field-help", opts.help));
    return wrap;
  }

  /* ---------- small facts ------------------------------------------------ */
  function clock(sec) {
    if (!sec) return null;
    return new Date(sec * 1000).toTimeString().slice(0, 8);
  }
  function countdown(sec) {
    if (!sec) return null;
    var d = Math.max(0, Math.round(sec - Date.now() / 1000));
    return Math.floor(d / 60) + ":" + ("0" + (d % 60)).slice(-2);
  }
  function mac(v) { return String(v || "").toLowerCase(); }
  function cameraLine(st, d) {
    var c = st.camera;
    if (!c || typeof c !== "object") return d.camera ? "configured" : null;
    var bits = [];
    bits.push(c.running ? "running" : "stopped");
    if (c.sensor) bits.push(c.sensor);
    if (c.width && c.height) bits.push(c.width + "×" + c.height);
    if (c.format) bits.push(c.format);
    if (c.frames != null) bits.push(c.frames + " frames");
    if (c.port) bits.push("port " + c.port);
    if (c.error) bits.push("error: " + c.error);
    return bits.join(" · ");
  }

  /* The held-open socket, from the board's own account of it. Reported over the
     polled route the link is meant to replace, which is the only end that can say
     the new one did not come up. */
  function linkLine(st, d) {
    if (!d.link) return null;
    var l = st.link;
    if (!l || typeof l !== "object") return "switched on, nothing reported yet";
    var bits = [l.state];
    if (l.in != null) bits.push(l.in + " in");
    if (l.out != null) bits.push(l.out + " out");
    if (l.failures) bits.push(l.failures + " failed");
    if (l.dropped) bits.push("last drop: " + l.dropped);
    return bits.join(" · ");
  }

  /* Where a device is in its life, in one place. Provisioning clears
     `enrolled` on purpose: what follows is a wait, not a failure. */
  function life(d, fresh) {
    if (d.enrolled) {
      return fresh ? { tone: "ok", short: "online", long: "online" }
                   : { tone: "warn", short: "offline",
                       long: "enrolled, not heard from" };
    }
    if (d.flashed) {
      return { tone: "warn", short: "waiting to enrol",
               long: "flashed " + Z.ago(d.flashed) + " — waiting for it to join and "
                     + "enrol, which takes a few seconds once it boots" };
    }
    return { tone: "idle", short: "never flashed", long: "never flashed" };
  }

  /* Which agent it is running. `state` only exists once it has reported, and a
     board that enrols and goes quiet leaves a stale version looking current. The
     probe is recorded at enrolment, so fall back to it and say so. */
  function agentLine(st, d) {
    if (st.agent) return st.agent;
    var probe = d.probe || {};
    if (!probe.agent) return null;
    return probe.agent + " — at enrolment; it has not reported since";
  }

  /* `nodes` is however the agent chose to describe its flow: a count, a list,
     or a map of node id to something. Say what it is rather than [object]. */
  function nodeLine(st) {
    var n = st.nodes;
    if (n == null) return null;
    if (typeof n === "number") return n + " nodes";
    if (Array.isArray(n)) return n.length + " nodes · " + n.join(", ");
    if (typeof n === "object") {
      var keys = Object.keys(n);
      return keys.length + " nodes · " + keys.join(", ");
    }
    return String(n);
  }

  function apState() { return (((S.status || {}).ap) || {}).state || {}; }

  /* Who is actually on the network. dnsmasq knows the addresses and the radio
     knows the signal; neither knows which config a board belongs to, so the join
     is here, on the MAC. */
  function clientFor(device) {
    var st = apState();
    var key = mac(device.mac);
    var out = { lease: null, station: null };
    if (!key) return out;
    (st.leases || []).forEach(function (l) {
      if (mac(l.mac) === key) out.lease = l;
    });
    (st.clients || []).forEach(function (c) {
      if (mac(c.mac) === key) out.station = c;
    });
    return out;
  }

  /* Anything on the network that is not one of ours. Worth seeing: it means
     something joined that you did not configure. */
  function strangers() {
    var st = apState();
    var known = {};
    S.devices.forEach(function (d) { if (d.mac) known[mac(d.mac)] = true; });
    var seen = {}, out = [];
    (st.leases || []).forEach(function (l) {
      if (known[mac(l.mac)]) return;
      seen[mac(l.mac)] = { mac: l.mac, ip: l.ip, name: l.name, expires: l.expires };
    });
    (st.clients || []).forEach(function (c) {
      if (known[mac(c.mac)]) return;
      var row = seen[mac(c.mac)] || { mac: c.mac };
      row.signal = c.signal;
      row.inactive_ms = c.inactive_ms;
      seen[mac(c.mac)] = row;
    });
    Object.keys(seen).forEach(function (k) { out.push(seen[k]); });
    return out;
  }

  /* `iw station dump` needs privilege on most drivers and comes back empty
     without it, and an empty dump is not evidence that nothing is associated. */
  function associated(link) {
    var st = apState();
    if (!st.up) return null;
    if (link.station) {
      return "yes" + (link.station.inactive_ms != null
        ? " · idle " + Math.round(link.station.inactive_ms / 1000) + "s" : "");
    }
    if (!(st.clients || []).length) {
      return link.lease ? "holds a lease; the station dump is empty"
                        : "cannot tell — the station dump is empty";
    }
    return "no";
  }

  function deviceById(id) {
    for (var i = 0; i < S.devices.length; i++) {
      if (S.devices[i].id === id) return S.devices[i];
    }
    return null;
  }

  function flowName(id) {
    for (var i = 0; i < S.flows.length; i++) {
      if (S.flows[i].id === id) return S.flows[i].name || id;
    }
    return id;
  }

  /* ---------- widgets ---------------------------------------------------- */
  function radioWidget() {
    var r = (S.status || {}).radio || {};
    var phy = r.phy;
    var iface = (r.interfaces || [])[0];
    var blocked = (r.rfkill || []).some(function (x) { return x.soft_blocked || x.hard_blocked; });
    var body = [];

    if (!phy) {
      body.push(el("p", "iot-empty", "No wireless phy found on this board."));
    } else {
      var modes = el("div", "iot-modes");
      phy.modes.forEach(function (m) {
        modes.appendChild(Z.Badge({ text: m, tone: m === "AP" ? "ok" : "idle" }));
      });
      body.push(rows([
        ["interface", iface ? iface.name : "—"],
        ["mode now", iface ? iface.type : "—"],
        ["link", iface ? iface.operstate : "—"],
        ["mac", iface ? iface.addr : null],
        ["2.4 GHz", phy.bands["2.4"] + " channels"],
        ["5 GHz", phy.bands["5"] ? phy.bands["5"] + " channels (no ESP32 can see these)" : "none"],
        ["rfkill", blocked ? "blocked" : "clear"]
      ]));
      body.push(el("p", "dev-card-id", "supported modes"));
      body.push(modes);
      if (r.note) {
        body.push(rows([["constraint", r.note, "prose"]]));
      }
      if (phy.combinations) {
        body.push(rows([["driver says", phy.combinations]]));
      }
    }
    var note = noticeFor("radio");
    if (note) body.push(note);
    if (r.hint && !note) body.push(hintNode(r.hint));

    // Turning the radio on needs privilege the console does not have — except
    // that logind usually grants this user an ACL on /dev/rfkill, which is why the
    // button is offered before the helper is installed.
    var toggle = Z.Button({
      label: S.busy ? "Working\u2026" : (blocked ? "Turn on" : "Turn off"),
      variant: blocked ? "primary" : "danger",
      size: "sm",
      disabled: S.busy || !S.config.iot_net || !phy,
      onClick: function () {
        var turningOn = blocked;
        S.busy = true;
        S.notice = notice("radio", "ok", turningOn ? "unblocking\u2026" : "blocking\u2026");
        render();
        post("/api/iot/radio/" + (turningOn ? "on" : "off"))
          .then(function (res) {
            S.notice = notice("radio", "ok",
              (turningOn ? "radio on" : "radio off") + " \u00b7 via " + (res.via || "helper"));
          })
          .catch(function (e) { S.notice = notice("radio", "critical", e.message); })
          .then(function () { S.busy = false; poll(); });
      }
    });

    return Z.Widget({
      title: "Radio",
      actions: [toggle],
      subtitle: phy ? "iw phy " + phy.phy + " · " + (iface ? iface.name : "no interface") : "iw dev",
      state: r.available ? "ok" : (blocked ? "warn" : "idle"),
      stateLabel: r.available ? "ready" : (blocked ? "blocked" : "not ready"),
      children: body
    });
  }

  function networkWidget() {
    var ap = ((S.status || {}).ap) || {};
    var st = ap.state || {};
    var canControl = ap.helper && S.config.iot_net;
    var body = [];
    var note = noticeFor("network");

    // Three states, and only one of them is a form. Everything else is a
    // sentence and a button.
    if (S.form) {
      body.push(networkForm(st));
      if (note) body.push(note);
      return Z.Widget({
        title: "Network", subtitle: "what the devices join",
        state: "idle", stateLabel: "setting up", children: body
      });
    }

    if (!st.configured) {
      var blank = el("div", "iot-empty");
      blank.appendChild(el("strong", null, "No network yet."));
      blank.appendChild(el("span", null,
        "Give it a name and a passphrase, and devices can join it."));
      body.push(blank);
      if (note) body.push(note);
      return Z.Widget({
        title: "Network", subtitle: "what the devices join",
        state: "idle", stateLabel: "not set up",
        actions: [Z.Button({
          label: "Set up", variant: "primary", size: "sm", disabled: !canControl,
          onClick: function () { S.form = draftNetwork(st); S.notice = null; render(); }
        })],
        children: body
      });
    }

    body.push(rows([
      ["name", st.ssid || "\u2014"],
      ["channel", st.channel ? String(st.channel) : "\u2014"],
      ["addresses", st.subnet + " \u00b7 this board is " + (st.gateway || "?")],
      ["devices", st.up ? String((st.clients || []).length) : "\u2014"]
    ]));
    if (note) body.push(note);
    if (!st.up && ap.hint) body.push(hintNode(ap.hint));
    if (!st.up && (st.missing || []).length) {
      var ul = el("ul", "iot-missing");
      st.missing.forEach(function (m) { ul.appendChild(el("li", null, m)); });
      body.push(ul);
    }

    return Z.Widget({
      title: "Network",
      subtitle: st.up ? "on the air" : "what the devices join",
      state: st.up ? "ok" : "idle",
      stateLabel: st.up ? "running" : "stopped",
      actions: [
        Z.Button({
          label: st.up ? "Stop" : "Start",
          variant: st.up ? "danger" : "primary", size: "sm",
          disabled: !canControl || S.busy,
          onClick: function () { startStop(st.up); }
        }),
        Z.Button({
          label: "Edit", size: "sm", disabled: !canControl || S.busy,
          onClick: function () { S.form = draftNetwork(st); S.notice = null; render(); }
        })
      ],
      children: body
    });
  }

  function draftNetwork(st) {
    return {
      interface: st.serving || (st.links && st.links.length
                                ? st.links[0].interface : "wlan0"),
      ssid: st.ssid || "auto-blox",
      psk: "",
      channel: st.channel || 6,
      subnet: st.subnet || "10.42.0.0/24"
    };
  }

  function startStop(running) {
    S.busy = true; render();
    post("/api/iot/network/" + (running ? "down" : "up"))
      .then(function () {
        S.notice = notice("network", "ok", running ? "stopped" : "on the air");
      })
      .catch(function (e) { S.notice = notice("network", "critical", e.message); })
      .then(function () { S.busy = false; poll(); });
  }

  /* The form. A wire needs nothing announced, so it only asks a radio for a
     name, a passphrase and a channel. */
  function networkForm(st) {
    var form = el("div", "iot-form");
    var links = st.links || [];
    if (links.length > 1) {
      form.appendChild(field("Carry it on", "interface",
        { options: links.map(function (l) { return l.interface; }),
          help: "a wire needs nothing but an address; a radio needs hostapd" }));
    }
    var chosen = links.filter(function (l) {
      return l.interface === S.form.interface;
    })[0];
    if (!chosen || chosen.kind === "wireless") {
      form.appendChild(field("Name", "ssid",
        { maxLength: 32, placeholder: "auto-blox",
          help: "what devices will look for" }));
      form.appendChild(field("Passphrase", "psk",
        { type: "password", maxLength: 63, help: "8 characters or more" }));
      form.appendChild(field("Channel", "channel",
        { options: st.channels || [1, 6, 11], number: true,
          help: "2.4 GHz only \u2014 no ESP32 can see a 5 GHz channel" }));
    } else {
      form.appendChild(el("div", "field-help",
        "On " + chosen.interface + " this behaves like any router: an address, "
        + "DHCP and a route. Plug a switch in and devices appear."
        + (chosen.carrier ? "" : " There is no cable in it yet.")));
    }
    form.appendChild(field("Addresses", "subnet",
      { placeholder: "10.42.0.0/24", help: "the range devices get" }));

    var acts = el("div", "iot-form-acts");
    acts.appendChild(Z.Button({
      label: S.busy ? "Saving\u2026" : "Save", variant: "primary", size: "sm",
      disabled: S.busy,
      onClick: function () {
        S.busy = true; render();
        post("/api/iot/network", S.form)
          .then(function () {
            S.form = null;
            S.notice = notice("network", "ok", "saved \u2014 press Start");
          })
          .catch(function (e) { S.notice = notice("network", "critical", e.message); })
          .then(function () { S.busy = false; poll(); });
      }
    }));
    acts.appendChild(Z.Button({
      label: "Cancel", size: "sm",
      onClick: function () { S.form = null; S.notice = null; render(); }
    }));
    form.appendChild(acts);
    return form;
  }

  /* Everything on the network that is not one of ours. A lease or a station
     with no matching config means something joined that you did not set up. */
  function strangersSection() {
    var st = apState();
    var list = strangers();
    if (!st.up || !list.length) return null;
    var box = el("div", "iot-section");
    box.appendChild(el("p", "dev-card-id", "not one of ours"));
    var wrap = el("div", "dev-list");
    list.forEach(function (c) {
      var card = el("div", "dev-card is-stale");
      var top = el("div", "dev-card-top");
      top.appendChild(el("span", "dev-card-name", c.name || c.ip || c.mac));
      top.appendChild(Z.Badge({ text: "unknown", tone: "warn" }));
      if (c.signal != null) top.appendChild(Z.Badge({ text: c.signal + " dBm", tone: "idle" }));
      card.appendChild(top);
      card.appendChild(el("div", "dev-card-id", c.mac));
      var meta = el("div", "dev-card-meta");
      [["ip", c.ip],
       ["lease until", clock(c.expires)],
       ["idle", c.inactive_ms != null ? Math.round(c.inactive_ms / 1000) + "s" : null]
      ].forEach(function (pair) {
        if (pair[1]) meta.appendChild(el("span", null, pair[0] + " " + pair[1]));
      });
      if (meta.childNodes.length) card.appendChild(meta);
      wrap.appendChild(card);
    });
    box.appendChild(wrap);
    return box;
  }

  function boardLabel(id) {
    for (var i = 0; i < S.boards.length; i++) {
      if (S.boards[i].board === id) return S.boards[i].label;
    }
    return id || "no profile";
  }

  /* One row per serial port the kernel is showing us. */
  function scanRow(port) {
    var card = el("div", "dev-card " + (port.ok ? "is-online" : "is-stale"));
    var top = el("div", "dev-card-top");
    top.appendChild(el("span", "dev-card-name", port.chip || port.product || "unknown board"));
    if (port.board) top.appendChild(Z.Badge({ text: boardLabel(port.board), tone: "ok" }));
    else if (port.ok) top.appendChild(Z.Badge({ text: "no profile", tone: "warn" }));
    card.appendChild(top);

    // The port is a kernel identifier: monospaced and verbatim, never a badge
    // (badges are uppercased, and /dev/ttyUSB0 is not).
    card.appendChild(el("div", "dev-card-id", port.port));

    var meta = el("div", "dev-card-meta");
    [["driver", port.driver], ["mac", port.mac], ["flash", port.flash],
     ["crystal", port.crystal], ["psram", port.psram === true ? "yes" : null]
    ].forEach(function (p) {
      if (p[1]) meta.appendChild(el("span", null, p[0] + " " + p[1]));
    });
    if (meta.childNodes.length) card.appendChild(meta);

    if (!port.ok) {
      var err = el("div", "iot-hint is-critical");
      err.appendChild(el("span", null, port.error || "could not identify this board"));
      card.appendChild(err);
      if (port.hint) card.appendChild(hintNode(port.hint));
      return card;
    }

    if (port.warning) {
      var warn = el("div", "iot-hint");
      warn.appendChild(el("span", null, port.warning));
      card.appendChild(warn);
    }

    if (S.draft && S.draft.port === port.port) {
      card.appendChild(draftForm());
    } else if (S.rehome && S.rehome.port === port.port) {
      card.appendChild(rehomeForm(port));
    } else {
      var acts = el("div", "iot-form-acts");
      acts.appendChild(Z.Button({
        label: "Make a config", variant: "primary", size: "sm",
        onClick: function () {
          S.draft = {
            port: port.port,
            name: suggestName(port),
            board: port.board || "",
            chip: port.chip || null, mac: port.mac || null,
            flash: port.flash || null, psram: port.psram === true,
            note: ""
          };
          S.notice = null;
          render();
        }
      }));
      var movable = S.devices.filter(function (d) { return d.mac !== port.mac; });
      if (movable.length) {
        acts.appendChild(Z.Button({
          label: "Move a config here", size: "sm",
          onClick: function () {
            S.rehome = { port: port.port, id: movable[0].id };
            S.draft = null;
            S.notice = null;
            render();
          }
        }));
      }
      card.appendChild(acts);
    }
    return card;
  }

  /* A config onto replacement hardware: the identity comes from this scan,
     and the name, flow and settings stay. */
  function rehomeForm(port) {
    var box = el("div", "iot-form");
    var f = el("label", "iot-field");
    f.appendChild(el("span", "iot-field-label", "Config to move onto this board"));
    var pick = el("select");
    pick.id = "rehome-" + port.port.replace(/[^A-Za-z0-9]/g, "");
    S.devices.filter(function (d) { return d.mac !== port.mac; }).forEach(function (d) {
      var o = el("option", null, (d.name || d.id) + (d.mac ? " \u00b7 was " + d.mac : ""));
      o.value = d.id;
      if (S.rehome.id === d.id) o.selected = true;
      pick.appendChild(o);
    });
    pick.addEventListener("change", function () { S.rehome.id = pick.value; });
    f.appendChild(pick);
    box.appendChild(f);
    box.appendChild(el("p", "iot-field-help",
      "It keeps its name, flow and settings. The old board's token stops " +
      "working, so flash this one next; it enrols as the same device."));
    var acts = el("div", "iot-form-acts");
    acts.appendChild(Z.Button({
      label: "Move it", variant: "primary", size: "sm", disabled: S.busy,
      onClick: function () {
        S.busy = true; render();
        post("/api/iot/devices/" + encodeURIComponent(S.rehome.id) + "/rehome", {
          port: port.port, mac: port.mac, chip: port.chip || null,
          flash: port.flash || null, psram: port.psram === true, board: port.board || null
        }).then(function (res) {
          var said = "moved " + (res.name || res.id) + " onto " + port.port +
                     " \u2014 flash it next";
          if ((res.warnings || []).length) said += ". " + res.warnings.join("; ");
          S.notice = notice("devices", (res.warnings || []).length ? "critical" : "ok", said);
          S.rehome = null;
        }).catch(function (e) {
          S.notice = notice("devices", "critical", e.message);
        }).then(function () { S.busy = false; S.mounted = null; poll(); });
      }
    }));
    acts.appendChild(Z.Button({
      label: "Cancel", size: "sm",
      onClick: function () { S.rehome = null; render(); }
    }));
    box.appendChild(acts);
    return box;
  }

  function suggestName(port) {
    var base = boardLabel(port.board) || port.chip || "device";
    var n = 1, name = base;
    while (S.devices.some(function (d) { return d.name === name; })) {
      n += 1; name = base + " " + n;
    }
    return name;
  }

  /* The config being created. The board is prefilled from what the scan found,
     and stays editable because a module variant can differ from its chip. */
  function draftForm() {
    S.form = S.draft;                 // field() edits whatever S.form points at
    var form = el("div", "iot-form");
    form.appendChild(field("Name", "name", { maxLength: 48 }));
    var opts = S.boards.map(function (b) { return b.board; });
    form.appendChild(field("Board profile", "board",
      { options: opts.length ? opts : [""],
        help: S.draft.board ? "matched from the scan" : "the scan did not match a profile" }));
    form.appendChild(field("Note", "note", { maxLength: 200, placeholder: "where it lives, what it does" }));
    var acts = el("div", "iot-form-acts");
    acts.appendChild(Z.Button({
      label: S.busy ? "Saving\u2026" : "Save config", variant: "primary", size: "sm",
      disabled: S.busy,
      onClick: function () {
        S.busy = true; render();
        post("/api/iot/devices", S.draft)
          .then(function (d) {
            S.draft = null; S.form = null;
            S.notice = notice("devices", "ok", "config saved for " + d.name);
          })
          .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
          .then(function () { S.busy = false; poll(); });
      }
    }));
    acts.appendChild(Z.Button({
      label: "Cancel", size: "sm",
      onClick: function () { S.draft = null; S.form = null; render(); }
    }));
    form.appendChild(acts);
    return form;
  }

  /* A summary. Everything you can do to a device is behind it, in the detail
     view, so the list stays a list. */
  function deviceCard(d) {
    var st = d.state || {};
    var link = clientFor(d);
    var fresh = !!d.online;        // the server's answer, not ours
    var card = el("div", "dev-card is-clickable " + (fresh ? "is-online" : "is-stale"));
    var top = el("div", "dev-card-top");
    top.appendChild(el("span", "dev-card-name", d.name || d.id));
    top.appendChild(Z.Badge({ text: boardLabel(d.board), tone: d.board ? "idle" : "warn" }));
    top.appendChild(Z.Badge({
      text: life(d, fresh).short,
      tone: life(d, fresh).tone
    }));
    if (d.mismatch) top.appendChild(Z.Badge({ text: "mismatch", tone: "critical" }));
    if (d.camera) top.appendChild(Z.Badge({ text: "camera", tone: "idle" }));
    card.appendChild(top);

    // The address is the first thing you want, so it goes where you look.
    var where = (link.lease && link.lease.ip) || st.ip || d.ip;
    card.appendChild(el("div", "dev-card-id",
      (where || "no address") + (d.mac ? " · " + d.mac : "")));

    var meta = el("div", "dev-card-meta");
    [["flow", st.flow_name || (d.flow ? flowName(d.flow) : "nothing deployed")],
     ["heard", Z.ago(d.last_seen)],
     ["signal", (link.station && link.station.signal != null)
       ? link.station.signal + " dBm" : (d.rssi != null ? d.rssi + " dBm" : null)],
     ["agent", st.agent]
    ].forEach(function (pair) {
      if (pair[1]) meta.appendChild(el("span", null, pair[0] + " " + pair[1]));
    });
    if (meta.childNodes.length) card.appendChild(meta);

    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.title = "Open " + (d.name || d.id);
    card.addEventListener("click", function () { selectDevice(d.id); });
    card.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        selectDevice(d.id);
      }
    });
    return card;
  }

  /* Flashing needs two things the console cannot know: which port the board is
     on, and the network passphrase, which only root can read. Neither is kept. */
  function flashForm(d) {
    S.form = S.form && S.form.__flash === d.id ? S.form : {
      __flash: d.id, port: d.port || "/dev/ttyUSB0", psk: "",
      ssid: ((((S.status || {}).ap || {}).state) || {}).ssid || ""
    };
    var withFlow = S.picked[d.id] || d.flow || "";
    var flowName = (S.flows.filter(function (f) { return f.id === withFlow; })[0] || {}).name;
    var form = el("div", "iot-form");
    if (withFlow) {
      form.appendChild(el("div", "field-help",
        "This will also write \u201c" + (flowName || withFlow) + "\u201d and the "
        + "files it needs over USB, so the board boots with everything and its "
        + "first sync fetches nothing."));
    }
    form.appendChild(field("Serial port", "port", { placeholder: "/dev/ttyUSB0" }));
    form.appendChild(field("Network", "ssid", { help: "the network the board should join" }));
    form.appendChild(field("Passphrase", "psk",
      { type: "password", help: "not stored — it is written into the board and forgotten" }));
    var acts = el("div", "iot-form-acts");
    acts.appendChild(Z.Button({
      label: "Erase and flash", variant: "danger", size: "sm",
      disabled: S.busy || S.flash.running,
      onClick: function () {
        var body = { port: S.form.port, ssid: S.form.ssid, psk: S.form.psk,
                     flow: withFlow || null };
        S.busy = true; render();
        post("/api/iot/devices/" + d.id + "/flash", body)
          .then(function () {
            S.flashFor = null; S.form = null;
            delete S.picked[d.id];
            S.flash.running = true;
            S.log.synced = false;
            S.notice = notice("flashing", "ok", "flashing " + d.name);
            setTab("flashing");
          })
          .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
          .then(function () { S.busy = false; poll(); });
      }
    }));
    acts.appendChild(Z.Button({
      label: "Cancel", size: "sm",
      onClick: function () { S.flashFor = null; S.form = null; render(); }
    }));
    form.appendChild(acts);
    form.appendChild(el("div", "field-help",
      "This erases the board and writes MicroPython. It takes a few minutes, and "
      + "slower still when esptool has no stub flasher."));
    return form;
  }

  /* ---------- the flash monitor ------------------------------------------ */
  /* The log is built once and appended to. The bundle's LogAppend only follows
     the tail if the reader was already at it, so scrolling up to read holds. */
  function toLogRow(r) {
    return { level: r.level, time: r.time, message: r.text };
  }

  function appendLog(rows) {
    if (!S.log.list || !rows.length) return;
    // LogStream drops an "empty" paragraph in when it is built with nothing,
    // and never takes it out again.
    var empty = S.log.list.parentNode &&
                S.log.list.parentNode.querySelector(".z-empty");
    if (empty) empty.parentNode.removeChild(empty);
    Z.LogAppend(S.log.list, rows.map(toLogRow), 500);
    S.log.count += rows.length;
    markTail();
  }

  /* Reconcile against the whole log, which is what the poll returns. A log
     shorter than what we have drawn means a new flash cleared it. */
  function syncLog() {
    if (!S.log.list) return;
    var rows = S.flash.log || [];
    if (!S.log.synced || rows.length < S.log.count) {
      S.log.list.textContent = "";
      S.log.count = 0;
    }
    appendLog(rows.slice(S.log.count));
    S.log.synced = true;
  }

  function atTail() {
    var l = S.log.list;
    if (!l) return true;
    return l.scrollTop + l.clientHeight >= l.scrollHeight - 24;
  }
  function markTail() {
    if (!S.log.widget) return;
    S.log.widget.classList.toggle("is-adrift", !atTail());
  }
  function toTail() {
    if (!S.log.list) return;
    S.log.list.scrollTop = S.log.list.scrollHeight;
    markTail();
  }

  function setFlashState() {
    if (!S.log.tools) return;
    S.log.tools.textContent = "";
    S.log.tools.appendChild(Z.StatusDot({
      state: S.flash.running ? "warn" : (S.log.count ? "ok" : "idle"),
      label: S.flash.running ? "running" : (S.log.count ? "done" : "idle"),
      pulse: S.flash.running
    }));
  }

  function flashingTab() {
    var rows = Math.max(14, Math.floor((window.innerHeight - 340) / 17));
    var stream = Z.LogStream({
      entries: (S.flash.log || []).map(toLogRow),
      live: true, rows: rows,
      emptyText: "Nothing has been flashed since this console started."
    });
    var list = stream.querySelector(".z-logs-list");
    S.log.list = list;
    S.log.count = (S.flash.log || []).length;
    S.log.synced = true;

    var body = [];
    var note = noticeFor("flashing");
    if (note) body.push(note);
    body.push(stream);
    var jump = el("div", "flash-jump");
    jump.appendChild(Z.Button({
      label: "Jump to the newest ↓", size: "sm", variant: "primary",
      onClick: toTail
    }));
    body.push(jump);
    body.push(el("p", "field-help",
      "This follows the newest line only while you are already at the bottom. "
      + "Scroll up and it holds where you put it."));

    var w = Z.Widget({
      title: "Flashing",
      subtitle: "scripts/iot-flash.py · live",
      state: "idle", stateLabel: "idle",
      children: body
    });
    w.classList.add("iot-flashlog");
    S.log.widget = w;
    S.log.tools = w.querySelector(".z-widget-tools");
    setFlashState();
    if (list) {
      list.addEventListener("scroll", markTail);
      list.scrollTop = list.scrollHeight;
    }
    return [[w, 12]];
  }

  /* Mounted once, then updated in place — see render(). */
  function updateFlashing() {
    if (!S.log.list || !S.log.list.isConnected) { S.mounted = null; return render(); }
    if (!streamLive() || !S.log.synced) syncLog();
    setFlashState();
  }

  /* ---------- devices ----------------------------------------------------- */
  function devicesTab() {
    if (S.selected) {
      var chosen = deviceById(S.selected);
      if (chosen) return deviceDetail(chosen);
      S.selected = null; S.pins = null;
    }

    var body = [];
    var note = noticeFor("devices");
    if (note) body.push(note);

    if (S.scan) {
      var head = el("div", "iot-section-head");
      head.appendChild(el("p", "dev-card-id", S.scan.ports.length
        ? "found on the usb bus" : "usb scan"));
      head.appendChild(Z.Button({
        label: "Clear", size: "sm",
        onClick: function () { S.scan = null; S.draft = null; S.form = null; render(); }
      }));
      body.push(head);
      if (!S.scan.ports.length) {
        body.push(el("p", "iot-empty", S.scan.hint || "nothing plugged in."));
      } else {
        var found = el("div", "dev-list");
        S.scan.ports.forEach(function (port) { found.appendChild(scanRow(port)); });
        body.push(found);
      }
      if (S.scan.stub_warning) {
        var sw = el("div", "iot-hint");
        sw.appendChild(el("span", null, S.scan.stub_warning));
        body.push(sw);
      }
      if (S.scan.hint) body.push(hintNode(S.scan.hint));
    }

    if (S.devices.length) {
      body.push(el("p", "dev-card-id", "configs — click one for the detail"));
      var list = el("div", "dev-list");
      S.devices.forEach(function (d) { list.appendChild(deviceCard(d)); });
      body.push(list);
    } else if (!S.scan) {
      var empty = el("div", "iot-empty");
      empty.appendChild(el("strong", null, "No device configured yet."));
      empty.appendChild(el("span", null,
        "Plug a board into USB and scan: the scan names the chip, you make a " +
        "config for it here, and flashing comes from that config."));
      body.push(empty);
    }

    var strange = strangersSection();
    if (strange) body.push(strange);

    var fresh = S.devices.filter(function (d) {
      return !!d.online;
    }).length;

    var w = Z.Widget({
      title: "Devices",
      subtitle: "scan → configure → flash → deploy",
      state: S.devices.length ? (fresh ? "ok" : "warn") : "idle",
      stateLabel: S.devices.length
        ? fresh + " of " + S.devices.length + " online" : "none",
      actions: [Z.Button({
        label: S.scanning ? "Scanning…" : "Scan USB",
        variant: "primary", size: "sm", disabled: S.scanning,
        onClick: function () { doScan(false); }
      })],
      children: body
    });
    return [[w, 12], [groupsWidget(), 12]];
  }

  /* Named lists of devices. Device command and Device event take a group
     wherever they take a device, as "group:<name>". */
  function groupsWidget() {
    var groups = S.groups || [];
    var body = [];
    var note = noticeFor("groups");
    if (note) body.push(note);

    function copy(g) { return { name: g.name, devices: g.devices.slice() }; }
    function save(next, said) {
      post("/api/iot/groups", { groups: next })
        .then(function (res) {
          S.groups = res.groups || [];
          S.notice = notice("groups", "ok", said);
          render();
        })
        .catch(function (e) { S.notice = notice("groups", "critical", e.message); render(); });
    }

    if (!groups.length) {
      body.push(el("p", "iot-empty",
        "No groups yet. A group lets one Device command reach several boards, " +
        "and one Device event listen to all of them."));
    }
    var list = el("div", "dev-list");
    groups.forEach(function (g, gi) {
      var card = el("article", "dev-card");
      var top = el("div", "dev-card-top");
      top.appendChild(el("span", "dev-card-name", g.name));
      top.appendChild(el("span", "dev-card-id",
        "group:" + g.name + " \u00b7 " + g.devices.length + " device(s)"));
      card.appendChild(top);
      var members = el("div", "iot-form-acts");
      S.devices.forEach(function (d) {
        var inside = g.devices.indexOf(d.id) >= 0;
        var b = Z.Button({
          label: d.name || d.id, size: "sm",
          variant: inside ? "primary" : "secondary",
          onClick: function () {
            var next = groups.map(copy);
            next[gi].devices = inside
              ? next[gi].devices.filter(function (x) { return x !== d.id; })
              : next[gi].devices.concat([d.id]);
            save(next, (inside ? "took " : "added ") + (d.name || d.id) +
                 (inside ? " out of " : " to ") + g.name);
          }
        });
        b.setAttribute("aria-pressed", inside ? "true" : "false");
        members.appendChild(b);
      });
      if (!S.devices.length) members.appendChild(el("span", "dev-card-id", "no devices to add"));
      card.appendChild(members);
      var acts = el("div", "iot-form-acts");
      acts.appendChild(Z.Button({
        label: "Delete group", variant: "danger", size: "sm",
        onClick: function () {
          save(groups.filter(function (_x, i) { return i !== gi; }).map(copy),
               "deleted " + g.name);
        }
      }));
      card.appendChild(acts);
      list.appendChild(card);
    });
    if (groups.length) body.push(list);

    var add = el("label", "iot-field");
    add.appendChild(el("span", "iot-field-label", "New group"));
    var input = el("input");
    input.id = "group-new";
    input.placeholder = "e.g. wheels";
    input.maxLength = 32;
    input.value = S.groupDraft || "";
    input.addEventListener("input", function () { S.groupDraft = input.value; });
    add.appendChild(input);
    body.push(add);
    var addActs = el("div", "iot-form-acts");
    addActs.appendChild(Z.Button({
      label: "Add group", size: "sm",
      onClick: function () {
        var name = (S.groupDraft || "").trim();
        if (!name) return;
        S.groupDraft = "";
        save(groups.map(copy).concat([{ name: name, devices: [] }]), "added " + name);
      }
    }));
    body.push(addActs);

    return Z.Widget({
      title: "Groups",
      subtitle: "named lists of devices, for Device command and Device event",
      state: "idle",
      stateLabel: groups.length ? groups.length + " group(s)" : "none",
      children: body
    });
  }

  function selectDevice(id) {
    S.selected = id;
    S.pins = null;
    S.said = null;
    S.notice = null;
    S.mounted = null;
    render();
    if (id) {
      api("/api/iot/devices/" + encodeURIComponent(id) + "/pins")
        .then(function (r) { S.pins = r; if (S.selected === id) render(); })
        .catch(function () {});
      loadSaid(id);
    }
  }

  /* What the board said, as the host recorded it — so a board that has
     stopped still shows the last thing it did. */
  function loadSaid(id) {
    return api("/api/iot/devices/" + encodeURIComponent(id) + "/log?limit=60")
      .then(function (r) {
        if (S.selected === id) { S.said = r.events || []; render(); }
      })
      .catch(function () { if (S.selected === id) { S.said = []; render(); } });
  }

  function logSection(d) {
    var wrap = el("div");
    var head = el("div", "iot-section-head");
    head.appendChild(el("p", "dev-card-id", "what it said"));
    head.appendChild(Z.Button({
      label: "Refresh", size: "sm",
      onClick: function () { loadSaid(d.id); }
    }));
    wrap.appendChild(head);
    if (S.said === null) {
      wrap.appendChild(el("div", "field-help", "reading…"));
      return wrap;
    }
    wrap.appendChild(Z.LogStream({
      entries: S.said.map(function (e) {
        return { time: e.time, level: e.level,
                 unit: e.kind === "log" ? null : e.kind,
                 message: e.message || e.kind || "" };
      }),
      rows: 14, live: false,
      emptyText: "nothing yet — a board that has never spoken has nothing here"
    }));
    wrap.appendChild(el("div", "field-help",
      "Newest last. Reports are not in here — only what the board chose to "
      + "say, which is what survives it going quiet. Kept in memory on this "
      + "host, so a console restart clears it."));
    return wrap;
  }

  /* One device, and everything known about it. The pin map is the same
     component Flow Studio's inspector draws, off the same endpoint. */
  function deviceDetail(d) {
    var link = clientFor(d);
    var st = d.state || {};
    var fresh = !!d.online;        // the server's answer, not ours
    var body = [];

    var back = el("div", "iot-section-head");
    back.appendChild(Z.Button({
      label: "‹ All devices", size: "sm",
      onClick: function () { selectDevice(null); }
    }));
    if (d.camera) {
      back.appendChild(Z.Button({
        label: "Cameras", size: "sm",
        onClick: function () { location.href = "/cameras"; }
      }));
    }
    body.push(back);

    var note = noticeFor("devices");
    if (note) body.push(note);

    if (d.mismatch) {
      var mm = el("div", "iot-hint is-critical");
      mm.appendChild(el("span", null, "This board does not match its config: " + d.mismatch));
      body.push(mm);
    }

    body.push(el("p", "dev-card-id", "what it is"));
    body.push(rows([
      ["name", d.name],
      ["id", d.id],
      ["board", boardLabel(d.board)],
      ["chip", d.chip],
      ["mac", d.mac],
      ["flash", d.flash],
      ["psram", d.psram ? "yes" : null],
      ["firmware", d.firmware],
      ["note", d.note, "prose"]
    ]));

    body.push(el("p", "dev-card-id", "on the network"));
    body.push(rows([
      ["state", life(d, fresh).long],
      ["address", st.ip || d.ip],
      ["lease", link.lease
        ? link.lease.ip + " until " + clock(link.lease.expires) : null],
      ["hostname", link.lease ? link.lease.name : null],
      ["signal", (link.station && link.station.signal != null)
        ? link.station.signal + " dBm"
        : (d.rssi != null ? d.rssi + " dBm, as the board reports it" : null)],
      ["associated", associated(link)],
      ["last report", Z.ago(d.last_seen)],
      ["enrolled", d.enrolled ? Z.ago(d.enrolled) : null]
    ]));
    if (!link.lease && !link.station && apState().up && d.enrolled) {
      body.push(hintNode(
        "It has a token but holds no lease and is not associated. It is either "
        + "powered down, out of range, or still joined to a different network."));
    }

    body.push(el("p", "dev-card-id", "what it is running"));
    body.push(rows([
      ["flow", st.flow_name || (d.flow ? flowName(d.flow) : "nothing deployed")],
      ["deployed", d.deployed ? Z.ago(d.deployed) : null],
      ["agent", agentLine(st, d)],
      ["free memory", Z.bytes(st.free_ram)],
      ["internal ram free", st.idf_free == null ? null : Z.bytes(st.idf_free)],
      ["uptime", Z.duration(st.uptime)],
      ["nodes", nodeLine(st)],
      ["camera", cameraLine(st, d)],
      ["link", linkLine(st, d)],
      ["reported", st.at ? Z.ago(st.at) : null]
    ]));
    if (d.link) {
      /* The link's own round trip: PING from here, the board's PONG back. */
      var said = (S.pinged || {})[d.id];
      var pingRow = el("div", "iot-form-acts");
      pingRow.appendChild(Z.Button({
        label: "Ping over the link", size: "sm", disabled: S.busy,
        onClick: function () {
          post("/api/iot/devices/" + encodeURIComponent(d.id) + "/ping")
            .then(function (res) {
              S.pinged = S.pinged || {};
              S.pinged[d.id] = (res.ok ? "round trip " : "") + res.detail +
                               " \u00b7 " + new Date().toLocaleTimeString();
              render();
            })
            .catch(function (e) {
              S.pinged = S.pinged || {};
              S.pinged[d.id] = e.message;
              render();
            });
        }
      }));
      if (said) pingRow.appendChild(el("span", "dev-card-id", said));
      body.push(pingRow);
    }

    body.push(logSection(d));
    body.push(pinSection(d));

    // Everything you could do to it, in one place.
    if (d.board) {
      var forBoard = S.flows.filter(function (f) { return f.board === d.board; });
      var row = el("div", "dev-deploy");
      var pick = el("select");
      var none = el("option", null, "— nothing —"); none.value = "";
      pick.appendChild(none);
      /* In S.picked, keyed by device, because the card is rebuilt on every
         poll; cleared once a deploy or flash has used it. */
      var chosen = S.picked[d.id];
      if (chosen === undefined) chosen = d.flow || "";
      forBoard.forEach(function (f) {
        var o = el("option", null, f.name || f.id);
        o.value = f.id;
        if (chosen === f.id) o.selected = true;
        pick.appendChild(o);
      });
      pick.addEventListener("change", function () {
        S.picked[d.id] = pick.value;
        loadCost(d, pick.value);
      });
      row.appendChild(pick);
      row.appendChild(Z.Button({
        label: "Deploy", variant: "primary", size: "sm",
        disabled: S.busy || !d.enrolled,
        onClick: function () { deploy(d, pick.value); }
      }));
      /* The wire, for a flow big enough that fetching it would take the board
         off the air for minutes. It writes the flow and its modules over USB,
         so the first sync has nothing to do. */
      row.appendChild(Z.Button({
        label: "Flash with this\u2026", size: "sm",
        variant: (costFor(d, chosen) || {}).wire ? "primary" : "secondary",
        disabled: S.busy || S.flash.running || !S.config.iot_net || !chosen,
        onClick: function () {
          S.picked[d.id] = pick.value;
          S.flashFor = d.id;
          S.notice = null;
          render();
        }
      }));
      body.push(row);
      var cost = costFor(d, chosen);
      if (cost) {
        body.push(el("div", "field-help" + (cost.wire ? " is-warn" : ""),
          cost.wire
            ? "Deploying this over the air means fetching " + Z.bytes(cost.bytes)
              + ". The board is unreachable while it syncs — 70 KB has taken six "
              + "minutes — and a board silent that long looks exactly like one "
              + "that has died. Use Flash with this\u2026 instead."
            : "Over the air this fetches " + Z.bytes(cost.bytes) + " of "
              + Z.bytes(cost.total) + "; the board already has the rest."));
      }
      if (!forBoard.length) {
        body.push(el("div", "field-help",
          "No flow is written for this board yet — make one in Flow Studio and "
          + "set its board to " + boardLabel(d.board) + "."));
      } else if (!d.enrolled) {
        body.push(el("div", "field-help",
          d.flashed
            ? "It was flashed " + Z.ago(d.flashed) + " and has not enrolled yet. A "
              + "flow can only be deployed over the air to a board that has — but "
              + "Flash with this\u2026 writes one over USB whatever its state."
            : "Flash this board first; a flow can only be deployed to one that "
              + "has enrolled."));
      }
    }

    if (S.flashFor === d.id) body.push(flashForm(d));
    body.push(deviceActions(d));

    var w = Z.Widget({
      title: d.name || d.id,
      subtitle: boardLabel(d.board) + (st.ip ? " · " + st.ip : ""),
      state: life(d, fresh).tone,
      stateLabel: life(d, fresh).short,
      pulse: fresh,
      children: body
    });
    return [[w, 12]];
  }

  /* The live pin map. Same markup and same stylesheet as the studio's
     inspector, because it is the same question being asked. */
  function pinSection(d) {
    var box = el("div", "iot-section");
    var head = el("div", "iot-section-head");
    head.appendChild(el("p", "dev-card-id", "pins and buses"));
    box.appendChild(head);

    if (!S.pins) {
      box.appendChild(el("p", "iot-empty",
        d.board ? "Reading the pin map…"
                : "No board profile, so there is no pin map to draw."));
      return box;
    }
    var data = S.pins;
    var age = data.reported_age;
    box.appendChild(rows([
      ["reported", age == null ? "never — this board has not reported yet"
                               : age + "s ago"],
      ["flow", data.flow_name || "nothing deployed"]
    ]));

    var pins = (data.pins || []).filter(function (p) { return p.usable !== false; });
    if (!pins.length) {
      box.appendChild(el("p", "iot-empty", "This profile lists no usable pins."));
      return box;
    }
    var map = el("div", "pinmap");
    pins.forEach(function (pin) {
      var b = el("button", "pin");
      b.type = "button";
      b.disabled = true;
      b.appendChild(el("span", "pin-num", String(pin.gpio)));
      b.appendChild(el("i", "pin-dot pin-dot--" + (pin.status || "unknown")));
      var label = pin.name || ("GPIO" + pin.gpio);
      if (pin.used_by && pin.used_by.length) {
        label += " · " + pin.used_by.map(function (u) { return u.node; }).join(", ");
      }
      var text = el("span", "pin-label", label);
      b.appendChild(text);
      if (pin.dir) {
        var v = el("span", pin.value ? "pin-high" : null,
                   pin.dir + (pin.value == null ? "" : " " + pin.value));
        b.appendChild(v);
      }
      b.title = [pin.name, (pin.caps || []).join(" "), pin.note]
        .filter(Boolean).join(" — ");
      map.appendChild(b);
    });
    box.appendChild(map);

    var legend = el("div", "pin-legend");
    [["free", "free"], ["bound", "used by the flow"], ["reserved", "reserved"]]
      .forEach(function (pair) {
        var sp = el("span");
        sp.appendChild(el("i", "pin-dot pin-dot--" + pair[0]));
        sp.appendChild(el("span", null, pair[1]));
        legend.appendChild(sp);
      });
    box.appendChild(legend);

    var buses = data.buses || {};
    var names = Object.keys(buses);
    if (names.length) {
      box.appendChild(rows(names.map(function (k) {
        var b = buses[k];
        var pairs = Object.keys(b).filter(function (x) { return x !== "note"; })
          .map(function (x) { return x + " " + b[x]; }).join("  ");
        return [k, pairs + (b.note ? "  · " + b.note : "")];
      })));
    }
    return box;
  }

  /* What deploying a flow to this device would have to fetch. Asked once per
     pair and remembered, because it only changes when a module or the flow does. */
  function costKey(d, flowId) { return d.id + "|" + (flowId || ""); }

  function costFor(d, flowId) {
    if (!flowId) return null;
    return S.costs[costKey(d, flowId)] || null;
  }

  function loadCost(d, flowId) {
    if (!flowId || S.costs[costKey(d, flowId)]) return render();
    api("/api/iot/devices/" + encodeURIComponent(d.id) + "/cost?flow="
        + encodeURIComponent(flowId))
      .then(function (c) { S.costs[costKey(d, flowId)] = c; })
      .catch(function () { /* a missing answer just means no hint */ })
      .then(render);
  }

  function deploy(d, flowId) {
    S.busy = true; render();
    post("/api/iot/devices/" + d.id + "/deploy", { flow: flowId || null })
      .then(function () {
        delete S.picked[d.id];
        S.notice = notice("devices", "ok",
          flowId ? "deployed to " + d.name : "cleared " + d.name);
      })
      .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
      .then(function () { S.busy = false; poll(); });
  }

  function deviceActions(d) {
    var acts = el("div", "iot-form-acts");
    /* Decides the firmware the next flash writes. A scan only sees PSRAM
       inside the chip; a WROVER's is outside it, so it is said here. */
    var pw = el("label", "iot-field");
    pw.appendChild(el("span", "iot-field-label", "PSRAM"));
    var ps = el("select");
    ps.id = "psram-" + d.id;
    [["", "none"], ["quad", "quad (WROVER, most S3)"], ["octal", "octal (S3 N16R8 and similar)"]]
      .forEach(function (o) {
        var opt = el("option", null, o[1]);
        opt.value = o[0];
        if ((d.psram || "") === o[0]) opt.selected = true;
        ps.appendChild(opt);
      });
    ps.addEventListener("change", function () {
      post("/api/iot/devices/" + d.id, { psram: ps.value || null })
        .then(function () {
          S.notice = notice("devices", "ok", "PSRAM " + (ps.value || "none") +
                            " — the next flash writes the build that uses it");
        })
        .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
        .then(function () { S.mounted = null; poll(); });
    });
    pw.appendChild(ps);
    acts.appendChild(pw);
    acts.appendChild(Z.Button({
      label: d.enrolled ? "Re-flash…" : "Flash…", size: "sm",
      variant: d.enrolled ? "secondary" : "primary",
      disabled: S.busy || S.flash.running || !S.config.iot_net,
      onClick: function () {
        S.flashFor = S.flashFor === d.id ? null : d.id;
        S.notice = null;
        render();
      }
    }));
    if (d.enrolled) {
      /* Off until turned on, per board: its modules cost memory an import on
         MicroPython does not give back before a reboot. */
      acts.appendChild(Z.Button({
        label: d.link ? "Stop holding a link" : "Hold a link open", size: "sm",
        disabled: S.busy,
        onClick: function () {
          S.busy = true; render();
          post("/api/iot/devices/" + d.id, { link: !d.link })
            .then(function () {
              S.notice = notice("devices", "ok", d.link
                ? "link switched off — the board will be told on its next poll"
                : "link switched on — the board pulls the modules and dials");
            })
            .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
            .then(function () { S.busy = false; S.mounted = null; poll(); });
        }
      }));
      acts.appendChild(Z.Button({
        label: "Reboot", size: "sm", disabled: S.busy,
        onClick: function () {
          post("/api/iot/devices/" + d.id + "/command", { op: "reboot" })
            .then(function () { S.notice = notice("devices", "ok", "reboot queued"); })
            .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
            .then(render);
        }
      }));
    }
    acts.appendChild(Z.Button({
      label: "Delete", variant: "danger", size: "sm", disabled: S.busy,
      onClick: function () {
        S.busy = true; render();
        post("/api/iot/devices/" + d.id + "/delete")
          .then(function () {
            S.selected = null; S.pins = null;
            S.notice = notice("devices", "ok", "deleted " + d.name);
          })
          .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
          .then(function () { S.busy = false; S.mounted = null; poll(); });
      }
    }));
    return acts;
  }

  /* ---------- bluetooth --------------------------------------------------- */
  /* Pairing a controller, and what is paired to what. Two separate questions on
     one screen: this host's own adapter, and what each field device says it has
     found — which is a board's own claim, arriving as an event, not something
     this end can see for itself. */

  function btAction(scope, label, path, body, tone) {
    return Z.Button({
      label: S.busy ? "Working\u2026" : label,
      variant: tone || "secondary", size: "sm", disabled: S.busy,
      onClick: function () {
        S.busy = true;
        S.notice = notice(scope, "ok", label.toLowerCase() + "\u2026");
        render();
        post(path, body)
          .then(function (res) {
            S.notice = notice(scope, res.ok === false ? "critical" : "ok",
                              res.detail || label + " done");
          })
          .catch(function (e) { S.notice = notice(scope, "critical", e.message); })
          .then(function () { S.busy = false; poll(); });
      }
    });
  }

  function btRow(d) {
    var card = el("article", "dev-card " +
                  (d.connected ? "is-online" : (d.paired ? "" : "is-stale")));
    var head = el("div", "dev-card-top");
    head.appendChild(Z.StatusDot({
      state: d.connected ? "ok" : (d.paired ? "idle" : "warn"),
      label: d.connected ? "connected" : (d.paired ? "paired" : "seen")
    }));
    head.appendChild(el("span", "dev-card-name", d.name || d.mac));
    if (d.pad) head.appendChild(Z.Badge({ text: "controller", tone: "ok" }));
    card.appendChild(head);
    card.appendChild(el("p", "dev-card-id", d.mac));

    var acts = el("div", "iot-form-acts");
    if (!d.paired) {
      /* Pairing re-opens discovery and holds it open, because BlueZ forgets a
         device it discovered but never paired — so this is not instant and the
         pad has to still be in pairing mode when it is pressed. */
      acts.appendChild(btAction("bt", "Pair", "/api/bt/pair", { mac: d.mac }, "primary"));
    } else if (d.connected) {
      acts.appendChild(btAction("bt", "Disconnect", "/api/bt/disconnect", { mac: d.mac }));
    } else {
      acts.appendChild(btAction("bt", "Connect", "/api/bt/connect", { mac: d.mac }, "primary"));
    }
    if (d.paired) {
      acts.appendChild(btAction("bt", "Forget", "/api/bt/forget", { mac: d.mac }, "danger"));
    }
    /* Only where there is something to browse. A Classic HID device has no GATT
       however connected it is — measured on a DualShock — so offering a browse
       that comes back empty would read as a broken button. */
    if (d.connected && !d.classic_hid) {
      acts.appendChild(Z.Button({
        label: "Browse", size: "sm", disabled: S.busy,
        onClick: function () { loadGatt(d.mac); }
      }));
    }
    card.appendChild(acts);
    return card;
  }

  function btAdapterWidget() {
    var bt = S.bt || {};
    var a = bt.adapter || {};
    var sc = bt.scan || {};
    var body = [];

    if (!S.bt) {
      /* A console without /api/bt is older code, not a missing tool. */
      body.push(el("p", "iot-empty",
        "This console did not answer /api/bt \u2014 it is probably running code " +
        "from before Bluetooth was added. Restart it and this fills in."));
    } else if (!bt.installed) {
      body.push(el("p", "iot-empty",
        "bluetoothctl is not installed, so this host cannot pair anything."));
    } else if (!a.present) {
      body.push(el("p", "iot-empty", bt.missing || "no adapter"));
    } else {
      body.push(rows([
        ["adapter", a.address],
        ["name", a.name],
        ["powered", a.powered ? "yes" : "no"],
        ["paired", String(bt.paired || 0)],
        ["connected", String(bt.connected || 0)]
      ]));
      /* The one thing worth knowing before pressing Scan (docs/RADIO.md). */
      body.push(rows([["shares its chip with wlan0",
        "One UNISOC part runs both. A scan has run beside the live "
        + "access point without trouble, once; if the chip does assert, it "
        + "resets and takes the AP, and the fleet, with it. Not worth doing "
        + "while something is driving.", "prose"]]));
    }

    var found = (sc.found || []);
    var pads = found.filter(function (f) { return f.pad; });
    if (sc.running) {
      body.push(el("p", "iot-empty",
        "Scanning\u2026 " + (sc.seconds_left || 0) + "s left. Hold the pad's " +
        "pair button until its light flashes quickly."));
    } else if (sc.at && !found.length) {
      body.push(el("p", "iot-empty",
        "That scan found nothing at all. Is the adapter powered?"));
    } else if (sc.at && !pads.length) {
      /* The usual case: bulbs, beacons and televisions, and no controller. */
      body.push(el("p", "iot-empty",
        "That scan saw " + found.length + " device(s) and none of them looks " +
        "like a controller \u2014 they are the bulbs, beacons and televisions " +
        "around you. A pad only advertises while it is in pairing mode: hold " +
        "its pair button until the light flashes quickly, then scan again. A " +
        "pad that is merely switched on, or already connected to a console or " +
        "a phone, will not appear."));
    } else if (sc.at && pads.length) {
      body.push(el("p", "iot-empty",
        pads.length + " of " + found.length + " look like a controller. " +
        "Pair re-opens discovery and holds it open, so keep the pad in pairing " +
        "mode while it runs \u2014 it can take most of a minute."));
    }
    if (sc.error) body.push(el("p", "iot-empty", "scan: " + sc.error));

    var note = noticeFor("bt");
    if (note) body.push(note);

    var scan = Z.Button({
      label: sc.running ? "Scanning\u2026" : "Scan",
      variant: "primary", size: "sm",
      disabled: S.busy || sc.running || !a.present,
      onClick: function () {
        S.notice = notice("bt", "ok", "scanning\u2026");
        render();
        post("/api/bt/scan", { seconds: 12 })
          .catch(function (e) { S.notice = notice("bt", "critical", e.message); })
          .then(function () { poll(); });
      }
    });

    return Z.Widget({
      title: "This host's Bluetooth",
      subtitle: a.present ? "hci0 \u00b7 " + (a.address || "") : "bluetoothctl",
      actions: [scan],
      state: !S.bt || !bt.installed || !a.present ? "warn"
        : (bt.connected ? "ok" : "idle"),
      stateLabel: !S.bt ? "no answer"
        : (!a.present ? "no adapter"
           : (bt.connected ? "connected" : (bt.paired ? "paired" : "nothing paired"))),
      children: body
    });
  }

  function btDevicesWidget() {
    var bt = S.bt || {};
    var seen = (bt.devices || []).slice();
    var found = ((bt.scan || {}).found || []);
    var known = {};
    seen.forEach(function (d) { known[d.mac] = true; });
    found.forEach(function (f) {
      if (!known[f.mac]) {
        seen.push({ mac: f.mac, name: f.name, paired: false,
                    connected: false, pad: f.pad });
      }
    });
    // Controllers first, then connected, then the rest: the list is read by
    // somebody trying to pair a pad, not by somebody auditing a radio.
    seen.sort(function (a, b) {
      return (b.pad ? 1 : 0) - (a.pad ? 1 : 0) ||
             (b.connected ? 1 : 0) - (a.connected ? 1 : 0) ||
             (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase());
    });

    var body = [];
    if (!seen.length) {
      body.push(el("p", "iot-empty",
        "Nothing known to this adapter yet. Press Scan with the pad in pairing mode."));
      var steps = el("ol", "iot-note");
      ["Hold the controller's pair button until its light flashes quickly.",
       "Press Scan and wait for it to appear below.",
       "Press Pair \u2014 that pairs, trusts and connects it in one go.",
       "Then add a Controller node to a flow that runs on this board."
      ].forEach(function (t) { steps.appendChild(el("li", null, t)); });
      body.push(steps);
    } else {
      var list = el("div", "dev-list");
      seen.forEach(function (d) { list.appendChild(btRow(d)); });
      body.push(list);
    }
    var padCount = seen.filter(function (d) { return d.pad; }).length;
    return Z.Widget({
      title: "Paired and nearby",
      subtitle: seen.length + " device(s)" +
                (padCount ? " \u00b7 " + padCount + " look like a controller" : ""),
      state: "idle",
      stateLabel: (bt.connected || 0) + " connected",
      children: body
    });
  }

  function btFieldWidget() {
    /* Each board's own claim, sent as an event when a Controller node's state
       changes; this host cannot see a device's BLE links. */
    var body = [];
    var any = false;
    var list = el("div", "dev-list");
    (S.devices || []).forEach(function (d) {
      var pad = d.pad;
      var card = el("article", "dev-card " +
                    (pad && pad.connected ? "is-online" : ""));
      var head = el("div", "dev-card-top");
      head.appendChild(Z.StatusDot({
        state: pad && pad.connected ? "ok" : "idle",
        label: pad && pad.connected ? "controller" : "none"
      }));
      head.appendChild(el("span", "dev-card-name", d.name || d.id));
      card.appendChild(head);
      if (pad) {
        any = true;
        card.appendChild(rows([
          ["controller", pad.connected ? (pad.name || "connected") : "gone"],
          ["address", pad.address],
          ["said", Z.ago(pad.at)]
        ]));
      } else {
        card.appendChild(el("p", "dev-card-id",
          "has never mentioned one \u2014 no flow on it has run a Controller node"));
      }
      list.appendChild(card);
    });
    if (!(S.devices || []).length) {
      body.push(el("p", "iot-empty", "No field devices configured yet."));
    } else {
      body.push(list);
      if (!any) {
        body.push(el("p", "iot-empty",
          "A board reports its controller when a Controller node connects one. " +
          "Boards have no Bluetooth transport yet, so this stays empty and a pad " +
          "flow fails safe: the axis nodes stay quiet and the Watchdog holds the " +
          "wheels."));
      }
    }
    return Z.Widget({
      title: "Controllers on field devices",
      subtitle: "from each board's own account of it",
      state: "idle",
      stateLabel: any ? "reported" : "nothing reported",
      children: body
    });
  }

  function loadGatt(mac) {
    S.btGatt = { mac: mac, loading: true };
    render();
    api("/api/bt/gatt?mac=" + encodeURIComponent(mac))
      .then(function (r) { S.btGatt = r; })
      .catch(function (e) { S.btGatt = { mac: mac, error: e.message }; })
      .then(render);
  }

  function charRow(ch) {
    var row = el("article", "dev-card");
    var top = el("div", "dev-card-top");
    top.appendChild(el("span", "dev-card-name", ch.name || ch.uuid));
    (ch.flags || []).forEach(function (f) {
      top.appendChild(Z.Badge({
        text: f,
        tone: f === "notify" || f === "indicate" ? "ok"
          : (f.indexOf("write") === 0 ? "warn" : "idle")
      }));
    });
    if (ch.notifying) top.appendChild(Z.Badge({ text: "live", tone: "ok" }));
    row.appendChild(top);
    row.appendChild(el("div", "dev-card-id", ch.uuid));
    if ((ch.value || []).length) {
      row.appendChild(rows([["last value",
        ch.value.map(function (b) {
          return ("0" + (b & 255).toString(16)).slice(-2);
        }).join(" ")]]));
    }

    var acts = el("div", "iot-form-acts");
    var can = ch.flags || [];
    if (can.indexOf("read") >= 0) {
      acts.appendChild(btAction("gatt", "Read", "/api/bt/read", { path: ch.path }));
    }
    if (can.indexOf("notify") >= 0 || can.indexOf("indicate") >= 0) {
      acts.appendChild(btAction("gatt", ch.notifying ? "Unsubscribe" : "Subscribe",
                                "/api/bt/notify",
                                { path: ch.path, on: !ch.notifying }));
    }
    if (can.indexOf("write") >= 0 || can.indexOf("write-without-response") >= 0) {
      // Wrapped rather than given a class of its own: `.iot-field input` is
      // where every input on this screen gets its look.
      var holder = el("div", "iot-field");
      var box = el("input");
      box.placeholder = "hex, e.g. 01ff";
      box.size = 10;
      holder.appendChild(box);
      acts.appendChild(holder);
      acts.appendChild(Z.Button({
        label: "Write", size: "sm", variant: "warn", disabled: S.busy,
        onClick: function () {
          S.busy = true;
          S.notice = notice("gatt", "ok", "writing\u2026");
          render();
          post("/api/bt/write", { path: ch.path, hex: box.value.trim() })
            .then(function (r) {
              S.notice = notice("gatt", r.ok ? "ok" : "critical",
                                r.detail || (r.ok ? "written" : "refused"));
            })
            .catch(function (e) { S.notice = notice("gatt", "critical", e.message); })
            .then(function () { S.busy = false; loadGatt(S.btGatt.mac); });
        }
      }));
    }
    if (acts.childNodes.length) row.appendChild(acts);
    return row;
  }

  function btGattWidget() {
    var g = S.btGatt;
    var body = [];
    if (g.loading) {
      body.push(el("p", "iot-empty", "Reading its attribute table\u2026"));
    } else if (g.error) {
      body.push(el("p", "iot-empty", g.error));
    } else if (!(g.services || []).length) {
      /* Two different situations and they need different words: nothing is
         connected, or it is connected and simply has no GATT. */
      var dev = g.device || {};
      body.push(el("p", "iot-empty", !dev.connected
        ? "Not connected, so it has no attribute table yet. Connect it first."
        : (dev.classic_hid
           ? "This is a Bluetooth Classic HID device. It has no GATT at all \u2014 " +
             "the kernel drives it through the HID stack instead, which is how " +
             "the Controller nodes read a gamepad. There is nothing here to browse."
           : "Connected, but it published no services. Some devices only do " +
             "once they have been bonded.")));
    } else {
      (g.services || []).forEach(function (svc) {
        var head = el("div", "dev-card-top");
        head.appendChild(el("span", "dev-card-name", svc.name || svc.uuid));
        head.appendChild(Z.Badge({ text: svc.primary ? "primary" : "secondary",
                                   tone: "idle" }));
        body.push(head);
        body.push(el("div", "dev-card-id", svc.uuid));
        var list = el("div", "dev-list");
        (svc.chars || []).forEach(function (ch) { list.appendChild(charRow(ch)); });
        if (!(svc.chars || []).length) {
          list.appendChild(el("p", "iot-empty", "no characteristics"));
        }
        body.push(list);
      });
    }
    var note = noticeFor("gatt");
    if (note) body.push(note);

    return Z.Widget({
      title: "Attributes on " + ((g.device || {}).name || g.mac),
      subtitle: (g.services || []).length + " service(s) \u00b7 " + g.mac,
      actions: [Z.Button({
        label: "Close", size: "sm",
        onClick: function () { S.btGatt = null; render(); }
      })],
      state: "idle",
      stateLabel: "read directly from BlueZ",
      children: body
    });
  }

  function bluetoothTab() {
    var out = [[btAdapterWidget(), 6], [btDevicesWidget(), 6]];
    if (S.btGatt) out.push([btGattWidget(), 12]);
    out.push([btFieldWidget(), 12]);
    return out;
  }

  /* ---------- boards ------------------------------------------------------ */
  function boardsTab() {
    var body = [];
    if (!S.boards.length) {
      body.push(el("p", "iot-empty", "No board profiles found."));
    } else {
      var list = el("div", "dev-list");
      S.boards.forEach(function (b) {
        var card = el("div", "dev-card");
        var top = el("div", "dev-card-top");
        top.appendChild(el("span", "dev-card-name", b.label));
        top.appendChild(Z.Badge({ text: b.counts.usable + " usable pins", tone: "idle" }));
        Object.keys(b.peripherals).forEach(function (k) {
          var v = b.peripherals[k];
          if (v === true) top.appendChild(Z.Badge({ text: k, tone: "ok" }));
          else if (v && v !== false) top.appendChild(Z.Badge({ text: k + " " + v, tone: "idle" }));
        });
        card.appendChild(top);
        var meta = el("div", "dev-card-meta");
        [["chip", b.chip], ["cores", b.cores], ["flash", b.flash_default_mb + " MB"],
         ["io", b.voltage + " V"], ["adc", b.counts.adc]].forEach(function (pair) {
          meta.appendChild(el("span", null, pair[0] + " " + pair[1]));
        });
        card.appendChild(meta);
        card.appendChild(el("div", "dev-card-id", b.source));
        list.appendChild(card);
      });
      body.push(list);
    }
    var w = Z.Widget({
      title: "Board profiles",
      subtitle: "generated · zero2w_console/data/boards",
      state: S.boards.length ? "ok" : "warn",
      stateLabel: S.boards.length + " known",
      children: body
    });
    return [[w, 12]];
  }

  /* ---------- enrollment -------------------------------------------------- */
  function enrollmentTab() {
    var dev = ((S.status || {}).devices) || {};
    var win = dev.enrollment;
    var open = !!win;
    var st = apState();
    var body = [];

    var note = noticeFor("enrollment");
    if (note) body.push(note);

    body.push(rows([
      ["window", open ? "open" : "closed"],
      ["closes in", open ? countdown(win.expires) : null],
      ["closes at", open ? clock(win.expires) : null],
      ["opened", open ? Z.ago(win.opened) : null],
      ["why", open ? (win.reason || "opened by hand") : null, "prose"],
      ["network to join", st.ssid || "no network configured"],
      ["devices enrolled", dev.enrolled != null ? String(dev.enrolled) : null],
      ["online now", dev.online != null ? String(dev.online) : null]
    ]));

    body.push(el("p", "iot-note", open
      ? "A board that asks to join right now is accepted and given its own "
        + "token — which is never this console's token."
      : "Enrollment is closed, and it does not need opening by hand: flashing a "
        + "board opens the window when the flasher finishes, which is when the "
        + "board is about to boot and ask."));

    // Who has a token, and how old it is.
    var enrolled = S.devices.filter(function (d) { return d.enrolled; });
    body.push(el("p", "dev-card-id", "boards with a token"));
    if (!enrolled.length) {
      body.push(el("p", "iot-empty", "None yet."));
    } else {
      var list = el("div", "dev-list");
      enrolled.forEach(function (d) {
        var fresh = !!d.online;        // the server's answer, not ours
        var card = el("div", "dev-card " + (fresh ? "is-online" : "is-stale"));
        var top = el("div", "dev-card-top");
        top.appendChild(el("span", "dev-card-name", d.name || d.id));
        top.appendChild(Z.Badge({ text: fresh ? "online" : "offline",
                                  tone: fresh ? "ok" : "warn" }));
        card.appendChild(top);
        var meta = el("div", "dev-card-meta");
        [["enrolled", Z.ago(d.enrolled)], ["last report", Z.ago(d.last_seen)],
         ["address", (d.state || {}).ip || d.ip]].forEach(function (pair) {
          if (pair[1]) meta.appendChild(el("span", null, pair[0] + " " + pair[1]));
        });
        card.appendChild(meta);
        card.addEventListener("click", function () {
          setTab("devices"); selectDevice(d.id);
        });
        card.classList.add("is-clickable");
        list.appendChild(card);
      });
      body.push(list);
    }

    // Boards that asked and were turned away: the usual reason a flash
    // "just failed".
    var refused = dev.refusals || [];
    body.push(el("p", "dev-card-id", "turned away"));
    if (!refused.length) {
      body.push(el("p", "iot-empty",
        "Nothing has been refused since this console started."));
    } else {
      var rl = el("div", "dev-list");
      refused.slice().reverse().forEach(function (r) {
        var card = el("div", "dev-card is-stale");
        var top = el("div", "dev-card-top");
        top.appendChild(el("span", "dev-card-name", r.device || "unknown device"));
        top.appendChild(Z.Badge({ text: "refused", tone: "critical" }));
        card.appendChild(top);
        card.appendChild(el("div", "dev-card-id", r.reason));
        var meta = el("div", "dev-card-meta");
        [["at", clock(r.at)], ["from", r.ip]].forEach(function (pair) {
          if (pair[1]) meta.appendChild(el("span", null, pair[0] + " " + pair[1]));
        });
        card.appendChild(meta);
        rl.appendChild(card);
      });
      body.push(rl);
    }

    body.push(hintNode(
      "The window lives in memory, so restarting the console closes it."));

    function hold(seconds, label) {
      return Z.Button({
        label: label, size: "sm",
        disabled: S.busy || !S.config.iot_net,
        onClick: function () {
          post("/api/iot/enrollment/open", { seconds: seconds })
            .then(function () {
              S.notice = notice("enrollment", "ok", "open for " + label.toLowerCase());
            })
            .catch(function (e) { S.notice = notice("enrollment", "critical", e.message); })
            .then(poll);
        }
      });
    }

    var acts = open
      ? [Z.Button({
          label: "Close", size: "sm", variant: "danger",
          disabled: S.busy || !S.config.iot_net,
          onClick: function () {
            post("/api/iot/enrollment/close")
              .then(function () { S.notice = null; })
              .catch(function (e) { S.notice = notice("enrollment", "critical", e.message); })
              .then(poll);
          }
        }), hold(900, "15 min")]
      : [hold(300, "Open"), hold(900, "15 min")];

    var w = Z.Widget({
      title: "Enrollment",
      subtitle: "per-device tokens · opens itself when you flash",
      actions: acts,
      state: open ? "warn" : "idle",
      stateLabel: open ? "open" : "closed",
      children: body
    });
    return [[w, 12]];
  }

  /* ---------- the setup workflow ----------------------------------------- */
  /* The setup steps in the order you do them; finished ones collapse. */
  function stepRow(n, opts) {
    var open = opts.done ? !!S.steps[opts.key] : true;
    var box = el("section", "iot-step" + (opts.done ? " is-done" : "") +
                            (open ? " is-open" : ""));
    var head = el("button", "iot-step-head");
    head.type = "button";
    head.setAttribute("aria-expanded", open ? "true" : "false");
    head.appendChild(el("span", "iot-step-n", opts.done ? "✓" : String(n)));
    head.appendChild(el("span", "iot-step-title", opts.title));
    head.appendChild(el("span", "iot-step-sum", opts.summary || ""));
    if (opts.done) {
      head.appendChild(el("span", "iot-step-caret", open ? "−" : "+"));
      head.addEventListener("click", function () {
        S.steps[opts.key] = !S.steps[opts.key];
        render();
      });
    } else {
      head.disabled = true;
    }
    box.appendChild(head);
    if (open && opts.body) {
      var body = el("div", "iot-step-body");
      (Array.isArray(opts.body) ? opts.body : [opts.body]).forEach(function (c) {
        if (c) body.appendChild(c);
      });
      box.appendChild(body);
    }
    return box;
  }

  function setupTab() {
    var r = (S.status || {}).radio || {};
    var st = apState();
    var dev = ((S.status || {}).devices) || {};
    var blocked = (r.rfkill || []).some(function (x) {
      return x.soft_blocked || x.hard_blocked;
    });
    var iface = (r.interfaces || [])[0];
    var wrap = el("div", "iot-steps");

    // 1 — the radio
    wrap.appendChild(stepRow(1, {
      key: "radio", title: "The radio",
      done: !!r.available && !blocked,
      summary: !r.phy ? "no wireless found"
        : (blocked ? "blocked" : "on · " + (iface ? iface.name : "?") +
           " · " + ((r.phy.modes || []).indexOf("AP") >= 0 ? "can host" : "cannot host")),
      body: radioWidget()
    }));

    // 2 — the network
    wrap.appendChild(stepRow(2, {
      key: "network", title: "The network",
      done: !!st.up,
      summary: !st.configured ? "not set up yet"
        : (st.up ? [st.ssid, st.channel ? "ch " + st.channel : null, st.gateway,
                    (st.clients || []).length + " connected"]
                     .filter(Boolean).join(" · ")
                 : st.ssid + " · stopped"),
      body: networkWidget()
    }));

    // 3 — enrollment, which now runs itself
    var win = dev.enrollment;
    wrap.appendChild(stepRow(3, {
      key: "enroll", title: "Letting boards join",
      done: true,
      summary: win ? "open · closes in " + countdown(win.expires)
                   : "automatic — opens itself when you flash",
      body: [el("p", "iot-note",
        win ? "The window is open, so a board that asks to join right now is "
              + "accepted and given its own token."
            : "There is nothing to do here. Flashing a board opens the window "
              + "for it and closes it again afterwards. The Enrollment tab has "
              + "the detail, and the buttons, if you want to open one by hand.")]
    }));

    // 4 — a board
    var scanBtn = Z.Button({
      label: S.scanning ? "Scanning…" : "Scan USB",
      variant: "primary", size: "sm", disabled: S.scanning,
      onClick: function () { doScan(true); }
    });
    var four = el("div", "iot-step-inner");
    four.appendChild(el("p", "iot-note",
      S.devices.length
        ? "Plug another board in and scan for it, or open the Devices tab."
        : "Plug a board into USB and scan. The scan names the chip, you make a "
          + "config for it, and flashing comes from that config."));
    var fourActs = el("div", "iot-form-acts");
    fourActs.appendChild(scanBtn);
    if (S.devices.length) {
      fourActs.appendChild(Z.Button({
        label: "Devices ›", size: "sm",
        onClick: function () { setTab("devices"); }
      }));
    }
    four.appendChild(fourActs);
    wrap.appendChild(stepRow(4, {
      key: "board", title: "A board", done: S.devices.length > 0,
      /* Flashed and enrolled are different counts: a board flashed a minute
         ago has not enrolled yet. */
      summary: S.devices.length
        ? [S.devices.length + " configured",
           S.devices.filter(function (d) { return d.flashed; }).length + " flashed",
           S.devices.filter(function (d) { return d.enrolled; }).length + " enrolled",
           S.devices.filter(function (d) { return d.flashed && !d.enrolled; }).length
             ? S.devices.filter(function (d) { return d.flashed && !d.enrolled; }).length
               + " waiting to join" : null].filter(Boolean).join(" · ")
        : "none yet",
      body: four
    }));

    // 5 — a flow on it
    var running = S.devices.filter(function (d) { return d.flow; });
    var five = el("div", "iot-step-inner");
    var boards = {};
    S.devices.forEach(function (d) { if (d.board) boards[d.board] = true; });
    var usable = S.flows.filter(function (f) { return f.board && boards[f.board]; });
    five.appendChild(el("p", "iot-note", usable.length
      ? "Pick a flow on a device in the Devices tab and deploy it. It runs from "
        + "the board's own flash and keeps running when this console is away."
      : "No flow is written for a board you have configured. Make one in Flow "
        + "Studio and set its board, and it becomes deployable here."));
    var fiveActs = el("div", "iot-form-acts");
    fiveActs.appendChild(Z.Button({
      label: "Flow Studio", size: "sm",
      onClick: function () { location.href = "/flows"; }
    }));
    if (S.devices.length) {
      fiveActs.appendChild(Z.Button({
        label: "Devices ›", size: "sm",
        onClick: function () { setTab("devices"); }
      }));
    }
    five.appendChild(fiveActs);
    wrap.appendChild(stepRow(5, {
      key: "flow", title: "A flow on it", done: running.length > 0,
      summary: running.length
        ? running.length + " running · " +
          running.map(function (d) { return d.name; }).join(", ")
        : (usable.length ? usable.length + " deployable" : "nothing to deploy yet"),
      body: five
    }));

    // 6 — a controller, which is its own branch: nothing above it needs
    // Bluetooth, and a fleet that never drives anything never comes here.
    var bt = S.bt || {};
    var adapter = bt.adapter || {};
    var padsHere = (bt.devices || []).filter(function (d) { return d.pad; });
    var joined = padsHere.filter(function (d) { return d.connected; });
    var onBoards = (S.devices || []).filter(function (d) {
      return d.pad && d.pad.connected;
    });
    var six = el("div", "iot-step-inner");
    six.appendChild(el("p", "iot-note",
      "Only needed to drive something from a gamepad. A controller can be "
      + "paired to this host, which reads it through the kernel, or to a field "
      + "device, which holds its own BLE link and keeps working with no console "
      + "at all \u2014 that second one is the whole reason a board can have a "
      + "deadman."));
    if (!adapter.present) {
      six.appendChild(el("p", "iot-note",
        "No adapter on this host. A field device can still pair its own."));
    }
    var sixActs = el("div", "iot-form-acts");
    sixActs.appendChild(Z.Button({
      label: "Bluetooth \u203a", size: "sm",
      onClick: function () { setTab("bluetooth"); }
    }));
    six.appendChild(sixActs);
    wrap.appendChild(stepRow(6, {
      key: "controller", title: "A controller",
      done: joined.length > 0 || onBoards.length > 0,
      summary: joined.length || onBoards.length
        ? [joined.length ? joined.length + " on this host" : null,
           onBoards.length ? onBoards.length + " on a board" : null]
            .filter(Boolean).join(" · ")
        : (adapter.present ? "optional \u00b7 nothing paired" : "optional \u00b7 no adapter"),
      body: six
    }));

    var w = Z.Widget({
      title: "Setup",
      subtitle: "in the order you actually do it",
      state: st.up ? (S.devices.length ? "ok" : "warn") : "idle",
      stateLabel: st.up ? (running.length ? "running" : "network up") : "not on the air",
      flush: true,
      children: [wrap]
    });
    return [[w, 12]];
  }

  /* ---------- tabs -------------------------------------------------------- */
  var TABS = [
    { id: "setup", label: "Setup" },
    { id: "devices", label: "Devices" },
    { id: "bluetooth", label: "Bluetooth" },
    { id: "enrollment", label: "Enrollment" },
    { id: "flashing", label: "Flashing" },
    { id: "boards", label: "Boards" }
  ];

  function tabBadge(id) {
    var dev = ((S.status || {}).devices) || {};
    if (id === "devices" && S.devices.length) {
      return { text: String(S.devices.length), tone: "idle" };
    }
    if (id === "bluetooth") {
      var bt = S.bt || {};
      if ((bt.scan || {}).running) return { text: "scanning", tone: "warn" };
      if (bt.connected) return { text: String(bt.connected), tone: "ok" };
      if (bt.paired) return { text: String(bt.paired), tone: "idle" };
      return null;
    }
    if (id === "enrollment" && dev.enrollment) return { text: "open", tone: "warn" };
    if (id === "flashing" && S.flash.running) return { text: "running", tone: "warn" };
    return null;
  }

  function setTab(id) {
    if (S.tab === id) return;
    S.tab = id;
    S.mounted = null;              // the next render rebuilds from scratch
    S.notice = null;
    try { localStorage.setItem("zero2w-iot-tab", id); } catch (e) {}
    if (location.hash !== "#" + id) {
      history.replaceState(null, "", "#" + id);
    }
    render();
  }

  function tabStrip() {
    var nav = el("div", "iot-tabs");
    nav.setAttribute("role", "tablist");
    TABS.forEach(function (t, i) {
      var b = el("button", "iot-tab" + (t.id === S.tab ? " is-active" : ""));
      b.type = "button";
      b.id = "iot-tab-" + t.id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", t.id === S.tab ? "true" : "false");
      b.tabIndex = t.id === S.tab ? 0 : -1;
      b.appendChild(el("span", null, t.label));
      var badge = tabBadge(t.id);
      if (badge) b.appendChild(Z.Badge({ text: badge.text, tone: badge.tone }));
      b.addEventListener("click", function () { setTab(t.id); });
      b.addEventListener("keydown", function (ev) {
        var step = ev.key === "ArrowRight" ? 1 : (ev.key === "ArrowLeft" ? -1 : 0);
        if (!step) return;
        ev.preventDefault();
        var next = TABS[(i + step + TABS.length) % TABS.length];
        setTab(next.id);
        var moved = document.getElementById("iot-tab-" + next.id);
        if (moved) moved.focus();
      });
      nav.appendChild(b);
    });
    return nav;
  }

  /* ---------- page ------------------------------------------------------- */
  function render() {
    var grid = document.getElementById("grid");

    /* The flashing tab owns its own DOM. Moving a scrolled element to a new
       parent resets its scrollTop, so once it is mounted it is updated in
       place and never rebuilt. */
    if (S.tab === "flashing" && S.mounted === "flashing" && !S.error) {
      return updateFlashing();
    }

    grid.textContent = "";
    S.mounted = null;
    if (S.error) {
      var w = Z.Widget({
        title: "IOT", subtitle: "/api/iot/status", state: "critical", stateLabel: "error",
        children: [el("p", "iot-empty", S.error)]
      });
      w.classList.add("span-12");
      grid.appendChild(w);
      return;
    }

    var strip = tabStrip();
    strip.classList.add("span-12");
    grid.appendChild(strip);

    var rows = S.tab === "setup" ? setupTab()
      : S.tab === "devices" ? devicesTab()
      : S.tab === "bluetooth" ? bluetoothTab()
      : S.tab === "enrollment" ? enrollmentTab()
      : S.tab === "flashing" ? flashingTab()
      : boardsTab();
    rows.forEach(function (row) {
      row[0].classList.add("span-" + row[1]);
      grid.appendChild(row[0]);
    });
    S.mounted = S.tab;
  }

  function buildTopbar() {
    var bar = document.getElementById("topbar");
    if (!bar) return;
    bar.textContent = "";
    var id = el("div", "topbar-id");
    id.appendChild(el("span", "topbar-host", "iot"));
    id.appendChild(el("span", "topbar-model", location.host));
    bar.appendChild(id);
  }

  function doScan(jump) {
    S.scanning = true; S.notice = null; render();
    api("/api/iot/scan")
      .then(function (r) {
        S.scan = r;
        if (jump && (r.ports || []).length) { S.mounted = null; S.tab = "devices"; }
      })
      .catch(function (e) { S.notice = notice("devices", "critical", e.message); })
      .then(function () { S.scanning = false; render(); });
  }

  /* ---------- the stream -------------------------------------------------- */
  /* The flasher publishes every line it reads onto the SSE bus; listening
     beats re-fetching the whole log. */
  function openStream() {
    if (S.stream) return;
    try {
      S.stream = new EventSource("/api/stream");
    } catch (e) { return; }
    S.stream.addEventListener("flash", function (ev) {
      try {
        var row = JSON.parse(ev.data);
      } catch (e) { return; }
      S.flash.log.push(row);
      if (S.flash.log.length > 400) S.flash.log = S.flash.log.slice(-400);
      if (!S.flash.running) { S.flash.running = true; }
      if (S.tab === "flashing" && S.log.list && S.log.synced) {
        appendLog([row]);
        setFlashState();
      }
    });
    S.stream.addEventListener("open", function () {
      // Whatever arrived while we were away is in the poll; resync from it.
      S.log.synced = false;
    });
    S.stream.onerror = function () {
      // Fall back to the poll until it comes back.
      S.log.synced = false;
    };
  }
  function streamLive() {
    return !!S.stream && S.stream.readyState === 1;
  }

  function poll() {
    var wants = [api("/api/iot/status"), api("/api/iot/devices"), api("/api/config"),
                 api("/api/iot/boards"), api("/api/flows"),
                 api("/api/bt").catch(function () { return null; })];
    // With the stream up, the log arrives line by line and re-fetching it only
    // invites a race between the two.
    wants.push(streamLive() && S.log.synced
      ? Promise.resolve(null)
      : api("/api/iot/flash"));
    wants.push(S.selected
      ? api("/api/iot/devices/" + encodeURIComponent(S.selected) + "/pins")
          .catch(function () { return null; })
      : Promise.resolve(null));

    Promise.all(wants)
      .then(function (res) {
        S.status = res[0];
        S.devices = res[1].devices || [];
        S.groups = res[1].groups || [];
        S.config = res[2] || S.config;
        S.boards = (res[3] || {}).boards || [];
        S.flows = (res[4] || {}).flows || [];
        if (res[5]) S.bt = res[5];
        if (res[6]) S.flash = res[6];
        if (S.selected) S.pins = res[7];
        S.error = null;
        chrome(true);
        render();
      })
      .catch(function (e) {
        S.error = e.message === "HTTP 401"
          ? "Not signed in. Open this page with ?t=<token> once."
          : "Could not read /api/iot/status: " + e.message;
        chrome(false);
        render();
      });
  }

  /* The dock says whether this page is still hearing from the board. It used
     to read "connecting" for as long as the tab was open. */
  function chrome(live) {
    if (!S.nav) return;
    S.nav.setClock(new Date().toTimeString().slice(0, 8));
    S.nav.setState(live ? "ok" : "critical", live ? "live" : "no stream", live);
  }

  function tick() {
    if (S.flash.running) return poll();        // the log is the point
    if (!S.form && !S.draft && !S.busy && !S.scanning) poll();
  }

  function start() {
    var hash = (location.hash || "").replace("#", "");
    var saved = null;
    try { saved = localStorage.getItem("zero2w-iot-tab"); } catch (e) {}
    var want = hash || saved;
    if (TABS.some(function (t) { return t.id === want; })) S.tab = want;

    buildTopbar();
    S.nav = window.Zero2WNav.mount("dock", "/iot");
    render();
    poll();
    openStream();
    S.timer = setInterval(tick, POLL_MS);

    window.addEventListener("hashchange", function () {
      var id = (location.hash || "").replace("#", "");
      if (TABS.some(function (t) { return t.id === id; })) setTab(id);
    });
    document.addEventListener("visibilitychange", function () {
      // Nothing here is worth waking a passively cooled board for.
      if (document.hidden) { clearInterval(S.timer); S.timer = null; }
      else if (!S.timer) { poll(); S.timer = setInterval(tick, POLL_MS); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
