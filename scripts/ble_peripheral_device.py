# Runs on the board; pushed and started by scripts/ble-peripheral.py.
#
# A BLE peripheral with a known attribute table — read, write and notify — for
# checking `zero2w_console/gatt.py` against a live link. Needs no network.
import bluetooth
import struct
import time

# A vendor UUID on purpose: `short_uuid` must leave it whole.
SERVICE = bluetooth.UUID("f0de0001-7a2b-4c3d-9e5f-0a1b2c3d4e5f")
CHAR_READ = bluetooth.UUID("f0de0002-7a2b-4c3d-9e5f-0a1b2c3d4e5f")
CHAR_WRITE = bluetooth.UUID("f0de0003-7a2b-4c3d-9e5f-0a1b2c3d4e5f")
CHAR_NOTIFY = bluetooth.UUID("f0de0004-7a2b-4c3d-9e5f-0a1b2c3d4e5f")

FLAG_READ = 0x0002
FLAG_WRITE = 0x0008
FLAG_NOTIFY = 0x0010

NAME = "zero2w-gatt"

CONNECT, DISCONNECT, WRITE, READ_REQUEST = 1, 2, 3, 4


def advertising(name):
    """Flags and the name; a 128-bit service UUID would not fit in 31 bytes."""
    payload = bytearray()
    payload += bytes((2, 0x01, 0x06))            # flags: LE general discoverable
    raw = name.encode()
    payload += bytes((len(raw) + 1, 0x09)) + raw  # complete local name
    return bytes(payload)


class Fixture:
    def __init__(self):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.conn = None
        self.running = False
        self.writes = []
        self.ticks = 0
        service = (SERVICE, (
            (CHAR_READ, FLAG_READ),
            (CHAR_WRITE, FLAG_WRITE | FLAG_READ),
            (CHAR_NOTIFY, FLAG_READ | FLAG_NOTIFY),
        ))
        ((self.h_read, self.h_write, self.h_notify),) = \
            self.ble.gatts_register_services((service,))
        self.ble.gatts_write(self.h_read, b"zero2w")
        self.ble.gatts_write(self.h_write, b"\x00")
        self.ble.gatts_write(self.h_notify, struct.pack("<I", 0))
        self.ble.irq(self.irq)

    def irq(self, event, data):
        try:
            self.handle(event, data)
        except Exception as exc:              # a raising irq is otherwise silent
            print("IRQ %d raised %r" % (event, exc))

    def handle(self, event, data):
        if event == CONNECT:
            self.conn = data[0]
            print("CONNECTED handle=%d" % self.conn)
        elif event == DISCONNECT:
            print("DISCONNECTED")
            self.conn = None
            if self.running:
                # Not while shutting down: that raises OSError(-30).
                self.ble.gap_advertise(100000, adv_data=advertising(NAME))
                print("ADVERTISING again")
        elif event == WRITE:
            _conn, handle = data
            # The buffer is reused, so copy before keeping it.
            got = bytes(self.ble.gatts_read(handle))
            self.writes.append(got)
            print("WRITE handle=%d len=%d %s" % (handle, len(got), got.hex()))

    def run(self, seconds):
        self.running = True
        self.ble.gap_advertise(100000, adv_data=advertising(NAME))
        print("FIXTURE up as %r" % NAME)
        print("HANDLES read=%d write=%d notify=%d"
              % (self.h_read, self.h_write, self.h_notify))
        print("SERVICE %s" % SERVICE)
        end = time.ticks_add(time.ticks_ms(), seconds * 1000)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            time.sleep_ms(1000)
            self.ticks += 1
            # Written whether or not anyone subscribed, so a read sees it move.
            self.ble.gatts_write(self.h_notify, struct.pack("<I", self.ticks))
            if self.conn is not None:
                try:
                    self.ble.gatts_notify(self.conn, self.h_notify)
                except Exception as exc:
                    print("NOTIFY failed %r" % exc)
            print("TICK %d writes=%d connected=%s"
                  % (self.ticks, len(self.writes), self.conn is not None))
        self.running = False
        print("FIXTURE done")


def run(seconds=120):
    import gc
    import esp32
    gc.collect()
    idf = esp32.idf_heap_info(esp32.HEAP_DATA)
    print("HEAP before mpy=%d idf=%d" % (gc.mem_free(), sum(h[1] for h in idf)))
    f = Fixture()
    gc.collect()
    idf = esp32.idf_heap_info(esp32.HEAP_DATA)
    print("HEAP after  mpy=%d idf=%d" % (gc.mem_free(), sum(h[1] for h in idf)))
    try:
        f.run(seconds)
    finally:
        try:
            f.ble.active(False)
        except Exception:
            pass
        print("RADIO off")
