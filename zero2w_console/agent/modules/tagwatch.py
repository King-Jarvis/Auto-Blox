"""Tag change on a device, and the rule for when it fires, which the host
imports too."""

def tag_wants(when, value, previous, truthy):
    """Whether a tag moving from `previous` to `value` fires a Tag change set
    to `when`. The host imports this too."""
    if when == "becomes true":
        return truthy(value) and not truthy(previous)
    if when == "becomes false":
        return truthy(previous) and not truthy(value)
    if when in ("rises", "falls"):
        try:
            now, was = float(value), float(previous)
        except (TypeError, ValueError):
            return False
        return now > was if when == "rises" else now < was
    return True

def tag_change(r, node_id, cfg, msg, hops):
    r._emit(node_id, msg, hops)

def poll(r):
    """Fire Tag change triggers on any tag that moved since the last tick.

    A tag seen for the first time is a baseline, not a change, so the values
    a sync delivers at boot fire nothing.
    """
    if not hasattr(r, "tag_nodes"):
        r.tag_nodes = [i for i, n in r.nodes.items() if n.get("type") == "tag.change"]
        r.tag_seen = {}
    tags = r.agent.tags
    seen = r.tag_seen
    for node_id in r.tag_nodes:
        cfg = r.nodes[node_id].get("config") or {}
        want = (cfg.get("tag") or "").strip()
        for name in ([want] if want else list(tags)):
            if name not in tags:
                continue
            value = tags[name]
            key = node_id + "\x00" + name
            if key not in seen:
                seen[key] = value
                continue
            was = seen[key]
            if value == was:
                continue
            seen[key] = value
            if tag_wants(cfg.get("when") or "changes", value, was, r.truthy):
                r.fire(node_id, value, {"tag": name, "previous": was})
