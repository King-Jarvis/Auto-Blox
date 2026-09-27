"""The agent's side of the link: policy, pulled only by a board that links."""
try:
    import ujson as json
except ImportError:
    import json

import time

# How long a ready link may hear nothing from the host before this board asks,
# and how long the answer gets.
QUIET_MS = 20000
ANSWER_MS = 5000


def start(agent, where):
    """Hold a socket open to the host, if the manifest says to."""
    if not where or not agent.token:
        return None
    if agent.link is not None:
        return agent.link
    try:
        import modules.linkclient as lc
    except ImportError:
        agent.log("warn", "told to link, but the client is not here")
        return None
    agent.linkmod, agent.wire = lc, lc.wire
    port = where.get("port")
    agent.link = lc.Client(agent.host_name(), port, agent.device, agent.token,
                           on_frame=on_frame, on_ready=on_ready)
    # The client hands itself to its callbacks, so this is how they get back to
    # the agent without this file holding a reference of its own.
    agent.link.agent = agent
    # Remembered so the next boot can dial before it syncs: a board that cannot
    # finish a sync never reaches the command poll, and a socket is the way back.
    if agent.cfg.get("link_port") != port:
        agent.cfg["link_port"] = port
        try:
            agent.save_config()
        except OSError as exc:
            agent.log("warn", "could not remember the link port: %s" % exc)
    return agent.link


def stop(agent, why="stopped"):
    if agent.link is not None:
        agent.link.close(why)
        agent.link = None
    # Forget the port too, or the next boot dials a host that has said no.
    if agent.cfg.get("link_port"):
        agent.cfg["link_port"] = None
        try:
            agent.save_config()
        except OSError:
            pass


def on_ready(client):
    agent = client.agent
    agent.log("ok", "link up to " + agent.host_name())


def on_frame(client, kind, body):
    """A verified frame from the host."""
    agent = client.agent
    try:
        doc = json.loads(body) if body else {}
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    if kind == agent.wire.COMMAND:
        try:
            agent.handle(doc)
        except Exception as exc:
            # Named, so the log says which command died.
            try:
                agent.log("critical", "%s failed: %s"
                          % (doc.get("op") or "command", exc))
            except Exception:
                # The likeliest reason to be here is MemoryError, and formatting
                # a message then allocating a frame for it can raise the same thing.
                # Serial is not much, but linkclient catches what escapes here and
                # passes, so letting it through restores the silence this ends.
                print("command failed:", exc)
    elif kind == agent.wire.TAGSET:
        for name, value in (doc.get("values") or {}).items():
            agent.tags[name] = value


def poll(agent, now):
    """One turn for the link, then the question of whether the host is
    alive."""
    agent.link.poll()
    check(agent, now)


def check(agent, now):
    """Has the host said anything lately? Ask, then give up on it."""
    if not agent.linked():
        agent.link_quiet = now
        agent.link_asked = False
        return
    heard = agent.link.frames_in
    if heard != agent.link_heard:
        agent.link_heard = heard
        agent.link_quiet = now
        agent.link_asked = False
        return
    quiet = time.ticks_diff(now, agent.link_quiet)
    if not agent.link_asked and quiet > QUIET_MS:
        agent.link_asked = True
        agent.link.send(agent.wire.PING)
    elif agent.link_asked and quiet > QUIET_MS + ANSWER_MS:
        agent.link.close("no answer in %dms" % quiet)
