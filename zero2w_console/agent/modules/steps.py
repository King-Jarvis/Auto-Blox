"""Step: one stage of a sequence, active from go until done."""

def _state(r):
    if not hasattr(r, "steps"):
        r.steps = {}           # node id -> [arrived ticks, wait ms, msg, hops]
    return r.steps

def _say(r, node_id, name, active):
    r.agent.log("ok" if active else "idle",
                ("step %s active" if active else "left step %s") % name,
                node=node_id, kind="step", payload={"active": active})

def _leave(r, node_id, cfg, msg, hops, timed_out):
    name = cfg.get("name") or node_id
    del r.steps[node_id]
    _say(r, node_id, name, False)
    out = dict(msg)
    meta = dict(out.get("meta") or {})
    meta["step"] = name
    meta["timed_out"] = timed_out
    out["meta"] = meta
    r._emit(node_id, out, hops, "left")
    r._emit(node_id, out, hops, "next")

def logic_step(r, node_id, cfg, msg, hops):
    steps = _state(r)
    port = r.arrived
    if port == "reset":
        if node_id in steps:
            del steps[node_id]
            _say(r, node_id, cfg.get("name") or node_id, False)
        return
    if port == "done":
        if node_id in steps:
            _leave(r, node_id, cfg, msg, hops, False)
        return
    if node_id in steps:
        return                 # already here: go again is not a re-entry
    steps[node_id] = [r.ticks(), int(r.num(cfg.get("leave_after"), 0)), msg, hops]
    name = cfg.get("name") or node_id
    _say(r, node_id, name, True)
    out = dict(msg)
    meta = dict(out.get("meta") or {})
    meta["step"] = name
    out["meta"] = meta
    r._emit(node_id, out, hops, "entered")

def poll(r):
    """Leave after: move on any step whose time is up."""
    steps = _state(r)
    for node_id in list(steps):
        at, wait, msg, hops = steps[node_id]
        if wait > 0 and r.since(at) >= wait:
            cfg = (r.nodes.get(node_id) or {}).get("config") or {}
            _leave(r, node_id, cfg, msg, hops, True)
