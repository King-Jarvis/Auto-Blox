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
     Every screen gets the same switch. This lived in app.js, which only the
     dashboard loads, so /flows and /iot had a light theme they could render but
     no way to ask for. The stored key and the ?theme= override are unchanged. */
  var THEME_KEY = "z2w-theme";

  function themeNow() {
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light" : "dark";
  }

  function labelFor(theme) { return theme === "light" ? "Dark" : "Light"; }

  function paintButtons(theme) {
    var btns = document.querySelectorAll("[data-theme-btn]");
    Array.prototype.forEach.call(btns, function (b) {
      if (b.lastChild) b.lastChild.textContent = labelFor(theme);
    });
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); }
    catch (e) { /* private mode: the choice just does not outlive the tab */ }
    paintButtons(theme);
  }

  function toggleTheme() { setTheme(themeNow() === "light" ? "dark" : "light"); }

  /* ?theme=light|dark wins, so a link or the launcher can pick one. */
  function restoreTheme() {
    var saved = null;
    var m = /[?&]theme=(light|dark)\b/.exec(location.search);
    if (m) saved = m[1];
    if (!saved) {
      try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
    }
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
    paintButtons(themeNow());
  }

  /* One switch, in the bar every screen already carries. The label is hidden on
     a narrow dock, where the glyph has to speak for itself. */
  function themeButton() {
    var b = Z.Button({
      label: labelFor(themeNow()), variant: "ghost", size: "sm",
      glyph: glyph("M8 2v2M8 12v2M2 8h2M12 8h2M4.5 4.5l1.5 1.5M10 10l1.5 1.5M11.5 4.5L10 6M6 10l-1.5 1.5", 12),
      onClick: toggleTheme
    });
    b.className += " z-dock-theme";
    b.setAttribute("data-theme-btn", "");
    b.title = "switch theme";
    return b;
  }

  root.Zero2WNav = {
    mount: mount, glyph: glyph, screens: screens,
    themeButton: themeButton, toggleTheme: toggleTheme,
    restoreTheme: restoreTheme, setTheme: setTheme
  };

  /* Applied as this file loads rather than from each page's start(), so a
     screen cannot forget it and no page paints in the wrong theme first. */
  restoreTheme();
})(window);
