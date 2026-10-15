#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["EasyMCP2221>=1.8", "hidapi"]
# ///
"""Raise the ICC_MAX current limit of some PMBus VRM controllers to 255 A through an MCP2221A USB-I2C adapter.

Supported controllers: PXE1610C, MP2955A, TPS53679, TPS53678.
Addresses are 7-bit hex (input like: 0x60, 60h, or 60).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import sys
import time
from typing import Callable, NamedTuple

import EasyMCP2221
import hid
from EasyMCP2221 import exceptions as mcp_exc
from EasyMCP2221.Constants import (
    CMD_POLL_STATUS_SET_PARAMETERS,
    CMD_RESET_CHIP,
    DEV_DEFAULT_PID,
    DEV_DEFAULT_VID,
    I2C_POLL_RESP_SCL,
    I2C_POLL_RESP_SDA,
    RESET_CHIP_SURE,
    RESET_CHIP_VERY_SURE,
    RESET_CHIP_VERY_VERY_SURE,
)

log = logging.getLogger("vrm_iccmax")

ADDR_MIN, ADDR_MAX = 0x03, 0x77
SPEED_MIN_HZ, SPEED_MAX_HZ = 47_000, 400_000
I2C_TIMEOUT_MS = 50
RETRY_DELAY_S = 0.02
BUS_BUSY_DELAY_S = 0.05
BUS_HELD_LOW_TIMEOUT_S = 3.0  # experimentally found some board's BMC polls every 2s so make this a little longer
REENUMERATION_DELAY_S, REENUMERATION_TIMEOUT_S = 0.5, 5.0  # the MCP2221A rejoining USB after a reset
MCP2221_RESET = [CMD_RESET_CHIP, RESET_CHIP_SURE, RESET_CHIP_VERY_SURE, RESET_CHIP_VERY_VERY_SURE]
NVM_COMMIT_TIME_S = 1.0
ATTEMPTS = 4

EXIT_OK, EXIT_FAILED, EXIT_ADAPTER, EXIT_ABORTED = 0, 1, 3, 4  # 2 is argparse's usage error
FAILURES = {"not found", "wrong chip", "failed"}

PAGE = 0x00  # PMBus PAGE command: selects the register page that later commands address
PMBUS_REVISION = 0x98
PMBUS_REVISIONS = {0x11, 0x22, 0x33, 0x44}  # PMBus 1.1 to 1.4
TARGET_ICC_MAX = 0xFF

ICC_MAX_REGISTER = {  # model: (page, command, size); little-endian, the low byte is the limit in amps
    "PXE1610C": (0x20, 0x73, 2),
    "MP2955A": (0x00, 0xEF, 1),
    # TODO: check whether the second rail of these dual-rail controllers (page 1) has its own ICC_MAX.
    "TPS53679": (0x00, 0xDA, 2),
    "TPS53678": (0x00, 0xDA, 2),
}

STORE_DEFAULT_ALL, STORE_USER_ALL = 0x11, 0x15
NVM_STORE_COMMAND = {"MP2955A": STORE_USER_ALL, "TPS53679": STORE_DEFAULT_ALL, "TPS53678": STORE_DEFAULT_ALL}

PXE_CONFIG_PAGE = 0x3F
PXE_SMBUS_PASSWORD_CMD, PXE_SMBUS_PASSWORD = 0x27, bytes([0x7C, 0xB3])
PXE_NVM_PASSWORD_CMD, PXE_NVM_PASSWORD = 0x29, bytes([0xD7, 0xEF])
PXE_NVM_UPLOAD_CMD = 0x34
PXE_NVM_COUNTER_PAGE, PXE_NVM_COUNTER_CMD = 0x50, 0x82

# format is (page, command, expected bytes), first matcher wins
# note block reads such as IC_DEVICE_ID (0xAD) return the byte count first
SIGNATURES = {
    "ISL69127": [(0x00, 0xAD, "04 00 23 D2 49")],
    "TPS53679": [(0x00, 0xAD, "01 79")],
    "TPS53678": [(0x00, 0xAD, "01 78")],
    "MP2955A": [(0x00, 0xBF, "55 25")],
    "PXE1610C": [(0x00, 0xFD, "B3"), (0x4F, 0x1A, "00"), (0x4F, 0x32, "15 04")],
    "Primarion (unknown model)": [(0x00, 0xFD, "B3"), (0x4F, 0x1A, "00")],
}


class AdapterError(Exception):
    """The MCP2221A or the I2C bus as a whole is unusable."""


class BusError(Exception):
    """A single I2C transaction failed on every attempt."""

    def __init__(self, message: str, nacked: bool = False):
        super().__init__(message)
        self.nacked = nacked


class PageNotSelected(RuntimeError):
    """PAGE did not read back as the page just written, so the command would address the wrong page."""


class Result(NamedTuple):
    status: str
    detail: str = ""


FAILURE_REASONS = {
    mcp_exc.NotAckError: "no ACK",
    mcp_exc.TimeoutError: "timeout",
    mcp_exc.LowSDAError: "SDA held low",
    mcp_exc.LowSCLError: "SCL held low",
}


class Bus:
    """PMBus transactions over an EasyMCP2221 device, with retries.

    When a page is given, PAGE is written before every command and read back to confirm it took
    effect; this is necessary to be resilient to bus collisions.
    """

    def __init__(self, device: EasyMCP2221.Device, attempts: int,
                 reset_adapter: Callable[[EasyMCP2221.Device], EasyMCP2221.Device] | None = None):
        self.device = device
        self.attempts = attempts
        self.reset_adapter = reset_adapter

    def read(self, addr: int, page: int | None, cmd: int, size: int, attempts: int | None = None) -> bytes:
        def transfer() -> bytes:
            self._select_page(addr, page)
            self.device.I2C_write(addr, [cmd], kind="nonstop", timeout_ms=I2C_TIMEOUT_MS)
            return bytes(self.device.I2C_read(addr, size, kind="restart", timeout_ms=I2C_TIMEOUT_MS))

        data = self._run(addr, f"read {where(page, cmd)}", transfer, attempts)
        log.debug("    0x%02X  R  %s -> %s", addr, where(page, cmd), data.hex(" ").upper())
        return data

    def write(self, addr: int, page: int | None, cmd: int, data: bytes = b"", attempts: int | None = None) -> None:
        def transfer() -> None:
            self._select_page(addr, page)
            self.device.I2C_write(addr, [cmd, *data], timeout_ms=I2C_TIMEOUT_MS)

        self._run(addr, f"write {where(page, cmd)}", transfer, attempts)
        log.debug("    0x%02X  W  %s  %s", addr, where(page, cmd), data.hex(" ").upper())

    def send_once(self, addr: int, page: int, cmd: int) -> None:
        self.select_page(addr, page)
        self._run(addr, f"write {where(page, cmd)}",
                  lambda: self.device.I2C_write(addr, [cmd], timeout_ms=I2C_TIMEOUT_MS), attempts=1)
        log.debug("    0x%02X  W  %s", addr, where(page, cmd))

    def select_page(self, addr: int, page: int) -> None:
        self._run(addr, f"write PAGE 0x{page:02X}", lambda: self._select_page(addr, page), None)
        log.debug("    0x%02X  W  PAGE 0x%02X", addr, page)

    def _select_page(self, addr: int, page: int | None) -> None:
        if page is None:
            return
        self.device.I2C_write(addr, [PAGE, page], timeout_ms=I2C_TIMEOUT_MS)
        self.device.I2C_write(addr, [PAGE], kind="nonstop", timeout_ms=I2C_TIMEOUT_MS)
        selected = self.device.I2C_read(addr, 1, kind="restart", timeout_ms=I2C_TIMEOUT_MS)[0]
        if selected != page:
            raise PageNotSelected(f"PAGE reads 0x{selected:02X} after selecting 0x{page:02X}")

    def _run(self, addr: int, what: str, transfer: Callable, attempts: int | None):
        attempts = attempts or self.attempts
        attempt, held_low_since, adapter_was_reset, failures = 1, None, False, []
        while True:
            try:
                return transfer()
            except OSError as e:
                raise AdapterError(f"USB communication with the MCP2221A failed: {e}") from e
            except (*FAILURE_REASONS, RuntimeError) as e:
                reason = FAILURE_REASONS.get(type(e), str(e))
                if isinstance(e, (mcp_exc.LowSDAError, mcp_exc.LowSCLError)) and attempts > 1:
                    held_low_since = held_low_since or time.monotonic()
                    if time.monotonic() - held_low_since > BUS_HELD_LOW_TIMEOUT_S:
                        if self.reset_adapter is None or adapter_was_reset:
                            raise AdapterError(f"I2C bus stuck: {reason} for over {BUS_HELD_LOW_TIMEOUT_S:g} s") from e
                        self.device = self.reset_adapter(self.device)
                        held_low_since, adapter_was_reset = None, True
                        continue
                    log.debug("    0x%02X  %s, waiting: %s", addr, reason, what)
                    time.sleep(BUS_BUSY_DELAY_S)
                    continue
                held_low_since = None
                failures.append(e)
                if attempt == attempts:
                    nacked = any(isinstance(f, mcp_exc.NotAckError) for f in failures)
                    reasons = dict.fromkeys(FAILURE_REASONS.get(type(f), str(f)) for f in failures)
                    raise BusError(f"{what} failed: {'; '.join(reasons)}", nacked) from e
                attempt += 1
                log.debug("    0x%02X  retry %d/%d: %s (%s)", addr, attempt, attempts, what, reason)
                time.sleep(RETRY_DELAY_S)


def where(page: int | None, cmd: int) -> str:
    return f"cmd 0x{cmd:02X}" if page is None else f"page 0x{page:02X} cmd 0x{cmd:02X}"


def icc_text(model: str, value: int) -> str:
    size = ICC_MAX_REGISTER[model][2]
    return f"0x{value:0{2 * size}X} ({value & 0xFF} A)"


def read_page(bus: Bus, addr: int) -> int | None:
    """Return the current PAGE, or None if nothing responds at addr."""
    try:
        return bus.read(addr, None, PAGE, 1)[0]
    except BusError as e:
        if e.nacked:
            return None
        raise


def is_pmbus(bus: Bus, addr: int) -> bool:
    def answer(cmd: int, size: int) -> bytes | None:
        try:
            return bus.read(addr, None, cmd, size)
        except BusError as e:
            if e.nacked:
                return None
            raise

    revision = answer(PMBUS_REVISION, 1)
    if revision is not None and revision[0] in PMBUS_REVISIONS:
        return True
    page0_checks = {(cmd, expected) for checks in SIGNATURES.values() for page, cmd, expected in checks if page == 0x00}
    return any(answer(cmd, len(bytes.fromhex(expected))) == bytes.fromhex(expected) for cmd, expected in page0_checks)


def identify(bus: Bus, addr: int) -> str | None:
    def matches(page: int, cmd: int, expected: str) -> bool:
        expected_bytes = bytes.fromhex(expected)
        try:
            return bus.read(addr, page, cmd, len(expected_bytes)) == expected_bytes
        except BusError as e:
            if e.nacked:
                return False
            raise

    return next((model for model, checks in SIGNATURES.items() if all(matches(*c) for c in checks)), None)


def restore_page(bus: Bus, addr: int, page: int) -> None:
    # other bus masters (eg. a BMC) may rely on the page they selected.
    try:
        bus.select_page(addr, page)
    except BusError as e:
        log.warning("could not restore PAGE 0x%02X at 0x%02X: %s", page, addr, e)


def read_icc_max(bus: Bus, addr: int, model: str) -> int:
    page, cmd, size = ICC_MAX_REGISTER[model]
    return int.from_bytes(bus.read(addr, page, cmd, size), "little")


def write_icc_max(bus: Bus, addr: int, model: str, value: int) -> None:
    page, cmd, size = ICC_MAX_REGISTER[model]
    bus.write(addr, page, cmd, value.to_bytes(size, "little"))


def pxe_nvm_writes_left(bus: Bus, addr: int) -> int:
    low, high = bus.read(addr, PXE_NVM_COUNTER_PAGE, PXE_NVM_COUNTER_CMD, 2)
    return high << 2 | low >> 6  # 10-bit counter: bits 9-2 in the high byte, bits 1-0 at the top of the low byte


def commit_to_nvm(bus: Bus, addr: int, model: str) -> None:
    if model != "PXE1610C":
        try:
            bus.send_once(addr, 0x00, NVM_STORE_COMMAND[model])
        finally:
            # wait the commit time even if there's an error because the command may have run anyway
            time.sleep(NVM_COMMIT_TIME_S)
        return
    bus.write(addr, PXE_CONFIG_PAGE, PXE_NVM_PASSWORD_CMD, PXE_NVM_PASSWORD)
    try:
        bus.send_once(addr, PXE_CONFIG_PAGE, PXE_NVM_UPLOAD_CMD)
    finally:
        # (same here)
        time.sleep(NVM_COMMIT_TIME_S)
        try:
            bus.write(addr, PXE_CONFIG_PAGE, PXE_NVM_PASSWORD_CMD, bytes(2))
        except BusError as e:
            log.warning("could not clear the NVM password at 0x%02X: %s", addr, e)


def set_icc_max(bus: Bus, addr: int, model: str, dry_run: bool, confirm: Callable[[str], bool]) -> Result:
    try:
        original_page = read_page(bus, addr)
        pmbus = original_page is not None and is_pmbus(bus, addr)
    except BusError as e:
        return Result("failed", str(e))
    if original_page is None:
        return Result("not found", "no device responds")
    if not pmbus:
        return Result("not found", "not recognised as a PMBus device; nothing was written to it")
    try:
        found = identify(bus, addr)
        if found is None:
            return Result("not found", "no supported controller recognised")
        if found != model:
            return Result("wrong chip", f"found {found}")
        log.info("  Detected %s", found)

        before = read_icc_max(bus, addr, model)
        log.info("  ICC_MAX: %s", icc_text(model, before))
        if before == TARGET_ICC_MAX:
            log.info("  Already at %d A, nothing to do", TARGET_ICC_MAX)
            return Result("already set", icc_text(model, before))

        writes_left = None
        if model == "PXE1610C":
            writes_left = pxe_nvm_writes_left(bus, addr)
            log.info("  NVM writes remaining: %d", writes_left)
            if writes_left == 0:
                return Result("failed", "no NVM writes remaining")

        change = f"{icc_text(model, before)} -> {icc_text(model, TARGET_ICC_MAX)}"
        if dry_run:
            log.info("  Dry run: would write ICC_MAX %s and commit it to NVM", icc_text(model, TARGET_ICC_MAX))
            return Result("would update", change)
        remaining = f" ({writes_left} writes remaining)" if writes_left is not None else ""
        if not confirm(f"Commit ICC_MAX {change} to NVM on {model} @ 0x{addr:02X}{remaining}?"):
            log.info("  Skipped")
            return Result("skipped")

        if model == "PXE1610C":
            bus.write(addr, PXE_CONFIG_PAGE, PXE_SMBUS_PASSWORD_CMD, PXE_SMBUS_PASSWORD)
        write_icc_max(bus, addr, model, TARGET_ICC_MAX)
        written = read_icc_max(bus, addr, model)
        if written != TARGET_ICC_MAX:
            return Result("failed", f"ICC_MAX read back as {icc_text(model, written)} after writing; NVM not committed")
        log.info("  Wrote ICC_MAX %s, read-back OK", icc_text(model, TARGET_ICC_MAX))

        commit_error = None
        try:
            commit_to_nvm(bus, addr, model)
        except BusError as e:
            commit_error = e
        if writes_left is None:
            if commit_error:
                return Result("failed", f"{commit_error}; unknown whether NVM was written: "
                                        "power-cycle the VRM, then re-run to check")
            log.info("  Committed to NVM")
        else:
            writes_left_after = pxe_nvm_writes_left(bus, addr)
            if commit_error and writes_left_after >= writes_left:
                return Result("failed", f"{commit_error}; NVM write counter unchanged, so nothing was committed")
            if commit_error:
                log.warning("%s at 0x%02X, but the NVM write counter dropped, so the upload ran", commit_error, addr)
            log.info("  Committed to NVM")
            log.info("  NVM writes remaining: %d", writes_left_after)
            if writes_left_after != writes_left - 1:
                log.warning(
                    "NVM write counter at 0x%02X went from %d to %d (expected %d)",
                    addr, writes_left, writes_left_after, writes_left - 1,
                )
        committed = read_icc_max(bus, addr, model)
        if committed != TARGET_ICC_MAX:
            return Result("failed", f"ICC_MAX reads {icc_text(model, committed)} after the NVM commit")
        log.info("  Done: ICC_MAX %s", change)
        return Result("updated", change)
    except BusError as e:
        return Result("failed", str(e))
    finally:
        restore_page(bus, addr, original_page)


def run_set(bus: Bus, model: str, addresses: list[int], dry_run: bool, confirm: Callable[[str], bool]) -> int:
    results = []
    for addr in addresses:
        log.info("%s @ 0x%02X", model, addr)
        result = set_icc_max(bus, addr, model, dry_run, confirm)
        if result.status in FAILURES:
            log.error("%s @ 0x%02X: %s", model, addr, result.detail)
        log.info("")
        results.append((addr, result))

    print("Summary")
    for addr, result in results:
        print(f"  0x{addr:02X}  {model}  {result.status:<12}  {result.detail}".rstrip())

    statuses = {result.status for _, result in results}
    if statuses & FAILURES:
        return EXIT_FAILED
    return EXIT_ABORTED if "skipped" in statuses else EXIT_OK


def scan(bus: Bus, start: int, end: int) -> int:
    def attempt(read: Callable[[], str]) -> str:
        try:
            return read()
        except BusError:
            return "n/a"

    log.info("Scanning 0x%02X-0x%02X ...", start, end)

    found = supported = unchecked = 0
    for addr in range(start, end + 1):
        try:
            original_page = read_page(bus, addr)
            pmbus = original_page is not None and is_pmbus(bus, addr)
        except BusError as e:
            log.error("0x%02X: %s", addr, e)
            unchecked += 1
            continue
        if original_page is None:
            log.debug("    0x%02X  no response", addr)
            continue
        if not pmbus:
            found += 1
            print(f"  0x{addr:02X}  not PMBus")
            continue
        try:
            model = identify(bus, addr)
            details = []
            if model in ICC_MAX_REGISTER:
                supported += 1
                details.append("ICC_MAX " + attempt(lambda: icc_text(model, read_icc_max(bus, addr, model))))
            if model == "PXE1610C":
                details.append("NVM writes remaining: " + attempt(lambda: str(pxe_nvm_writes_left(bus, addr))))
        except BusError as e:
            log.error("0x%02X: could not identify the device: %s", addr, e)
            unchecked += 1
            continue
        finally:
            restore_page(bus, addr, original_page)
        found += 1
        print(f"  0x{addr:02X}  {model or "unknown PMBus device"}  {', '.join(details)}".rstrip())

    log.info("")
    if found:
        print(f"Found {found} device{'s' if found != 1 else ''} ({supported} supported).")
    else:
        print("No devices found.")
    if unchecked:
        print(f"{unchecked} address{'es' if unchecked != 1 else ''} could not be checked; run the scan again.")
        return EXIT_FAILED
    return EXIT_OK


def open_device(devnum: int, speed_hz: int) -> EasyMCP2221.Device:
    device = EasyMCP2221.Device(devnum=devnum)
    device.I2C_speed(speed_hz)
    return device


@contextlib.contextmanager
def raw_adapter(devnum: int):
    devices = hid.enumerate(DEV_DEFAULT_VID, DEV_DEFAULT_PID)
    if devnum >= len(devices):
        raise AdapterError(f"MCP2221A #{devnum} is not connected")
    handle = hid.device()
    try:
        handle.open_path(devices[devnum]["path"])
        yield handle
    except OSError as e:
        raise AdapterError(f"cannot open the MCP2221A: {e}") from e
    finally:
        handle.close()


def send_report(handle: hid.device, command: list[int]) -> None:
    handle.write([0, *command] + [0] * (64 - len(command)))  # report ID 0, then one 64-byte report


def bus_released(handle: hid.device) -> bool:
    started = time.monotonic()
    while time.monotonic() - started <= BUS_HELD_LOW_TIMEOUT_S:
        send_report(handle, [CMD_POLL_STATUS_SET_PARAMETERS])
        status = handle.read(64, 1000)
        if status[I2C_POLL_RESP_SCL] and status[I2C_POLL_RESP_SDA]:
            return True
        time.sleep(BUS_BUSY_DELAY_S)
    return False


def reboot_adapter(devnum: int) -> None:
    log.warning("resetting the MCP2221A")
    with raw_adapter(devnum) as handle:
        send_report(handle, MCP2221_RESET)

    started = time.monotonic()
    while True:
        time.sleep(REENUMERATION_DELAY_S)
        try:
            with raw_adapter(devnum) as handle:
                released = bus_released(handle)
            break
        except AdapterError:
            if time.monotonic() - started > REENUMERATION_TIMEOUT_S:
                raise AdapterError("the MCP2221A did not reconnect after a reset") from None
    if not released:
        raise AdapterError("I2C bus stuck: still held low after resetting the MCP2221A")


def open_adapter(devnum: int, speed_hz: int) -> EasyMCP2221.Device:
    # first check to see if the  I2C engine has locked up holding SDA low and reboot if so
    with raw_adapter(devnum) as handle:
        released = bus_released(handle)
    if not released:
        reboot_adapter(devnum)

    started = time.monotonic()
    while True:
        try:
            return open_device(devnum, speed_hz)
        except (mcp_exc.LowSDAError, mcp_exc.LowSCLError) as e:
            if time.monotonic() - started > BUS_HELD_LOW_TIMEOUT_S:
                raise AdapterError(f"cannot open the MCP2221A: I2C bus stuck, {FAILURE_REASONS[type(e)]}") from e
            time.sleep(BUS_BUSY_DELAY_S)
        except (RuntimeError, OSError) as e:
            raise AdapterError(f"cannot open the MCP2221A: {e}") from e


def reset_adapter(device: EasyMCP2221.Device, devnum: int, speed_hz: int) -> EasyMCP2221.Device:
    device.hidhandler.close()  # frees the HID device for raw_adapter
    reboot_adapter(devnum)
    return open_adapter(devnum, speed_hz)


def i2c_address(text: str) -> int:
    match = re.fullmatch(r"0x([0-9a-f]{1,2})|([0-9a-f]{1,2})h?", text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid I2C address: {text!r}")
    addr = int(match.group(1) or match.group(2), 16)
    if not ADDR_MIN <= addr <= ADDR_MAX:
        raise argparse.ArgumentTypeError(f"I2C address {text} is outside 0x{ADDR_MIN:02X}-0x{ADDR_MAX:02X}")
    return addr


def i2c_speed(text: str) -> int:
    try:
        speed = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid speed: {text!r}") from None
    if not SPEED_MIN_HZ <= speed <= SPEED_MAX_HZ:
        raise argparse.ArgumentTypeError(f"speed must be {SPEED_MIN_HZ}-{SPEED_MAX_HZ} Hz")
    return speed


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--device", type=int, default=0, metavar="N", help="MCP2221A index (default 0)")
    common.add_argument("--speed", type=i2c_speed, default=SPEED_MAX_HZ, metavar="HZ",
                        help=f"I2C clock, {SPEED_MIN_HZ}-{SPEED_MAX_HZ} (default {SPEED_MAX_HZ})")
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="log every I2C transaction")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only print results, warnings, and errors")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    scan_parser = commands.add_parser("scan", parents=[common], help="identify controllers (read-only)",
                                      description="Identify PMBus controllers.")
    scan_parser.add_argument("start", nargs="?", type=i2c_address, metavar="START",
                             help=f"first address (default 0x{ADDR_MIN:02X}) (if only start given, scan only this address)")
    scan_parser.add_argument("end", nargs="?", type=i2c_address, metavar="END",
                             help=f"last address (default 0x{ADDR_MAX:02X})")

    set_parser = commands.add_parser("set", parents=[common], help="set ICC_MAX to 255 A and commit to NVM",
                                     description="Set ICC_MAX to 255 A and commit it to NVM.")
    set_parser.add_argument("chip", type=str.upper, choices=list(ICC_MAX_REGISTER), metavar="CHIP",
                            help=f"one of: {', '.join(m.lower() for m in ICC_MAX_REGISTER)}")
    set_parser.add_argument("addresses", nargs="+", type=i2c_address, metavar="ADDR")
    set_parser.add_argument("-n", "--dry-run", action="store_true", help="only report what would change, no permanent writes")
    set_parser.add_argument("-y", "--yes", action="store_true", help="commit without asking for confirmation")

    args = parser.parse_args(argv)
    if args.command == "scan":
        if args.start is None:
            args.start, args.end = ADDR_MIN, ADDR_MAX
        elif args.end is None:
            args.end = args.start
        elif args.start > args.end:
            scan_parser.error("START must not be greater than END")
    elif not (args.yes or args.dry_run or sys.stdin.isatty()):
        set_parser.error("confirmation needs an interactive terminal; use --yes to commit without asking")
    return args


class _StderrFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname.lower()}: {record.getMessage()}"


def setup_logging(verbose: bool, quiet: bool) -> None:
    stdout = logging.StreamHandler(sys.stdout)
    stdout.addFilter(lambda record: record.levelno < logging.WARNING)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(_StderrFormatter())
    log.handlers = [stdout, stderr]
    log.setLevel(logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO)
    log.propagate = False


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    log.info("vrm-iccmax")
    try:
        device = open_adapter(args.device, args.speed)
        log.info("Adapter: MCP2221A, serial %s, I2C %g kHz", device.usbserial, args.speed / 1000)
        log.info("")
        reset = lambda stuck: reset_adapter(stuck, args.device, args.speed)  # noqa: E731
        if args.command == "scan":
            return scan(Bus(device, ATTEMPTS, reset), args.start, args.end)
        addresses = list(dict.fromkeys(args.addresses))
        if len(addresses) < len(args.addresses):
            log.warning("duplicate addresses ignored")
        confirm = (lambda _question: True) if args.yes else ask
        return run_set(Bus(device, ATTEMPTS, reset), args.chip, addresses, args.dry_run, confirm)
    except AdapterError as e:
        log.error("%s", e)
        return EXIT_ADAPTER
    except KeyboardInterrupt:
        log.error("aborted")
        return EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
