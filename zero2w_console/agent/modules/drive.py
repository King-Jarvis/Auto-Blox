"""The arithmetic behind the drive blocks — dead zone, sign split, ramp."""


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def deadband(raw, cfg):
    """(value, inside) — small values collapse to nothing."""
    threshold = abs(_num(cfg.get("threshold"), 0.1))
    size = raw if raw >= 0 else -raw
    if size <= threshold:
        return 0.0, True
    if (cfg.get("rescale") or "yes") != "yes":
        return raw, False
    full = abs(_num(cfg.get("full"), 1))
    span = full - threshold
    if span <= 0:
        # Nothing left to stretch into; anything past the threshold is full.
        return (full if raw > 0 else -full), False
    value = (size - threshold) / span * full
    if value > full:
        value = full
    return (value if raw > 0 else -value), False


def split_drive(raw, cfg):
    """(value, forward) — one part of a signed value, for one pin."""
    full = abs(_num(cfg.get("full"), 1))
    if full == 0:
        full = 1.0
    scale = _num(cfg.get("scale"), 100)
    forward = raw >= 0
    if (cfg.get("invert") or "no") == "yes":
        forward = not forward
        raw = -raw
    part = cfg.get("part") or "size"

    if part == "direction":
        return (1 if forward else 0), forward

    size = (raw if raw >= 0 else -raw) / full
    if size > 1:
        size = 1.0
    value = _lift(size, cfg, scale)
    if part == "forward only":
        return (value if raw > 0 else 0.0), forward
    if part == "back only":
        return (value if raw < 0 else 0.0), forward
    return value, forward


def _lift(size, cfg, scale):
    """`size` in 0..1 onto 0, or onto floor..scale."""
    floor = abs(_num(cfg.get("floor"), 0))
    top = abs(scale)
    if size <= 0:
        return 0.0
    if floor <= 0:
        return size * scale
    if floor > top:                 # a floor above the ceiling is just the top
        floor = top
    return floor + size * (top - floor)


def ramp_toward(current, target, elapsed, cfg):
    """(value, arrived) — move `current` at most `rate * elapsed` toward
    `target`, counting no more than `max_gap_ms` of any one gap."""
    if elapsed < 0:
        elapsed = 0.0
    cap = _num(cfg.get("max_gap_ms"), 250)
    if cap > 0 and elapsed > cap / 1000.0:
        elapsed = cap / 1000.0
    gap = target - current
    if gap == 0:
        return current, True
    # Away from zero is a rise; back towards it is a fall. Crossing zero does
    # both, and takes the slower of the two rules on the way out.
    size_now = current if current >= 0 else -current
    size_next = target if target >= 0 else -target
    rising = size_next > size_now
    rate = _num(cfg.get("rate_up" if rising else "rate_down"), 1)
    if rate <= 0:
        return target, True
    step = rate * elapsed
    if gap > 0:
        value = current + step
        if current < 0 and value > 0:
            return 0.0, False           # the dead step, on the way up
        if value >= target:
            return target, True
    else:
        value = current - step
        if current > 0 and value < 0:
            return 0.0, False           # and on the way down
        if value <= target:
            return target, True
    return value, False
