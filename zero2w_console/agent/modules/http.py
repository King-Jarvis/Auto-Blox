"""Outbound HTTP for the agent — pulled only when a flow uses it."""
import agent


def call(cfg, msg):
    url = cfg.get("url")
    if not url:
        return 0, None
    method = (cfg.get("method") or "GET").upper()
    body = cfg.get("body")
    if body in (None, ""):
        body = None
    elif isinstance(body, str):
        body = agent.json.loads(body) if body.strip().startswith("{") else {"payload": body}
    status, raw = agent.request(method, url, body, None, int(cfg.get("timeout", 15)))
    try:
        return status, agent.json.loads(raw)
    except Exception:
        return status, raw.decode() if isinstance(raw, bytes) else raw
