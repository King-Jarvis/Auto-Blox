"""Zero 2W field agent — MicroPython."""
try:
    import ujson as json
except ImportError:
    import json
try:
    import usocket as socket
except ImportError:
    import socket

import gc
import machine
import network
import os
import sys
import time

CONFIG = "config.json"
FLOW = "flow.json"
# Bump on every change a board would report; it is the only way to tell.
VERSION = "0.9.3"

# Set by main.py when agent.mpy would not load and this is agent_src.py.
BOOT_NOTE = None

# How long the flow runs between command polls. docs/CONTEXT.md §5.
WORK_MS = 1500

# Zero: parking stopped the flow and starved the watchdog. docs/CONTEXT.md §5.
COMMAND_WAIT = 0

LINK_POLL_MS = 20

# Seconds before a board with no host runs its flow anyway. docs/CONTEXT.md §5.
FLOW_GRACE = 20

# Seconds a module that would not download is left alone before another try.
FETCH_RETRY = 300


# -- storage
def load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


def ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass


# -- http
class HTTPError(Exception):
    pass


def request(method, url, body=None, headers=None, timeout=30, to_file=None):
    """One HTTP/1.1 round trip over a raw socket."""
    proto, _, rest = url.partition("://")
    hostport, _, path = rest.partition("/")
    path = "/" + path
    host, _, port = hostport.partition(":")
    port = int(port or (443 if proto == "https" else 80))

    payload = b""
    if body is not None:
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body

    head = "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n" % (method, path, hostport)
    for k, v in (headers or {}).items():
        head += "%s: %s\r\n" % (k, v)
    if body is not None:
        head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
    head += "\r\n"

    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(addr)
        s.write(head.encode())
        if payload:
            s.write(payload)
        if to_file:
            # Headers a little at a time, then the body straight to flash:
            # never held whole. docs/CONTEXT.md §5.
            head_raw = b""
            while b"\r\n\r\n" not in head_raw:
                chunk = s.read(64)
                if not chunk:
                    break
                head_raw += chunk
                if len(head_raw) > 4096:   # a header this big is not ours
                    break
            head_raw, _, rest = head_raw.partition(b"\r\n\r\n")
            lines = head_raw.split(b"\r\n")
            try:
                status = int(lines[0].split()[1])
            except (IndexError, ValueError):
                raise HTTPError("bad response")
            if status == 200:
                if "/" in to_file:
                    ensure_dir(to_file.rsplit("/", 1)[0])
                with open(to_file, "wb") as fh:
                    if rest:
                        fh.write(rest)
                    while True:
                        chunk = s.read(512)
                        if not chunk:
                            break
                        fh.write(chunk)
            return status, b""
        raw = b""
        while True:
            chunk = s.read(512)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 262144:          # a device has no business buffering more
                break
    finally:
        try:
            s.close()
        except Exception:
            pass

    head_raw, _, body_raw = raw.partition(b"\r\n\r\n")
    lines = head_raw.split(b"\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise HTTPError("bad response")
    if b"transfer-encoding: chunked" in head_raw.lower():
        body_raw = _dechunk(body_raw)
    return status, body_raw


def _dechunk(raw):
    out = b""
    while raw:
        line, _, rest = raw.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out += rest[:n]
        raw = rest[n + 2:]
    return out


# -- agent
class Agent:
    def __init__(self):
        # Since this run began: time.time() survives machine.reset().
        self.started = time.time()
        self.cfg = load_json(CONFIG, {}) or {}
        self.host = self.cfg.get("host") or ""
        self.token = self.cfg.get("token")
        self.device = self.cfg.get("device")
        # The shared tags as last heard, and the ones changed since reporting.
        self.tags = {}
        self.tags_written = {}
        self.wlan = None
        self.runner = None
        self.failures = 0
        self.online = False
        self.synced = False
        self.next_try = 0
        self.camera = None
        self.next_report = 0
        self.fetch_wait = {}   # module -> when to try it again
        # The held-open socket to the host, when told to keep one. None polls.
        self.link = None
        self.next_link = 0
        self.linkmod = None             # modules.linkclient, once imported
        self.linkpol = None             # modules.linkagent, once imported
        self.wire = None                # its frame types
        # How long the host has been silent on a ready link, and whether asked.
        self.link_quiet = 0
        self.link_asked = False
        self.link_heard = 0
        # One attempt per boot on the port that worked last time, before any
        # sync. False once it has been spent.
        self.link_remembered = bool(self.cfg.get("link_port"))
        # Said once, as soon as there is anywhere to say it. test_agent_loop.
        self.boot_error = BOOT_NOTE
        self.flow_due = 0       # when to run the flow with no host
        # What the flow on flash is called, so a report never reads it back.
        self.flow_id = None
        self.flow_name = None
        self.flow_on = False
        # What the host's document hashes to, and what is actually running on
        # it. A deploy and the save before it both queue a reload.
        self.flow_sha = None
        self.running_sha = None

    # -- the network
    def status_word(self):
        """Why the last attempt did not work, in the driver's own words."""
        if self.wlan is None:
            return "radio off"
        try:
            code = self.wlan.status()
        except Exception:
            return "unknown"
        for name in dir(network):
            if name.startswith("STAT_") and getattr(network, name) == code:
                return name[5:].lower().replace("_", " ")
        return str(code)

    def radio(self):
        """Bring the station interface up, and nothing else."""
        if self.wlan is not None:
            return self.wlan
        try:
            self.wlan = network.WLAN(network.STA_IF)
            self.wlan.active(True)
            # A station sleeps between beacons by default, which adds a wait to
            # every round trip. A device on mains power has no reason to.
            try:
                self.wlan.config(pm=network.WLAN.PM_NONE)
            except (AttributeError, ValueError, OSError):
                pass
            # dBm, and the one lever software has on a sagging rail.
            want = self.cfg.get("txpower")
            if want is not None:
                try:
                    self.wlan.config(txpower=float(want))
                except (AttributeError, ValueError, OSError, TypeError):
                    pass
        except Exception as exc:
            self.wlan = None
            print("radio:", exc)
        return self.wlan

    def connect(self, tries=20):
        """Try to join. Never fatal — the flow in flash runs either way."""
        if self.wlan is None:
            self.radio()
        if self.wlan is None:
            return False
        if self.wlan.isconnected():
            self.online = True
            return True
        ssid = self.cfg.get("ssid")
        if not ssid:
            return False
        try:
            # CONNECTING means a previous attempt is still running, and
            # connect() again only logs "cannot set config".
            if self.wlan.status() != network.STAT_CONNECTING:
                self.wlan.connect(ssid, self.cfg.get("psk"))
        except (OSError, AttributeError):
            return False
        for _ in range(tries):
            if self.wlan.isconnected():
                self.online = True
                return True
            time.sleep(0.5)
        # Leave the radio idle rather than wedged mid-attempt: a station stuck
        # in CONNECTING cannot even scan.
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        return False

    def ip(self):
        try:
            return self.wlan.ifconfig()[0]
        except Exception:
            return None

    def rssi(self):
        try:
            return self.wlan.status("rssi")
        except Exception:
            return None

    # -- talking to the host
    def call(self, method, path, body=None, timeout=30):
        headers = {}
        if self.token:
            headers["X-Device-Token"] = self.token
        status, raw = request(method, self.host + path, body, headers, timeout)
        if status == 401:
            raise HTTPError("unauthorised — this device needs to be provisioned again")
        if status >= 400:
            raise HTTPError("%s %s -> %d" % (method, path, status))
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def probe(self):
        """What this board actually is, as opposed to what it was configured as."""
        info = {"firmware": "micropython " + ".".join(str(x) for x in sys.implementation.version),
                "agent": VERSION,
                "platform": sys.platform,
                "freq_hz": machine.freq(),
                "flash_bytes": None, "psram_bytes": None,
                "unique_id": _hexlify(machine.unique_id())}
        try:
            info["chip"] = sys.implementation._machine
        except AttributeError:
            info["chip"] = sys.platform
        try:
            import esp
            info["flash_bytes"] = esp.flash_size()
        except Exception:
            pass
        try:
            gc.collect()
            info["free_ram"] = gc.mem_free()
        except Exception:
            pass
        return info

    def enroll(self):
        out = self.call("POST", "/api/iot/enroll", {
            "device": self.device,
            "enroll": self.cfg.get("enroll"),
            "probe": self.probe(),
        })
        self.token = out.get("token")
        self.cfg["token"] = self.token
        self.cfg["enroll"] = None
        save_json(CONFIG, self.cfg)
        return self.token

    # -- becoming what the host says
    def sync(self):
        # The largest single allocation here; 6,657 bytes were refused with
        # 93,232 free. The collector does not compact. docs/CONTEXT.md §5.
        gc.collect()
        man = self.call("GET", "/api/iot/manifest?v=" + VERSION
                        + ("" if self.synced else "&boot=1"))
        # The shared tags as the fleet holds them, so a board that has just come
        # up starts from the same numbers as everything else.
        for name, value in (man.get("tags") or {}).items():
            if name not in self.tags_written:      # do not undo an unsent write
                self.tags[name] = value
        ensure_dir("modules")
        have = load_json("modules.json", {}) or {}
        # Running from source because agent.mpy would not load: fetch it again
        # only if it is damaged on flash. An intact one the firmware refuses
        # would just fail again after the restart.
        if BOOT_NOTE and _sha_of("agent.mpy") != have.get("agent.mpy"):
            have.pop("agent.mpy", None)
        stale = None
        for mod in man.get("modules", []):
            name, want = mod["name"], mod["sha"]
            if have.get(name) == want and _exists(name):
                continue
            if self.fetch_wait.get(name, 0) > time.time():
                continue
            if name == "agent.mpy" and not _exists("agent_src.py"):
                continue        # never without the source to fall back on
            # Straight to flash through a part file: a transfer that dies half
            # way leaves the old module rather than a truncated one.
            part = name + ".part"
            try:
                status, _ = request("GET", self.host + "/api/iot/module/" + name,
                                    None, {"X-Device-Token": self.token},
                                    to_file=part)
            except Exception as exc:
                self.fetch_failed(name, mod.get("bytes"), exc)
                _unlink(part)
                continue
            if status != 200:
                self.fetch_failed(name, mod.get("bytes"), status)
                _unlink(part)
                continue
            # A truncated transfer must not become the live module.
            got = _sha_of(part)
            if got is not None and got != want:
                self.log("critical", "%s arrived wrong (%s, wanted %s) — keeping "
                         "the old one" % (name, got, want))
                _unlink(part)
                continue
            if "/" in name:
                ensure_dir(name.rsplit("/", 1)[0])
            _unlink(name)
            os.rename(part, name)
            if name[-4:] == ".mpy":
                _unlink(name[:-4] + ".py")      # a .py would be imported instead
            have[name] = want
            self.fetch_wait.pop(name, None)
            self.log("ok", "pulled " + name)
            # An import is cached, so new code for a loaded module needs a boot.
            if name.rsplit(".", 1)[0].replace("/", ".") in sys.modules:
                stale = name
        save_json("modules.json", have)
        if stale:
            self.log("warn", "%s changed while running — restarting" % stale)
            time.sleep(1)
            machine.reset()

        flow = man.get("flow")
        if flow:
            save_json(FLOW, flow)
        else:
            try:
                os.remove(FLOW)
            except OSError:
                pass
        # Also here: a sync can replace the document without restarting
        # anything. `flow_on` is what the loop compares. test_agent_redeploy.
        self.flow_id = (flow or {}).get("id")
        self.flow_name = (flow or {}).get("name")
        self.flow_on = bool(flow) and flow.get("enabled") is not False
        self.flow_sha = man.get("flow_sha")
        return man

    def fetch_failed(self, name, size, why):
        self.fetch_wait[name] = time.time() + FETCH_RETRY
        self.log("warn", "could not fetch %s (%s bytes): %s — flashing over USB "
                 "is the way in" % (name, size, why))

    def try_flow(self):
        """Run whatever `flow.json` now holds, and survive it not starting.

        A deploy and the save before it both queue a reload; the second is a
        no-op, because a restart costs heap only a reboot gives back.
        """
        if self.runner and (not self.flow_on
                            or self.runner.flow.get("id") != self.flow_id
                            or self.running_sha != self.flow_sha):
            self.runner.stop()
            self.runner = None
            self.stop_camera()
        if self.runner:
            return
        try:
            self.start_flow()
        except Exception as exc:
            self.runner = None
            self.boot_error = "the deployed flow would not start: %s" % exc
        self.running_sha = self.flow_sha

    def start_flow(self):
        flow = load_json(FLOW)
        # Kept so `report()` never reads flow.json back.
        self.flow_id = (flow or {}).get("id")
        self.flow_name = (flow or {}).get("name")
        if not flow:
            self.runner = None
            return None
        # Switched off in the studio means not run from flash either.
        if flow.get("enabled") is False:
            self.runner = None
            self.log("idle", "%s is switched off"
                     % (flow.get("name") or flow.get("id") or "flow"))
            return None
        # Before the import and the Runner: a board that cannot start its flow
        # stops without raising, so this line is the only evidence of its room.
        self.log("idle", "starting %s, %d free"
                 % (flow.get("id") or "flow", gc.mem_free()))
        try:
            import flow as flowmod
        except ImportError:
            self.log("critical", "flow runner is missing")
            return None
        self.runner = flowmod.Runner(flow, self)
        self.runner.start()
        self.log("ok", "running %s, %d free"
                 % (flow.get("name") or flow.get("id") or "flow",
                    gc.mem_free()))
        return self.runner

    # -- the camera
    def start_camera(self, frame_size=None, fmt=None):
        """Only if this device is meant to have one — it costs RAM and a socket."""
        try:
            import modules.camera as cam
        except ImportError:
            self.log("warn", "no camera module on this device")
            return None
        cam.agent = self
        try:
            out = cam.start(frame_size=frame_size or self.cfg.get("frame_size"),
                            fmt=fmt or self.cfg.get("format"))
            self.camera = cam
            self.log("ok", "camera up: %s %dx%d %s on port %d"
                     % (out.get("sensor"), out.get("width"), out.get("height"),
                        out.get("format"), out.get("port")))
            return out
        except Exception as exc:
            self.log("critical", "camera did not start: %s" % exc)
            return None

    def stop_camera(self):
        if self.camera:
            try:
                self.camera.stop()
            except Exception:
                pass
            self.camera = None

    # -- the link
    # Every decision about the link lives in modules/linkagent.py, pulled only by
    # a board whose config says link: true. What stays here has to work on a board
    # that has never heard of it.
    def host_name(self):
        """The bare host out of the console URL — no scheme, no port, no
        path."""
        rest = self.host.partition("://")[2] or self.host
        return rest.partition("/")[0].partition(":")[0]

    def link_policy(self):
        if self.linkpol is None:
            try:
                import modules.linkagent as la
            except ImportError:
                self.log("warn", "told to link, but the policy is not here")
                return None
            self.linkpol = la
        return self.linkpol

    def start_link(self, where):
        """Dial the host, if the manifest says to and the modules are here."""
        if not where or not self.token or self.link is not None:
            return self.link
        policy = self.link_policy()
        return policy.start(self, where) if policy else None

    def stop_link(self, why="stopped"):
        policy = self.linkpol
        if policy is not None:
            policy.stop(self, why)
        elif self.link is not None:
            self.link.close(why)
            self.link = None

    def poll_link(self):
        """A turn for the link, at most every LINK_POLL_MS."""
        if self.link is None:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self.next_link) < 0:
            return
        self.next_link = time.ticks_add(now, LINK_POLL_MS)
        if self.linkpol is not None:
            self.linkpol.poll(self, now)
        else:
            self.link.poll()

    def save_config(self):
        save_json(CONFIG, self.cfg)

    # -- which way to speak
    def linked(self):
        """True while the held-open socket is up and verified."""
        return self.link is not None and self.link.state == self.linkmod.READY

    def frame_type(self, name):
        """A frame number, or None on a board with no link module."""
        return getattr(self.wire, name, None) if self.wire else None

    def tell(self, kind, path, body):
        """One message to the host, by whichever route is up — the link when
        it is ready, HTTP when it is not."""
        if kind is not None and self.linked() and self.link.send(kind, body):
            return True
        if not self.online or not self.token:
            return False
        try:
            self.call("POST", path, body, timeout=10)
            return True
        except Exception:
            return False

    # -- telemetry
    def report(self):
        """What this board is doing, so the console is not guessing."""
        import gc as _gc
        _gc.collect()
        body = {
            "ip": self.ip(), "rssi": self.rssi(),
            "uptime": time.time() - self.started,
            "free_ram": _gc.mem_free(),
            "idf_free": _idf_free(),
            # What is running, or what is on flash when nothing is.
            "flow": (self.runner.flow.get("id") if self.runner else self.flow_id),
            "flow_name": (self.runner.flow.get("name") if self.runner
                          else self.flow_name),
            # Deployed-and-off looks exactly like deployed-and-broken otherwise.
            "running": bool(self.runner),
            "pins": self.runner.pin_states() if self.runner else {},
            "fired": self.runner.take_fired() if self.runner else [],
            # An older flow runner than this agent has no take_outputs.
            "outputs": self.runner.take_outputs()
            if hasattr(self.runner, "take_outputs") else {},
            "nodes": self.runner.node_summary() if self.runner else {},
            "camera": self.camera.info() if self.camera else None,
            # How the link is doing. It rides whichever route this report took:
            # a status that could only travel over the thing it describes would be
            # no use at all.
            "link": self.link.status() if self.link else None,
            "agent": VERSION,
        }
        written = self.tags_written
        if written:
            body["tags"] = written
            self.tags_written = {}
        if not self.tell(self.frame_type("STATE"), "/api/iot/state", body) \
                and written:
            # Nothing took it, so the writes are still unsent. A tag this board
            # changed is the one thing here the host cannot work out for itself.
            for name, value in written.items():
                self.tags_written.setdefault(name, value)

    # -- events
    def log(self, level, message, node=None, payload=None, kind="log"):
        # Telemetry is not worth making a frame wait for, and offline is normal
        # for a field device. Dropped, never queued: a ten second timeout inside
        # the tick loop would stall the flow it is reporting on.
        if not self.linked() and (not self.online or not self.token):
            print("[%s] %s" % (level, message))
            return
        self.tell(self.frame_type("EVENT"), "/api/iot/event", {
            "kind": kind, "level": level, "message": message, "node": node,
            "payload": payload, "ip": self.ip(), "rssi": self.rssi(),
            "uptime": time.time() - self.started,
        })

    def serve(self):
        """One pass of the actual work: tick the flow, answer the camera."""
        if self.runner:
            self.runner.tick()
        if self.camera:
            self.camera.poll()
        if self.link:
            self.poll_link()

    # -- the loop
    def handle(self, cmd):
        op = cmd.get("op")
        if op == "reload":
            man = self.sync()
            self.try_flow()
            # Switched off from the host, the link goes down here. Switched on it
            # comes up, but only if the modules are already on flash.
            if man.get("link"):
                self.start_link(man["link"])
            else:
                self.stop_link("switched off")
            # The camera is not started here: a message reaching its feed does it.
        elif op == "fire" and self.runner:
            self.runner.fire(cmd.get("node"), cmd.get("payload", 1))
        elif op == "set" and self.runner:
            self.runner.set_pin(cmd.get("gpio"), cmd.get("value"))
        elif op == "reboot":
            machine.reset()
        elif op == "resync":
            self.synced = False
        elif op == "tags":
            # Pushed from the host. Not echoed back: this is where it came from.
            for name, value in (cmd.get("values") or {}).items():
                self.tags[name] = value
        elif op == "camera-on":
            self.start_camera(cmd.get("frame_size"), cmd.get("format"))
        elif op == "camera-off":
            self.stop_camera()
        elif op == "stop" and self.runner:
            self.runner.stop()
            self.runner = None

    def run(self):
        """Radio, then network, then — in the loop, not here — the flow."""
        self.radio()
        for _ in range(2):
            if self.online or self.connect():
                break
        gc.collect()
        # Compile the runner while the heap can still hand the compiler a large
        # contiguous block: this is the allocation that killed the motor board,
        # failing at 89,456 bytes free and working at 107,744. docs/CONTEXT.md §5.
        if _exists(FLOW):
            try:
                import flow             # noqa: F401 — imported for the cache
            except Exception as exc:
                print("agent: the flow runner will not import:", exc)
        # Starting it waits for the loop: after the first sync, or FLOW_GRACE.
        self.flow_due = time.time() + FLOW_GRACE
        # No camera here either: nothing in a flow acts by existing.
        print("agent %s: device=%s host=%s" % (VERSION, self.device, self.host))

        while True:
            try:
                # Work first, and keep working. Talking to the host fills the gaps.
                deadline = time.ticks_add(time.ticks_ms(), WORK_MS)
                while time.ticks_diff(deadline, time.ticks_ms()) > 0:
                    self.serve()
                    time.sleep_ms(5)

                if not self.online and time.time() >= self.next_try:
                    if self.connect():
                        self.log("ok", "joined %s as %s, %d free"
                                 % (self.cfg.get("ssid"), self.ip(),
                                    gc.mem_free()))
                    else:
                        # Back off to a minute: retrying a network that is not
                        # there costs power for nothing.
                        self.failures += 1
                        self.next_try = time.time() + min(60, 5 * self.failures)
                        print("no network yet (%s): %r, retrying in %ds"
                              % (self.status_word(), self.cfg.get("ssid"),
                                 self.next_try - time.time()))

                if self.online and not self.token:
                    self.enroll()
                if self.online and self.token and self.boot_error:
                    # Before the sync, and once.
                    said, self.boot_error = self.boot_error, None
                    self.log("critical", said)
                if self.online and self.token and self.link_remembered:
                    # Before the sync: a board that cannot finish one must still
                    # be reachable. One attempt. test_agent_link.
                    self.link_remembered = False
                    self.start_link({"port": self.cfg.get("link_port")})
                if self.online and self.token and not self.synced:
                    man = self.sync()
                    self.try_flow()
                    self.synced = True
                    self.log("ok", "online at " + str(self.ip()))
                    # After the flow, and only after a sync: the modules it needs
                    # may have arrived in it.
                    self.start_link(man.get("link"))

                # No host to be had. Run what is in flash anyway — but not while
                # the radio is still trying, the window this board dies in.
                if not self.runner and not self.synced \
                        and time.time() >= self.flow_due:
                    self.try_flow()

                self.serve()
                if self.online and self.token and time.time() >= self.next_report:
                    self.report()
                    self.serve()
                    # Often enough that the editor's flashes look live, rare
                    # enough that a field device is not shouting.
                    self.next_report = time.time() + 2

                if self.online and self.token and not self.linked():
                    # Only when the link is not carrying them; it comes back
                    # the moment the link drops. docs/CONTEXT.md §6.
                    cmds = self.call("GET",
                                     "/api/iot/commands?wait=%d" % COMMAND_WAIT,
                                     None, timeout=COMMAND_WAIT + 15)
                    self.serve()
                    for cmd in (cmds or {}).get("commands", []):
                        self.handle(cmd)
                    self.failures = 0
            except Exception as exc:
                self.failures += 1
                print("agent:", exc)
                # And to the host: a board on its own supply has no serial line.
                try:
                    self.log("critical", "agent loop: %s" % exc)
                except Exception:
                    pass
                if self.wlan is not None and not self.wlan.isconnected():
                    self.online = False
                    self.synced = False
                time.sleep(min(30, 2 ** min(self.failures, 5)))
            gc.collect()


def _sha_of(path, chunk=512):
    """Sixteen hex characters of SHA-256, matching `fleet.sha`, or None."""
    try:
        try:
            import uhashlib as hashlib
        except ImportError:
            import hashlib
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
        raw = digest.digest()
    except Exception:
        return None
    return _hexlify(raw)[:16]


def _exists(name):
    try:
        os.stat(name)
        return True
    except OSError:
        return False


def _unlink(name):
    try:
        os.remove(name)
    except OSError:
        pass


def _idf_free():
    """Internal heap free: where WiFi and BLE allocate, and where MicroPython
    grows its own heap from. gc.mem_free() counts a grown area as free."""
    try:
        import esp32
        # Regions under 1MB: the internal ones, leaving any PSRAM out.
        return sum(h[1] for h in esp32.idf_heap_info(esp32.HEAP_DATA)
                   if h[0] < 1048576)
    except Exception:
        return None


def _hexlify(raw):
    return "".join("%02x" % b for b in raw)


def main():
    try:
        Agent().run()
    except Exception as exc:
        # Drop to the REPL rather than reboot-loop.
        sys.print_exception(exc) if hasattr(sys, "print_exception") else print(exc)
