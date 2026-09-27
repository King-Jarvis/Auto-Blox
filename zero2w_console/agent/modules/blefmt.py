"""A characteristic's bytes as a value, and back. The host imports this and a
device will pull it, so a reading means the same thing on both."""
try:
    import ustruct as struct
except ImportError:
    import struct
try:
    import ubinascii as binascii
except ImportError:
    import binascii

# name -> struct code, all little-endian as BLE sends them
NUMBERS = {"uint8": "<B", "int8": "<b", "uint16-le": "<H", "int16-le": "<h",
           "uint32-le": "<I", "int32-le": "<i", "float32-le": "<f"}

FORMATS = ("hex", "utf8", "uint8", "int8", "uint16-le", "int16-le",
           "uint32-le", "int32-le", "float32-le")


class FormatError(ValueError):
    pass


def decode(fmt, data):
    """Bytes off the air as the value a flow works with."""
    data = bytes(data)
    if fmt == "utf8":
        try:
            return data.decode("utf-8")
        except (UnicodeError, ValueError):
            raise FormatError("not UTF-8: %s" % _hex(data))
    code = NUMBERS.get(fmt)
    if code is None:
        return _hex(data)
    size = struct.calcsize(code)
    if len(data) < size:
        raise FormatError("%s needs %d bytes, got %d" % (fmt, size, len(data)))
    value = struct.unpack(code, data[:size])[0]
    if fmt == "float32-le":
        value = round(value, 6)
    return value


def encode(fmt, value):
    """A flow's value as the bytes to write."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if fmt == "utf8":
        return str(value).encode("utf-8")
    code = NUMBERS.get(fmt)
    if code is None:
        text = "".join(str(value).split()).replace(":", "")
        if text[:2] in ("0x", "0X"):
            text = text[2:]
        try:
            return binascii.unhexlify(text)
        except (ValueError, TypeError):
            raise FormatError("%r is not hex" % value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FormatError("%r is not a number" % value)
    if fmt != "float32-le":
        number = int(round(number))
    try:
        return struct.pack(code, number)
    except Exception:                    # struct.error, OverflowError
        raise FormatError("%s does not fit in %s" % (value, fmt))


def _hex(data):
    return binascii.hexlify(bytes(data)).decode()
