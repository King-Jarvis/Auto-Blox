/* The camera feed, shared by the dashboard widget and the Cameras screen.
 *
 * A device sends what its sensor will actually give — RGB565 or greyscale on the
 * boards here — and the host turns each frame into a PNG. So this polls images
 * rather than holding a stream open, which also means a sleeping device costs
 * nothing.
 */
(function (root) {
  "use strict";

  var MS = {
    tile: 1000,        // the tiles, unless a flow asks for something else
    open: 500,         // the one you are actually looking at
    list: 15000,       // the set of cameras, which rarely changes
    beat: 100          // how often we check whether any tile is due
  };

  /* How often this camera wants asking; a Camera-to-screen node sets it. Asking
     faster than the device can capture and send achieves nothing — load() below
     drops a request while one is in flight — so this is for asking less often. */
  function everyMs(cam) {
    var want = cam && Number(cam.every_ms);
    return want > 0 ? Math.max(MS.beat, want) : MS.tile;
  }

  /* Tracks when each camera was last asked, so one timer can serve tiles
     running at different rates without a timer each. */
  function pacer() {
    var last = {};
    return {
      due: function (cam) {
        var now = Date.now();
        var at = last[cam.id] || 0;
        if (now - at < everyMs(cam)) return false;
        last[cam.id] = now;
        return true;
      },
      forget: function (id) { delete last[id]; }
    };
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function frameURL(cam) {
    // A cache-buster, because the point of the next frame is that it is next.
    return "/api/iot/devices/" + encodeURIComponent(cam.id) +
           "/camera.png?ts=" + Date.now();
  }

  /* Two stacked images per camera, and only the loaded one is shown: setting src
     on a single <img> blanks it until the new frame arrives, which at one frame a
     second reads as a flicker. */
  function swapper(cls) {
    var box = el("div", cls);
    var a = el("img", "cam-img is-shown");
    var b = el("img", "cam-img");
    box.appendChild(a);
    box.appendChild(b);
    var front = a;
    var loading = false;
    var shown = 0;                 // when the frame on screen actually arrived
    return {
      el: box,
      /* How old the picture in this box is, in seconds. Measured here rather than
         from the camera list, which is only re-fetched every 15s. */
      age: function () { return shown ? (Date.now() - shown) / 1000 : null; },
      /* `turn` 90 asks the screen for the quarter turn a JPEG could not be
         given on the board (fleet.cameras says which). */
      load: function (url, alt, turn) {
        box.classList.toggle("is-quarter", turn === 90);
        // Over wifi a frame can take longer than the refresh interval, and asking
        // early only queues them up.
        if (loading) return;
        loading = true;
        var back = front === a ? b : a;
        back.alt = alt || "";
        back.onload = function () {
          loading = false;
          shown = Date.now();
          back.classList.add("is-shown");
          front.classList.remove("is-shown");
          front = back;
          box.classList.remove("is-dark");
        };
        back.onerror = function () { loading = false; box.classList.add("is-dark"); };
        back.src = url;
      }
    };
  }

  /* How old the picture on screen is. The tile's own measurement first, the
     server's frame cache only as a fallback before the first frame arrives, read
     against the server's clock so a wrong browser clock invents nothing. */
  function frameAge(view, cam, serverNow) {
    var own = view && view.age();
    if (own != null) return own;
    if (!cam || !cam.last_frame) return null;
    return Math.max(0, (serverNow || Date.now() / 1000) - cam.last_frame);
  }

  function ageText(age) {
    if (age == null) return "no frame yet";
    age = Math.round(age);
    if (age < 2) return "now";
    if (age < 90) return age + "s ago";
    if (age < 5400) return Math.round(age / 60) + "m ago";
    return Math.round(age / 3600) + "h ago";
  }

  /* A frame older than four refresh intervals is not a live picture, whatever
     the device's last report said. */
  function isLive(age, cam) {
    return !!(cam && cam.online) && age != null && age < 8;
  }

  function metaLine(cam) {
    return [cam.board, cam.ip, cam.size].filter(Boolean).join(" · ");
  }

  /* ---- the enlarged view ------------------------------------------------ */
  var current = null;

  function onKey(ev) {
    if (ev.key === "Escape") { ev.stopPropagation(); closeModal(); }
  }

  function openModal(cam) {
    closeModal();
    var scrim = el("div", "cam-modal");
    scrim.id = "cam-modal";
    var frame = el("div", "cam-modal-inner");
    var head = el("div", "cam-modal-head");
    head.appendChild(el("span", "cam-modal-name", cam.name || cam.id));
    var meta = el("span", "cam-modal-meta", metaLine(cam));
    head.appendChild(meta);
    var shut = el("button", "cam-modal-close", "✕");
    shut.type = "button";
    shut.setAttribute("aria-label", "close");
    shut.addEventListener("click", function (ev) { ev.stopPropagation(); closeModal(); });
    head.appendChild(shut);
    frame.appendChild(head);

    var view = swapper("cam-modal-frame");
    frame.appendChild(view.el);
    var foot = el("div", "cam-modal-foot",
                  "click anywhere, or press Escape, to close");
    frame.appendChild(foot);

    frame.addEventListener("click", function (ev) { ev.stopPropagation(); });
    scrim.addEventListener("click", closeModal);
    document.body.appendChild(scrim);
    scrim.appendChild(frame);
    document.addEventListener("keydown", onKey, true);

    current = {
      cam: cam, view: view, scrim: scrim,
      timer: setInterval(function () {
        if (!document.hidden) view.load(frameURL(cam), cam.name, cam.screen_turn);
      }, MS.open)
    };
    view.load(frameURL(cam), cam.name, cam.screen_turn);
    shut.focus();
    return current;
  }

  function closeModal() {
    if (!current) return;
    clearInterval(current.timer);
    if (current.scrim.parentNode) current.scrim.parentNode.removeChild(current.scrim);
    document.removeEventListener("keydown", onKey, true);
    current = null;
  }

  function openCamera() { return current && current.cam; }

  /* ---- polling ---------------------------------------------------------- */
  /* Every timer here is owned and stoppable, and none of them runs while the tab
     is hidden — nothing here is worth waking a passively cooled board for. */
  function ticker(fn, ms) {
    var id = setInterval(function () {
      if (document.hidden) return;
      fn();
    }, ms);
    return { stop: function () { clearInterval(id); id = null; } };
  }

  root.Zero2WCam = {
    MS: MS, el: el, everyMs: everyMs, pacer: pacer,
    frameURL: frameURL, swapper: swapper,
    frameAge: frameAge, ageText: ageText, isLive: isLive, metaLine: metaLine,
    openModal: openModal, closeModal: closeModal, openCamera: openCamera,
    ticker: ticker
  };
})(window);
