"""Tags — the board's shared memory, the way a PLC has one."""
import json
import os
import re
import threading
import time

from . import paths
CONFIG_DIR = paths.CONFIG_DIR
TAGS_FILE = os.path.join(CONFIG_DIR, "tags.json")

# A name you can type into a field without quoting it, and read back later.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")

TYPES = ("number", "text", "bool")

MAX_TAGS = 500          # a table, not a database
MAX_TEXT = 2000         # one tag cannot become a log file


class TagError(Exception):
    """A tag definition or write that cannot be honoured."""


def coerce(value, kind):
    """A value as the tag's type, or a TagError saying why not."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() not in (
            "", "0", "0.0", "off", "false", "no", "none", "null")
    if kind == "number":
        if isinstance(value, bool):
            return 1 if value else 0
        try:
            out = float(value)
        except (TypeError, ValueError):
            raise TagError("%r is not a number" % (value,))
        return int(out) if out == int(out) else out
    text = "" if value is None else str(value)
    if len(text) > MAX_TEXT:
        raise TagError("that text is too long for a tag (%d characters)" % len(text))
    return text


def initial_for(kind):
    return {"number": 0, "bool": False}.get(kind, "")


def clean(tag):
    """One tag definition, validated. Raises TagError on anything wrong."""
    name = str(tag.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise TagError("%r is not a usable tag name — letters, digits and "
                       "underscores, starting with a letter" % name)
    kind = tag.get("type") or "number"
    if kind not in TYPES:
        raise TagError("%r is not a tag type" % kind)
    initial = tag.get("initial")
    initial = initial_for(kind) if initial is None else coerce(initial, kind)
    return {
        "name": name,
        "type": kind,
        "initial": initial,
        "retain": bool(tag.get("retain")),
        # A shared tag is carried to every enrolled device and written back when
        # one changes it. Off by default: the link is slow, and most tags are this
        # board's own business.
        "share": bool(tag.get("share")),
        # Back to `initial` everywhere when any board boots, so a board cannot
        # come up already armed. Only means something on a shared tag.
        "boot_reset": bool(tag.get("boot_reset")),
        "desc": str(tag.get("desc") or "")[:200],
        "unit": str(tag.get("unit") or "")[:16],
    }


class TagStore:
    """The table on disk: definitions always, retained values as well."""

    def __init__(self, path=TAGS_FILE):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    def _read(self):
        if not os.path.exists(self.path):
            return {"tags": [], "rev": 0}
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or not isinstance(doc.get("tags"), list):
                return {"tags": [], "rev": 0}
        except (ValueError, OSError):
            return {"tags": [], "rev": 0}
        try:
            doc["rev"] = int(doc.get("rev") or 0)
        except (TypeError, ValueError):
            doc["rev"] = 0
        return doc

    def load(self):
        with self.lock:
            return self._read()

    def save(self, doc):
        """Replace the table."""
        tags, seen = [], set()
        for raw in doc.get("tags", [])[:MAX_TAGS]:
            tag = clean(raw)
            if tag["name"].lower() in seen:
                raise TagError("there are two tags called %r" % tag["name"])
            seen.add(tag["name"].lower())
            if tag["retain"] and "value" in raw:
                try:
                    tag["value"] = coerce(raw["value"], tag["type"])
                except TagError:
                    pass                      # a bad stored value is not fatal
            tags.append(tag)
        with self.lock:
            out = {"tags": tags, "rev": self._read()["rev"] + 1}
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(out, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)         # atomic: never a half table
            return out


class TagTable:
    """The tags as they are right now, and who wants telling when they
    change."""

    def __init__(self, store=None, bus=None):
        self.store = store or TagStore()
        self.bus = bus
        self.lock = threading.RLock()
        self.defs = {}          # name -> definition
        self.values = {}        # name -> current value
        self.changed_at = {}    # name -> when it last changed
        self.watchers = []      # fn(name, value, previous)
        self.reload()

    # -- the table ---------------------------------------------------------
    def reload(self):
        doc = self.store.load()
        with self.lock:
            defs, values = {}, {}
            for tag in doc.get("tags", []):
                name = tag["name"]
                defs[name] = tag
                # A retained tag comes back as it was; everything else starts
                # where its definition says, because that describes right now.
                if tag.get("retain") and "value" in tag:
                    values[name] = tag["value"]
                else:
                    values[name] = tag.get("initial", initial_for(tag["type"]))
            self.defs, self.values = defs, values
            self.changed_at = {n: time.time() for n in defs}
        return doc

    def define(self, tags):
        """Replace the table, keeping the current value of anything that
        survives — editing a description should not blank what the
        machine knows."""
        held = self.snapshot()
        out = []
        for raw in tags:
            tag = clean(raw)
            if tag["retain"] and tag["name"] in held:
                try:
                    tag["value"] = coerce(held[tag["name"]], tag["type"])
                except TagError:
                    tag["value"] = tag["initial"]
            out.append(tag)
        doc = self.store.save({"tags": out})
        self.reload()
        # The store keeps values only for retained tags, so put the rest back
        # from memory rather than letting a redefinition reset them.
        with self.lock:
            for name in self.defs:
                if name in held:
                    try:
                        self.values[name] = coerce(held[name], self.defs[name]["type"])
                    except TagError:
                        pass
        return doc

    def list(self):
        """Every tag with what it is holding, for the table on screen."""
        with self.lock:
            return [dict(self.defs[n],
                         value=self.values.get(n),
                         changed=self.changed_at.get(n))
                    for n in sorted(self.defs)]

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def get(self, name):
        with self.lock:
            return self.values.get(name)

    def has(self, name):
        with self.lock:
            return name in self.defs

    # -- writing -----------------------------------------------------------
    def shared(self):
        """The tags the fleet gets, as {name: value}."""
        with self.lock:
            return {n: self.values.get(n)
                    for n in self.defs if self.defs[n].get("share")}

    def reset_for_boot(self, source=None):
        """Put every shared boot_reset tag back to its initial value."""
        with self.lock:
            names = [n for n, d in self.defs.items()
                     if d.get("share") and d.get("boot_reset")]
        return [n for n in names
                if self.set(n, self.defs[n]["initial"], source=source)[1]]

    def is_shared(self, name):
        with self.lock:
            return bool((self.defs.get(name) or {}).get("share"))

    def set(self, name, value, source=None):
        """Write one tag."""
        with self.lock:
            tag = self.defs.get(name)
            if not tag:
                raise TagError("there is no tag called %r" % name)
            new = coerce(value, tag["type"])
            old = self.values.get(name)
            changed = new != old
            self.values[name] = new
            if changed:
                self.changed_at[name] = time.time()
            retain = tag.get("retain")
        if changed and retain:
            self._persist()
        if changed:
            self._announce(name, new, old, source)
        return new, changed

    def _persist(self):
        """Write retained values back."""
        try:
            with self.lock:
                tags = []
                for name in sorted(self.defs):
                    tag = dict(self.defs[name])
                    if tag.get("retain"):
                        tag["value"] = self.values.get(name)
                    tags.append(tag)
            self.store.save({"tags": tags})
        except Exception:
            pass

    def _announce(self, name, value, previous, source=None):
        for fn, wants_source in list(self.watchers):
            try:
                if wants_source:
                    fn(name, value, previous, source)
                else:
                    fn(name, value, previous)
            except Exception as exc:
                # Caught, because one bad watcher must not stop the others or fail
                # the write — but said, because the watcher that carries a tag to the
                # fleet is on the path a motor is driven by.
                if self.bus:
                    self.bus.publish("flow", {
                        "time": time.strftime("%H:%M:%S"),
                        "node": "tags", "level": "critical", "kind": "tag",
                        "message": "%s did not reach a watcher: %s" % (name, exc),
                    })
        if self.bus:
            self.bus.publish("tag", {"name": name, "value": value,
                                     "previous": previous, "source": source,
                                     "time": time.strftime("%H:%M:%S")})

    def watch(self, fn):
        """Register a watcher, remembering now whether it wants `source`."""
        wants_source = True
        try:
            import inspect
            params = inspect.signature(fn).parameters
            wants_source = "source" in params or any(
                p.kind == p.VAR_KEYWORD or p.kind == p.VAR_POSITIONAL
                for p in params.values()) or len(params) >= 4
        except (TypeError, ValueError):
            pass                    # a builtin or C callable: try the full form
        with self.lock:
            self.watchers.append((fn, wants_source))
