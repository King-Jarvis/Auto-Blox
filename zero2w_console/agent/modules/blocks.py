"""The standard blocks, pulled only by the flows that use them."""

def logic_edge(r, node_id, cfg, msg, hops):
    now = r.truthy(msg.get("payload"))
    was = r.edges.get(node_id)
    r.edges[node_id] = now
    if was is None or was == now:
        return
    edge = "rising" if now else "falling"
    want = cfg.get("edges") or "rising"
    if want != "any" and want != edge:
        return
    out = dict(msg)
    meta = dict(out.get("meta") or {})
    meta["edge"] = edge
    out["meta"] = meta
    r._emit(node_id, out, hops)

def logic_count(r, node_id, cfg, msg, hops):
    start = r.num(cfg.get("start"), 0)
    if r.arrived == "reset":
        r.counts[node_id] = start
        return
    count = r.counts.get(node_id, start) + r.num(cfg.get("step"), 1)
    target = r.num(cfg.get("target"), 0)
    if target > 0:
        hit = count >= target
    elif target < 0:
        hit = count <= target
    else:
        hit = False
    if hit and (cfg.get("auto_reset") or "reset") == "reset":
        r.counts[node_id] = start
    else:
        r.counts[node_id] = count
    if target and not hit:
        return
    out = dict(msg)
    out["payload"] = count
    meta = dict(out.get("meta") or {})
    meta["count"] = count
    meta["hit"] = hit
    out["meta"] = meta
    r._emit(node_id, out, hops)

def logic_hysteresis(r, node_id, cfg, msg, hops):
    value = r.num(msg.get("payload"), 0)
    was = r.hyst.get(node_id)
    state = bool(was)
    if value >= r.num(cfg.get("on_above"), 1):
        state = True
    elif value <= r.num(cfg.get("off_below"), 0):
        state = False
    changed = was is None or state != was
    r.hyst[node_id] = state
    if not changed and (cfg.get("emit") or "on change") == "on change":
        return
    out = dict(msg)
    out["payload"] = cfg.get("on_value", "1") if state else cfg.get("off_value", "0")
    meta = dict(out.get("meta") or {})
    meta["state"] = "on" if state else "off"
    meta["input"] = value
    out["meta"] = meta
    r._emit(node_id, out, hops)

def logic_timer(r, node_id, cfg, msg, hops):
    """The same three behaviours as the host."""
    mode = cfg.get("mode") or "wait until true for"
    ms = int(r.num(cfg.get("ms"), 2000))
    ms = 1 if ms < 1 else (3600000 if ms > 3600000 else ms)
    now = r.truthy(msg.get("payload"))
    out = dict(msg)
    meta = dict(out.get("meta") or {})

    if mode == "one and then nothing for":
        until = r.timer_state.get(node_id)
        if until is not None and time.ticks_diff(until, r.ticks()) > 0:
            return
        r.timer_state[node_id] = time.ticks_add(r.ticks(), ms)
        meta["held_ms"] = 0
        out["meta"] = meta
        return r._emit(node_id, out, hops)

    if mode == "keep it true for":
        if now:
            r._drop_waiting(node_id)
            if r.timer_state.get(node_id):
                return
            r.timer_state[node_id] = True
            meta["held_ms"] = 0
            out["meta"] = meta
            return r._emit(node_id, out, hops)
        if not r.timer_state.get(node_id):
            return
        r._drop_waiting(node_id)
        r.timer_state[node_id] = False
        meta["held_ms"] = ms
        out["meta"] = meta
        return r._wait_then_emit(node_id, out, hops, ms)

    if now:
        if r._waiting(node_id):
            return
        meta["held_ms"] = ms
        out["meta"] = meta
        return r._wait_then_emit(node_id, out, hops, ms)
    r._drop_waiting(node_id)

# A timer's pending message is parked in the same queue everything else uses,
# marked so it can be found again and dropped.

def logic_latch(r, node_id, cfg, msg, hops):
    # Anything that is not the reset anchor sets it, so an edge with no
    # toPort — which arrives as "in" — latches rather than landing nowhere.
    if node_id not in r.latches:
        r.latches[node_id] = cfg.get("start") == "on"
    was = r.latches[node_id]
    port = "reset" if (r.arrived or "in") == "reset" else "in"
    state = port != "reset"
    r.latches[node_id] = state
    if state == was and (cfg.get("emit") or "on change") == "on change":
        return
    out = dict(msg)
    out["payload"] = r.render(cfg.get("on_value", "1") if state
                            else cfg.get("off_value", "0"), msg)
    meta = dict(out.get("meta") or {})
    meta["latched"] = state
    meta["by"] = port
    out["meta"] = meta
    r._emit(node_id, out, hops)

def safety_watchdog(r, node_id, cfg, msg, hops):
    # Being fed moves the deadline; tick() does the firing. Nothing is
    # armed until the first message, so an unused watchdog never trips.
    timeout = int(r.num(r.render(cfg.get("timeout"), msg), 500))
    timeout = 20 if timeout < 20 else (600000 if timeout > 600000 else timeout)
    held = r.dogs.get(node_id)
    came_back = held is not None and held[2]
    r.dogs[node_id] = [r.ticks(), timeout, False, hops]
    if came_back and (cfg.get("recovery") or "no") == "yes":
        out = dict(msg)
        meta = dict(out.get("meta") or {})
        meta["starved"] = False
        meta["silent_ms"] = 0
        out["meta"] = meta
        r._emit(node_id, out, hops)

def math_scale(r, node_id, cfg, msg, hops):
    raw = r.num(msg.get("payload"), 0)
    in_min, in_max = r.num(cfg.get("in_min"), 0), r.num(cfg.get("in_max"), 1)
    if in_max == in_min:
        return
    out_min, out_max = r.num(cfg.get("out_min"), 0), r.num(cfg.get("out_max"), 1)
    frac = (raw - in_min) / (in_max - in_min)
    clamped = False
    if (cfg.get("clamp") or "yes") == "yes" and (frac < 0 or frac > 1):
        frac = 0 if frac < 0 else 1
        clamped = True
    value = round(out_min + frac * (out_max - out_min),
                  int(r.num(cfg.get("decimals"), 1)))
    out = dict(msg)
    out["payload"] = value
    meta = dict(out.get("meta") or {})
    meta["raw"] = raw
    meta["clamped"] = clamped
    out["meta"] = meta
    r._emit(node_id, out, hops)

def math_smooth(r, node_id, cfg, msg, hops):
    if r.arrived == "reset":
        r.windows.pop(node_id, None)
        return
    raw = r.num(msg.get("payload"), 0)
    mode = cfg.get("mode") or "running average"
    if mode == "weighted":
        # One float of memory rather than a list, which is the reason to reach
        # for this one on a device with kilobytes.
        weight = r.num(cfg.get("weight"), 0.3)
        weight = 0.01 if weight < 0.01 else (1.0 if weight > 1 else weight)
        held = r.windows.get(node_id)
        value = raw if held is None else held[0] + weight * (raw - held[0])
        r.windows[node_id] = [value]
        samples = 1
    else:
        size = int(r.num(cfg.get("window"), 5))
        size = 1 if size < 1 else (64 if size > 64 else size)
        window = r.windows.get(node_id) or []
        window.append(raw)
        if len(window) > size:
            del window[:len(window) - size]
        r.windows[node_id] = window
        samples = len(window)
        if mode == "lowest":
            value = min(window)
        elif mode == "highest":
            value = max(window)
        else:
            value = sum(window) / float(samples)
    out = dict(msg)
    out["payload"] = round(value, int(r.num(cfg.get("decimals"), 2)))
    meta = dict(out.get("meta") or {})
    meta["raw"] = raw
    meta["samples"] = samples
    out["meta"] = meta
    r._emit(node_id, out, hops)

# -- the drive blocks --------------------------------------------------
# Arithmetic in modules/drive.py, which the host imports too.
