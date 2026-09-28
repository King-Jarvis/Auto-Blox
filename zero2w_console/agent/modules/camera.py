"""The camera, and how its pictures reach the host.

With a picture stream (modules/media.py) they are pushed over TLS while the
console holds a lease. Without one, a plain socket on PORT answers the host's
requests, as before; that is the fallback for a console without a certificate.
"""
try:
    import usocket as socket
except ImportError:
    import socket

import camera as _camera
import gc

PORT = 8080

agent = None            # set by the agent before start(), for the stream
_cam = None
_srv = None
_stream = None
_frames = 0
_last_error = None
_format = "rgb565"
_said = None


def flips(turn, mirror):
    """(vflip, hmirror) for a JPEG, which cannot be turned after it is made.

    The host turns raw frames by rotating and then mirroring; the sensor's
    flips come first and a quarter turn is left to the screen, so each case
    is worked out to land on the same picture (tests/test_camera_stream.py)."""
    turn = int(turn or 0) % 360
    if turn == 90:
        return mirror, False
    if turn == 180:
        return True, not mirror
    if turn == 270:
        return not mirror, True
    return False, mirror


def _orient(turn, mirror):
    v, h = flips(turn, mirror)
    for name, on in (("vflip", v), ("hmirror", h)):
        try:
            setattr(_cam, name, on)
        except Exception:
            try:
                getattr(_cam, "set_" + name)(on)
            except Exception:
                pass


def _mounting():
    """(turn, mirror) from the running flow's Camera feed node."""
    try:
        for node in agent.runner.flow.get("nodes", []):
            if node.get("type") == "camera.feed":
                cfg = node.get("config") or {}
                return int(cfg.get("rotate") or 0), str(cfg.get("mirror")) == "yes"
    except Exception:
        pass
    return 0, False


def _sensor(frame_size=None, fmt=None, turn=0, mirror=False):
    """The sensor itself, at this size and format; nothing sent anywhere."""
    global _cam, _format
    kw = {}
    if frame_size:
        size = getattr(_camera.FrameSize, str(frame_size).upper(), None)
        if size is not None:
            kw["frame_size"] = size
    fmt = str(fmt or "").lower()
    if fmt == "jpeg":
        kw["pixel_format"] = _camera.PixelFormat.JPEG
        kw["jpeg_quality"] = 12
        kw["fb_count"] = 2
        # The newest frame, not the oldest queued: a stream wants now.
        kw["grab_mode"] = _camera.GrabMode.LATEST
        _format = "jpeg"
    elif fmt in ("grey", "gray", "greyscale", "grayscale"):
        kw["pixel_format"] = _camera.PixelFormat.GRAYSCALE
        _format = "grayscale"
    else:
        _format = "rgb565"
    _cam = _camera.Camera(**kw)
    _cam.init()
    if _format == "jpeg":
        if not turn and not mirror:
            turn, mirror = _mounting()
        _orient(turn, mirror)
    _cam.capture()          # the first frame off this sensor is often dark


def start(port=PORT, frame_size=None, fmt=None, turn=0, mirror=False):
    """Bring the sensor up, then the stream or, failing that, the socket."""
    global _srv, _stream, _last_error
    if _cam is None:
        _sensor(frame_size, fmt, turn, mirror)
    if _stream is None and _srv is None:
        _stream = _open_stream()
    if _stream is None and _srv is None:
        _srv = socket.socket()
        _srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _srv.bind(("0.0.0.0", port))
        _srv.listen(1)
        _srv.setblocking(False)
    _last_error = None
    return info()


def still(frame_size=None, fmt=None):
    """One picture for Camera capture, as the message carries it: from the
    running camera as it is, or from one brought up at this size and format
    for the frame and switched off again, so a capture never leaves the sensor
    holding RAM."""
    global _cam
    was = _cam is not None
    if not was:
        _sensor(frame_size, fmt)
    try:
        return {"data": _cam.capture(), "format": _format,
                "width": _cam.get_pixel_width(), "height": _cam.get_pixel_height()}
    finally:
        if not was:
            try:
                _cam.deinit()
            except Exception:
                pass
            _cam = None


def _open_stream():
    if agent is None:
        return None
    try:
        import modules.media as media
    except ImportError:
        return None
    got = media.offer(agent)
    if not got:
        return None
    port, name, cert = got
    return media.Stream(agent.host_name(), port, agent.device, agent.token, cert, name)


def stop():
    global _cam, _srv, _stream
    if _stream is not None:
        _stream.close("the camera stopped")
        _stream = None
    if _srv is not None:
        try:
            _srv.close()
        except Exception:
            pass
        _srv = None
    if _cam is not None:
        try:
            _cam.deinit()
        except Exception:
            pass
        _cam = None


def info():
    if _cam is None:
        return {"running": False, "error": _last_error}
    return {"running": True, "port": _stream.port if _stream else PORT,
            "stream": _stream.status()["state"] if _stream else None,
            "frames": _frames, "width": _cam.get_pixel_width(),
            "height": _cam.get_pixel_height(), "format": _format,
            "sensor": _cam.get_sensor_name(), "error": _last_error}


def capture():
    if _cam is None:
        return None
    return _cam.capture()


def poll():
    """A turn for the stream, or one connection if anyone is asking. Returns
    quickly when there is nothing to do."""
    global _frames, _last_error, _said
    if _stream is not None:
        _stream.poll()
        if _stream.due():
            buf = capture()
            if buf and _stream.push(_format, _cam.get_pixel_width(),
                                    _cam.get_pixel_height(), buf):
                _frames += 1
        if _stream.said and _stream.said != _said and agent is not None:
            _said = _stream.said
            agent.log("warn", _said)
        return True
    if _srv is None:
        return False
    try:
        conn, _addr = _srv.accept()
    except OSError:
        return False            # nothing waiting — the normal case
    try:
        conn.settimeout(5)
        try:
            request = conn.readline() or b""
        except OSError:
            request = b""
        path = b"/"
        parts = request.split()
        if len(parts) > 1:
            path = parts[1]
        while True:             # drain the headers
            try:
                line = conn.readline()
            except OSError:
                break
            if not line or line == b"\r\n":
                break

        if path.startswith(b"/frame"):
            gc.collect()
            buf = capture()
            if not buf:
                _send(conn, 503, b"no frame", "text/plain")
            else:
                _frames += 1
                _send(conn, 200, buf, "application/octet-stream", [
                    ("X-Width", _cam.get_pixel_width()),
                    ("X-Height", _cam.get_pixel_height()),
                    ("X-Format", _format),
                    ("X-Sensor", _cam.get_sensor_name()),
                ])
        else:
            import json
            _send(conn, 200, json.dumps(info()).encode(), "application/json")
    except Exception as exc:
        _last_error = str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return True


def _send(conn, status, body, ctype, headers=None):
    head = "HTTP/1.1 %d OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n" % (
        status, ctype, len(body))
    for key, value in (headers or []):
        head += "%s: %s\r\n" % (key, value)
    head += "Connection: close\r\n\r\n"
    conn.write(head.encode())
    conn.write(body)
