"""Pictures leaving the camera: Send to host and Save to SD.

A board pulls this for a flow with either node. The host imports it too, so a
file saved on the Pi is named exactly as one saved on a board's card.

A picture rides in the message as msg["picture"] — {"data", "format",
"width", "height"} — beside the payload rather than as it, so a Log node after
a Camera capture prints a size, not fifteen kilobytes of JPEG.
"""
import os
import time

EXT = {"jpeg": "jpg"}
LOG = "log.txt"
KEEP = 200
SD = "/sd"


def picture(msg):
    """The picture this message carries, or None."""
    pic = msg.get("picture")
    if isinstance(pic, dict) and pic.get("data"):
        return pic
    return None


def folder_name(text):
    """Letters, digits, - and _ only: never a path out of the captures root."""
    out = ""
    for ch in str(text or ""):
        if ch.isalpha() or ch.isdigit() or ch in "-_":
            out += ch
    return out[:32] or "captures"


def stamp(when=None):
    t = when or time.localtime()
    return "%04d%02d%02d-%02d%02d%02d" % tuple(t[:6])


def file_name(pic, when=None, n=1):
    """20260928-141503-001.jpg; a raw frame says its shape, since nothing in
    the bytes does: 20260928-141503-001-160x120-grayscale.raw."""
    fmt = pic.get("format") or "raw"
    if fmt in EXT:
        return "%s-%03d.%s" % (stamp(when), n, EXT[fmt])
    return "%s-%03d-%dx%d-%s.raw" % (stamp(when), n, pic.get("width") or 0,
                                      pic.get("height") or 0, fmt)


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _mkdirs(path):
    here = "/" if path.startswith("/") else ""
    for part in path.split("/"):
        if not part:
            continue
        here = here + part if here in ("", "/") else here + "/" + part
        if not _exists(here):
            os.mkdir(here)


def prune(folder, keep):
    """The oldest pictures beyond `keep`. Names start with the time, so name
    order is age order. 0 keeps everything."""
    if not keep or keep <= 0:
        return []
    names = sorted(n for n in os.listdir(folder) if n != LOG)
    gone = names[:max(0, len(names) - keep)]
    for n in gone:
        try:
            os.remove(folder + "/" + n)
        except OSError:
            pass
    return gone


def save(root, cfg, msg, convert=None):
    """Write what the message carries under root/<folder>, and return the path.

    A picture becomes a file of its own. A message with no picture appends its
    payload as one line of log.txt in the same folder, so a sensor reading can
    be kept the same way. `convert` lets the host turn a raw frame into a PNG.
    """
    folder = root.rstrip("/") + "/" + folder_name(cfg.get("folder"))
    _mkdirs(folder)
    pic = picture(msg)
    if pic is None:
        path = folder + "/" + LOG
        with open(path, "a") as fh:
            fh.write("%s %s\n" % (stamp(), msg.get("payload")))
        return path
    when = time.localtime()
    n = 1
    while True:
        name = file_name(pic, when, n)
        data = pic["data"]
        if convert:
            data, name = convert(pic, name)
        path = folder + "/" + name
        if not _exists(path):
            break
        n += 1
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        keep = int(cfg.get("keep"))
    except (TypeError, ValueError):
        keep = KEEP
    prune(folder, keep)
    return path


# -- on a board -----------------------------------------------------------
_pending = None         # (kind, picture) waiting for the stream
_own = None             # a stream opened for stills, when the camera has none
_quiet = 0              # when _own last had something to do
_said = None
IDLE_MS = 20000


def _mount():
    """The board's microSD, mounted at /sd once. One-bit mode: on an ESP32-CAM
    the four-bit bus also takes GPIO4, which is the flash LED, and GPIO12, a
    strapping pin."""
    if _exists(SD):
        return SD
    import machine
    os.mount(machine.SDCard(slot=1, width=1), SD)
    return SD


def _meta(msg, **kw):
    meta = dict(msg.get("meta") or {})
    meta.update(kw)
    return meta


def sd_save(runner, node_id, cfg, msg, hops):
    out = dict(msg)
    try:
        out["meta"] = _meta(msg, file=save(_mount(), cfg, msg))
    except Exception as exc:
        runner.agent.log("warn", "not saved to SD: %s" % exc, node=node_id)
    runner._emit(node_id, out, hops)


def picture_send(runner, node_id, cfg, msg, hops):
    global _pending
    pic = picture(msg)
    if pic is None:
        runner.agent.log("warn", "nothing to send: no picture in this message "
                         "(a Camera capture goes before this)", node=node_id)
    else:
        if _pending is not None:
            runner.agent.log("warn", "the last picture had not gone yet; this "
                             "one replaces it", node=node_id)
        _pending = (str(cfg.get("kind") or "picture")[:32], pic)
    runner._emit(node_id, msg, hops)


def _live():
    """The camera's own stream, when the live view has one open."""
    import sys
    cam = sys.modules.get("modules.camera")
    return getattr(cam, "_stream", None) if cam else None


def _open(agent):
    import modules.media as media
    got = media.offer(agent)
    if not got:
        return None
    port, name, cert = got
    return media.Stream(agent.host_name(), port, agent.device, agent.token, cert, name)


def poll(runner):
    """Every tick: get a waiting picture onto the stream, and let a stream
    opened only for stills go once it has been idle a while."""
    global _pending, _own, _quiet, _said
    if _pending is None and _own is None:
        return
    stream = _live() or _own
    if _pending is not None and stream is None:
        try:
            _own = stream = _open(runner.agent)
        except Exception as exc:
            _own = stream = None
            _said = str(exc)
        if stream is None:
            runner.agent.log("warn", "picture not sent: this console has no "
                             "encrypted stream to take it (%s)" % (_said or "no certificate"))
            _pending = None
            return
        _quiet = time.ticks_ms()
    if _own is not None:
        _own.poll()
    lc = __import__("modules.linkclient", None, None, ("x",))
    if _pending is not None and stream.state == lc.READY and not stream.outbox:
        kind, pic = _pending
        _pending = None
        _quiet = time.ticks_ms()
        if not stream.send_still(kind, pic["format"], pic["width"], pic["height"],
                                 pic["data"]):
            runner.agent.log("warn", "a %d byte picture is too big to send; use "
                             "JPEG or a smaller size" % len(pic["data"]))
    if _own is not None and _pending is None and \
            time.ticks_diff(time.ticks_ms(), _quiet) > IDLE_MS:
        _own.close("no pictures to send")
        _own = None


def stop(runner):
    global _pending, _own
    _pending = None
    if _own is not None:
        try:
            _own.close("the flow stopped")
        except Exception:
            pass
        _own = None
