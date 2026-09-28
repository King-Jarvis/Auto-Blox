/* The nav bar, in one place: every screen carries the same four buttons in the
 * same order, so the bar never moves under you. The Dock component renders
 * buttons and knows nothing about hrefs, so the wiring is ours. A page may pass
 * `extra` items; they come after the screens, behind a divider, and are handed
 * back so the caller can wire them itself.
 */
(function (root) {
  "use strict";
  var Z = root.Zero2W;
  var NS = "http://www.w3.org/2000/svg";

  function glyph(d, size) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", size || 14);
    svg.setAttribute("height", size || 14);
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.5");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
    return svg;
  }

  /* The four screens, in the order they appear everywhere. */
  function screens() {
    return [
      { label: "Overview", href: "/",
        glyph: glyph("M2 9h5V3H2zM9 13h5V7H9zM2 13h5v-2H2zM9 5h5V3H9z") },
      { label: "Flows", href: "/flows",
        glyph: glyph("M3 4h4v4H3zM9 8h4v4H9zM7 6h2M11 8V6H7") },
      { label: "IOT", href: "/iot",
        glyph: glyph("M8 12h.01M4.5 9.5a5 5 0 017 0M2 6.5a9 9 0 0112 0") },
      { label: "Cameras", href: "/cameras",
        glyph: glyph("M2 5.5h2.5L6 3.5h4l1.5 2H14v7H2zM10 9a2 2 0 11-4 0 2 2 0 014 0") }
    ];
  }

  /* `active` is the href of the screen you are on. `extra` is an optional list
     of further dock items; each rendered button is returned alongside its item. */
  function mount(mountId, active, extra) {
    var host = document.getElementById(mountId);
    if (!host) return null;
    var items = screens();
    items.forEach(function (it) {
      if (it.href === active) { it.active = true; }
    });
    var breakAt = items.length;
    items = items.concat(extra || []);

    var node = Z.Dock({
      host: "auto-blox", address: location.host,
      state: "idle", stateLabel: "connecting",
      clock: "--:--:--", items: items
    });
    // The theme switch rides along at the right-hand end, so it is in the same
    // place on every screen rather than rebuilt into each page's own bar.
    var tail = node.querySelector(".z-dock-tail");
    if (tail) tail.appendChild(themeButton());
    host.textContent = "";
    host.appendChild(node);

    var buttons = node.querySelectorAll(".z-dock-item");
    var pairs = [];
    Array.prototype.forEach.call(buttons, function (b, i) {
      var it = items[i];
      if (!it) return;
      // A hairline between the screens and whatever a page appended.
      if (i === breakAt) b.classList.add("is-nav-break");
      if (it.href && it.href !== active) {
        b.addEventListener("click", function () { location.href = it.href; });
      } else if (it.href === active) {
        b.disabled = false;                   // you are here; the click is a no-op
        b.addEventListener("click", function () {
          root.scrollTo({ top: 0, behavior: "smooth" });
        });
      }
      pairs.push({ item: it, button: b });
    });

    return {
      el: node,
      items: pairs,
      extras: pairs.slice(breakAt),
      setClock: function (text) {
        var c = node.querySelector(".z-dock-clock");
        if (c && text) c.textContent = text;
      },
      setHost: function (text) {
        var h = node.querySelector(".z-dock-host");
        if (h && text) h.textContent = text;
      },
      /* Replaces only the dot, so nothing else in the tail is disturbed. */
      setState: function (st, label, pulse) {
        var tail = node.querySelector(".z-dock-tail");
        var dot = tail && tail.querySelector(".z-dot-row");
        if (!tail || !dot) return;
        tail.replaceChild(Z.StatusDot({ state: st, label: label, pulse: !!pulse }), dot);
      }
    };
  }

  /* ---- theme ------------------------------------------------------------
     The theme belongs to the console, not the browser: it is saved on the Pi
     and every page is served already wearing it (data-theme on <html>), so the
     phone, the desktop and the sign-in page agree and nothing flashes. This
     menu lists Dark, Light and anything imported, imports a theme file, and
     hands out the kit for designing one with Claude. ?theme=dark|light|custom
     shows one for this page load only, which is how the page checks run. */
  var themes = { list: [], active: null };
  var menuEl = null;

  function themeNow() {
    return document.documentElement.getAttribute("data-theme") || "dark";
  }

  function api(path, body) {
    var opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    };
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ("HTTP " + r.status)); e.body = j; throw e; }
        return j;
      });
    });
  }

  /* Swap the page to a theme without a reload: the built-in two are in
     tokens.css already; an imported one is /theme.css, fetched again. */
  function apply(id) {
    var custom = id !== "dark" && id !== "light";
    var link = document.querySelector('link[href^="/theme.css"]');
    if (custom && link) link.href = "/theme.css?v=" + Date.now();
    document.documentElement.setAttribute("data-theme", custom ? "custom" : id);
  }

  function took(j) {
    if (j && j.themes) { themes.list = j.themes; themes.active = j.active; }
    return j;
  }

  function useTheme(id) {
    return api("/api/themes/active", { id: id }).then(took).then(function () {
      apply(id);
      if (menuEl) fillMenu(menuEl);
    });
  }

  function closeMenu() {
    if (!menuEl) return;
    menuEl.remove(); menuEl = null;
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
  }
  function outside(ev) {
    if (menuEl && !menuEl.contains(ev.target) && !ev.target.closest("[data-theme-btn]")) closeMenu();
  }
  function onKey(ev) { if (ev.key === "Escape") closeMenu(); }

  function item(text, onClick, cls) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "theme-item" + (cls ? " " + cls : "");
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  }

  function fillMenu(menu, problems) {
    menu.textContent = "";
    var head = document.createElement("div");
    head.className = "theme-head";
    head.textContent = "Theme";
    menu.appendChild(head);

    themes.list.forEach(function (t) {
      var on = t.id === themes.active;
      var it = item((on ? "✓ " : "") + t.name, function () {
        if (!on) useTheme(t.id).catch(function (e) { fillMenu(menu, [e.message]); });
      }, on ? "is-on" : "");
      it.setAttribute("aria-pressed", on ? "true" : "false");
      if (!t.builtin) it.appendChild(tagSpan(t.base));
      menu.appendChild(it);
    });

    var sep = document.createElement("div"); sep.className = "theme-sep"; menu.appendChild(sep);

    var file = document.createElement("input");
    file.type = "file"; file.accept = ".json,application/json"; file.hidden = true;
    file.addEventListener("change", function () {
      var f = file.files && file.files[0];
      if (!f) return;
      f.text().then(function (text) {
        var doc;
        try { doc = JSON.parse(text); }
        catch (e) { throw new Error("that file is not JSON: " + e.message); }
        return api("/api/themes", doc);
      }).then(took).then(function (j) {
        return useTheme(j.id);
      }).catch(function (e) {
        fillMenu(menu, (e.body && e.body.problems) || [e.message]);
      });
    });
    menu.appendChild(file);
    menu.appendChild(item("Import a theme…", function () { file.click(); }));

    var kit = document.createElement("a");
    kit.className = "theme-item";
    kit.href = "/api/themes/kit.zip";
    kit.setAttribute("download", "auto-blox-theme.zip");
    kit.textContent = "Download the theme kit";
    kit.title = "A Claude skill: give it photos or a mood and it designs a theme to import here";
    menu.appendChild(kit);

    var mine = themes.list.filter(function (t) { return t.id === themes.active && !t.builtin; })[0];
    if (mine) {
      menu.appendChild(item("Delete " + mine.name, function () {
        api("/api/themes/delete", { id: mine.id }).then(took).then(function () {
          apply(themes.active); fillMenu(menu);
        }).catch(function (e) { fillMenu(menu, [e.message]); });
      }, "is-danger"));
    }

    if (problems && problems.length) {
      var box = document.createElement("div");
      box.className = "theme-problems";
      box.setAttribute("role", "alert");
      var lead = document.createElement("p");
      lead.textContent = "Not imported. Take these back to Claude:";
      box.appendChild(lead);
      var ul = document.createElement("ul");
      problems.slice(0, 12).forEach(function (p) {
        var li = document.createElement("li"); li.textContent = p; ul.appendChild(li);
      });
      if (problems.length > 12) {
        var more = document.createElement("li");
        more.textContent = "and " + (problems.length - 12) + " more";
        ul.appendChild(more);
      }
      box.appendChild(ul);
      menu.appendChild(box);
    }
  }

  function tagSpan(text) {
    var t = document.createElement("span");
    t.className = "theme-tag";
    t.textContent = text;
    return t;
  }

  function place(menu, btn) {
    var r = btn.getBoundingClientRect();
    var w = menu.offsetWidth, h = menu.offsetHeight, vw = innerWidth, vh = innerHeight;
    var left = Math.max(8, Math.min(r.right - w, vw - w - 8));
    var top = r.bottom + 6 + h <= vh - 8 ? r.bottom + 6 : Math.max(8, r.top - 6 - h);
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  function openMenu(btn) {
    if (menuEl) { closeMenu(); return; }
    menuEl = document.createElement("div");
    menuEl.className = "theme-menu";
    menuEl.setAttribute("role", "menu");
    fillMenu(menuEl);
    document.body.appendChild(menuEl);
    place(menuEl, btn);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    api("/api/themes").then(took).then(function () {
      if (menuEl) { fillMenu(menuEl); place(menuEl, btn); }
    }).catch(function () { /* the menu still offers the built-in two */ });
  }

  function setTheme(id) { return useTheme(id); }

  function toggleTheme() { return useTheme(themeNow() === "light" ? "dark" : "light"); }

  /* ?theme=<id> shows that theme for this page load only; nothing is saved.
     How the page checks run every screen in each theme. */
  function restoreTheme() {
    // The choice used to live in this browser; it lives on the console now.
    try { localStorage.removeItem("z2w-theme"); } catch (e) { /* ignore */ }
    var m = /[?&]theme=([a-z0-9-]+)/.exec(location.search);
    if (!m) return;
    var id = m[1];
    if (id !== "dark" && id !== "light") {
      var link = document.querySelector('link[href^="/theme.css"]');
      if (link) link.href = "/theme.css?id=" + encodeURIComponent(id);
    }
    document.documentElement.setAttribute("data-theme", id === "dark" || id === "light" ? id : "custom");
  }

  /* One button, in the bar every screen already carries. The label is hidden
     on a narrow dock, where the glyph has to speak for itself. */
  function themeButton() {
    var b = Z.Button({
      label: "Theme", variant: "ghost", size: "sm",
      glyph: glyph("M8 2v2M8 12v2M2 8h2M12 8h2M4.5 4.5l1.5 1.5M10 10l1.5 1.5M11.5 4.5L10 6M6 10l-1.5 1.5", 12),
      onClick: function () { openMenu(b); }
    });
    b.className += " z-dock-theme";
    b.setAttribute("data-theme-btn", "");
    b.setAttribute("aria-haspopup", "menu");
    b.title = "Choose, import or design a theme";
    return b;
  }

  // The built-in two, until the console says what else there is.
  themes.list = [{ id: "dark", name: "Dark", base: "dark", builtin: true },
                 { id: "light", name: "Light", base: "light", builtin: true }];
  themes.active = themeNow() === "custom" ? null : themeNow();

  root.Zero2WNav = {
    mount: mount, glyph: glyph, screens: screens,
    themeButton: themeButton, toggleTheme: toggleTheme,
    restoreTheme: restoreTheme, setTheme: setTheme
  };

  /* Applied as this file loads rather than from each page's start(), so a
     screen cannot forget it and no page paints in the wrong theme first. */
  restoreTheme();
})(window);
