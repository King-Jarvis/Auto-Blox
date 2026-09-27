"""Arithmetic, evaluated the same way on the host and on a device."""


class ExprError(Exception):
    """A formula that cannot be read, or cannot be worked out."""


DIGITS = "0123456789"
NAME_START = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
NAME_REST = NAME_START + DIGITS
# Longest first, so <= is never read as < followed by =.
OPERATORS = ("**", "//", "<=", ">=", "==", "!=",
             "+", "-", "*", "/", "%", "<", ">", "(", ")", ",")

CONSTANTS = {"pi": 3.141592653589793, "e": 2.718281828459045}


def _sqrt(x):
    """Newton's method: MicroPython's math module is not always there."""
    if x < 0:
        raise ExprError("cannot take the square root of a negative number")
    if x == 0:
        return 0.0
    guess = float(x)
    for _ in range(40):
        nxt = (guess + x / guess) / 2
        if abs(nxt - guess) < 1e-12:
            return nxt
        guess = nxt
    return guess


def _clamp(x, low, high):
    if low > high:
        low, high = high, low
    return low if x < low else (high if x > high else x)


def _floor(x):
    i = int(x)
    return i - 1 if (x < 0 and x != i) else i


def _ceil(x):
    i = int(x)
    return i + 1 if (x > 0 and x != i) else i


FUNCTIONS = {
    "abs": (1, 1, lambda a: abs(a)),
    "int": (1, 1, lambda a: int(a)),
    "float": (1, 1, lambda a: float(a)),
    "floor": (1, 1, _floor),
    "ceil": (1, 1, _ceil),
    "sqrt": (1, 1, _sqrt),
    "round": (1, 2, lambda a, b=0: round(a, int(b))),
    "min": (2, 8, min),
    "max": (2, 8, max),
    "clamp": (3, 3, _clamp),
}


def tokenize(text):
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch in DIGITS or (ch == "." and i + 1 < n and text[i + 1] in DIGITS):
            j, seen_dot, seen_exp = i, False, False
            while j < n:
                c = text[j]
                if c in DIGITS:
                    j += 1
                elif c == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    j += 1
                elif c in "eE" and not seen_exp and j > i:
                    seen_exp = True
                    j += 1
                    if j < n and text[j] in "+-":
                        j += 1
                else:
                    break
            chunk = text[i:j]
            try:
                out.append(("num", float(chunk) if (seen_dot or seen_exp) else int(chunk)))
            except ValueError:
                raise ExprError("%r is not a number" % chunk)
            i = j
            continue
        if ch in NAME_START:
            j = i
            while j < n and text[j] in NAME_REST:
                j += 1
            out.append(("name", text[i:j]))
            i = j
            continue
        for op in OPERATORS:
            if text[i:i + len(op)] == op:
                out.append(("op", op))
                i += len(op)
                break
        else:
            raise ExprError("there is a %r in this formula, which means "
                            "nothing here" % ch)
    return out


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, op):
        kind, value = self.take()
        if kind != "op" or value != op:
            raise ExprError("expected %r" % op)

    # comparison < additive < term < power < unary < atom
    def parse(self):
        value = self.comparison()
        if self.i < len(self.t):
            kind, val = self.peek()
            raise ExprError("this formula has %r left over" % (val,))
        return value

    def comparison(self):
        left = self.additive()
        kind, op = self.peek()
        if kind == "op" and op in ("<", "<=", ">", ">=", "==", "!="):
            self.take()
            right = self.additive()
            if op == "<":
                ok = left < right
            elif op == "<=":
                ok = left <= right
            elif op == ">":
                ok = left > right
            elif op == ">=":
                ok = left >= right
            elif op == "==":
                ok = left == right
            else:
                ok = left != right
            return 1 if ok else 0
        return left

    def additive(self):
        value = self.term()
        while True:
            kind, op = self.peek()
            if kind != "op" or op not in ("+", "-"):
                return value
            self.take()
            right = self.term()
            value = value + right if op == "+" else value - right

    def term(self):
        value = self.unary()
        while True:
            kind, op = self.peek()
            if kind != "op" or op not in ("*", "/", "//", "%"):
                return value
            self.take()
            right = self.unary()
            if op in ("/", "//", "%") and right == 0:
                raise ExprError("this formula divides by zero")
            if op == "*":
                value = value * right
            elif op == "/":
                value = value / right
            elif op == "//":
                value = value // right
            else:
                value = value % right

    def unary(self):
        kind, op = self.peek()
        if kind == "op" and op in ("+", "-"):
            self.take()
            value = self.unary()
            return -value if op == "-" else value
        return self.power()

    def power(self):
        base = self.atom()
        kind, op = self.peek()
        if kind == "op" and op == "**":
            self.take()
            # Right-associative, and through unary so 2 ** -1 reads.
            exponent = self.unary()
            if abs(exponent) > 64:
                raise ExprError("that power is too large to work out here")
            try:
                return base ** exponent
            except (OverflowError, ZeroDivisionError):
                raise ExprError("that power cannot be worked out")
        return base

    def atom(self):
        kind, value = self.take()
        if kind == "num":
            return value
        if kind == "op" and value == "(":
            inner = self.comparison()
            self.expect(")")
            return inner
        if kind == "name":
            if value in CONSTANTS:
                return CONSTANTS[value]
            spec = FUNCTIONS.get(value)
            if not spec:
                raise ExprError("there is no %r here — a value has to be "
                                "put in with {{a variable}}" % value)
            low, high, fn = spec
            self.expect("(")
            args = []
            if not (self.peek() == ("op", ")")):
                args.append(self.comparison())
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.comparison())
            self.expect(")")
            if not low <= len(args) <= high:
                raise ExprError("%s takes %d argument%s, not %d"
                                % (value, low, "" if low == 1 else "s", len(args)))
            try:
                return fn(*args)
            except ExprError:
                raise
            except Exception:
                raise ExprError("%s could not be worked out" % value)
        raise ExprError("this formula stops short")


def evaluate(text):
    """One formula to one number. Raises ExprError, never anything else."""
    if text is None:
        raise ExprError("there is no formula here")
    tokens = tokenize(str(text))
    if not tokens:
        raise ExprError("there is no formula here")
    try:
        return _Parser(tokens).parse()
    except ExprError:
        raise
    except IndexError:
        raise ExprError("this formula stops short")
    except (OverflowError, ValueError, TypeError):
        raise ExprError("this formula cannot be worked out")


def tidy(value, places=None):
    """A number fit to put in a payload: whole floats lose their point."""
    if places is not None:
        value = round(value, int(places))
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return int(value)
    return value
