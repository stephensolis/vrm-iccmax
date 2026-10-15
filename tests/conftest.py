import sys
from pathlib import Path

import pytest
from EasyMCP2221 import exceptions as mcp_exc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vrm_iccmax as vrm  # noqa: E402


class FakeChip:
    """A PMBus controller: paged registers, NACK on unknown commands, NVM commits recorded.

    Registers are keyed by (page, command); a page of None means the command ignores PAGE.
    """

    def __init__(self, registers, store_commands=()):
        self.registers = dict(registers)
        self.store_commands = set(store_commands)
        self.page = 0x2F  # nonzero value to simulate another active master
        self.pending_cmd = None
        self.commits = 0
        self.nack = set()
        self.ignore_writes = set()
        self.lost_page_writes = 0  # PAGE writes acknowledged but not applied

    def key(self, cmd):
        return (self.page, cmd) if (self.page, cmd) in self.registers else (None, cmd)

    def known(self, cmd):
        key = self.key(cmd)
        return cmd == vrm.PAGE or ((self.page, cmd) not in self.nack and (key in self.registers or (self.page, cmd) in self.store_commands))

    def write(self, data, kind):
        cmd, payload = data[0], data[1:]
        if not self.known(cmd):
            raise mcp_exc.NotAckError("Device did not ACK.")
        if kind == "nonstop":
            self.pending_cmd = cmd
        elif cmd == vrm.PAGE:
            if self.lost_page_writes:
                self.lost_page_writes -= 1
            else:
                self.page = payload[0]
        elif not payload:
            self.store(cmd)
        elif (self.page, cmd) not in self.ignore_writes:
            self.registers[(self.page, cmd)] = payload

    def read(self, size):
        cmd, self.pending_cmd = self.pending_cmd, None
        if cmd == vrm.PAGE:
            return bytes([self.page])
        return self.registers[self.key(cmd)][:size]

    def store(self, cmd):
        self.commits += 1


def pxe_counter(writes_left):
    return bytes([(writes_left & 0b11) << 6, writes_left >> 2])


class FakePXE1610C(FakeChip):
    def __init__(self, icc_max=0x00B4, writes_left=12):
        super().__init__(
            {
                (None, 0xFD): b"\xb3",  # answers on any page, as on the real chips
                (0x4F, 0x1A): b"\x00",
                (0x4F, 0x32): b"\x15\x04",
                (0x20, 0x73): icc_max.to_bytes(2, "little"),
                (0x20, 0x82): b"\x00\x81",  # what page 0x20 returns for the NVM counter command
                (0x50, 0x82): pxe_counter(writes_left),
                (0x3F, 0x27): bytes(2),
                (0x3F, 0x29): bytes(2),
            },
            store_commands={(0x3F, 0x34)},
        )
        self.writes_left = writes_left
        self.counter_stuck = False

    def store(self, cmd):
        if self.registers[(0x3F, 0x29)] != vrm.PXE_NVM_PASSWORD:
            raise mcp_exc.NotAckError("Device did not ACK.")
        self.commits += 1
        if not self.counter_stuck:
            self.writes_left -= 1
            self.registers[(0x50, 0x82)] = pxe_counter(self.writes_left)


def fake_mp2955a(icc_max=0xB4):
    return FakeChip({(None, 0xBF): b"\x55\x25", (0x00, 0xEF): bytes([icc_max])}, {(0x00, 0x15)})


def fake_tps5367x(device_id=0x79, icc_max=0x00B4):
    return FakeChip({(None, 0xAD): bytes([0x01, device_id]), (0x00, 0xDA): icc_max.to_bytes(2, "little")}, {(0x00, 0x11)})


def fake_unknown():
    return FakeChip({(None, 0x98): b"\x33", (None, 0xAD): b"\x01\x58\x6a\xff\xff"})


class FakeEEPROM:
    """A 256-byte EEPROM: acknowledges everything; a write sets the address pointer, then stores data."""

    def __init__(self):
        self.memory = bytearray(range(256))
        self.pointer = 0
        self.stores = []

    def write(self, data, kind):
        self.pointer = data[0]
        if len(data) > 1:
            self.stores.append(bytes(data))
            self.memory[self.pointer:self.pointer + len(data) - 1] = data[1:]

    def read(self, size):
        return bytes(self.memory[self.pointer:self.pointer + size])


class FakeHidHandler:
    closed = False

    def close(self):
        self.closed = True


class FakeDevice:
    """Stands in for EasyMCP2221.Device: routes I2C transfers to chips and records them."""

    usbserial = "TEST0001"

    def __init__(self, chips):
        self.chips = chips
        self.hidhandler = FakeHidHandler()
        self.transfers = []
        self.fail_next = {}  # addr: [exception, ...] raised by the next transfers to that address

    def chip(self, addr):
        pending = self.fail_next.get(addr)
        if pending:
            raise pending.pop(0)
        if addr not in self.chips:
            raise mcp_exc.NotAckError("Device did not ACK.")
        return self.chips[addr]

    def I2C_speed(self, speed):
        pass

    def I2C_write(self, addr, data, kind="regular", timeout_ms=20):
        self.transfers.append(("W", addr, kind, bytes(data)))
        self.chip(addr).write(bytes(data), kind)

    def I2C_read(self, addr, size=1, kind="regular", timeout_ms=20):
        self.transfers.append(("R", addr, kind, size))
        return self.chip(addr).read(size)

    def writes(self, addr):
        return [data for op, a, kind, data in self.transfers if op == "W" and a == addr and kind == "regular"]

class FakeHid:
    """Stands in for the hid module: MCP2221As that report SDA/SCL levels and can be rebooted."""

    def __init__(self, count=1, sda_low=False):
        self.count = count
        self.sda_low = sda_low
        self.sda_low_after_reset = False
        self.failed_opens_after_reset = 2  # the adapter is briefly gone while it reconnects
        self.failing_opens = 0
        self.reports = []

    def enumerate(self, vid, pid):
        assert (vid, pid) == (0x04D8, 0x00DD)
        return [{"path": b"mcp%d" % i} for i in range(self.count)]

    def device(self):
        return FakeHidHandle(self)

    def resets(self):
        return [report for report in self.reports if report[1] == 0x70]


class FakeHidHandle:
    def __init__(self, fake_hid):
        self.hid = fake_hid

    def open_path(self, path):
        if self.hid.failing_opens:
            self.hid.failing_opens -= 1
            raise OSError("open failed")

    def write(self, data):
        self.hid.reports.append(list(data))
        if data[1] == 0x70:
            self.hid.failing_opens = self.hid.failed_opens_after_reset
            self.hid.sda_low = self.hid.sda_low_after_reset

    def read(self, size, timeout_ms):
        status = [0] * 64
        status[22], status[23] = 1, 0 if self.hid.sda_low else 1
        return status

    def close(self):
        pass


@pytest.fixture
def usb(monkeypatch):
    """A fake MCP2221A on USB; EasyMCP2221.Device opens it as `opened`, unless `open_errors` says otherwise."""
    fake_hid = FakeHid()
    fake_hid.opened = FakeDevice({})
    fake_hid.open_errors = []

    def open_device(**kwargs):
        if fake_hid.open_errors:
            raise fake_hid.open_errors.pop(0)
        return fake_hid.opened

    monkeypatch.setattr(vrm, "hid", fake_hid)
    monkeypatch.setattr(vrm.EasyMCP2221, "Device", open_device)
    return fake_hid


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Fake time: sleeping advances the clock instantly."""
    now = [0.0]
    monkeypatch.setattr(vrm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vrm.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now
