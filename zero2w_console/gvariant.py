"""GVariant text form — what `gdbus` prints — as plain Python.

`busctl --json` cannot stand in: `ManufacturerData` is `a{qv}`, keyed by uint16,
and systemd refuses to dump a non-string key as JSON. The shapes that matter:

    'Name': <'Oura Ring 5'>                 a variant around a string
    <{uint16 315: <[byte 0x02, 0x00]>}>     a dict keyed by a number
    'ServiceData': <{'0000a201-...': <b''>}>   a byte array as a bytes literal
    'AdvertisingFlags': <[byte 0x06]>       a type name on the first element only
    @a{sv} {}                               an explicit type on an empty container
    ({...},)                                a one-element tuple's trailing comma
"""
TYPES = ("byte", "boolean", "int16", "uint16", "int32", "uint32", "int64",
         "uint64", "double", "string", "objectpath", "signature", "handle")


class GVariantError(ValueError):
    pass


class _P:
    def __init__(self, text):
        self.s = text
        self.i = 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def take(self, ch):
        self.ws()
        if self.i < len(self.s) and self.s[self.i] == ch:
            self.i += 1
            return True
        return False

    def expect(self, ch):
        if not self.take(ch):
            raise GVariantError("wanted %r at %d: %r"
                                % (ch, self.i, self.s[self.i:self.i + 30]))

    def word(self):
        self.ws()
        start = self.i
        while self.i < len(self.s) and (self.s[self.i].isalnum()
                                       or self.s[self.i] in "_"):
            self.i += 1
        return self.s[start:self.i]

    def value(self):
        self.ws()
        ch = self.peek()
        if ch == "@":                       # @a{sv} {} — an explicit type
            self.i += 1
            while self.i < len(self.s) and self.s[self.i] not in " \t\r\n":
                self.i += 1
            return self.value()
        if ch == "<":
            self.i += 1
            out = self.value()
            self.expect(">")
            return out
        if ch == "(":
            self.i += 1
            out = []
            if not self.take(")"):
                while True:
                    out.append(self.value())
                    if self.take(")"):
                        break
                    self.expect(",")
                    # A one-element tuple prints as (x,), so the comma may be
                    # the last thing before the bracket.
                    if self.take(")"):
                        break
            return tuple(out)
        if ch == "[":
            self.i += 1
            out = []
            if not self.take("]"):
                while True:
                    out.append(self.value())
                    if self.take("]"):
                        break
                    self.expect(",")
            return out
        if ch == "{":
            self.i += 1
            out = {}
            if not self.take("}"):
                while True:
                    key = self.value()
                    self.expect(":")
                    out[key] = self.value()
                    if self.take("}"):
                        break
                    self.expect(",")
            return out
        if ch in "'\"":
            return self.string(ch)
        if ch == "b" and self.s[self.i + 1:self.i + 2] in ("'", '"'):
            # GVariant prints a byte array as a Python-style bytes literal when
            # it can, including b'' for an empty one.
            self.i += 1
            return self.bytestring(self.s[self.i])
        if ch == "-" or ch.isdigit():
            return self.number()
        word = self.word()
        if word in TYPES:                   # a leading type name: byte 0x02
            return self.value()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "nothing":
            return None
        if word:
            raise GVariantError("unexpected %r at %d" % (word, self.i))
        raise GVariantError("nothing to read at %d" % self.i)

    def string(self, quote):
        self.ws()
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1]
                width = {"u": 4, "U": 8}.get(nxt)
                if width:
                    out.append(chr(int(self.s[self.i + 2:self.i + 2 + width], 16)))
                    self.i += 2 + width
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise GVariantError("unterminated string")

    def bytestring(self, quote):
        self.i += 1
        out = bytearray()
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1]
                if nxt == "x":
                    out.append(int(self.s[self.i + 2:self.i + 4], 16))
                    self.i += 4
                    continue
                if nxt in "01234567":
                    end = self.i + 2
                    while end < len(self.s) and self.s[end] in "01234567" and end - self.i < 4:
                        end += 1
                    out.append(int(self.s[self.i + 1:end], 8) & 0xFF)
                    self.i = end
                    continue
                out.append(ord({"n": "\n", "t": "\t", "r": "\r",
                                "0": "\0"}.get(nxt, nxt)))
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return bytes(out)
            out += c.encode()
            self.i += 1
        raise GVariantError("unterminated byte string")

    def number(self):
        self.ws()
        start = self.i
        if self.s[self.i] == "-":
            self.i += 1
        if self.s[self.i:self.i + 2].lower() == "0x":
            self.i += 2
            while self.i < len(self.s) and self.s[self.i] in "0123456789abcdefABCDEF":
                self.i += 1
            return int(self.s[start:self.i], 16)
        seen_dot = False
        while self.i < len(self.s) and (self.s[self.i].isdigit()
                                       or self.s[self.i] in ".eE+-"):
            if self.s[self.i] == ".":
                seen_dot = True
            if self.s[self.i] in "+-" and self.s[self.i - 1] not in "eE":
                break
            self.i += 1
        text = self.s[start:self.i]
        return float(text) if (seen_dot or "e" in text.lower()) else int(text)


def parse(text):
    p = _P(text)
    out = p.value()
    p.ws()
    if p.i != len(p.s):
        raise GVariantError("trailing %r" % p.s[p.i:p.i + 40])
    return out
