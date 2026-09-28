/* Flow Studio — a node editor for GPIO and network automation on this board.
   Nodes and ports are rendered from the server's node registry, so the editor
   and the runtime can never disagree about what a node type looks like. */
(function () {
  "use strict";

  var Z = window.Zero2W;
  var NODE_W = 184, PORT_TOP = 20, PORT_STEP = 18, NODE_H_DEFAULT = 76;

  /* A logical port (in / out / true / false) is separate from the physical
     anchors it can be reached from. Inputs may be entered from the left or the
     top, outputs may leave from the right or the bottom, and every port gets an
     anchor on each side its direction allows. Triggers get no input anchors. */
  var IN_SIDES = ["left", "top"];
  var OUT_SIDES = ["right", "bottom"];
  var DIRS = { left: [-1, 0], right: [1, 0], top: [0, -1], bottom: [0, 1] };
  var SVGNS = "http://www.w3.org/2000/svg";

  var S = {
    registry: {}, doc: { flows: [] }, flowId: null,
    sel: null,               // {kind:"node"|"link", id}
    activeSteps: {},         // "flow/node" -> true while a Step is active
    pins: null, pinSel: null,
    rev: 0, conflict: null,
    view: { x: 0, y: 0, k: 1 }, pointers: {}, nodeH: {}, ghost: null,
    drag: null, link: null, pan: null,
    runs: [], config: { gpio_write: true }, dirty: false,
    hw: null, undo: [], redo: [], applying: false, openMenu: null, pinSig: null, libOpen: false,
    linkPop: false,          // the connection panel under the device chip
    costs: {},               // flow id -> what deploying it would make a board fetch
    variables: [], lastField: null,
    outputs: {},       // "flow/node" -> what that node last produced
    tags: [],          // the tag table, with what each one holds
    tagEdit: null,     // the tag whose definition is open for editing
    examples: [],      // the catalogue, fetched when first opened
    registryAll: {},   // every node type, for rendering what is already there
    boardNodes: {},    // board -> the node types it can run, for the flow list
    boards: [],        // board profiles, for "runs on"
    devices: [],       // IOT configs, for "link to"
    profile: null,     // the full profile of the open flow's board
    devstate: null,    // what the linked device last reported about itself
    mode: "index", sort: "title", filter: "", pinch: null
  };

  /* Snapshot the whole document before a mutation. Cheap at this size, and it
     makes undo exact rather than an inverse-operation guess. */
  function snapshot() {
    if (S.applying) return;
    S.undo.push(JSON.stringify(S.doc));
    if (S.undo.length > 60) S.undo.shift();
    S.redo.length = 0;
  }
  function restore(stackFrom, stackTo) {
    if (!stackFrom.length) return;
    stackTo.push(JSON.stringify(S.doc));
    var prev = stackFrom.pop();
    S.applying = true;
    try {
      S.doc = JSON.parse(prev);
      if (!S.doc.flows.some(function (f) { return f.id === S.flowId; })) {
        S.flowId = S.doc.flows.length ? S.doc.flows[0].id : null;
      }
      S.sel = null; S.dirty = true;
      renderBar(); renderCanvas(); renderSide();
    } finally { S.applying = false; }
  }
  function undo() { restore(S.undo, S.redo); }
  function redo() { restore(S.redo, S.undo); }

  /* ---------- tiny helpers --------------------------------------------- */
  function el(t, c, x) { var n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; }
  function sv(t, a) { var n = document.createElementNS(SVGNS, t); for (var k in a) if (a[k] != null) n.setAttribute(k, a[k]); return n; }
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) {
        var err = new Error(b.error || ("HTTP " + r.status));
        err.status = r.status;   // a 409 is a conflict to resolve, not a failure
        err.body = b;
        throw err;
      });
      return r.json();
    });
  }
  function uid(p) { return p + "_" + Math.random().toString(36).slice(2, 8); }
  function boardLabel(id) {
    for (var i = 0; i < S.boards.length; i++) {
      if (S.boards[i].board === id) return S.boards[i].label;
    }
    return id;
  }
  function flow() { return S.doc.flows.filter(function (f) { return f.id === S.flowId; })[0] || null; }
  function def(type) {
    return S.registryAll[type] || S.registry[type] ||
      { label: type, inputs: ["in"], outputs: ["out"], fields: [], series: "series-1" };
  }

  function nodeHeight(n) { return S.nodeH[n.id] || NODE_H_DEFAULT; }

  /* Every anchor a node exposes, as offsets from its top-left corner. */
  function anchorsFor(n) {
    var d = def(n.type);
    var h = nodeHeight(n);
    var out = [];
    /* A port is a name, or {name, side} when the node wants it on one
       particular side — which is how a node with two inputs can say which is
       which. A plain name still gets an anchor on both of its allowed sides. */
    function place(ports, sides, kind) {
      if (!ports.length) return;
      sides.forEach(function (side) {
        // Only the ports that belong on this side, and spaced among
        // themselves: the offsets below divide by how many share the edge.
        var here = ports.filter(function (p) {
          return !p || typeof p === "string" || !p.side || p.side === side;
        });
        var count = here.length;
        here.forEach(function (p, i) {
          var name = typeof p === "string" ? p : p.name;
          var ox, oy;
          if (side === "left" || side === "right") {
            ox = side === "left" ? 0 : NODE_W;
            oy = PORT_TOP + i * PORT_STEP;
          } else {
            ox = ((i + 1) * NODE_W) / (count + 1);
            oy = side === "top" ? 0 : h;
          }
          out.push({ port: name, kind: kind, side: side, ox: ox, oy: oy,
                     index: ports.indexOf(p),
                     // What to call it on the canvas. A port can name a config
                     // key instead, so the label says what it currently does.
                     label: (p && p.label_from
                             && (n.config || {})[p.label_from]) || (p && p.label) || name });
        });
      });
    }
    place(d.inputs || [], IN_SIDES, "in");
    place(d.outputs || [], OUT_SIDES, "out");
    return out;
  }

  /* While a connection is being dragged, an anchor that cannot legally accept
     it is hidden rather than shown and refused: outputs cannot receive, a node
     cannot feed itself, and a link that already exists cannot be made twice. */
  function anchorLocked(nodeId, kind, port) {
    if (!S.link) return false;
    // A drag from an output seeks an input, and vice versa.
    var want = S.link.dir === "in" ? "out" : "in";
    if (kind !== want) return true;
    if (nodeId === S.link.from) return true;             // no self-connection
    var f = flow();
    if (!f) return false;
    var from = S.link.dir === "in" ? nodeId : S.link.from;
    var fromPort = S.link.dir === "in" ? port : S.link.port;
    var to = S.link.dir === "in" ? S.link.from : nodeId;
    var toPort = S.link.dir === "in" ? S.link.port : port;
    return (f.edges || []).some(function (e) {
      return e.from === from && (e.fromPort || "out") === fromPort &&
             e.to === to && (e.toPort || "in") === toPort;
    });
  }

  /* Hiding the illegal anchors by re-rendering tore out the very element that
     received the pointerdown, which releases implicit pointer capture and can fire
     pointercancel — killing the drag the instant it began. Toggle classes on the
     live elements instead: no teardown, no flicker, far cheaper. */
  function applyLockState() {
    var host = document.getElementById("nodes");
    if (!host) return;
    Array.prototype.forEach.call(host.querySelectorAll(".port"), function (dot) {
      var locked = anchorLocked(dot.dataset.node, dot.dataset.kind, dot.dataset.port);
      dot.classList.toggle("is-locked", locked);
      dot.classList.toggle("is-target", !locked && !!S.link);
    });
    Array.prototype.forEach.call(host.querySelectorAll(".port-tag"), function (tag) {
      tag.classList.toggle("is-locked", !!S.link);
    });
  }

  function clearLockState() {
    var host = document.getElementById("nodes");
    if (!host) return;
    Array.prototype.forEach.call(host.querySelectorAll(".port, .port-tag"), function (n) {
      n.classList.remove("is-locked", "is-target");
    });
  }

  /* Absolute canvas position of one anchor, for drawing links. */
  function anchorPoint(n, port, side, kind) {
    var list = anchorsFor(n);
    for (var i = 0; i < list.length; i++) {
      var a = list[i];
      if (a.port === port && a.kind === kind && (!side || a.side === side)) {
        return { x: n.x + a.ox, y: n.y + a.oy, side: a.side };
      }
    }
    // the recorded side no longer exists (node type changed): fall back
    for (var j = 0; j < list.length; j++) {
      if (list[j].kind === kind) {
        return { x: n.x + list[j].ox, y: n.y + list[j].oy, side: list[j].side };
      }
    }
    return null;
  }

  /* ---------- flow index ------------------------------------------------ */
  function triggersOf(f) {
    var seen = {};
    (f.nodes || []).forEach(function (n) {
      var d = def(n.type);
      if (d.kind === "trigger") seen[n.type] = d.label;
    });
    return Object.keys(seen).map(function (k) { return { type: k, label: seen[k] }; });
  }
  function manualNode(f) {
    return (f.nodes || []).filter(function (n) { return n.type === "manual.fire"; })[0] || null;
  }
  function pinsOf(f) {
    var out = [];
    (f.nodes || []).forEach(function (n) {
      var g = (n.config || {}).gpio;
      if (g == null) return;
      var p = pinByGpio(g);
      var label = p ? String(p.phys) : ("gpio" + g);
      if (out.indexOf(label) === -1) out.push(label);
    });
    return out;
  }

  /* The dropdowns still waiting for a choice: a pin, a device, a flow. The
     examples arrive with these empty on purpose, and so does a new node, so the
     node and its card say what is left rather than the run log saying later
     that nothing happened. A field whose empty means something (Device event's
     "any device") is not waiting for anything. */
  var PICKED_FROM = ["devices", "ble_devices", "flows", "nodes_in_flow"];
  function unchosen(n) {
    var cfg = n.config || {};
    return (def(n.type).fields || []).filter(function (fl) {
      var v = cfg[fl.key];
      if (v !== null && v !== undefined && String(v).trim() !== "") return false;
      if (fl.blank_ok || !fieldApplies(n, fl)) return false;
      return fl.kind === "pin" ||
        (fl.kind === "combo" && PICKED_FROM.indexOf(fl.options_from) !== -1);
    }).map(function (fl) { return fl.label.toLowerCase(); });
  }
  function unchosenIn(f) {
    return (f.nodes || []).filter(function (n) { return unchosen(n).length; }).length;
  }

  /* Enabling is allowed — it is the author's flow — but not in silence. */
  function warnUnchosen(f) {
    var first = (f.nodes || []).filter(function (n) { return unchosen(n).length; })[0];
    if (!first) return;
    toast(def(first.type).label + " still needs a " + unchosen(first)[0] +
          (unchosenIn(f) > 1 ? ", and " + (unchosenIn(f) - 1) + " more node" +
           (unchosenIn(f) > 2 ? "s" : "") + " need setting up" : ""), "warn");
  }

  function sortedFlows() {
    var list = S.doc.flows.slice();
    var q = S.filter.trim().toLowerCase();
    if (q) {
      list = list.filter(function (f) {
        var hay = (f.name || "") + " " + triggersOf(f).map(function (t) { return t.label; }).join(" ");
        return hay.toLowerCase().indexOf(q) !== -1;
      });
    }
    var by = S.sort;
    list.sort(function (a, b) {
      if (by === "trigger") {
        var ta = (triggersOf(a)[0] || {}).label || "~";
        var tb = (triggersOf(b)[0] || {}).label || "~";
        if (ta !== tb) return ta.localeCompare(tb);
      } else if (by === "status") {
        var ea = a.enabled === false ? 1 : 0, eb = b.enabled === false ? 1 : 0;
        if (ea !== eb) return ea - eb;
      } else if (by === "size") {
        var na = (a.nodes || []).length, nb = (b.nodes || []).length;
        if (na !== nb) return nb - na;
      }
      return (a.name || "").localeCompare(b.name || "");
    });
    return list;
  }

  function renderIndex() {
    var host = document.getElementById("index-view");
    host.innerHTML = "";

    var tools = el("div", "index-tools");
    var search = el("input");
    search.type = "search";
    search.placeholder = "filter flows";
    search.value = S.filter;
    search.setAttribute("aria-label", "filter flows");
    search.addEventListener("input", function () { S.filter = search.value; renderList(); });
    tools.appendChild(search);

    var sort = el("select");
    sort.setAttribute("aria-label", "sort flows");
    [["title", "Sort: title"], ["trigger", "Sort: trigger"],
     ["status", "Sort: enabled first"], ["size", "Sort: size"]].forEach(function (o) {
      var op = el("option", null, o[1]);
      op.value = o[0];
      if (S.sort === o[0]) op.selected = true;
      sort.appendChild(op);
    });
    sort.addEventListener("change", function () { S.sort = sort.value; renderList(); });
    tools.appendChild(sort);
    // Tags are the board's, not a flow's, so they belong somewhere reachable
    // without opening one.
    tools.appendChild(Z.Button({
      label: "Tags" + (S.tags.length ? " (" + S.tags.length + ")" : ""),
      size: "sm", variant: S.tagsOpen ? "primary" : "secondary",
      onClick: function () { S.tagsOpen = !S.tagsOpen; renderIndex(); }
    }));
    var ex = Z.Button({ label: "Examples", size: "sm" });
    attachMenu(ex, buildExampleMenu, "is-examples");
    tools.appendChild(ex);
    tools.appendChild(Z.Button({ label: "New flow", variant: "primary", size: "sm", onClick: newFlow }));
    host.appendChild(tools);

    if (S.tagsOpen) {
      var tags = el("div", "index-tags");
      tags.appendChild(tagPanel());
      host.appendChild(tags);
    }

    var list = el("div", "flow-list");
    list.id = "flow-list";
    host.appendChild(list);
    renderList();
  }

  function renderList() {
    if (S.openMenu) S.openMenu();
    var list = document.getElementById("flow-list");
    if (!list) return;
    var host = document.getElementById("index-view");
    var scroll = host ? host.scrollTop : 0;
    list.innerHTML = "";
    var flows = sortedFlows();
    if (!flows.length) {
      list.appendChild(el("div", "index-empty",
        S.doc.flows.length ? "No flow matches that filter." : "No flows yet. Create one to begin."));
      return;
    }
    flows.forEach(function (f) { list.appendChild(flowCard(f)); });
    if (host) host.scrollTop = scroll;
  }

  /* A dropdown anchored to a button but rendered on <body> in viewport
     coordinates, so no scrolling ancestor can clip it. buildItems() returns
     the elements to show, and is called fresh on every open. */
  function attachMenu(btn, buildItems, cls) {
    var menu = el("div", "card-menu" + (cls ? " " + cls : ""));
    menu.setAttribute("role", "menu");

    function close() {
      if (menu.parentNode) menu.parentNode.removeChild(menu);
      btn.setAttribute("aria-expanded", "false");
      if (S.openMenu === close) S.openMenu = null;
      document.removeEventListener("click", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", onScroll, true);
    }
    function onDoc(ev) { if (!menu.contains(ev.target) && ev.target !== btn) close(); }
    // A capture-phase scroll listener also fires for scrolling INSIDE the
    // menu, which closed the variable list the moment you tried to scroll it.
    function onScroll(ev) { if (!menu.contains(ev.target)) close(); }
    function onKey(ev) { if (ev.key === "Escape") { close(); btn.focus(); } }

    function place() {
      var r = btn.getBoundingClientRect(), gap = 4, margin = 8;
      menu.style.visibility = "hidden";
      menu.style.left = "0px";
      menu.style.top = "0px";
      document.body.appendChild(menu);
      var mw = menu.offsetWidth, mh = menu.offsetHeight;
      var vw = document.documentElement.clientWidth;
      var vh = document.documentElement.clientHeight;
      var top = r.bottom + gap;
      if (top + mh > vh - margin) top = Math.max(margin, r.top - gap - mh);
      var left = r.right - mw;
      if (left < margin) left = margin;
      if (left + mw > vw - margin) left = Math.max(margin, vw - margin - mw);
      menu.style.left = Math.round(left) + "px";
      menu.style.top = Math.round(top) + "px";
      menu.style.visibility = "";
    }

    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      if (menu.parentNode) { close(); return; }
      if (S.openMenu) S.openMenu();
      menu.innerHTML = "";
      buildItems(menu, close);
      place();
      S.openMenu = close;
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("click", onDoc, true);
      document.addEventListener("keydown", onKey, true);
      window.addEventListener("resize", close);
      window.addEventListener("scroll", onScroll, true);
      var first = menu.querySelector(".card-menu-item");
      if (first) first.focus();
    });
    return { close: close };
  }

  function tick(label, on) { return on ? label + "  \u2713" : label; }
  function menuHeading(text) { return el("div", "card-menu-head", text); }
  function menuNote(text) { return el("div", "card-menu-note", text); }

  /* A flow bound to a board runs on that device, from its own flash, so the host
     stops arming it. Nodes the board cannot run are kept and flagged rather than
     deleted — losing someone's work to a dropdown is worse. */
  function setFlowBoard(f, board) {
    if ((f.board || null) === (board || null)) return;
    snapshot();
    f.board = board || null;
    if (!board) { f.device = null; }
    else if (f.device) {
      var linked = S.devices.filter(function (d) { return d.id === f.device; })[0];
      if (!linked || linked.board !== board) f.device = null;
    }
    markDirty(); save();
    if (S.flowId === f.id) loadBoard();
    renderList();
  }

  function setFlowDevice(f, id) {
    if ((f.device || null) === (id || null)) return;
    snapshot();
    f.device = id || null;
    markDirty(); save(); renderList();
  }

  /* Node types this flow's board cannot run. Checked against that board's
     own list: S.registry is only the open flow's, and checking every card
     against it flagged a camera flow's nodes after a motor flow was opened. */
  function unsupported(f) {
    if (!f || !f.board) return [];
    var nodes = boardNodes(f.board);
    if (!nodes) return [];
    return (f.nodes || []).filter(function (n) { return !nodes[n.type]; });
  }

  /* A board's node list, fetched once; the list redraws when it arrives. */
  function boardNodes(board) {
    var have = S.boardNodes[board];
    if (have === undefined) {
      S.boardNodes[board] = null;
      api("/api/flows/registry?board=" + encodeURIComponent(board))
        .then(function (d) { S.boardNodes[board] = d.nodes || {}; renderList(); })
        .catch(function () {});
    }
    return have || null;
  }

  function menuItem(label, fn, danger) {
    var b = el("button", "card-menu-item" + (danger ? " is-danger" : ""), label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
    return b;
  }

  function flowCard(f) {
    var on = f.enabled !== false;
    var card = el("div", "flow-card is-clickable " + (on ? "is-on" : "is-off"));
    card.title = "Open in the editor";

    // The card opens the editor, except where a real control lives. The name
    // is an input, so clicking it lands the caret and renames instead.
    card.addEventListener("click", function (ev) {
      if (ev.target.closest("input, button, select, textarea, a, .z-btn")) return;
      // Do not navigate out from under someone selecting text.
      var sel = window.getSelection && window.getSelection();
      if (sel && String(sel) .length) return;
      openEditor(f.id);
    });

    var top = el("div", "flow-card-top");
    top.appendChild(Z.StatusDot({ state: on ? "ok" : "idle", label: on ? "enabled" : "disabled" }));

    var name = el("span", "flow-card-name-text", f.name || "Untitled flow");

    function beginRename() {
      var input = el("input", "flow-card-name");
      input.value = f.name || "";
      input.setAttribute("aria-label", "flow name");
      function commit(saveIt) {
        var next = input.value.trim() || "Untitled flow";
        if (saveIt && next !== f.name) {
          snapshot(); f.name = next; markDirty(); save();
        }
        renderList();
      }
      input.addEventListener("keydown", function (ev) {
        ev.stopPropagation();
        if (ev.key === "Enter") { ev.preventDefault(); commit(true); }
        if (ev.key === "Escape") { ev.preventDefault(); commit(false); }
      });
      input.addEventListener("blur", function () { commit(true); });
      name.replaceWith(input);
      input.focus();
      input.select();
    }
    top.appendChild(name);
    card.appendChild(top);

    var trig = el("div", "flow-card-triggers");
    var ts = triggersOf(f);
    if (ts.length) {
      ts.forEach(function (t) { trig.appendChild(Z.Badge({ text: t.label, tone: on ? "ok" : "idle" })); });
      if (f.board) {
        var dev = S.devices.filter(function (d) { return d.id === f.device; })[0];
        trig.appendChild(Z.Badge({ text: boardLabel(f.board), tone: "idle" }));
        trig.appendChild(Z.Badge({
          text: dev ? dev.name : "not linked", tone: dev ? "ok" : "warn"
        }));
        var bad = unsupported(f).length;
        if (bad) {
          trig.appendChild(Z.Badge({
            text: bad + " node" + (bad === 1 ? "" : "s") + " this board cannot run",
            tone: "critical"
          }));
        }
      }
    } else {
      trig.appendChild(Z.Badge({ text: "no trigger", tone: "warn" }));
    }
    var todo = unchosenIn(f);
    if (todo) {
      trig.appendChild(Z.Badge({
        text: todo + " node" + (todo === 1 ? "" : "s") + " to set up", tone: "warn"
      }));
    }
    card.appendChild(trig);

    var meta = el("div", "flow-card-meta");
    meta.appendChild(el("span", null, (f.nodes || []).length + " nodes"));
    meta.appendChild(el("span", null, (f.edges || []).length + " links"));
    var pins = pinsOf(f);
    if (pins.length) meta.appendChild(el("span", null, "pins " + pins.join(", ")));
    meta.appendChild(idWithCopy(f.id, "flow id"));
    card.appendChild(meta);

    var acts = el("div", "flow-card-actions");
    acts.appendChild(Z.Button({
      label: on ? "Disable" : "Enable", size: "sm", variant: on ? "secondary" : "primary",
      onClick: function () {
        snapshot(); f.enabled = !on; markDirty(); save(); renderList();
        if (!on) warnUnchosen(f);
      }
    }));
    var man = manualNode(f);
    if (man) {
      acts.appendChild(Z.Button({
        label: "Run", size: "sm", variant: "ghost",
        onClick: function () { runNodeIn(f.id, man.id); }
      }));
    }
    acts.appendChild(overflowMenu(f, beginRename));
    card.appendChild(acts);
    return card;
  }

  /* The secondary actions live behind a single control so the card stays
     readable. Rename is here rather than inline, so a click on the title can
     mean "open this flow" like the rest of the card. */
  function overflowMenu(f, beginRename) {
    var wrap = el("div", "card-menu-wrap");
    var btn = el("button", "card-menu-btn", "\u22ef");
    btn.type = "button";
    btn.title = "More actions";
    btn.setAttribute("aria-label", "more actions for " + (f.name || f.id));
    attachMenu(btn, function (menu, close) {
      menu.appendChild(menuItem("Edit flow", function () { close(); openEditor(f.id); }));
      menu.appendChild(menuItem("Rename", function () { close(); beginRename(); }));
      menu.appendChild(menuItem("Duplicate", function () { close(); duplicate(f); }));
      menu.appendChild(menuHeading("Runs on"));
      menu.appendChild(menuItem(tick("This host", !f.board), function () {
        close(); setFlowBoard(f, null);
      }));
      S.boards.forEach(function (b) {
        menu.appendChild(menuItem(tick(b.label, f.board === b.board), function () {
          close(); setFlowBoard(f, b.board);
        }));
      });
      if (f.board) {
        menu.appendChild(menuHeading("Linked device"));
        var matches = S.devices.filter(function (d) { return d.board === f.board; });
        menu.appendChild(menuItem(tick("Not linked", !f.device), function () {
          close(); setFlowDevice(f, null);
        }));
        matches.forEach(function (d) {
          menu.appendChild(menuItem(tick(d.name, f.device === d.id), function () {
            close(); setFlowDevice(f, d.id);
          }));
        });
        if (!matches.length) {
          menu.appendChild(menuNote("no config for this board yet — make one on the IOT screen"));
        }
      }
      menu.appendChild(menuItem("Delete", function () { close(); deleteFlowById(f.id); }, true));
    });
    wrap.appendChild(btn);
    return wrap;
  }

  /* The catalogue, as a menu. Each one says what it demonstrates, because a
     list of names is no more useful than the list of nodes it is made of. */
  function buildExampleMenu(menu, close) {
    if (!S.examples.length) {
      menu.appendChild(menuNote("Loading…"));
      loadExamples().then(function () { if (S.openMenu) { close(); } });
      return;
    }
    menu.appendChild(menuHeading("Add an example"));
    menu.appendChild(menuNote(
      "They start simple and build up. Each arrives switched off with its "
      + "pins and devices empty: choose yours from the dropdowns, then "
      + "enable it."));
    var step = null;
    S.examples.forEach(function (ex) {
      if (ex.step_name && ex.step_name !== step) {
        step = ex.step_name;
        menu.appendChild(menuHeading(ex.step + " \u00b7 " + step));
      }
      var it = menuItem("", function () { close(); addExample(ex); });
      it.textContent = "";
      it.classList.add("var-item");
      var head = el("span", "var-head");
      head.appendChild(el("span", "var-name", ex.name));
      if (ex.board) head.appendChild(el("span", "var-type", boardLabel(ex.board)));
      it.appendChild(head);
      it.appendChild(el("span", "var-desc", ex.about));
      menu.appendChild(it);
    });
  }

  function loadExamples() {
    return api("/api/flows/examples").then(function (d) {
      S.examples = d.examples || [];
    }).catch(function () {});
  }

  /* Copied in under a fresh id, so an example can be added twice and edited
     freely without the catalogue changing under it. */
  function addExample(ex) {
    var copy = JSON.parse(JSON.stringify(ex));
    var id = uid("flow");
    var map = {};
    copy.nodes.forEach(function (n) { map[n.id] = uid("n"); n.id = map[n.id]; });
    copy.edges.forEach(function (e) {
      e.id = uid("e");
      e.from = map[e.from] || e.from;
      e.to = map[e.to] || e.to;
    });
    var flow = { id: id, name: ex.name, enabled: false,
                 nodes: copy.nodes, edges: copy.edges };
    if (ex.board) flow.board = ex.board;

    // An example that reads a tag needs the tag to exist, or it opens
    // pointing at nothing.
    var wanted = ex.needs_tags || [];
    var have = {};
    S.tags.forEach(function (t) { have[t.name] = true; });
    var missing = wanted.filter(function (t) { return !have[t.name]; });

    snapshot();
    S.doc.flows.push(flow);
    var done = function () {
      markDirty();
      save();
      openEditor(id);
      var todo = unchosenIn(flow);
      toast("added " + ex.name + (todo ? " \u2014 " + todo + " node" +
            (todo === 1 ? "" : "s") + " to set up" : ""), "ok");
    };
    if (!missing.length) return done();
    saveTags(S.tags.map(tagDef).concat(missing)).then(done);
  }

  function duplicate(f) {
    snapshot();
    var copy = JSON.parse(JSON.stringify(f));
    var map = {};
    copy.id = uid("flow");
    copy.name = (f.name || "flow") + " copy";
    copy.enabled = false;           // never start a clone running by surprise
    (copy.nodes || []).forEach(function (n) { var nid = uid("n"); map[n.id] = nid; n.id = nid; });
    (copy.edges || []).forEach(function (e) {
      e.id = uid("e"); e.from = map[e.from] || e.from; e.to = map[e.to] || e.to;
    });
    S.doc.flows.push(copy);
    markDirty(); save(); renderList();
  }
  function deleteFlowById(id) {
    var f = S.doc.flows.filter(function (x) { return x.id === id; })[0];
    if (!f) return;
    if (!window.confirm("Delete flow \u201c" + (f.name || f.id) + "\u201d? This cannot be undone.")) return;
    snapshot();
    S.doc.flows = S.doc.flows.filter(function (x) { return x.id !== id; });
    if (S.flowId === id) S.flowId = null;
    markDirty(); save(); renderList();
  }
  function runNodeIn(flowId, nodeId) {
    api("/api/flows/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow: flowId, node: nodeId })
    }).then(function () { toast("ran " + flowId, "ok"); })
      .catch(function (e) { toast(e.message, "critical"); });
  }

  function showFatal(msg) {
    var host = document.getElementById("index-view");
    if (!host) return;
    var b = el("div", "banner-line");
    b.appendChild(el("span", null, msg));
    host.insertBefore(b, host.firstChild);
    if (window.console && console.error) console.error(msg);
  }

  /* ---------- view routing ---------------------------------------------- */
  function setView(v) {
    S.mode = v;
    document.body.classList.toggle("view-index", v === "index");
    document.body.classList.toggle("view-editor", v === "editor");
    closeDrawers();
    if (v === "index") { location.hash = ""; renderIndex(); }
    else { location.hash = "#edit/" + S.flowId; }
    renderBar();
  }
  function openEditor(id, selectNode, selectLink) {
    S.flowId = id;
    S.sel = selectNode ? { kind: "node", id: selectNode }
          : selectLink ? { kind: "link", id: selectLink } : null;
    setView("editor");
    renderPalette(); renderCanvas(); renderSide(); renderFoot(); fitView();
    loadBoard();      // swaps the palette and the pin list for this flow's board
  }
  /* Drawers cover the whole height on a phone, including the buttons that
     opened them, so each needs its own way out. */
  function drawerClose() {
    var b = el("button", "drawer-close", "\u2715");
    b.type = "button";
    b.title = "Close";
    b.setAttribute("aria-label", "close panel");
    b.addEventListener("click", closeDrawers);
    return b;
  }

  function closeDrawers() {
    document.body.classList.remove("drawer-palette", "drawer-side");
  }

  /* ---------- top bar --------------------------------------------------- */
  function renderBar() {
    var bar = document.getElementById("studio-bar");
    bar.innerHTML = "";

    if (S.mode === "index") {
      bar.appendChild(el("span", "studio-title", "flows"));
      bar.appendChild(el("span", "spacer"));
      if (S.dirty) bar.appendChild(Z.Button({ label: "Save \u2022", variant: "primary", size: "sm", onClick: save }));
      return;
    }

    var f = flow();
    bar.appendChild(Z.Button({
      label: "\u2039 Flows", variant: "ghost", size: "sm",
      onClick: function () { setView("index"); }
    }));

    // Drawer toggles only appear on narrow screens (CSS controls visibility).
    var toggles = el("div", "drawer-toggles");
    toggles.appendChild(Z.Button({
      label: "Nodes", size: "sm",
      onClick: function () {
        document.body.classList.remove("drawer-side");
        document.body.classList.toggle("drawer-palette");
      }
    }));
    toggles.appendChild(Z.Button({
      label: "Inspect", size: "sm",
      onClick: function () {
        document.body.classList.remove("drawer-palette");
        document.body.classList.toggle("drawer-side");
      }
    }));
    bar.appendChild(toggles);

    if (f) {
      var name = el("input", "flow-name");
      name.value = f.name || "";
      name.setAttribute("aria-label", "flow name");
      // Not markDirty(): a full renderBar() would replace the element being
      // typed into and drop the caret on every keystroke.
      name.addEventListener("input", function () {
        f.name = name.value;
        S.dirty = true;
        syncSave();
      });
      bar.appendChild(name);
      /* The dot is the state; the button is the verb. A button labelled with
         the state reads "Enabled" to someone about to press it to enable. */
      var live = f.enabled !== false;
      bar.appendChild(Z.StatusDot({
        state: live ? "ok" : "idle", label: live ? "enabled" : "disabled"
      }));
      bar.appendChild(Z.Button({
        label: live ? "Disable" : "Enable",
        variant: live ? "secondary" : "primary", size: "sm",
        /* Saved on the click, the same as the identical button on the flows
           screen. This one only marked the document dirty, so pressing Disable in
           the studio did nothing until Save was pressed too. `save()` is also what
           pushes the reload that reaches the board. */
        onClick: function () {
          snapshot();
          f.enabled = !live;
          markDirty();
          save().then(function (ok) {
            if (ok) toast((f.enabled === false ? "disabled " : "enabled ")
                          + (f.name || f.id), "ok");
            if (ok && f.enabled !== false) warnUnchosen(f);
          });
        }
      }));
      var man = manualNode(f);
      if (man) {
        bar.appendChild(Z.Button({
          label: "Run", size: "sm", variant: "ghost",
          onClick: function () { runNodeIn(f.id, man.id); }
        }));
      }
      var dev = linkedDevice(f);
      if (dev) {
        var live = dev.flow === f.id;
        bar.appendChild(Z.Button({
          label: live ? "Redeploy" : "Deploy", size: "sm",
          variant: live ? "secondary" : "primary",
          onClick: function () {
            // Deploying sends what is on disk, so unsaved work goes first.
            if (!S.dirty) return deployTo(f);
            save({ noRedeploy: true }).then(function (ok) {
              if (ok) deployTo(f);
            });
          }
        }));
      }
      // Whether the hardware is actually there, without opening the inspector.
      var st = deviceStatus();
      if (st) {
        var chip = el("span", "device-chip is-" + st.tone);
        chip.appendChild(el("i", "device-dot"));
        chip.appendChild(el("span", null, st.short));
        chip.title = st.text;
        /* A popover, not the drawer, which a wide screen already shows. */
        chip.addEventListener("click", function (ev) {
          ev.stopPropagation();
          S.linkPop = !S.linkPop;
          renderBar();
        });
        var wrap = el("span", "chip-wrap");
        wrap.appendChild(chip);
        if (S.linkPop) wrap.appendChild(linkPanel(st));
        bar.appendChild(wrap);
      }
    }

    bar.appendChild(el("span", "spacer"));
    var ub = Z.Button({ label: "Undo", size: "sm", onClick: undo });
    if (!S.undo.length) { ub.disabled = true; ub.setAttribute("aria-disabled", "true"); }
    bar.appendChild(ub);
    var rb = Z.Button({ label: "Redo", size: "sm", onClick: redo });
    if (!S.redo.length) { rb.disabled = true; rb.setAttribute("aria-disabled", "true"); }
    bar.appendChild(rb);
    var sb = Z.Button({
      label: S.dirty ? "Save \u2022" : "Save", variant: S.dirty ? "primary" : "secondary",
      size: "sm", onClick: save
    });
    sb.id = "studio-save";
    bar.appendChild(sb);
  }

  /* The Save button's own state, patched in place. Anything that can change
     `S.dirty` while a text field has focus must go through this rather than
     renderBar(), which would take the focused element away with it. */
  function syncSave() {
    var b = document.getElementById("studio-save");
    if (!b) return;
    b.lastChild.textContent = S.dirty ? "Save \u2022" : "Save";
    b.classList.toggle("z-btn--primary", !!S.dirty);
    b.classList.toggle("z-btn--secondary", !S.dirty);
  }

  function markDirty() { S.dirty = true; renderBar(); }

  /* ---------- palette --------------------------------------------------- */
  function renderPalette() {
    var p = document.getElementById("palette");
    p.innerHTML = "";
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "Nodes"));
    head.appendChild(drawerClose());
    p.appendChild(head);

    var groups = {};
    Object.keys(S.registry).forEach(function (type) {
      var d = S.registry[type];
      (groups[d.group] = groups[d.group] || []).push({ type: type, d: d });
    });
    // The known groups in the order they are useful, then anything else —
    // a node in a new group must not vanish from the palette.
    var order = ["Triggers", "Logic", "Maths", "Actions"];
    order.concat(Object.keys(groups).filter(function (g) {
      return order.indexOf(g) === -1;
    })).forEach(function (g) {
      if (!groups[g]) return;
      p.appendChild(el("div", "palette-group", g));
      groups[g].forEach(function (item) {
        var n = el("div", "palette-item");
        n.style.borderLeftColor = "var(--" + (item.d.series || "series-1") + ")";
        n.draggable = true;
        var box = el("div");
        box.appendChild(el("div", "palette-item-label", item.d.label));
        box.appendChild(el("div", "palette-item-sum", item.d.summary || ""));
        n.appendChild(box);
        n.addEventListener("dragstart", function (ev) {
          ev.dataTransfer.setData("text/plain", item.type);
          ev.dataTransfer.effectAllowed = "copy";
        });
        // Touch devices never fire dragstart, so a tap has to add the node
        // outright or the palette is unusable on a phone.
        n.addEventListener("click", function () {
          var c = viewportCentre();
          addNode(item.type, c.x - NODE_W / 2, c.y - 30);
          closeDrawers();
        });
        n.title = "Tap to add at the centre, or drag onto the canvas";
        p.appendChild(n);
      });
    });
  }

  /* Canvas coordinates of the middle of what is currently on screen. */
  function viewportCentre() {
    var wrap = document.getElementById("canvas-wrap");
    if (!wrap) return { x: 120, y: 120 };
    var r = wrap.getBoundingClientRect();
    return { x: (r.width / 2 - S.view.x) / S.view.k,
             y: (r.height / 2 - S.view.y) / S.view.k };
  }

  /* ---------- canvas ---------------------------------------------------- */
  function applyView() {
    var t = "translate(" + S.view.x + "px," + S.view.y + "px) scale(" + S.view.k + ")";
    document.getElementById("nodes").style.transform = t;
    var g = document.getElementById("link-layer");
    if (g) g.setAttribute("transform",
      "translate(" + S.view.x + "," + S.view.y + ") scale(" + S.view.k + ")");
  }

  function renderCanvas() {
    var host = document.getElementById("nodes");
    var svg = document.getElementById("links");
    host.innerHTML = "";
    svg.innerHTML = "";
    var layer = sv("g", { id: "link-layer" });
    svg.appendChild(layer);

    var f = flow();
    document.getElementById("canvas-hint").textContent = f
      ? "drag to pan \u00b7 wheel to zoom \u00b7 drag a port to connect \u00b7 Del removes"
      : "create a flow to begin";
    if (!f) { applyView(); return; }

    // Nodes first: a bottom anchor sits at the node's real height, which is
    // only known once it is laid out.
    (f.nodes || []).forEach(function (n) { host.appendChild(nodeEl(f, n)); });
    measureNodes(host, f);
    (f.nodes || []).forEach(function (n) { positionBottomAnchors(host, n); });

    drawLinks(f);
    applyView();
  }

  /* Links only. Dragging a node re-runs this instead of rebuilding every node,
     which is what made dragging crawl on the board. */
  function drawLinks(f) {
    var svg = document.getElementById("links");
    if (!svg) return;
    svg.innerHTML = "";
    var layer = sv("g", { id: "link-layer" });
    svg.appendChild(layer);
    (f.edges || []).forEach(function (e) {
      var a = nodeById(f, e.from), b = nodeById(f, e.to);
      if (!a || !b) return;
      var pa = anchorPoint(a, e.fromPort || "out", e.fromSide, "out");
      var pb = anchorPoint(b, e.toPort || "in", e.toSide, "in");
      if (!pa || !pb) return;
      var d = bez(pa, pb);
      var cls = "link" + (e.fromPort === "true" ? " link--true" : e.fromPort === "false" ? " link--false" : "") +
        (S.sel && S.sel.kind === "link" && S.sel.id === e.id ? " is-selected" : "");
      layer.appendChild(sv("path", { class: cls, d: d }));
      var hit = sv("path", { class: "link-hit", d: d });
      hit.addEventListener("click", function (ev) {
        ev.stopPropagation(); S.sel = { kind: "link", id: e.id }; renderCanvas(); renderSide();
      });
      layer.appendChild(hit);
      if (S.sel && S.sel.kind === "link" && S.sel.id === e.id) {
        layer.appendChild(linkDeleteHandle(e, pa, pb));
      }
    });
    applyView();
  }

  /* The midpoint of the curve, where the delete control sits. Cubic Bezier at
     t = 0.5 is (P0 + 3C1 + 3C2 + P3) / 8. */
  function curveMid(a, b) {
    var da = DIRS[a.side] || DIRS.right;
    var db = DIRS[b.side] || DIRS.left;
    var span = Math.max(40, Math.hypot(b.x - a.x, b.y - a.y) / 2);
    var c1 = { x: a.x + da[0] * span, y: a.y + da[1] * span };
    var c2 = { x: b.x + db[0] * span, y: b.y + db[1] * span };
    return { x: (a.x + 3 * c1.x + 3 * c2.x + b.x) / 8,
             y: (a.y + 3 * c1.y + 3 * c2.y + b.y) / 8 };
  }

  function linkDeleteHandle(e, pa, pb) {
    var m = curveMid(pa, pb);
    var g = sv("g", { class: "link-del", role: "button", tabindex: "0" });
    g.appendChild(sv("circle", { cx: r2(m.x), cy: r2(m.y), r: 9 }));
    g.appendChild(sv("line", { x1: r2(m.x - 3.5), y1: r2(m.y - 3.5), x2: r2(m.x + 3.5), y2: r2(m.y + 3.5) }));
    g.appendChild(sv("line", { x1: r2(m.x + 3.5), y1: r2(m.y - 3.5), x2: r2(m.x - 3.5), y2: r2(m.y + 3.5) }));
    var title = sv("title", {});
    title.textContent = "Remove this connection";
    g.appendChild(title);
    function kill(ev) {
      ev.stopPropagation(); ev.preventDefault();
      var f = flow(); if (!f) return;
      snapshot();
      f.edges = (f.edges || []).filter(function (x) { return x.id !== e.id; });
      S.sel = null; markDirty(); renderCanvas(); renderSide();
    }
    g.addEventListener("click", kill);
    g.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") kill(ev);
    });
    return g;
  }

  function measureNodes(host, f) {
    (f.nodes || []).forEach(function (n) {
      var box = host.querySelector('.node[data-id="' + n.id + '"]');
      if (box) S.nodeH[n.id] = box.offsetHeight || NODE_H_DEFAULT;
    });
  }

  /* Bottom anchors are placed after measuring, since CSS cannot know the
     height at the time the element is built. */
  function positionBottomAnchors(host, n) {
    var box = host.querySelector('.node[data-id="' + n.id + '"]');
    if (!box) return;
    var h = nodeHeight(n);
    Array.prototype.forEach.call(box.querySelectorAll('.port[data-side="bottom"]'), function (p) {
      p.style.top = (h - 8) + "px";
    });
    Array.prototype.forEach.call(box.querySelectorAll('.port-tag[data-side="bottom"]'), function (t) {
      t.style.top = (h + 6) + "px";
    });
  }

  /* Control points leave each end along that end's own side, so a link out of
     the bottom of a node dives downward instead of shooting sideways. */
  function bez(a, b) {
    var da = DIRS[a.side] || DIRS.right;
    var db = DIRS[b.side] || DIRS.left;
    var span = Math.max(40, Math.hypot(b.x - a.x, b.y - a.y) / 2);
    var c1x = a.x + da[0] * span, c1y = a.y + da[1] * span;
    var c2x = b.x + db[0] * span, c2y = b.y + db[1] * span;
    return "M" + r2(a.x) + " " + r2(a.y) +
           " C" + r2(c1x) + " " + r2(c1y) + "," + r2(c2x) + " " + r2(c2y) +
           "," + r2(b.x) + " " + r2(b.y);
  }
  function r2(v) { return Math.round(v * 100) / 100; }
  function oppositeOf(side) {
    return { left: "right", right: "left", top: "bottom", bottom: "top" }[side] || "left";
  }
  function nodeById(f, id) { return (f.nodes || []).filter(function (n) { return n.id === id; })[0]; }

  function nodeEl(f, n) {
    var d = def(n.type);
    var box = el("div", "node" + (S.sel && S.sel.kind === "node" && S.sel.id === n.id ? " is-selected" : "") +
      (S.profile && !S.registry[n.type] ? " is-unsupported" : "") +
      (S.activeSteps[f.id + "/" + n.id] ? " is-active" : "") +
      (unchosen(n).length ? " is-unchosen" : ""));
    box.style.left = n.x + "px";
    box.style.top = n.y + "px";
    box.dataset.id = n.id;

    var head = el("div", "node-head");
    head.style.borderLeftColor = "var(--" + (d.series || "series-1") + ")";
    head.appendChild(el("span", "node-title", d.label));
    if (d.kind === "trigger" && n.type === "manual.fire") {
      head.appendChild(Z.Button({
        label: "Run", variant: "ghost", size: "sm",
        onClick: function (ev) { ev.stopPropagation(); runNode(n); }
      }));
    }
    box.appendChild(head);

    var body = el("div", "node-body");
    var todo = unchosen(n);
    if (todo.length) body.appendChild(el("div", "node-todo", "choose a " + todo.join(", ")));
    body.appendChild(summaryFor(n, d));
    box.appendChild(body);

    var multi = {
      in: (d.inputs || []).length > 1,
      out: (d.outputs || []).length > 1
    };
    anchorsFor(n).forEach(function (a) {
      var dot = el("div", "port port--" + a.side + " port--" + a.kind);
      dot.dataset.node = n.id;
      dot.dataset.port = a.port;
      dot.dataset.kind = a.kind;
      dot.dataset.side = a.side;
      dot.title = a.kind + " \u00b7 " + a.port + " \u00b7 " + a.side;
      if (a.side === "left" || a.side === "right") {
        dot.style.top = (a.oy - 8) + "px";
      } else {
        dot.style.left = (a.ox - 8) + "px";
        if (a.side === "top") dot.style.top = "-8px";
        // bottom is positioned after measuring
      }
      dot.addEventListener("pointerdown", function (ev) {
        trackDown(ev);
        if (pointerCount() >= 2) { beginPinch(); return; }
        startLink(ev, n, a);
      });
      box.appendChild(dot);

      // label the ports only where a node has more than one of that kind
      if (multi[a.kind]) {
        var tag = el("span", "port-tag port-tag--" + a.side, a.label || a.port);
        tag.dataset.side = a.side;
        if (a.side === "left" || a.side === "right") {
          tag.style.top = (a.oy - 6) + "px";
        } else {
          tag.style.left = (a.ox - 18) + "px";
          if (a.side === "top") tag.style.top = "-20px";
        }
        box.appendChild(tag);
      }
    });

    box.addEventListener("pointerdown", function (ev) {
      if (ev.target.classList.contains("port") || ev.target.closest(".z-btn")) return;
      trackDown(ev);
      if (pointerCount() >= 2) { beginPinch(); return; }
      startDrag(ev, n);
    });
    return box;
  }

  function summaryFor(n, d) {
    var frag = document.createDocumentFragment();
    var cfg = n.config || {};
    var bits = [];
    if (cfg.gpio != null) {
      var pin = pinByGpio(cfg.gpio);
      bits.push(pin ? ("pin " + pin.phys + " · " + pin.label) : ("gpio " + cfg.gpio));
    }
    (d.fields || []).forEach(function (fl) {
      if (fl.key === "gpio") return;
      var v = cfg[fl.key];
      if (v === undefined || v === "" || v === null) return;
      bits.push(fl.key + "=" + String(v).slice(0, 26));
    });
    if (!bits.length) { var em = el("em", null, "not configured"); frag.appendChild(em); return frag; }
    frag.appendChild(document.createTextNode(bits.join("  ")));
    return frag;
  }

  /* ---------- dragging, linking, panning -------------------------------- */
  function canvasPoint(ev) {
    var r = document.getElementById("canvas-wrap").getBoundingClientRect();
    return { x: (ev.clientX - r.left - S.view.x) / S.view.k, y: (ev.clientY - r.top - S.view.y) / S.view.k };
  }

  /* Every gesture comes through pointer events, tracked in one map. Mixing them
     with touchstart/touchmove meant a two-finger pinch also started a one-finger
     pan, and the two fought over the transform. */
  function pointerCount() { var n = 0; for (var k in S.pointers) n++; return n; }

  function trackDown(ev) { S.pointers[ev.pointerId] = { x: ev.clientX, y: ev.clientY }; }
  function trackUp(ev) { delete S.pointers[ev.pointerId]; }

  function twoPointers() {
    var out = [];
    for (var k in S.pointers) out.push(S.pointers[k]);
    return out.length >= 2 ? [out[0], out[1]] : null;
  }

  function beginPinch() {
    var p = twoPointers();
    if (!p) return;
    // a second finger cancels whatever one finger had started
    S.drag = null; S.link = null; S.pan = null;
    document.getElementById("canvas-wrap").classList.remove("is-panning");
    S.pinch = {
      d: Math.max(1, Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y)),
      k: S.view.k,
      cx: (p[0].x + p[1].x) / 2,
      cy: (p[0].y + p[1].y) / 2,
      vx: S.view.x, vy: S.view.y
    };
  }

  function updatePinch() {
    var p = twoPointers();
    if (!p || !S.pinch) return;
    var wrap = document.getElementById("canvas-wrap");
    var r = wrap.getBoundingClientRect();
    var dist = Math.max(1, Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y));
    var k = Math.min(2, Math.max(0.3, S.pinch.k * (dist / S.pinch.d)));
    var mx = S.pinch.cx - r.left, my = S.pinch.cy - r.top;
    // keep the pinch midpoint fixed on the canvas while scaling
    S.view.x = mx - (mx - S.pinch.vx) * (k / S.pinch.k);
    S.view.y = my - (my - S.pinch.vy) * (k / S.pinch.k);
    S.view.k = k;
    applyView();
  }

  function startDrag(ev, n) {
    ev.preventDefault();
    snapshot();
    var p = canvasPoint(ev);
    S.drag = { id: n.id, dx: p.x - n.x, dy: p.y - n.y };
    S.sel = { kind: "node", id: n.id };
    renderCanvas(); renderSide();
  }

  function startLink(ev, n, a) {
    ev.preventDefault(); ev.stopPropagation();
    S.link = { from: n.id, port: a.port, side: a.side, dir: a.kind,
               x1: n.x + a.ox, y1: n.y + a.oy };
    applyLockState();                       // no re-render: see applyLockState
    var layer = document.getElementById("link-layer");
    if (layer) {
      S.ghost = sv("path", { class: "link link--ghost", d: "" });
      layer.appendChild(S.ghost);
    }
  }

  function onMove(ev) {
    if (S.pointers[ev.pointerId]) {
      S.pointers[ev.pointerId].x = ev.clientX;
      S.pointers[ev.pointerId].y = ev.clientY;
    }
    if (pointerCount() >= 2) { updatePinch(); return; }
    if (S.drag) {
      var f = flow(); if (!f) return;
      var n = nodeById(f, S.drag.id); if (!n) return;
      var p = canvasPoint(ev);
      n.x = Math.round((p.x - S.drag.dx) / 4) * 4;   // snap to the 4px grid
      n.y = Math.round((p.y - S.drag.dy) / 4) * 4;
      var box = document.querySelector('.node[data-id="' + n.id + '"]');
      if (box) { box.style.left = n.x + "px"; box.style.top = n.y + "px"; }
      drawLinks(f);                                   // not a full rebuild
    } else if (S.link) {
      // Only the ghost is updated: rebuilding every node on each pointer move
      // is what made dragging a connection crawl on the board.
      var p2 = canvasPoint(ev);
      if (S.ghost) {
        S.ghost.setAttribute("d", bez(
          { x: S.link.x1, y: S.link.y1, side: S.link.side },
          { x: p2.x, y: p2.y, side: oppositeOf(S.link.side) }));
      }
    } else if (S.pan) {
      S.view.x = S.pan.vx + (ev.clientX - S.pan.sx);
      S.view.y = S.pan.vy + (ev.clientY - S.pan.sy);
      applyView();
    }
  }

  function onUp(ev) {
    trackUp(ev);
    if (pointerCount() < 2) S.pinch = null;
    if (pointerCount() > 0) return;          // still mid-gesture
    if (S.drag) { S.drag = null; markDirty(); }
    else if (S.link) {
      // With touch, ev.target is where the gesture began, not where it ended.
      var t = document.elementFromPoint(ev.clientX, ev.clientY) || ev.target;
      if (t && t.classList && t.classList.contains("port") &&
          !t.classList.contains("is-locked") &&
          !anchorLocked(t.dataset.node, t.dataset.kind, t.dataset.port)) {
        if (S.link.dir === "out") {
          addEdge(S.link.from, S.link.port, S.link.side,
                  t.dataset.node, t.dataset.port, t.dataset.side);
        } else {
          // started from an input: the link runs the other way
          addEdge(t.dataset.node, t.dataset.port, t.dataset.side,
                  S.link.from, S.link.port, S.link.side);
        }
      }
      S.link = null; S.ghost = null; clearLockState(); renderCanvas();
    }
    if (S.pan) { S.pan = null; document.getElementById("canvas-wrap").classList.remove("is-panning"); }
  }

  /* ---------- graph edits ---------------------------------------------- */
  function addNode(type, x, y) {
    snapshot();
    var f = flow();
    if (!f) { newFlow(); f = flow(); }
    var d = def(type), cfg = {};
    (d.fields || []).forEach(function (fl) { if (fl.default !== undefined) cfg[fl.key] = fl.default; });
    var n = { id: uid("n"), type: type, x: Math.round(x / 4) * 4, y: Math.round(y / 4) * 4, config: cfg };
    f.nodes = f.nodes || [];
    f.nodes.push(n);
    S.sel = { kind: "node", id: n.id };
    markDirty(); renderCanvas(); renderSide();
  }
  function addEdge(from, fromPort, fromSide, to, toPort, toSide) {
    var f = flow(); if (!f) return;
    f.edges = f.edges || [];
    var dup = f.edges.some(function (e) {
      return e.from === from && (e.fromPort || "out") === fromPort && e.to === to;
    });
    if (dup) return;
    snapshot();
    // The sides are presentation only: the runtime walks ports, not geometry.
    f.edges.push({ id: uid("e"), from: from, fromPort: fromPort, fromSide: fromSide,
                   to: to, toPort: toPort, toSide: toSide });
    markDirty();
  }
  function removeSelected() {
    var f = flow(); if (!f || !S.sel) return;
    snapshot();
    if (S.sel.kind === "node") {
      f.nodes = (f.nodes || []).filter(function (n) { return n.id !== S.sel.id; });
      f.edges = (f.edges || []).filter(function (e) { return e.from !== S.sel.id && e.to !== S.sel.id; });
    } else {
      f.edges = (f.edges || []).filter(function (e) { return e.id !== S.sel.id; });
    }
    S.sel = null; markDirty(); renderCanvas(); renderSide();
  }
  function newFlow() {
    snapshot();
    var f = { id: uid("flow"), name: "New flow", enabled: true, nodes: [], edges: [] };
    S.doc.flows.push(f); S.flowId = f.id; S.sel = null;
    markDirty(); save();
    openEditor(f.id);
  }
  /* The document carries a revision. Sending the one this page loaded is what
     stops a stale tab from overwriting a save made somewhere else: the server
     refuses with 409 and hands back what is on disk. */
  function save(opts) {
    var was = flow();
    var body = JSON.stringify({ flows: S.doc.flows, rev: S.rev });
    return api("/api/flows", { method: "POST", headers: { "Content-Type": "application/json" }, body: body })
      .then(function (r) {
        if (r && typeof r.rev === "number") S.rev = r.rev;
        S.conflict = null;
        S.dirty = false; renderBar(); loadPins(); renderConflict();
        if (S.mode === "index") renderList();
        // The caller is about to deploy this itself; not twice.
        if (was && !(opts && opts.noRedeploy)) redeployIfLive(was);
        /* Things that saved fine and will not work. Same moment as the wire
           warning and for the same reason: this is when someone is looking.
           Flashed on the node too, so it is findable in a graph this size. */
        (r && r.advice || []).forEach(function (a) {
          var mine = was && a.flow === was.id;
          toast((mine ? "" : (a.flow_name || a.flow) + ": ") + a.message,
                a.level === "critical" ? "critical" : "warn");
          if (mine && a.node) flashNode(a.node);
        });
        // Saving is when someone finds out a flow has grown past what is
        // comfortable to send over the air, well before the board goes quiet.
        if (was) {
          if (S.costs) delete S.costs[was.id];
          loadWireCost(was).then(function () {
            var warn = wireWarning(was);
            if (warn) toast(warn, "warn");
          });
        }
        return true;
      })
      .catch(function (e) {
        if (e.status === 409 && e.body) {
          S.conflict = e.body;
          renderConflict();
          toast("save refused: " + e.message, "warn");
          return false;
        }
        toast("save failed: " + e.message, "critical");
        return false;
      });
  }

  /* ---------- deploying to the linked device ----------------------------- */
  /* The device record names the flow it is currently running, so "is this the
     one already out there?" needs no extra call. */
  function linkedDevice(f) {
    if (!f || !f.device) return null;
    return (S.devices || []).filter(function (d) { return d.id === f.device; })[0] || null;
  }

  /* What the linked device would have to fetch. `device_pins` carries it for
     the flow currently deployed; for any other, ask. Cached per flow, because it
     only moves when a module or the flow does. */
  function wireWarning(f) {
    var cost = (S.devstate && S.devstate.cost) || null;
    if (S.costs && S.costs[f.id] !== undefined) cost = S.costs[f.id];
    if (!cost || !cost.wire) return null;
    return "This is " + Z.bytes(cost.bytes) + " for the board to fetch. It is "
      + "unreachable while it syncs \u2014 70 KiB has taken six minutes \u2014 so "
      + "deploy it over the wire instead: IOT \u203a the device \u203a pick this "
      + "flow \u203a Flash with this.";
  }

  function loadWireCost(f) {
    var dev = linkedDevice(f);
    if (!dev || !f.id) return Promise.resolve(null);
    S.costs = S.costs || {};
    if (S.costs[f.id] !== undefined) return Promise.resolve(S.costs[f.id]);
    return api("/api/iot/devices/" + encodeURIComponent(dev.id) + "/cost?flow="
               + encodeURIComponent(f.id))
      .then(function (c) { S.costs[f.id] = c; return c; })
      .catch(function () { S.costs[f.id] = null; return null; });
  }

  function deployTo(f, quiet) {
    var dev = linkedDevice(f);
    if (!dev) return Promise.resolve(false);
    return api("/api/iot/devices/" + encodeURIComponent(dev.id) + "/deploy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow: f.id })
    }).then(function () {
      dev.flow = f.id;                       // so the next save knows it is live
      renderBar();                           // Deploy becomes Redeploy
      var warn = wireWarning(f);
      toast((quiet ? "Redeployed to " : "Deployed to ") + (dev.name || dev.id)
            + (warn ? " \u2014 " + warn : ""), warn ? "warn" : "ok");
      return true;
    }).catch(function (e) {
      toast("deploy failed: " + e.message, "critical");
      return false;
    });
  }

  /* After a save, push the change out if this flow is the one that device is
     already running. Deploy is a live reload on the agent, not a reboot, so
     this does not interrupt anything that was not already being replaced. */
  function redeployIfLive(f) {
    var dev = linkedDevice(f);
    if (!dev || dev.flow !== f.id) return;
    deployTo(f, true);
  }

  /* One bar, fixed to the top of the page, offering the only two honest choices.
     Nothing is merged automatically and nothing is discarded unasked. */
  function renderConflict() {
    var old = document.getElementById("conflict-bar");
    if (old) old.parentNode.removeChild(old);
    document.body.classList.toggle("has-conflict", !!S.conflict);
    if (!S.conflict) return;

    var bar = el("div", "conflict-bar");
    bar.id = "conflict-bar";
    bar.appendChild(el("strong", null, "Not saved."));
    bar.appendChild(el("span", null,
      "Another tab or device saved these flows since this page loaded (revision " +
      S.conflict.rev + ", " + S.conflict.flows + " flow" + (S.conflict.flows === 1 ? "" : "s") +
      " on disk). Choose which copy wins."));
    var acts = el("div", "conflict-acts");
    acts.appendChild(Z.Button({
      label: "Load theirs", size: "sm",
      onClick: function () {
        var theirs = S.conflict.current || { flows: [] };
        S.rev = theirs.rev || 0;
        S.doc = { flows: (theirs.flows || []) };
        S.conflict = null; S.dirty = false;
        S.undo = []; S.redo = [];     // those snapshots belong to a document that is gone
        var gone = !S.doc.flows.some(function (f) { return f.id === S.flowId; });
        if (gone) S.flowId = S.doc.flows.length ? S.doc.flows[0].id : null;
        renderConflict(); loadPins();
        // Editing a flow the saved copy does not have is a dead end — go back
        // to the index rather than leave a canvas belonging to nothing.
        if (S.mode === "editor" && gone) { setView("index"); }
        else if (S.mode === "index") { renderIndex(); }
        else { renderBar(); renderCanvas(); renderSide(); }
        toast("loaded the saved copy", "ok");
      }
    }));
    acts.appendChild(Z.Button({
      label: "Keep mine", variant: "danger", size: "sm",
      onClick: function () {
        S.rev = S.conflict.rev;       // save against what is on disk now
        S.conflict = null;
        renderConflict();
        save();
      }
    }));
    bar.appendChild(acts);
    document.body.insertBefore(bar, document.body.firstChild);
  }
  function runNode(n) {
    api("/api/flows/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ flow: S.flowId, node: n.id }) })
      .catch(function (e) { toast(e.message, "critical"); });
  }

  /* ---------- inspector + pin map -------------------------------------- */
  /* Rebuilding the pane throws away scroll position, focus, the caret and the
     open/closed state of the library. Capture them, rebuild, put them back. */
  function unsupportedNote(n) {
    if (!S.profile || !n || S.registry[n.type]) return null;
    var box = el("div", "side-warn");
    box.appendChild(el("strong", null, "This board cannot run this node."));
    box.appendChild(el("span", null,
      def(n.type).label + " needs something only the host has. Delete it, or move " +
      "this flow back to the host to keep it."));
    return box;
  }

  /* The bound board's pins, drawn the same way the 40-pin header is: the only
     differences should be the pins and where the readings come from. */
  function boardPanel() {
    var box = el("div");
    var dev = S.devstate && S.devstate.device;
    var live = !!(dev && dev.online);
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, S.profile.label + " pins"));
    head.appendChild(Z.Badge({
      text: live ? "live" : (dev ? (dev.enrolled ? "offline" : "never connected")
                                 : "no device linked"),
      tone: live ? "ok" : (dev && dev.enrolled ? "warn" : "idle")
    }));
    box.appendChild(head);

    box.appendChild(deviceBanner());

    var legend = el("div", "pin-legend");
    [["free", "free"], ["bound", "in this flow"], ["reserved", "not available"]]
      .forEach(function (p) {
        var sp = el("span");
        var d = el("i", "pin-dot pin-dot--" + p[0]); d.style.display = "inline-block";
        sp.appendChild(d); sp.appendChild(document.createTextNode(p[1]));
        legend.appendChild(sp);
      });
    box.appendChild(legend);

    var pins = boardPins();
    var grid = el("div", "pinmap");
    grid.style.padding = "8px 12px";
    var left = el("div"), right = el("div");
    var half = Math.ceil(pins.length / 2);
    pins.forEach(function (p, i) {
      var b = el("button", "pin" + (S.pinSel === p.gpio ? " is-selected" : "") +
        (p.status === "reserved" ? "" : " is-clickable"));
      b.type = "button";
      b.appendChild(el("span", "pin-num", String(p.gpio)));
      b.appendChild(el("span", "pin-dot pin-dot--" + p.status));
      // The number is already in its own column, so the label carries what is
      // actually worth knowing about the pin instead of repeating it.
      var text = pinCaption(p);
      b.appendChild(el("span", "pin-label" + (p.held ? " pin-high" : ""), text));
      b.title = boardPinTitle(p);
      if (p.status !== "reserved") {
        b.addEventListener("click", function () {
          S.pinSel = (S.pinSel === p.gpio ? null : p.gpio); renderSide();
        });
      }
      (i < half ? left : right).appendChild(b);
    });
    grid.appendChild(left); grid.appendChild(right);
    box.appendChild(grid);

    var sel = pins.filter(function (p) { return p.gpio === S.pinSel; })[0];
    if (sel) box.appendChild(boardPinDetail(sel));
    box.appendChild(busPanel());
    return box;
  }

  /* Where this flow actually stands with its hardware. Absence of news is the
     thing people were left guessing at, so every case says something. */
  function deviceStatus() {
    var f = flow();
    if (!f || !f.board) return null;
    var dev = S.devstate && S.devstate.device;
    var linked = S.devices.filter(function (d) { return d.id === f.device; })[0];
    if (!f.device) {
      return { tone: "idle", short: "not linked",
               text: "This flow is written for " + boardLabel(f.board) +
                     " but is not linked to a device yet. Pick one under \u22ef \u203a " +
                     "Linked device, then deploy it from the IOT screen." };
    }
    var name = (linked && linked.name) || (dev && dev.name) || f.device;
    if (!dev || !dev.enrolled) {
      return { tone: "warn", short: "never connected",
               text: name + " has never reported in. Flash it from the IOT screen, " +
                     "and check it can reach this console on the network it was " +
                     "given \u2014 until then nothing here is live." };
    }
    if (!dev.online) {
      return { tone: "warn", short: "offline",
               text: name + " last reported " + Z.ago(dev.last_seen) + ". The flow in its " +
                     "flash keeps running while it is away; these readings are the " +
                     "last it sent." };
    }
    var running = S.devstate.flow;
    if (running !== f.id) {
      return { tone: "warn", short: "not deployed",
               text: name + " is online but running " +
                     (S.devstate.flow_name ? "\u201c" + S.devstate.flow_name + "\u201d"
                                           : "nothing") +
                     ", not this flow. Deploy it from the IOT screen to change that." };
    }
    return { tone: "ok", short: "live",
             text: name + " is running this flow, last heard from "
                     + Z.ago(dev.last_seen) + "." };
  }

  /* Everything known about the connection, in one place, because the question
     "is it there" has about six answers and the chip has room for one word. The
     link rows are the held-open socket; the rest is the board's own last word. */
  function linkPanel(st) {
    var box = el("div", "link-pop");
    box.addEventListener("click", function (ev) { ev.stopPropagation(); });
    var head = el("div", "link-pop-head");
    head.appendChild(el("strong", null, st.short));
    var x = el("button", "link-pop-close", "\u2715");
    x.type = "button";
    x.title = "Close";
    x.addEventListener("click", function () { S.linkPop = false; renderBar(); });
    head.appendChild(x);
    box.appendChild(head);
    box.appendChild(el("p", "link-pop-text", st.text));

    var dev = (S.devstate && S.devstate.device) || {};
    var link = dev.link;
    var rows = [];
    if (link && typeof link === "object") {
      rows.push(["link", link.state]);
      rows.push(["frames", (link["in"] || 0) + " in, " + (link.out || 0) + " out"]);
      if (link.failures) rows.push(["failed dials", String(link.failures)]);
      if (link.dropped) rows.push(["last drop", link.dropped]);
    } else {
      rows.push(["link", "not held open \u2014 commands go by the poll, which "
                         + "takes a second or two"]);
    }
    rows.push(["address", dev.ip]);
    rows.push(["signal", dev.rssi != null ? dev.rssi + " dBm" : null]);
    rows.push(["agent", dev.agent
      ? dev.agent + (dev.reported ? "" : " \u2014 at enrolment; it has not reported since")
      : null]);
    rows.push(["free memory", Z.bytes(dev.free_ram)]);
    rows.push(["up for", Z.duration(dev.uptime)]);
    rows.push(["flow running", dev.running === undefined ? null
                               : (dev.running ? "yes" : "no")]);
    rows.push(["last report", dev.last_seen
      ? Z.ago(dev.last_seen) : null]);

    var list = el("div", "link-pop-rows");
    rows.forEach(function (r) {
      if (r[1] == null || r[1] === "") return;
      var line = el("div", "link-pop-row");
      line.appendChild(el("span", "link-pop-key", r[0]));
      line.appendChild(el("span", "link-pop-val", String(r[1])));
      list.appendChild(line);
    });
    box.appendChild(list);
    return box;
  }

  function deviceBanner() {
    var st = deviceStatus();
    if (!st) return el("span");
    var box = el("div", "device-banner is-" + st.tone);
    box.appendChild(el("strong", null, st.short));
    box.appendChild(el("span", null, st.text));
    var dev = S.devstate && S.devstate.device;
    if (dev && (dev.ip || dev.agent || dev.free_ram)) {
      var meta = el("div", "device-meta");
      [["ip", dev.ip], ["rssi", dev.rssi != null ? dev.rssi + " dBm" : null],
       ["agent", dev.agent], ["free ram", dev.free_ram ? Math.round(dev.free_ram / 1024) + " KB" : null]
      ].forEach(function (r) {
        if (r[1]) meta.appendChild(el("span", null, r[0] + " " + r[1]));
      });
      if (meta.childNodes.length) box.appendChild(meta);
    }
    if (dev && dev.mismatch) {
      box.appendChild(el("div", "device-meta", "mismatch: " + dev.mismatch));
    }
    return box;
  }

  /* Why a pin is unavailable, in one word. */
  function shortWhy(note) {
    // Most specific first: a pin can be both off the header and wired to the
    // camera, and "camera" is the more useful of the two.
    var n = note || "";
    if (/SPI flash/.test(n)) return "flash";
    if (/PSRAM/.test(n)) return "psram";
    if (/camera/.test(n)) return "camera";
    if (/microSD/.test(n)) return "sd";
    if (/not brought out/.test(n)) return "no pin";
    return "taken";
  }

  function pinCaption(p) {
    if (p.status === "reserved") return shortWhy(p.note);
    if (p.live) {
      if (p.live.dir === "pwm") return p.live.value + "%";
      return (p.live.dir === "in" ? "in " : "out ") + (p.live.value ? "\u25b2" : "\u25bc");
    }
    if (p.used_by && p.used_by.length) {
      return p.used_by[0].type.split(".")[0];
    }
    var caps = (p.caps || []).filter(function (c) {
      return c !== "in" && c !== "out" && c !== "pwm" && c !== "rtc";
    });
    return caps.length ? caps.join(" ") : "free";
  }

  function boardPinTitle(p) {
    var bits = [p.label];
    if (p.caps && p.caps.length) bits.push(p.caps.join(", "));
    if (p.note) bits.push(p.note);
    if (p.used_by && p.used_by.length) {
      bits.push("used by " + p.used_by.map(function (n) { return n.type; }).join(", "));
    }
    if (p.live) bits.push("reported " + p.live.dir + " = " + p.live.value);
    return bits.join(" \u00b7 ");
  }

  function boardPinDetail(p) {
    var d = el("div", "pin-detail");
    d.appendChild(el("div", "pin-detail-head", p.label));
    var rows = el("div", "board-rows");
    [["can do", (p.caps || []).join(", ") || "nothing"],
     ["status", p.status === "bound" ? "used by this flow" :
                p.status === "reserved" ? "not available" : "free"],
     ["reported", p.live ? (p.live.dir + " = " + p.live.value) : "nothing yet"],
     ["note", p.note || null]].forEach(function (r) {
      if (r[1] == null) return;
      rows.appendChild(el("span", "board-row-k", r[0]));
      rows.appendChild(el("span", "board-row-v", r[1]));
    });
    d.appendChild(rows);
    (p.used_by || []).forEach(function (n) {
      var line = el("div", "field-help", n.type + " \u00b7 " + n.id);
      d.appendChild(line);
    });
    return d;
  }

  /* Buses, so the board's I2C/SPI defaults are as visible as its pins. */
  function busPanel() {
    var box = el("div");
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "Buses and peripherals"));
    box.appendChild(head);
    var rows = el("div", "board-rows");
    var buses = (S.profile && S.profile.buses) || {};
    Object.keys(buses).forEach(function (name) {
      var cfg = buses[name], bits = [];
      Object.keys(cfg).forEach(function (k) {
        if (k !== "note") bits.push(k + " IO" + cfg[k]);
      });
      rows.appendChild(el("span", "board-row-k", name));
      rows.appendChild(el("span", "board-row-v", bits.join("  ") || "\u2014"));
      if (cfg.note) {
        rows.appendChild(el("span", "board-row-k", ""));
        rows.appendChild(el("span", "board-row-v is-note", cfg.note));
      }
    });
    var per = (S.profile && S.profile.peripherals) || {};
    var have = Object.keys(per).filter(function (k) { return per[k] && per[k] !== false; });
    rows.appendChild(el("span", "board-row-k", "has"));
    rows.appendChild(el("span", "board-row-v", have.map(function (k) {
      return per[k] === true ? k : k + " (" + per[k] + ")";
    }).join(", ") || "\u2014"));
    var cam = S.devstate && S.devstate.device && S.devstate.device.camera;
    if (cam) {
      rows.appendChild(el("span", "board-row-k", "camera"));
      rows.appendChild(el("span", "board-row-v", cam.running
        ? (cam.sensor || "camera") + " " + cam.width + "x" + cam.height +
          " \u00b7 " + (cam.frames || 0) + " frames"
        : "not running"));
    }
    box.appendChild(rows);
    return box;
  }

  function renderSide() {
    var side = document.getElementById("side");
    if (!side) return;
    var scroll = side.scrollTop;
    var act = document.activeElement;
    var focusId = act && side.contains(act) ? act.id : null;
    var selStart = null, selEnd = null;
    if (focusId && act.selectionStart != null) {
      selStart = act.selectionStart; selEnd = act.selectionEnd;
    }
    var lib = side.querySelector(".var-lib");
    S.libOpen = lib ? lib.open : S.libOpen;

    buildSide(side);

    var reopened = side.querySelector(".var-lib");
    if (reopened && S.libOpen) reopened.open = true;
    side.scrollTop = scroll;
    if (focusId) {
      var again = document.getElementById(focusId);
      if (again) {
        again.focus();
        if (selStart != null && again.setSelectionRange) {
          try { again.setSelectionRange(selStart, selEnd); } catch (e) { /* not a text field */ }
        }
        S.lastField = again;
      }
    }
  }

  function buildSide(side) {
    side.innerHTML = "";
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "Inspector"));
    head.appendChild(drawerClose());
    side.appendChild(head);

    var f = flow();
    var n = f && S.sel && S.sel.kind === "node" ? nodeById(f, S.sel.id) : null;
    if (!n) {
      side.appendChild(el("div", "inspector-empty",
        S.sel && S.sel.kind === "link"
          ? "Link selected. Click the \u2715 on the link, or press Delete."
          : "Select a node to configure it."));
    } else {
      var d = def(n.type);
      var sec = el("div", "pane-section");
      var warn = unsupportedNote(n);
      if (warn) sec.appendChild(warn);
      var title = el("div", "field");
      title.appendChild(el("label", null, "Node"));
      var ident = el("div", "field-help");
      ident.appendChild(el("span", null, d.label + " \u00b7 "));
      ident.appendChild(idWithCopy(n.id, "node id"));
      title.appendChild(ident);
      if (d.summary) title.appendChild(el("div", "node-summary", d.summary));
      sec.appendChild(title);
      if (d.docs && d.docs.length) {
        var docs = el("div", "node-docs");
        d.docs.forEach(function (para) { docs.appendChild(el("p", null, para)); });
        sec.appendChild(docs);
      }
      if (n.type === "http.webhook") sec.appendChild(webhookHelp(n));
      (d.fields || []).forEach(function (fl) {
        if (!fieldApplies(n, fl)) return;
        sec.appendChild(fieldEl(n, fl));
      });
      sec.appendChild(outputPanel(f, n, d));
      var del = Z.Button({ label: "Delete node", variant: "danger", size: "sm", onClick: removeSelected });
      sec.appendChild(del);
      sec.appendChild(variableLibrary());
      side.appendChild(sec);
    }
    side.appendChild(tagPanel());
    side.appendChild(S.profile ? boardPanel() : pinMap());
    if (!S.profile) side.appendChild(hardwarePanel());
  }

  /* ---------- output preview -------------------------------------------- */
  function outputKey(flowId, nodeId) { return flowId + "/" + nodeId; }

  /* An output arrived on the stream. Update the panel in place if the node it
     belongs to is the one on screen — re-rendering the inspector here would
     fight whoever is typing in it. */
  function noteOutput(r) {
    S.outputs[outputKey(r.flow, r.node)] = r;
    flashNode(r.node);
    var f = flow();
    if (!f || !S.sel || S.sel.kind !== "node" || S.sel.id !== r.node) return;
    if (r.flow !== f.id) return;
    var host = document.getElementById("node-output");
    var n = nodeById(f, r.node);
    if (host && n) fillOutput(host, def(n.type), r);
  }

  /* The value a declared field is actually holding right now, if anything. */
  function liveValue(r, name) {
    if (!r || r.stopped) return undefined;
    if (name === "payload") return r.payload;
    if (name === "meta") return r.meta;
    if (name.indexOf("meta.") === 0) {
      return r.meta ? r.meta[name.slice(5)] : undefined;
    }
    return undefined;
  }

  function showValue(v) {
    if (v === undefined) return null;
    if (v === null) return "null";
    if (typeof v === "object") { try { return JSON.stringify(v); } catch (e) { return String(v); } }
    return String(v);
  }

  /* The shape a node produces, with what it last actually produced beside it.
     The structure is there before the flow has ever run, because knowing what
     a node will hand on is the thing you need while you are still wiring. */
  function fillOutput(host, d, r) {
    host.textContent = "";

    var head = el("div", "out-head");
    if (r && !r.stopped) {
      head.appendChild(el("span", "out-when", "last run " + r.time));
      if (r.port && r.port !== "out") {
        head.appendChild(el("span", "out-port", "via " + r.port));
      }
    } else if (r && r.stopped) {
      head.appendChild(el("span", "out-stopped", "stopped here · " + r.time));
    } else {
      head.appendChild(el("span", "out-when", "not run yet"));
    }
    host.appendChild(head);

    var fields = (d && d.emits) || [];
    if (!fields.length) {
      host.appendChild(el("div", "field-help", "This node passes its message on unchanged."));
      return;
    }

    var table = el("div", "out-fields");
    fields.forEach(function (f) {
      var row = el("div", "out-field");
      var top = el("div", "out-field-top");
      var name = el("code", "out-name", f.name === "meta" ? "{{meta.*}}" : "{{" + f.name + "}}");
      top.appendChild(name);
      top.appendChild(el("span", "out-kind", f.kind));
      row.appendChild(top);

      var live = showValue(liveValue(r, f.name));
      var val = el("div", "out-value");
      if (live !== null) {
        val.appendChild(el("span", "out-live", live));
      } else {
        // No run yet, or this field was not in it: show what it would look
        // like, marked as an example so it is never mistaken for a reading.
        val.appendChild(el("span", "out-eg",
          f.example === undefined || f.example === null
            ? (f.kind === "same" ? "unchanged" : "—")
            : "eg " + showValue(f.example)));
      }
      row.appendChild(val);
      row.appendChild(el("div", "out-desc", f.desc || ""));
      table.appendChild(row);
    });
    host.appendChild(table);

    // Anything the run carried that the node never said it would.
    if (r && !r.stopped && r.meta) {
      var declared = {};
      fields.forEach(function (f) { declared[f.name] = true; });
      var extra = Object.keys(r.meta).filter(function (k) {
        return !declared["meta." + k] && !declared.meta;
      });
      if (extra.length) {
        var more = el("div", "out-fields out-fields--extra");
        more.appendChild(el("div", "field-help",
          "Also carried through from earlier in the flow:"));
        extra.forEach(function (k) {
          var row = el("div", "out-field");
          var top = el("div", "out-field-top");
          top.appendChild(el("code", "out-name", "{{meta." + k + "}}"));
          top.appendChild(el("span", "out-kind", "passed on"));
          row.appendChild(top);
          row.appendChild(el("div", "out-value",
            el("span", "out-live", showValue(r.meta[k]))));
          more.appendChild(row);
        });
        host.appendChild(more);
      }
    }

    if (r && r.stopped) {
      host.appendChild(el("div", "field-help",
        "Nothing was passed on last time, so the branch ended there. An If "
        + "that did not match, or a Throttle inside its gap, does this."));
    }
  }

  function outputPanel(f, n, d) {
    var box = el("div", "field");
    box.appendChild(el("label", null, "Output"));
    box.appendChild(el("div", "field-help",
      "What this node hands to the next one. Names are what you type into "
      + "the node after it."));
    var host = el("div", "out-box");
    host.id = "node-output";
    fillOutput(host, d, S.outputs[outputKey(f.id, n.id)]);
    box.appendChild(host);
    // A flow bound to a board runs on the device, and the device reports which
    // nodes fired, not what they produced. Saying so beats an empty box.
    if (f.board) {
      box.appendChild(el("div", "field-help",
        "This flow runs on " + boardLabel(f.board) + ", which reports which "
        + "nodes fired but not what they passed on — so nothing appears "
        + "here. Run it with no board linked to see its output."));
    }
    return box;
  }

  /* ---------- the tag table ----------------------------------------------
     Shared memory, on screen. A PLC's tag table is the first thing anyone opens
     when they want to know what a machine is doing, so this shows live values
     rather than only definitions. */
  function tagPanel() {
    var box = el("div");
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "Tags"));
    var add = Z.Button({ label: "Add", size: "sm", variant: "ghost", onClick: addTag });
    head.appendChild(add);
    box.appendChild(head);

    var sec = el("div", "pane-section");
    if (!S.tags.length) {
      sec.appendChild(el("div", "field-help",
        "Shared memory for this board. Any flow can write one, any field can "
        + "read it as {{tag.name}}, and a Tag change trigger can start a flow "
        + "when it moves — so the thing that measures something does not "
        + "have to know what cares about it."));
      box.appendChild(sec);
      return box;
    }

    S.tags.forEach(function (t) { sec.appendChild(tagRow(t)); });
    box.appendChild(sec);
    return box;
  }

  function tagRow(t) {
    var open = S.tagEdit === t.name;
    var row = el("div", "tag-row");
    var top = el("div", "tag-top");
    top.appendChild(el("code", "tag-name", "{{tag." + t.name + "}}"));
    top.appendChild(el("span", "tag-type", t.type + (t.retain ? " · kept" : "")));
    row.appendChild(top);

    var line = el("div", "tag-line");
    var input = el("input", "tag-value");
    input.type = "text";
    input.id = "tagv_" + t.name;
    input.value = t.value === null || t.value === undefined ? "" : String(t.value);
    input.setAttribute("aria-label", t.name + " value");
    input.addEventListener("change", function () { writeTag(t.name, input.value); });
    line.appendChild(input);
    if (t.unit) line.appendChild(el("span", "tag-unit", t.unit));
    var edit = el("button", "id-copy" + (open ? " is-open" : ""));
    edit.type = "button";
    edit.textContent = "⋯";
    edit.title = open ? "done" : "edit this tag";
    edit.setAttribute("aria-expanded", open ? "true" : "false");
    edit.addEventListener("click", function () {
      S.tagEdit = open ? null : t.name;
      renderTags();
    });
    line.appendChild(edit);
    row.appendChild(line);
    if (t.desc && !open) row.appendChild(el("div", "field-help", t.desc));
    if (open) row.appendChild(tagEditor(t));
    return row;
  }

  /* The definition, as fields rather than a queue of prompts. Everything
     commits on blur and saves the table, so there is nothing to confirm. */
  function tagEditor(t) {
    var box = el("div", "tag-edit");
    box.appendChild(tagText(t, "name", "Name"));
    box.appendChild(tagChoice(t, "type", "Type", ["number", "text", "bool"]));
    box.appendChild(tagText(t, "unit", "Unit"));
    box.appendChild(tagText(t, "desc", "What it is for"));
    box.appendChild(tagCheck(t, "retain", "Keep its value across a restart"));
    box.appendChild(tagCheck(t, "share", "Share it with the field devices"));
    if (t.share) {
      box.appendChild(tagCheck(t, "boot_reset",
        "Go back to its initial value when a board boots"));
    }

    var del = Z.Button({
      label: "Delete tag", variant: "danger", size: "sm",
      onClick: function () {
        S.tagEdit = null;
        saveTags(S.tags.filter(function (x) { return x.name !== t.name; })
          .map(tagDef));
      }
    });
    box.appendChild(del);
    return box;
  }

  /* One field of a definition. Each carries a stable id so the inspector's
     rebuild can put the caret back where it was. */
  function tagEditField(t, key, label, control) {
    var wrap = el("div", "tag-edit-field");
    var id = "tagf_" + t.name + "_" + key;
    var lab = el("label", null, label);
    lab.setAttribute("for", id);
    control.id = id;
    wrap.appendChild(lab);
    wrap.appendChild(control);
    return wrap;
  }

  function commitTagField(t, key, value) {
    var was = t.name;
    var next = S.tags.map(function (x) {
      var d = tagDef(x);
      if (x.name === was) d[key] = value;
      return d;
    });
    // Renaming keeps the editor open on the row that moved.
    if (key === "name") S.tagEdit = value;
    saveTags(next);
  }

  function tagText(t, key, label) {
    var input = el("input");
    input.type = "text";
    input.value = t[key] == null ? "" : String(t[key]);
    input.addEventListener("change", function () {
      commitTagField(t, key, input.value.trim());
    });
    return tagEditField(t, key, label, input);
  }

  function tagChoice(t, key, label, options) {
    var sel = el("select");
    options.forEach(function (o) {
      var op = el("option", null, o);
      op.value = o;
      if (t[key] === o) op.selected = true;
      sel.appendChild(op);
    });
    sel.addEventListener("change", function () {
      commitTagField(t, key, sel.value);
    });
    return tagEditField(t, key, label, sel);
  }

  function tagCheck(t, key, label) {
    var wrap = el("div", "tag-check");
    var box = el("input");
    box.type = "checkbox";
    box.id = "tagf_" + t.name + "_" + key;
    box.checked = !!t[key];
    box.addEventListener("change", function () {
      commitTagField(t, key, box.checked);
    });
    var lab = el("label", null, label);
    lab.setAttribute("for", box.id);
    wrap.appendChild(box);
    wrap.appendChild(lab);
    return wrap;
  }

  /* The table shows on the index and in the inspector. Redraw whichever is
     actually on screen: on the index the other would take the field away from
     whoever is typing in it. */
  function renderTags() {
    if (S.mode === "index") {
      if (S.tagsOpen) renderIndex();
    } else {
      renderSide();
    }
  }

  function loadTags() {
    return api("/api/tags").then(function (d) {
      S.tags = d.tags || [];
    }).catch(function () {});
  }

  function saveTags(tags) {
    return api("/api/tags", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags: tags })
    }).then(function (d) {
      S.tags = d.tags || [];
      renderTags();
      return true;
    }).catch(function (e) {
      toast("tag table refused: " + e.message, "warn");
      return false;
    });
  }

  function writeTag(name, value) {
    api("/api/tags/set", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, value: value })
    }).then(function (d) {
      // The box showing the new value is the acknowledgement; a toast for
      // every keystroke-commit in a table would be constant noise.
      S.tags.forEach(function (t) { if (t.name === name) t.value = d.value; });
    }).catch(function (e) {
      toast("could not write " + name + ": " + e.message, "warn");
    });
  }

  /* A new tag arrives as a row with its fields open, rather than as a queue
     of dialogs to answer before anything exists. */
  function addTag() {
    var taken = {};
    S.tags.forEach(function (t) { taken[t.name] = true; });
    var name = "new_tag", n = 2;
    while (taken[name]) { name = "new_tag_" + n; n += 1; }
    S.tagEdit = name;
    saveTags(S.tags.map(tagDef).concat([{
      name: name, type: "number", initial: null,
      retain: false, desc: "", unit: ""
    }])).then(function (ok) {
      if (!ok) return;
      var field = document.getElementById("tagf_" + name + "_name");
      if (field) { field.focus(); field.select(); }
    });
  }

  function tagDef(t) {
    return { name: t.name, type: t.type, initial: t.initial,
             retain: !!t.retain, share: !!t.share, boot_reset: !!t.boot_reset,
             desc: t.desc || "", unit: t.unit || "" };
  }

  /* What this board can actually do right now, and what is missing. */
  function hardwarePanel() {
    var box = el("div");
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "Buses"));
    box.appendChild(head);
    if (!S.hw) { box.appendChild(el("div", "inspector-empty", "probing\u2026")); return box; }

    var rows = [
      ["I2C", S.hw.i2c.available, S.hw.i2c.hint,
       (S.hw.i2c.buses || []).map(function (b) { return "i2c-" + b.bus; }).join(", ")],
      ["SPI", S.hw.spi.available, S.hw.spi.hint,
       (S.hw.spi.devices || []).map(function (d) { return d.path; }).join(", ")],
      ["PWM (hardware)", S.hw.pwm.hardware, S.hw.pwm.hint,
       (S.hw.pwm.chips || []).map(function (c) { return c.chip + " \u00d7" + c.channels; }).join(", ")],
      ["PWM (software)", true, null, (S.hw.softpwm || []).length + " active"]
    ];
    var sec = el("div", "pane-section");
    rows.forEach(function (r) {
      var line = el("div", "pin-detail");
      var t = el("div");
      t.appendChild(Z.StatusDot({ state: r[1] ? "ok" : "idle", label: r[0] }));
      line.appendChild(t);
      if (r[3]) line.appendChild(el("div", null, r[3]));
      if (!r[1] && r[2]) line.appendChild(el("div", "field-help", r[2]));
      sec.appendChild(line);
    });
    (S.hw.mqtt || []).forEach(function (m) {
      var line = el("div", "pin-detail");
      line.appendChild(Z.StatusDot({ state: m.connected ? "ok" : "critical", label: "MQTT " + m.broker }));
      if (m.topics && m.topics.length) line.appendChild(el("div", null, m.topics.join(", ")));
      if (m.error && !m.connected) line.appendChild(el("div", "field-help", m.error));
      sec.appendChild(line);
    });
    box.appendChild(sec);
    if (S.hw.overlays && (S.hw.overlays.available || []).length) box.appendChild(overlayPicker());
    return box;
  }

  /* The header's buses are device-tree overlays, set in /boot/armbianEnv.txt
     and applied at boot. Choosing here shows the pins on the map above and
     writes the one command to run, since editing /boot needs root. */
  function busChoice() {
    if (!S.busPick) S.busPick = (S.hw.overlays.current || []).slice();
    return S.busPick;
  }

  function busPins() {
    var byPhys = {};
    var avail = (S.hw && S.hw.overlays && S.hw.overlays.available) || [];
    busChoice().forEach(function (name) {
      avail.filter(function (o) { return o.name === name; }).forEach(function (o) {
        o.pins.forEach(function (p) {
          if (p.phys) (byPhys[p.phys] = byPhys[p.phys] || []).push(name);
        });
      });
    });
    return byPhys;
  }

  function overlayCommand(names, file) {
    var edit = "sudo sed -i -e '/^overlays=/d'" +
      (names.length ? " -e '$a overlays=" + names.join(" ") + "'" : "") + " " + file;
    return edit + " && sudo reboot";
  }

  function overlayPicker() {
    var ov = S.hw.overlays;
    var chosen = busChoice();
    var sec = el("div", "pane-section");
    sec.appendChild(el("div", "pin-detail", "Put a bus on the header"));
    sec.appendChild(el("div", "field-help",
      "Choose what the header's pins should be. Their pins light up on the map " +
      "above. Set now: " + ((ov.current || []).join(" ") || "nothing") + "."));

    [["i2c", "I2C"], ["spi", "SPI"], ["pwm", "Hardware PWM"], ["uart", "UART"]].forEach(function (k) {
      var usable = ov.available.filter(function (o) { return o.kind === k[0] && o.on_header; });
      var not = ov.available.filter(function (o) { return o.kind === k[0] && !o.on_header; });
      var group = el("div", "pin-detail");
      group.appendChild(el("div", null, k[1]));
      var row = el("div", "pin-actions");
      row.style.flexWrap = "wrap";
      usable.forEach(function (o) {
        var on = chosen.indexOf(o.name) >= 0;
        var b = Z.Button({
          label: o.name + " \u00b7 " + o.pins.map(function (p) { return p.phys; }).join("/"),
          size: "sm", variant: on ? "primary" : "secondary",
          onClick: function () {
            S.busPick = on ? chosen.filter(function (x) { return x !== o.name; })
                           : chosen.concat([o.name]);
            renderSide();
          }
        });
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.title = o.pins.map(function (p) { return p.pin + " on pin " + p.phys; }).join(", ");
        row.appendChild(b);
      });
      if (!usable.length) row.appendChild(el("span", "field-help", "none reach the header"));
      group.appendChild(row);
      if (not.length) {
        group.appendChild(el("div", "field-help", not.map(function (o) {
          return o.name + ": " + (o.note || "its pins are not on the header");
        }).join("; ")));
      }
      sec.appendChild(group);
    });

    // What would collide: two buses on one pin, or a pin a flow already uses.
    var warn = [];
    var byPhys = busPins();
    Object.keys(byPhys).forEach(function (phys) {
      if (byPhys[phys].length > 1) warn.push("pin " + phys + " is in " + byPhys[phys].join(" and "));
      var p = ((S.pins && S.pins.pins) || []).filter(function (x) { return String(x.phys) === phys; })[0];
      if (p && p.bound && p.bound.length) warn.push("pin " + phys + " is used by " + p.bound.join(", "));
      if (p && p.status === "reserved") warn.push("pin " + phys + " is claimed by " + (p.consumer || "the kernel"));
    });
    warn.forEach(function (w) {
      var line = el("div", "banner-line is-warn");
      line.appendChild(el("span", null, w));
      sec.appendChild(line);
    });

    var same = chosen.slice().sort().join(" ") === (ov.current || []).slice().sort().join(" ");
    if (same) {
      sec.appendChild(el("div", "field-help", "That is what is set now; nothing to run."));
    } else {
      var cmd = overlayCommand(chosen, ov.file || "/boot/armbianEnv.txt");
      var box = el("div", "endpoint");
      box.appendChild(el("div", "endpoint-label", "Run this, then the board reboots"));
      box.appendChild(el("pre", "endpoint-curl", cmd));
      var acts = el("div", "endpoint-actions");
      acts.appendChild(Z.Button({
        label: "Copy command", size: "sm",
        onClick: function () { copyText(cmd, "command copied"); }
      }));
      acts.appendChild(Z.Button({
        label: "Back to what is set", size: "sm", variant: "ghost",
        onClick: function () { S.busPick = (ov.current || []).slice(); renderSide(); }
      }));
      box.appendChild(acts);
      sec.appendChild(box);
    }
    return sec;
  }

  /* showIf: {otherKey: [values]} — hide a setting that cannot apply, so a node
     never shows Pulse (ms) when the action is not pulse. */
  function fieldApplies(n, fl) {
    if (!fl.showIf) return true;
    var cfg = n.config || {};
    for (var key in fl.showIf) {
      var want = fl.showIf[key];
      var have = cfg[key];
      if (have === undefined) {
        // fall back to that field's own default before deciding
        var other = (def(n.type).fields || []).filter(function (f) { return f.key === key; })[0];
        have = other ? other.default : undefined;
      }
      // null means "once that field is set at all": Counter's Then only
      // applies with a target, and a target of 0 is none.
      if (want === null) {
        if (have === null || have === undefined || String(have) === "" || Number(have) === 0) return false;
        continue;
      }
      if (want.indexOf(String(have)) === -1) return false;
    }
    return true;
  }

  /* The webhook node is only useful if you can see and copy its actual URL,
     with the path you typed and a token that works. */
  function webhookHelp(n) {
    var box = el("div", "endpoint");
    var path = ((n.config || {}).path || "").replace(/^\/+/, "");
    if (!path) {
      box.appendChild(el("p", "field-help", "Set a path to get a URL."));
      return box;
    }
    var token = tokenFromPage();
    var url = location.origin + "/api/hook/" + encodeURIComponent(path);
    var shown = url + (token ? "?t=" + token : "");

    box.appendChild(el("div", "endpoint-label", "Endpoint"));
    var u = el("code", "endpoint-url", shown);
    box.appendChild(u);

    var row = el("div", "endpoint-actions");
    row.appendChild(Z.Button({
      label: "Copy URL", size: "sm",
      onClick: function () { copyText(shown, "URL copied"); }
    }));
    var curl = 'curl -X POST "' + shown + '" \\\n  -H "Content-Type: application/json" \\\n  -d \'{"payload":"hello"}\'';
    row.appendChild(Z.Button({
      label: "Copy curl", size: "sm", variant: "ghost",
      onClick: function () { copyText(curl, "curl copied"); }
    }));
    box.appendChild(row);

    box.appendChild(el("pre", "endpoint-curl", curl));
    if (!token) {
      box.appendChild(el("p", "field-help",
        "Add ?t=<token> or an X-Console-Token header; the token is in " +
        "~/.config/auto-blox/token"));
    }
    return box;
  }

  function tokenFromPage() {
    var m = /[?&]t=([^&#]+)/.exec(location.search || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  /* An id with a copy button beside it. Ids here are generated, not chosen,
     so they are the one thing in this editor nobody can retype from memory. */
  function idWithCopy(id, what) {
    var wrap = el("span", "id-copy-wrap");
    wrap.appendChild(el("span", null, id));
    var b = el("button", "id-copy");
    b.type = "button";
    b.title = "copy " + what;
    b.setAttribute("aria-label", "copy " + what + " " + id);
    b.appendChild(glyphCopy());
    b.addEventListener("click", function (ev) {
      // The card behind this opens the editor, and the inspector header is
      // inside a clickable row — neither should fire on a copy.
      ev.preventDefault();
      ev.stopPropagation();
      copyText(id, what + " copied");
    });
    wrap.appendChild(b);
    return wrap;
  }

  function glyphCopy() {
    var svg = sv("svg", { viewBox: "0 0 16 16", width: 11, height: 11,
                          fill: "none", stroke: "currentColor",
                          "stroke-width": 1.5, "stroke-linejoin": "round",
                          "aria-hidden": "true" });
    svg.appendChild(sv("rect", { x: 5.5, y: 5.5, width: 8, height: 8, rx: 1.5 }));
    svg.appendChild(sv("path", { d: "M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5" }));
    return svg;
  }

  function copyText(text, okMsg) {
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); toast(okMsg, "ok"); }
      catch (e) { toast("could not copy \u2014 select it by hand", "warn"); }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast(okMsg, "ok"); }, fallback);
    } else { fallback(); }
  }

  function insertAtCursor(input, text) {
    var start = input.selectionStart, end = input.selectionEnd;
    if (start == null) { input.value += text; }
    else {
      input.value = input.value.slice(0, start) + text + input.value.slice(end);
      var pos = start + text.length;
      input.setSelectionRange(pos, pos);
    }
    input.focus();
    // .value changes do not fire "change"; the field listener needs telling.
    input.dispatchEvent(new Event("change"));
  }

  /* The {x} button beside a field: every variable, grouped, with its example. */
  function varButton(input) {
    var btn = el("button", "var-btn", "{x}");
    btn.type = "button";
    btn.title = "Insert a variable";
    btn.setAttribute("aria-label", "insert a variable");
    attachMenu(btn, function (menu, close) {
      var groups = {};
      (S.variables || []).forEach(function (v) { (groups[v.group] = groups[v.group] || []).push(v); });
      var order = ["Message", "Context", "Readings", "Board"];
      order.concat(Object.keys(groups).filter(function (g) { return order.indexOf(g) === -1; }))
        .forEach(function (g) {
          if (!groups[g]) return;
          menu.appendChild(el("div", "menu-group", g));
          groups[g].forEach(function (v) {
            var it = menuItem("", function () { close(); insertAtCursor(input, "{{" + v.name + "}}"); });
            it.textContent = "";
            it.classList.add("var-item");
            var head = el("span", "var-head");
            head.appendChild(el("code", "var-name", "{{" + v.name + "}}"));
            head.appendChild(el("span", "var-type", v.type || ""));
            head.appendChild(el("span", "var-eg", varNow(v)));
            it.appendChild(head);
            it.appendChild(el("span", "var-desc", v.desc || ""));
            it.title = v.desc || "";
            menu.appendChild(it);
          });
        });
    }, "is-vars");
    return btn;
  }

  /* Where a `combo` field gets its list. Each returns [{value, label}] from
     state the editor already holds, so opening the inspector costs no fetch.
     `n` is the node being edited, for options that depend on its own config. */
  var COMBO_SOURCES = {
    flows: function () {
      return sortedFlows().map(function (f) {
        return { value: f.id, label: (f.name || "Untitled flow") + " · " + f.id };
      });
    },
    /* The nodes of whichever flow the sibling field names. Triggers first:
       calling a flow means firing one of its triggers, and the rest are only
       here because a node id is a node id. */
    nodes_in_flow: function (n, fl) {
      var target = (n.config || {})[fl.from_key || "flow"];
      var f = S.doc.flows.filter(function (x) { return x.id === target; })[0];
      if (!f) return [];
      var all = (f.nodes || []).map(function (nd) {
        var d = def(nd.type);
        return { value: nd.id, label: (d.label || nd.type) + " · " + nd.id,
                 trigger: d.kind === "trigger" };
      });
      return all.filter(function (o) { return o.trigger; })
        .concat(all.filter(function (o) { return !o.trigger; }));
    },
    devices: function () {
      return (S.devices || []).map(function (d) {
        return { value: d.id, label: (d.name || d.id) +
                 (d.board ? " · " + boardLabel(d.board) : "") };
      }).concat((S.groups || []).map(function (g) {
        return { value: "group:" + g.name,
                 label: g.name + " · group of " + g.devices.length };
      }));
    },
    /* What this host's Bluetooth adapter knows, connected ones first. */
    ble_devices: function () {
      return (S.ble || []).map(function (d) {
        return { value: d.mac, label: (d.name || d.mac) + " · " + d.mac +
                 (d.connected ? " · connected" : (d.paired ? " · paired" : "")) };
      });
    },
    tags: function () {
      return (S.tags || []).map(function (t) {
        return { value: t.name,
                 label: t.name + " · " + t.type
                        + (t.unit ? " (" + t.unit + ")" : "") };
      });
    }
  };

  /* The "type it myself" option needs a value no real id would have. A node
     actually called this would only ever get the text box, which is harmless. */
  var COMBO_CUSTOM = "__type_it__";

  /* A dropdown of what exists, without giving up free text — an id can still
     be typed, or templated, which the list could never offer. */
  function comboEl(n, fl, cur, commit) {
    var box = el("div", "combo");
    var options = (COMBO_SOURCES[fl.options_from] || function () { return []; })(n, fl);
    var known = options.some(function (o) { return String(o.value) === String(cur); });
    var typing = cur != null && cur !== "" && !known;

    var sel = el("select");
    var none = el("option", null, fl.placeholder || "— choose —");
    none.value = "";
    sel.appendChild(none);
    options.forEach(function (o) {
      var op = el("option", null, o.label);
      op.value = o.value;
      if (!typing && String(cur) === String(o.value)) op.selected = true;
      sel.appendChild(op);
    });
    var custom = el("option", null, "Type it myself…");
    custom.value = COMBO_CUSTOM;
    if (typing) custom.selected = true;
    sel.appendChild(custom);

    var text = el("input");
    text.type = "text";
    text.value = cur == null ? "" : cur;
    text.className = "combo-text";
    if (!typing) text.style.display = "none";

    sel.addEventListener("change", function () {
      if (sel.value === COMBO_CUSTOM) {
        text.style.display = "";
        text.focus();
        return;                       // nothing committed until they type
      }
      text.style.display = "none";
      text.value = sel.value;
      commit(sel.value);
      renderSide();                   // a choice can feed a sibling's list
    });
    text.addEventListener("change", function () { commit(text.value); });
    text.addEventListener("focus", function () { S.lastField = text; });

    box.appendChild(sel);
    box.appendChild(text);
    if (!options.length) {
      box.appendChild(el("div", "field-help", fl.empty || "Nothing to choose from yet."));
    }
    return { box: box, focusable: sel };
  }

  function fieldEl(n, fl) {
    var note = null;
    var wrap = el("div", "field");
    var id = "f_" + n.id + "_" + fl.key;
    var lab = el("label", null, fl.label);
    lab.setAttribute("for", id);
    wrap.appendChild(lab);
    var cur = (n.config || {})[fl.key];
    var input, templatable = false;

    function commit(value) {
      snapshot();
      n.config = n.config || {};
      n.config[fl.key] = value;
      markDirty();
      renderCanvas();
    }

    if (fl.kind === "combo") {
      var combo = comboEl(n, fl, cur, commit);
      combo.focusable.id = id;
      lab.setAttribute("for", id);
      wrap.appendChild(combo.box);
      if (fl.help) wrap.appendChild(el("div", "field-help", fl.help));
      return wrap;
    }

    if (fl.kind === "select" || fl.kind === "pin") {
      input = el("select");
      if (fl.kind === "pin") {
        var none = el("option", null, "\u2014 choose a pin \u2014"); none.value = ""; input.appendChild(none);
        var onBoard = !!S.profile;
        (onBoard ? boardPins() : (S.pins ? S.pins.pins : [])).forEach(function (p) {
          if (p.kind !== "gpio") return;
          var text;
          if (onBoard) {
            // The capabilities are the point here: an ADC pin and a plain one
            // look identical until something reads nothing at 2am.
            var caps = (p.caps || []).filter(function (c) {
              return c !== "in" && c !== "out";
            });
            text = p.label + (caps.length ? " \u00b7 " + caps.join(", ") : "") +
              (p.status === "reserved" ? " (not available)" : "");
          } else {
            text = "pin " + p.phys + " \u00b7 " + p.label +
              (p.status === "reserved" ? " (reserved)" : p.status === "bound" ? " (in use)" : "");
          }
          var o = el("option", null, text);
          o.value = String(p.gpio);
          if (p.note) o.title = p.note;
          if (p.status === "reserved") o.disabled = true;
          if (String(cur) === String(p.gpio)) o.selected = true;
          input.appendChild(o);
        });
      } else {
        (fl.options || []).forEach(function (opt) {
          var o = el("option", null, opt); o.value = opt;
          if (String(cur) === String(opt)) o.selected = true;
          input.appendChild(o);
        });
      }
      input.addEventListener("change", function () {
        commit(fl.kind === "pin" ? (input.value === "" ? null : Number(input.value)) : input.value);
        renderSide();   // a choice can reveal or hide other fields
      });
      if (fl.kind === "pin") {
        // These are not this board's header pins, and the difference is not
        // obvious from a list of numbers.
        note = S.profile
          ? ("pins on the " + S.profile.label + ", not this board's 40-pin header")
          : "pins on this board's 40-pin header";
      }
    } else if (fl.kind === "textarea") {
      templatable = true;
      input = el("textarea");
      input.value = cur == null ? "" : cur;
      input.addEventListener("change", function () { commit(input.value); });
    } else {
      templatable = true;
      // A number field holding a template has to be a text box, so the field
      // switches type rather than refusing the value.
      var isTpl = typeof cur === "string" && cur.indexOf("{{") !== -1;
      input = el("input");
      input.type = (fl.kind === "number" && !isTpl) ? "number" : "text";
      if (fl.kind === "number" && !isTpl) {
        if (fl.min != null) input.min = fl.min;
        if (fl.max != null) input.max = fl.max;
      }
      input.value = cur == null ? "" : cur;
      input.addEventListener("change", function () {
        var v = input.value;
        if (fl.kind === "number" && input.type === "number") v = v === "" ? null : Number(v);
        commit(v);
      });
    }
    input.id = id;
    input.addEventListener("focus", function () { S.lastField = input; });

    if (templatable) {
      var row = el("div", "field-row");
      row.appendChild(input);
      row.appendChild(varButton(input));
      wrap.appendChild(row);
      if (fl.kind === "number") {
        var hint = el("div", "field-help",
          input.type === "number"
            ? "Accepts a variable too \u2014 pick one with {x} to switch this to a formula."
            : "Formula mode. Clear the field to go back to a plain number.");
        wrap.appendChild(hint);
      }
    } else {
      wrap.appendChild(input);
    }
    if (fl.help) wrap.appendChild(el("div", "field-help", fl.help));
    if (note) wrap.appendChild(el("div", "field-help", note));
    return wrap;
  }

  /* What the variable holds right now, or its sample when it has no value in
     the current context (meta.* is only set by the trigger that produced it). */
  function varNow(v) {
    if (v.value != null && v.value !== "") return v.value;
    if (v.example != null) return "eg " + v.example;
    return "";
  }

  /* The browsable library, under the node's own fields. */
  function variableLibrary() {
    var box = el("details", "var-lib");
    if (S.libOpen) box.open = true;
    box.addEventListener("toggle", function () { S.libOpen = box.open; });
    var sum = el("summary", null, "Variable library (" + (S.variables || []).length + ")");
    box.appendChild(sum);
    var groups = {};
    (S.variables || []).forEach(function (v) { (groups[v.group] = groups[v.group] || []).push(v); });
    ["Message", "Context", "Readings", "Board"].forEach(function (g) {
      if (!groups[g]) return;
      box.appendChild(el("div", "menu-group", g));
      groups[g].forEach(function (v) {
        var row = el("button", "var-row");
        row.type = "button";
        row.title = "Insert into the field you last used";
        row.appendChild(el("code", "var-name", "{{" + v.name + "}}"));
        row.appendChild(el("span", "var-type", v.type || ""));
        row.appendChild(el("span", "var-eg", varNow(v)));
        row.appendChild(el("span", "var-desc", v.desc || ""));
        row.addEventListener("click", function () {
          if (S.lastField && document.contains(S.lastField)) {
            insertAtCursor(S.lastField, "{{" + v.name + "}}");
          } else {
            toast("click a text field first, then pick a variable", "warn");
          }
        });
        box.appendChild(row);
      });
    });
    return box;
  }

  function pinByGpio(g) {
    if (!S.pins) return null;
    var hit = S.pins.pins.filter(function (p) { return p.gpio === Number(g); });
    return hit[0] || null;
  }

  function pinMap() {
    var box = el("div");
    var head = el("div", "pane-head");
    head.appendChild(el("span", null, "40-pin header"));
    if (S.pins && S.pins.access) {
      head.appendChild(Z.Badge({
        text: S.pins.access.ok ? "live" : "no access",
        tone: S.pins.access.ok ? "ok" : "critical"
      }));
    }
    box.appendChild(head);

    if (S.pins && S.pins.access && !S.pins.access.ok) {
      var warn = el("div", "banner-line is-warn");
      warn.appendChild(el("span", null, S.pins.access.hint || S.pins.access.error || "GPIO unavailable"));
      box.appendChild(warn);
    }

    var legend = el("div", "pin-legend");
    [["free", "free"], ["bound", "in a flow"], ["reserved", "kernel"], ["power", "power"], ["ground", "gnd"]]
      .forEach(function (p) {
        var s = el("span");
        var d = el("i", "pin-dot pin-dot--" + p[0]); d.style.display = "inline-block";
        s.appendChild(d); s.appendChild(document.createTextNode(p[1]));
        legend.appendChild(s);
      });
    box.appendChild(legend);

    var grid = el("div", "pinmap");
    grid.style.padding = "8px 12px";
    var left = el("div"), right = el("div");
    var buses = S.hw && S.hw.overlays ? busPins() : {};
    (S.pins ? S.pins.pins : []).forEach(function (p) {
      var bus = buses[p.phys];
      var b = el("button", "pin" + (p.row === "right" ? " pin--right" : "") +
        (S.pinSel === p.phys ? " is-selected" : "") +
        (bus ? " is-bus" : "") +
        (p.kind === "gpio" ? " is-clickable" : ""));
      b.type = "button";
      b.appendChild(el("span", "pin-num", String(p.phys)));
      var dot = el("span", "pin-dot pin-dot--" + (p.kind === "gpio" ? p.status : p.kind));
      b.appendChild(dot);
      var lbl = el("span", "pin-label" + (p.held ? " pin-high" : ""),
                   p.label + (p.held ? " ▲" : "") + (bus ? " \u00b7 " + bus.join(", ") : ""));
      b.appendChild(lbl);
      b.title = pinTitle(p) + (bus ? "\nwould be " + bus.join(", ") : "");
      if (p.kind === "gpio") {
        b.addEventListener("click", function () { S.pinSel = (S.pinSel === p.phys ? null : p.phys); renderSide(); });
      }
      (p.row === "left" ? left : right).appendChild(b);
    });
    grid.appendChild(left); grid.appendChild(right);
    box.appendChild(grid);

    if (S.pinSel != null) {
      var p = (S.pins.pins || []).filter(function (x) { return x.phys === S.pinSel; })[0];
      if (p) box.appendChild(pinDetail(p));
    }
    return box;
  }

  function pinTitle(p) {
    if (p.kind !== "gpio") return "pin " + p.phys + " · " + p.label;
    var t = "pin " + p.phys + " · " + p.label + " · gpio " + p.gpio + " · wPi " + p.wpi;
    if (p.consumer) t += "\nclaimed by: " + p.consumer;
    if (p.bound && p.bound.length) t += "\nused by: " + p.bound.join(", ");
    return t;
  }

  function pinDetail(p) {
    var d = el("div", "pin-detail");
    function row(k, v) {
      var r = el("div");
      r.appendChild(document.createTextNode(k + " "));
      var b = el("b", null, v); r.appendChild(b);
      d.appendChild(r);
    }
    row("physical", "pin " + p.phys);
    row("bank", p.label);
    row("sunxi gpio", String(p.gpio));
    row("wiringOP", "wPi " + p.wpi);
    row("chip/line", "gpiochip" + p.chip + " : " + p.line);
    row("status", p.status + (p.consumer ? " (" + p.consumer + ")" : ""));
    if (p.bound && p.bound.length) row("flows", p.bound.join(", "));
    if (p.held != null) row("driven", p.held ? "HIGH by this console" : "LOW by this console");

    if (p.status !== "reserved" && S.pins.access.ok && S.config.gpio_write) {
      var acts = el("div", "pin-actions");
      acts.appendChild(Z.Button({ label: "High", size: "sm", variant: "primary", onClick: function () { writePin(p.gpio, 1); } }));
      acts.appendChild(Z.Button({ label: "Low", size: "sm", onClick: function () { writePin(p.gpio, 0); } }));
      acts.appendChild(Z.Button({ label: "Release", size: "sm", variant: "ghost", onClick: function () { releasePin(p.gpio); } }));
      d.appendChild(acts);
      d.appendChild(el("div", "field-help", "Test drives this pin directly. Release returns it to high-impedance input."));
    }
    return d;
  }

  function writePin(gpio, v) {
    api("/api/gpio/write", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gpio: gpio, value: v }) })
      .then(function () { toast("gpio " + gpio + " -> " + v, "ok"); loadPins(); })
      .catch(function (e) { toast(e.message, "critical"); });
  }
  function releasePin(gpio) {
    api("/api/gpio/release", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gpio: gpio }) })
      .then(function () { loadPins(); }).catch(function () {});
  }

  /* ---------- run log --------------------------------------------------- */
  function renderFoot() {
    var foot = document.getElementById("studio-foot");
    if (!foot) return;
    foot.innerHTML = "";
    foot.appendChild(Z.LogStream({
      entries: S.runs.map(runRow), rows: 8, emptyText: "no flow activity yet"
    }));
    var list = foot.querySelector(".z-logs-list");
    if (list) list.scrollTop = list.scrollHeight;
  }

  function runRow(r) {
    return { time: r.time, level: r.level, unit: r.node, message: r.message };
  }

  /* Append one run-log line, keeping the newest in view unless the reader has
     scrolled up to read something. */
  function appendFoot(r) {
    var foot = document.getElementById("studio-foot");
    var list = foot && foot.querySelector(".z-logs-list");
    if (!list) { renderFoot(); return; }
    Z.LogAppend(list, runRow(r), 200);
  }

  function toast(msg, level) {
    var row = { time: new Date().toTimeString().slice(0, 8), level: level || "idle", node: "studio", message: msg };
    S.runs.push(row);
    if (S.runs.length > 200) S.runs = S.runs.slice(-200);
    appendFoot(row);
  }
  /* A Step's active state, kept for re-renders and applied to the live node. */
  function setStepActive(r) {
    var key = (r.flow || "") + "/" + r.node;
    var on = !!(r.payload && r.payload.active);
    if (on) S.activeSteps[key] = true; else delete S.activeSteps[key];
    var n = document.querySelector('.node[data-id="' + r.node + '"]');
    var f = flow();
    if (n && f && f.id === r.flow) n.classList.toggle("is-active", on);
  }

  function flashNode(id) {
    var n = document.querySelector('.node[data-id="' + id + '"]');
    if (!n) return;
    n.classList.add("is-firing");
    setTimeout(function () { n.classList.remove("is-firing"); }, 400);
  }

  /* ---------- data ------------------------------------------------------ */
  /* A flow bound to a board takes its palette and its pins from that board's
     profile instead of this host's header. Both come from the generated
     profile, so there is one source for what a pin can do. */
  function loadBoard() {
    var f = flow();
    var board = f && f.board;
    if (!board) {
      S.profile = null;
      S.registry = S.registryAll;
      return loadPins();
    }
    return Promise.all([
      api("/api/iot/boards?board=" + encodeURIComponent(board))
        .then(function (d) { S.profile = d; })
        .catch(function () { S.profile = null; }),
      api("/api/flows/registry?board=" + encodeURIComponent(board))
        .then(function (d) { S.registry = S.boardNodes[board] = d.nodes || {}; })
        .catch(function () { S.registry = S.registryAll; }),
      loadDeviceState()
    ]).then(function () {
      renderPalette(); renderCanvas(); renderSide(); renderBar();
    });
  }

  /* What the linked device last said about itself. Absent is a real answer —
     it means nothing has ever reported, which is worth showing plainly. */
  function loadDeviceState() {
    var f = flow();
    if (!f || !f.device) { S.devstate = null; return Promise.resolve(); }
    return api("/api/iot/devices/" + encodeURIComponent(f.device) + "/pins")
      .then(function (d) { S.devstate = d; })
      .catch(function () { S.devstate = null; });
  }

  /* The profile's pins in the shape the pin dropdown already speaks, marked
     with what this flow does to each and what the device last reported. */
  function boardPins() {
    if (!S.profile) return [];
    var f = flow();
    var used = {};
    ((f && f.nodes) || []).forEach(function (n) {
      var gpio = (n.config || {}).gpio;
      if (gpio === null || gpio === undefined || gpio === "") return;
      gpio = Number(gpio);
      (used[gpio] = used[gpio] || []).push(n);
    });
    var live = {};
    if (S.devstate) {
      (S.devstate.pins || []).forEach(function (p) {
        if (p.live) live[p.gpio] = p.live;
      });
    }
    return S.profile.pins.map(function (p) {
      var on = used[p.gpio] || [];
      return {
        gpio: p.gpio, phys: null, label: p.name, kind: "gpio",
        status: !p.usable ? "reserved" : (on.length ? "bound" : "free"),
        caps: p.caps, note: p.note, input_only: p.input_only,
        strapping: p.strapping, used_by: on,
        live: live[p.gpio] || null,
        held: !!(live[p.gpio] && live[p.gpio].value)
      };
    });
  }

  function loadPins() {
    return Promise.all([
      api("/api/gpio/pins").then(function (d) { S.pins = d; }).catch(function () {}),
      api("/api/hardware").then(function (d) { S.hw = d; }).catch(function () {})
    ]).then(function () { renderSide(); });
  }

  /* What the background poll calls. Rebuilding the inspector under someone who
     is typing in it, or scrolling it, is what made the pane jump and drop focus,
     so the poll defers instead of redrawing. */
  function refreshSide() {
    if (document.hidden) return Promise.resolve();   // nothing to redraw into
    return Promise.all([
      api("/api/gpio/pins").then(function (d) { S.pins = d; }).catch(function () {}),
      api("/api/hardware").then(function (d) { S.hw = d; }).catch(function () {}),
      loadDeviceState()
    ]).then(function () {
      if (S.openMenu) return;                       // a dropdown is open
      var side = document.getElementById("side");
      if (side && side.contains(document.activeElement)) return;  // being used
      var sig = pinSignature();
      if (sig === S.pinSig) return;                 // nothing actually changed
      S.pinSig = sig;
      renderSide();
    });
  }

  /* Cheap fingerprint of everything the pin map and buses panel draw. */
  function pinSignature() {
    if (!S.pins) return "";
    var parts = [S.pins.access && S.pins.access.ok ? "1" : "0"];
    (S.pins.pins || []).forEach(function (p) {
      if (p.kind !== "gpio") return;
      parts.push(p.phys + ":" + p.status + ":" + (p.held == null ? "" : p.held) +
                 ":" + (p.consumer || "") + ":" + (p.bound || []).length);
    });
    if (S.hw) {
      parts.push((S.hw.i2c && S.hw.i2c.available ? "i" : "") +
                 (S.hw.spi && S.hw.spi.available ? "s" : "") +
                 (S.hw.pwm && S.hw.pwm.hardware ? "p" : "") +
                 ((S.hw.softpwm || []).length));
    }
    return parts.join("|");
  }
  /* Frame the whole graph so a saved flow is visible without panning. */
  function fitView() {
    var f = flow();
    var wrap = document.getElementById("canvas-wrap");
    if (!f || !(f.nodes || []).length || !wrap) { S.view = { x: 40, y: 40, k: 1 }; return applyView(); }
    var xs = f.nodes.map(function (n) { return n.x; });
    var ys = f.nodes.map(function (n) { return n.y; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs) + NODE_W;
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys) + 90;
    var r = wrap.getBoundingClientRect();
    var pad = 40;
    var k = Math.min(1, (r.width - pad * 2) / Math.max(1, maxX - minX),
                        (r.height - pad * 2) / Math.max(1, maxY - minY));
    // Never auto-fit below a readable size. Node body text is 10px, so at 0.55
    // it renders around 5px. Narrow screens get a higher floor and pan instead;
    // manual zoom still reaches 0.3 for a deliberate overview.
    var floor = document.documentElement.clientWidth < 700 ? 0.85 : 0.55;
    k = Math.max(floor, k);
    S.view.k = k;
    S.view.x = pad - minX * k + Math.max(0, (r.width - pad * 2 - (maxX - minX) * k) / 2);
    S.view.y = pad - minY * k;
    applyView();
  }

  function boot() {
    S.nav = window.Zero2WNav.mount("dock", "/flows");
    // The clock in the bar is only honest if something winds it.
    setInterval(function () {
      if (S.nav && !document.hidden) {
        S.nav.setClock(new Date().toTimeString().slice(0, 8));
      }
    }, 1000);

    var scrim = el("div", "drawer-scrim");
    scrim.addEventListener("click", closeDrawers);
    document.body.appendChild(scrim);

    // A panel that only closes by its own button is a panel left open.
    document.addEventListener("click", function () {
      if (S.linkPop) { S.linkPop = false; renderBar(); }
    });

    var wrap = document.getElementById("canvas-wrap");
    wrap.addEventListener("pointerdown", function (ev) {
      if (ev.target !== wrap && ev.target.id !== "links" && ev.target.id !== "nodes") return;
      trackDown(ev);
      if (pointerCount() >= 2) { beginPinch(); return; }
      S.pan = { sx: ev.clientX, sy: ev.clientY, vx: S.view.x, vy: S.view.y };
      wrap.classList.add("is-panning");
      S.sel = null; renderCanvas(); renderSide();
    });
    wrap.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var r = wrap.getBoundingClientRect();
      var mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var k = Math.min(2, Math.max(0.3, S.view.k * (ev.deltaY < 0 ? 1.1 : 0.9)));
      S.view.x = mx - (mx - S.view.x) * (k / S.view.k);
      S.view.y = my - (my - S.view.y) * (k / S.view.k);
      S.view.k = k;
      applyView();
    }, { passive: false });
    wrap.addEventListener("dragover", function (ev) { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
    wrap.addEventListener("drop", function (ev) {
      ev.preventDefault();
      var type = ev.dataTransfer.getData("text/plain");
      if (!type || !S.registry[type]) return;
      var p = canvasPoint(ev);
      addNode(type, p.x - NODE_W / 2, p.y - 20);
    });
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (ev.key === "Escape") {
        if (document.body.className.indexOf("drawer-") !== -1) { closeDrawers(); return; }
        if (S.mode === "editor") { setView("index"); return; }
      }
      if (S.mode !== "editor") return;
      if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); removeSelected(); }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) redo(); else undo();
      }
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y") { ev.preventDefault(); redo(); }
    });
    window.addEventListener("beforeunload", function (ev) {
      if (S.dirty) { ev.preventDefault(); ev.returnValue = ""; }
    });


    Promise.all([
      api("/api/flows/registry").then(function (d) {
        S.registry = d.nodes || {};
        S.registryAll = d.nodes || {};
        S.variables = d.variables || [];
      }),
      api("/api/iot/boards").then(function (d) { S.boards = d.boards || []; })
        .catch(function () {}),
      api("/api/iot/devices").then(function (d) {
        S.devices = d.devices || [];
        S.groups = d.groups || [];
      })
        .catch(function () {}),
      api("/api/flows").then(function (d) {
        S.doc = d && d.flows ? d : { flows: [] };
        S.rev = S.doc.rev || 0;
        delete S.doc.rev;
        S.flowId = S.doc.flows.length ? S.doc.flows[0].id : null;
      }),
      api("/api/config").then(function (c) { S.config = c; }).catch(function () {}),
      api("/api/flows/runs").then(function (d) { S.runs = d.runs || []; }).catch(function () {}),
      api("/api/flows/outputs").then(function (d) {
        S.outputs = d.outputs || {};
        S.activeSteps = {};
        (d.steps || []).forEach(function (k) { S.activeSteps[k] = true; });
      }).catch(function () {}),
      loadTags(),
      api("/api/bt").then(function (d) { S.ble = d.devices || []; })
        .catch(function () {}),
      loadExamples(),
      loadPins()
    ]).then(function () {
      // ?flow=<id> and #edit/<id> both open the editor. The query form is the
      // shareable one: fragments get dropped by some link handlers.
      var q = /[?&]flow=([^&#]+)/.exec(location.search || "");
      var h = /^#edit\/(.+)$/.exec(location.hash || "");
      var want = q ? decodeURIComponent(q[1]) : (h ? h[1] : null);
      var qn = /[?&]node=([^&#]+)/.exec(location.search || "");
      var ql = /[?&]link=([^&#]+)/.exec(location.search || "");
      // Caught here, so a render error cannot fail silently into the index.
      try {
        if (want && S.doc.flows.some(function (f) { return f.id === want; })) {
          openEditor(want, qn ? decodeURIComponent(qn[1]) : null,
                     ql ? decodeURIComponent(ql[1]) : null);
        } else {
          setView("index");
        }
      } catch (err) {
        setView("index");
        showFatal("Could not open that flow: " + (err && err.message ? err.message : err));
      }
      renderFoot();
    }).catch(function (e) {
      setView("index");
      showFatal("Load failed: " + (e && e.message ? e.message : e));
    });

    window.addEventListener("hashchange", function () {
      var m = /^#edit\/(.+)$/.exec(location.hash || "");
      if (m && S.flowId !== m[1] && S.doc.flows.some(function (f) { return f.id === m[1]; })) {
        openEditor(m[1]);
      } else if (!m && S.mode !== "index") {
        setView("index");
      }
    });

    if (/[?&]snapshot=1\b/.test(location.search)) { return; }
    var es = new EventSource("/api/stream");
    es.addEventListener("open", function () {
      if (S.nav) S.nav.setState("ok", "live", true);
    });
    es.onerror = function () {
      if (S.nav) S.nav.setState("critical", "no stream", false);
    };
    /* A tag moved. Patch the value in place rather than re-rendering the
       inspector, which would take the field away from whoever is typing. */
    es.addEventListener("tag", function (ev) {
      try {
        var t = JSON.parse(ev.data);
        S.tags.forEach(function (row) {
          if (row.name === t.name) row.value = t.value;
        });
        // Whichever copy of the table is on screen. Never while it has the
        // caret: a value updating under someone mid-edit loses what they typed.
        var host = S.mode === "index"
          ? document.getElementById("index-view")
          : document.getElementById("side");
        if (!host || host.contains(document.activeElement)) return;
        Array.prototype.forEach.call(host.querySelectorAll(".tag-row"), function (row) {
          var code = row.querySelector(".tag-name");
          var box = row.querySelector(".tag-value");
          if (code && box && code.textContent === "{{tag." + t.name + "}}") {
            box.value = t.value === null || t.value === undefined ? "" : String(t.value);
            box.classList.add("is-fresh");
            setTimeout(function () { box.classList.remove("is-fresh"); }, 600);
          }
        });
      } catch (e) {}
    });
    es.addEventListener("flow", function (ev) {
      try {
        var r = JSON.parse(ev.data);
        // A device reports every node it ran, which is what makes the canvas
        // look alive — but those are not log lines. Flash them and say nothing.
        if (r.kind === "fired") { flashNode(r.node); return; }
        if (r.kind === "step") setStepActive(r);
        // A device chooses its own `kind`, so "output" from a device counts
        // only when the console marked it as taken from the board's report.
        if (r.kind === "output" && (!r.device || r.from_report === true)) {
          noteOutput(r); return;
        }
        S.runs.push(r);
        if (S.runs.length > 200) S.runs = S.runs.slice(-200);
        appendFoot(r); flashNode(r.node);
      } catch (e) {}
    });
    setInterval(refreshSide, 4000);

  }

  document.addEventListener("DOMContentLoaded", boot);
})();
