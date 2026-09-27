"""Raw sensor frames into PNG, with the standard library."""
import struct
import zlib


def rgb565_to_rgb(raw, width, height, swap=False):
    """Two bytes per pixel to three."""
    out = bytearray(width * height * 3)
    expect = width * height * 2
    if len(raw) < expect:
        raise ValueError("frame is %d bytes, expected %d" % (len(raw), expect))
    hi, lo = (1, 0) if swap else (0, 1)
    for i in range(width * height):
        value = (raw[i * 2 + hi] << 8) | raw[i * 2 + lo]
        r = (value >> 11) & 0x1F
        g = (value >> 5) & 0x3F
        b = value & 0x1F
        # Replicate the high bits rather than shifting, so white stays white.
        out[i * 3] = (r << 3) | (r >> 2)
        out[i * 3 + 1] = (g << 2) | (g >> 4)
        out[i * 3 + 2] = (b << 3) | (b >> 2)
    return bytes(out)


def orient(pixels, width, height, channels, rotate=0, mirror=False):
    """Turn a frame the right way up, and mirror it if it is a bare sensor."""
    rotate = int(rotate or 0) % 360
    if rotate % 90:
        raise ValueError("rotation must be a quarter turn")
    if not rotate and not mirror:
        return bytes(pixels), width, height

    # Rows of packed pixels; every operation below is a reordering of these.
    stride = width * channels
    rows = [pixels[y * stride:(y + 1) * stride] for y in range(height)]

    if rotate == 180:
        rows = [_reverse_row(r, channels) for r in reversed(rows)]
    elif rotate in (90, 270):
        # Each new row is a column of the old picture: clockwise reads the columns
        # left to right and bottom to top, anticlockwise right to left and top down.
        turned = []
        for i in range(width):              # which new row
            row = bytearray(height * channels)
            for j in range(height):         # where along it
                if rotate == 90:
                    sy, sx = height - 1 - j, i
                else:
                    sy, sx = j, width - 1 - i
                off = sx * channels
                row[j * channels:(j + 1) * channels] = \
                    rows[sy][off:off + channels]
            turned.append(bytes(row))
        rows = turned
        width, height = height, width

    if mirror:
        rows = [_reverse_row(r, channels) for r in rows]
    return b"".join(rows), width, height


def _reverse_row(row, channels):
    """A row left-to-right, keeping each pixel's channels in order."""
    if channels == 1:
        return bytes(reversed(row))
    out = bytearray(len(row))
    count = len(row) // channels
    for i in range(count):
        src = (count - 1 - i) * channels
        out[i * channels:(i + 1) * channels] = row[src:src + channels]
    return bytes(out)


def _chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def png(pixels, width, height, channels=3, level=6):
    """A PNG from packed pixel rows. 3 channels is RGB, 1 is greyscale."""
    if channels not in (1, 3):
        raise ValueError("channels must be 1 or 3")
    if len(pixels) != width * height * channels:
        raise ValueError("expected %d bytes of pixels, got %d"
                         % (width * height * channels, len(pixels)))
    stride = width * channels
    # Each scanline carries a filter byte; 0 means "none", which compresses
    # well enough for a 160x120 frame and keeps this short.
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows += pixels[y * stride:(y + 1) * stride]
    colour = 0 if channels == 1 else 2
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), level))
            + _chunk(b"IEND", b""))


def frame_to_png(raw, width, height, fmt="rgb565", swap=False,
                 rotate=0, mirror=False):
    """One captured frame, whatever the sensor could give us, as a PNG."""
    fmt = (fmt or "rgb565").lower()
    if fmt in ("grayscale", "greyscale", "gray", "grey"):
        if len(raw) < width * height:
            raise ValueError("frame is %d bytes, expected %d" % (len(raw), width * height))
        pixels, channels = bytes(raw[:width * height]), 1
    elif fmt in ("rgb888", "rgb"):
        pixels, channels = bytes(raw[:width * height * 3]), 3
    else:
        pixels, channels = rgb565_to_rgb(raw, width, height, swap), 3
    pixels, width, height = orient(pixels, width, height, channels,
                                   rotate, mirror)
    return png(pixels, width, height, channels=channels)
