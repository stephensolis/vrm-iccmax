import argparse

import pytest
from EasyMCP2221 import exceptions as mcp_exc

import vrm_iccmax as vrm
from conftest import FakeChip, FakeDevice, FakeEEPROM, FakePXE1610C, fake_mp2955a, fake_tps5367x, fake_unknown, pxe_counter

ADDR = 0x60
YES = lambda question: True  # noqa: E731
NO = lambda question: False  # noqa: E731


def setup(chip, attempts=vrm.ATTEMPTS):
    device = FakeDevice({ADDR: chip} if chip else {})
    return device, vrm.Bus(device, attempts)


def icc_register(chip, model):
    page, cmd, _ = vrm.ICC_MAX_REGISTER[model]
    return int.from_bytes(chip.registers[(page, cmd)], "little")


SUPPORTED = [
    ("PXE1610C", lambda **kw: FakePXE1610C(**kw)),
    ("MP2955A", lambda **kw: fake_mp2955a(**kw)),
    ("TPS53679", lambda **kw: fake_tps5367x(0x79, **kw)),
    ("TPS53678", lambda **kw: fake_tps5367x(0x78, **kw)),
]


# --- set ---


@pytest.mark.parametrize("model, make", SUPPORTED)
def test_set_updates_and_commits_once(model, make):
    chip = make()
    _, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, model, dry_run=False, confirm=YES)
    assert result.status == "updated"
    assert icc_register(chip, model) == 0xFF
    assert chip.commits == 1
    assert chip.page == 0x2F


@pytest.mark.parametrize("model, make", SUPPORTED)
def test_set_skips_when_already_at_target(model, make):
    chip = make(icc_max=0xFF)
    device, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, model, dry_run=False, confirm=YES).status == "already set"
    assert {data[0] for data in device.writes(ADDR)} == {vrm.PAGE}
    assert chip.commits == 0


@pytest.mark.parametrize("model, make", SUPPORTED)
def test_set_dry_run_writes_only_page(model, make):
    chip = make()
    device, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, model, dry_run=True, confirm=YES)
    assert result.status == "would update"
    assert {data[0] for data in device.writes(ADDR)} == {vrm.PAGE}
    assert chip.commits == 0
    assert chip.page == 0x2F


@pytest.mark.parametrize("model, make", SUPPORTED)
def test_set_declined_changes_nothing(model, make):
    chip = make()
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, model, dry_run=False, confirm=NO).status == "skipped"
    assert icc_register(chip, model) == 0xB4
    assert chip.commits == 0


def test_set_full_word_compared_against_target():
    chip = fake_tps5367x(icc_max=0x01FF)
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "TPS53679", dry_run=False, confirm=YES).status == "updated"
    assert icc_register(chip, "TPS53679") == 0x00FF


def test_set_no_device():
    _, bus = setup(None)
    assert vrm.set_icc_max(bus, ADDR, "MP2955A", False, YES) == vrm.Result("not found", "no device responds")


def test_set_unrecognised_device():
    _, bus = setup(fake_unknown())
    result = vrm.set_icc_max(bus, ADDR, "MP2955A", False, YES)
    assert result == vrm.Result("not found", "no supported controller recognised")


@pytest.mark.parametrize("chip, requested, found", [
    (fake_tps5367x(0x78), "TPS53679", "TPS53678"),
    (FakePXE1610C(), "MP2955A", "PXE1610C"),
])
def test_set_wrong_chip(chip, requested, found):
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, requested, False, YES) == vrm.Result("wrong chip", f"found {found}")
    assert chip.commits == 0


@pytest.mark.parametrize("model, make", SUPPORTED)
def test_set_read_back_mismatch_prevents_commit(model, make):
    chip = make()
    page, cmd, _ = vrm.ICC_MAX_REGISTER[model]
    chip.ignore_writes.add((page, cmd))
    _, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, model, False, YES)
    assert result.status == "failed" and "NVM not committed" in result.detail
    assert chip.commits == 0
    assert chip.page == 0x2F


def test_set_fails_when_value_not_retained_after_commit():
    chip = fake_mp2955a()
    chip.store = lambda cmd: chip.registers.update({(0x00, 0xEF): b"\xb4"})
    _, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, "MP2955A", False, YES)
    assert result == vrm.Result("failed", "ICC_MAX reads 0xB4 (180 A) after the NVM commit")


@pytest.mark.parametrize("model, make, store", [
    ("PXE1610C", lambda: FakePXE1610C(), (0x3F, 0x34)),
    ("MP2955A", lambda: fake_mp2955a(), (0x00, 0x15)),
    ("TPS53679", lambda: fake_tps5367x(), (0x00, 0x11)),
])
def test_set_commit_is_never_retried(model, make, store):
    chip = make()
    chip.nack.add(store)
    device, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, model, False, YES)
    assert result.status == "failed"
    assert device.writes(ADDR).count(bytes([store[1]])) == 1


def test_set_warns_when_page_cannot_be_restored(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chip = fake_mp2955a(icc_max=0xFF)
    device, bus = setup(chip)
    original_write = chip.write

    def fail_restore(data, kind):
        if data == bytes([vrm.PAGE, 0x2F]):
            raise mcp_exc.NotAckError()
        original_write(data, kind)

    chip.write = fail_restore
    vrm.set_icc_max(bus, ADDR, "MP2955A", False, YES)
    assert "warning: could not restore PAGE 0x2F at 0x60: write PAGE 0x2F failed: no ACK" in capsys.readouterr().err


def test_set_survives_adapter_lockup_and_restores_page():
    chip = FakePXE1610C()
    chips = {ADDR: chip}
    locks_up, recovered = FakeDevice(chips), FakeDevice(chips)
    bus = vrm.Bus(locks_up, vrm.ATTEMPTS, reset_adapter=lambda stuck_device: recovered)
    original_write = chip.write

    def lock_up_on_icc_write(data, kind):
        if data[:1] == b"\x73" and kind == "regular" and bus.device is locks_up:
            locks_up.fail_next[ADDR] = [mcp_exc.LowSDAError()] * 1000
            raise mcp_exc.LowSDAError()
        original_write(data, kind)

    chip.write = lock_up_on_icc_write
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES).status == "updated"
    assert icc_register(chip, "PXE1610C") == 0xFF
    assert chip.commits == 1
    assert chip.page == 0x2F


def test_set_fails_rather_than_reporting_not_found_when_presence_is_unknown():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.TimeoutError()] * vrm.ATTEMPTS
    assert vrm.set_icc_max(bus, ADDR, "MP2955A", False, YES) == vrm.Result("failed", "read cmd 0x00 failed: timeout")


def test_set_never_writes_to_a_device_that_is_not_pmbus():
    eeprom = FakeEEPROM()
    device, bus = setup(eeprom)
    result = vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES)
    assert result == vrm.Result("not found", "not recognised as a PMBus device; nothing was written to it")
    assert eeprom.stores == []


# --- PXE1610C specifics ---


def test_pxe_counter_decodes_values_above_0x3f():
    chip = FakePXE1610C()
    chip.registers[(0x50, 0x82)] = bytes([0xC0, 0x7F])
    _, bus = setup(chip)
    assert vrm.pxe_nvm_writes_left(bus, ADDR) == 511


def test_pxe_update_with_large_counter():
    chip = FakePXE1610C(writes_left=500)
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES).status == "updated"
    assert chip.writes_left == 499


def test_pxe_sequence_unlocks_commits_and_clears_password():
    chip = FakePXE1610C()
    device, bus = setup(chip)
    vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES)
    commands = [data for data in device.writes(ADDR) if data[0] != vrm.PAGE]
    assert commands == [
        b"\x27\x7c\xb3",  # SMBus password
        b"\x73\xff\x00",  # ICC_MAX
        b"\x29\xd7\xef",  # NVM password
        b"\x34",          # upload to NVM
        b"\x29\x00\x00",  # clear NVM password
    ]


def test_pxe_refuses_when_no_nvm_writes_left():
    chip = FakePXE1610C(writes_left=0)
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES) == vrm.Result("failed", "no NVM writes remaining")
    assert icc_register(chip, "PXE1610C") == 0xB4


def test_pxe_upload_error_with_counter_unchanged_reports_nothing_committed():
    chip = FakePXE1610C()
    chip.nack.add((0x3F, 0x34))
    _, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES)
    assert result == vrm.Result(
        "failed", "write page 0x3F cmd 0x34 failed: no ACK; NVM write counter unchanged, so nothing was committed")


def test_pxe_upload_error_with_counter_dropped_is_a_success(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chip = FakePXE1610C()
    store = chip.store

    def upload_then_lose_ack(cmd):
        store(cmd)
        raise mcp_exc.NotAckError()

    chip.store = upload_then_lose_ack
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES).status == "updated"
    assert chip.commits == 1
    assert ("warning: write page 0x3F cmd 0x34 failed: no ACK at 0x60, but the NVM write counter dropped, "
            "so the upload ran") in capsys.readouterr().err


def test_store_error_without_counter_reports_unknown_nvm_state():
    chip = fake_tps5367x()
    chip.nack.add((0x00, 0x11))
    _, bus = setup(chip)
    result = vrm.set_icc_max(bus, ADDR, "TPS53679", False, YES)
    assert result.status == "failed"
    assert result.detail.endswith("unknown whether NVM was written: power-cycle the VRM, then re-run to check")


def test_commit_waits_full_time_even_after_an_error(clock):
    chip = FakePXE1610C()
    chip.nack.add((0x3F, 0x34))
    _, bus = setup(chip)
    with pytest.raises(vrm.BusError):
        vrm.commit_to_nvm(bus, ADDR, "PXE1610C")
    assert clock[0] >= vrm.NVM_COMMIT_TIME_S
    assert chip.registers[(0x3F, 0x29)] == bytes(2)


def test_pxe_nvm_password_cleared_when_upload_fails():
    chip = FakePXE1610C()
    chip.nack.add((0x3F, 0x34))
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES).status == "failed"
    assert chip.registers[(0x3F, 0x29)] == bytes(2)


def test_pxe_stuck_counter_warns_but_succeeds(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chip = FakePXE1610C()
    chip.counter_stuck = True
    _, bus = setup(chip)
    assert vrm.set_icc_max(bus, ADDR, "PXE1610C", False, YES).status == "updated"
    assert "warning: NVM write counter at 0x60 went from 12 to 12 (expected 11)" in capsys.readouterr().err


def test_pxe_counter_helper_round_trips():
    assert pxe_counter(511) == bytes([0xC0, 0x7F])


# --- bus ---


def test_bus_read_wire_sequence():
    chip = FakePXE1610C()
    device, bus = setup(chip)
    assert bus.read(ADDR, 0x20, 0x73, 2) == b"\xb4\x00"
    assert device.transfers == [
        ("W", ADDR, "regular", b"\x00\x20"),
        ("W", ADDR, "nonstop", b"\x00"),
        ("R", ADDR, "restart", 1),
        ("W", ADDR, "nonstop", b"\x73"),
        ("R", ADDR, "restart", 2),
    ]


def test_bus_write_wire_sequence():
    chip = FakePXE1610C()
    device, bus = setup(chip)
    bus.write(ADDR, 0x20, 0x73, b"\xff\x00")
    assert device.transfers == [
        ("W", ADDR, "regular", b"\x00\x20"),
        ("W", ADDR, "nonstop", b"\x00"),
        ("R", ADDR, "restart", 1),
        ("W", ADDR, "regular", b"\x73\xff\x00"),
    ]


def test_lost_page_write_is_caught_before_the_command():
    chip = FakePXE1610C()
    device, bus = setup(chip)
    bus.read(ADDR, 0x20, 0x73, 2)
    chip.lost_page_writes = 1  # acknowledged but not applied, as seen after a bus collision
    assert vrm.pxe_nvm_writes_left(bus, ADDR) == 12  # not page 0x20's 00 81, which decodes as 516


def test_page_that_never_takes_effect_fails_the_transaction():
    chip = FakePXE1610C()
    _, bus = setup(chip)
    chip.lost_page_writes = 1000
    with pytest.raises(vrm.BusError, match="^write page 0x20 cmd 0x73 failed: PAGE reads 0x2F after selecting 0x20$"):
        bus.write(ADDR, 0x20, 0x73, b"\xff\x00")
    assert chip.registers[(0x20, 0x73)] == b"\xb4\x00"
    assert (0x2F, 0x73) not in chip.registers


def test_bus_retries_transient_errors():
    device, bus = setup(FakePXE1610C())
    device.fail_next[ADDR] = [mcp_exc.NotAckError(), mcp_exc.TimeoutError(), RuntimeError("engine busy")]
    assert bus.read(ADDR, 0x20, 0x73, 2) == b"\xb4\x00"


def test_bus_gives_up_after_all_attempts():
    device, bus = setup(FakePXE1610C())
    device.fail_next[ADDR] = [mcp_exc.NotAckError() for _ in range(vrm.ATTEMPTS)]
    with pytest.raises(vrm.BusError, match=r"^read page 0x20 cmd 0x73 failed: no ACK$"):
        bus.read(ADDR, 0x20, 0x73, 2)


def test_bus_usb_failure_is_fatal():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [OSError("gone")]
    with pytest.raises(vrm.AdapterError, match="USB communication with the MCP2221A failed"):
        bus.read(ADDR, 0x00, 0xEF, 1)


@pytest.mark.parametrize("error", [mcp_exc.LowSDAError, mcp_exc.LowSCLError])
def test_bus_waits_out_held_low_lines_without_using_attempts(error):
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [error()] * 20 + [mcp_exc.NotAckError()] * (vrm.ATTEMPTS - 1)
    assert bus.read(ADDR, 0x00, 0xEF, 1) == b"\xb4"


def test_bus_held_low_counts_attempts_again_after_other_errors():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.LowSDAError(), mcp_exc.NotAckError()] * vrm.ATTEMPTS
    with pytest.raises(vrm.BusError, match="no ACK$"):
        bus.read(ADDR, 0x00, 0xEF, 1)


def test_bus_resets_adapter_after_held_low_timeout_and_continues(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chips = {ADDR: fake_mp2955a()}
    stuck, recovered = FakeDevice(chips), FakeDevice(chips)
    stuck.fail_next[ADDR] = [mcp_exc.LowSDAError()] * 1000
    bus = vrm.Bus(stuck, vrm.ATTEMPTS, reset_adapter=lambda stuck_device: recovered)
    assert bus.read(ADDR, 0x00, 0xEF, 1) == b"\xb4"
    assert bus.device is recovered
    assert bus.device is recovered


def test_bus_resets_adapter_only_once_per_transaction():
    resets = []

    def reset(device):
        resets.append(device)
        return still_stuck

    still_stuck = FakeDevice({ADDR: fake_mp2955a()})
    still_stuck.fail_next[ADDR] = [mcp_exc.LowSDAError()] * 1000
    bus = vrm.Bus(still_stuck, vrm.ATTEMPTS, reset_adapter=reset)
    with pytest.raises(vrm.AdapterError, match="^I2C bus stuck: SDA held low for over 3 s$"):
        bus.read(ADDR, 0x00, 0xEF, 1)
    assert resets == [still_stuck]


def test_bus_held_low_too_long_is_fatal(clock):
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.LowSDAError()] * 1000
    with pytest.raises(vrm.AdapterError, match=r"^I2C bus stuck: SDA held low for over 3 s$"):
        bus.read(ADDR, 0x00, 0xEF, 1)
    assert vrm.BUS_HELD_LOW_TIMEOUT_S < clock[0] < vrm.BUS_HELD_LOW_TIMEOUT_S + 0.5


def test_send_once_retries_the_page_write():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.NotAckError()]  # the first transfer is the PAGE write
    bus.send_once(ADDR, 0x00, 0x15)
    assert device.chips[ADDR].commits == 1
    assert device.writes(ADDR).count(b"\x15") == 1


def test_send_once_never_repeats_the_command_even_when_bus_held_low():
    chip = fake_mp2955a()
    original_write = chip.write

    def held_low_on_command(data, kind):
        if data == b"\x15":
            raise mcp_exc.LowSDAError()
        original_write(data, kind)

    chip.write = held_low_on_command
    device, bus = setup(chip)
    with pytest.raises(vrm.BusError, match="^write page 0x00 cmd 0x15 failed: SDA held low$"):
        bus.send_once(ADDR, 0x00, 0x15)
    assert device.writes(ADDR).count(b"\x15") == 1


def test_bus_error_reports_whether_every_attempt_was_nacked():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.NotAckError()] * vrm.ATTEMPTS
    with pytest.raises(vrm.BusError) as all_nacked:
        bus.read(ADDR, 0x00, 0xEF, 1)
    device.fail_next[ADDR] = [mcp_exc.TimeoutError(), RuntimeError("Internal I2C engine timeout.")] * 2
    with pytest.raises(vrm.BusError) as never_answered:
        bus.read(ADDR, 0x00, 0xEF, 1)
    assert (all_nacked.value.nacked, never_answered.value.nacked) == (True, False)
    assert str(never_answered.value) == "read page 0x00 cmd 0xEF failed: timeout; Internal I2C engine timeout."


def test_one_nack_among_inconclusive_failures_is_the_answer():
    device, bus = setup(fake_mp2955a())
    device.fail_next[ADDR] = [mcp_exc.NotAckError(), RuntimeError("Internal I2C engine timeout.")] * 2
    with pytest.raises(vrm.BusError) as error:
        bus.read(ADDR, 0x00, 0xEF, 1)
    assert error.value.nacked


def test_attempts_lost_to_page_selection_do_not_count_against_a_nack():
    chip = fails_on(FakePXE1610C(), 0xAD, mcp_exc.NotAckError)  # 0xAD is not implemented
    _, bus = setup(chip)
    chip.lost_page_writes = 1
    with pytest.raises(vrm.BusError) as error:
        bus.read(ADDR, 0x00, 0xAD, 2)
    assert error.value.nacked
    assert str(error.value) == "read page 0x00 cmd 0xAD failed: PAGE reads 0x2F after selecting 0x00; no ACK"


def test_attempts_that_all_lost_page_selection_are_not_a_nack():
    chip = FakePXE1610C()
    _, bus = setup(chip)
    chip.lost_page_writes = 1000
    with pytest.raises(vrm.BusError) as error:
        bus.read(ADDR, 0x00, 0xAD, 2)
    assert not error.value.nacked


# --- scan ---


def test_scan_identifies_and_restores_page(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    pxe = FakePXE1610C(writes_left=12)
    device = FakeDevice({0x20: fake_tps5367x(0x79, icc_max=0x00C8), 0x60: pxe, 0x62: fake_unknown()})
    assert vrm.scan(vrm.Bus(device, vrm.ATTEMPTS), 0x03, 0x77) == vrm.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    rows = [line.split(None, 1) for line in lines if line.startswith("  0x")]
    assert [(addr, " ".join(rest.split())) for addr, rest in rows] == [
        ("0x20", "TPS53679 ICC_MAX 0x00C8 (200 A)"),
        ("0x60", "PXE1610C ICC_MAX 0x00B4 (180 A), NVM writes remaining: 12"),
        ("0x62", "unknown PMBus device"),
    ]
    assert "Found 3 devices (2 supported)." in lines
    assert pxe.page == 0x2F
    assert pxe.registers[(0x3F, 0x27)] == bytes(2)  # scan never unlocks


def test_scan_reports_unknown_primarion(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chip = FakePXE1610C()
    chip.registers[(0x4F, 0x32)] = b"\x16\x04"
    vrm.scan(vrm.Bus(FakeDevice({ADDR: chip}), vrm.ATTEMPTS), ADDR, ADDR)
    assert "  0x60  Primarion (unknown model)" in capsys.readouterr().out


def fails_on(chip, cmd, error):
    """Make every transfer that sends cmd to chip fail with error."""
    original_write = chip.write

    def write(data, kind):
        if data[:1] == bytes([cmd]):
            raise error()
        original_write(data, kind)

    chip.write = write
    return chip


def test_identify_raises_instead_of_misidentifying_on_non_nack_errors():
    chip = fails_on(FakePXE1610C(), 0x32, mcp_exc.TimeoutError)  # PXE1610C's model check
    _, bus = setup(chip)
    with pytest.raises(vrm.BusError, match="read page 0x4F cmd 0x32 failed: timeout"):
        vrm.identify(bus, ADDR)


def test_scan_reports_addresses_it_could_not_check(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    chip = fails_on(FakePXE1610C(), 0x32, mcp_exc.TimeoutError)
    device = FakeDevice({0x60: chip, 0x62: fake_mp2955a()})
    assert vrm.scan(vrm.Bus(device, vrm.ATTEMPTS), 0x60, 0x62) == vrm.EXIT_FAILED
    captured = capsys.readouterr()
    assert "error: 0x60: could not identify the device: read page 0x4F cmd 0x32 failed: timeout" in captured.err
    assert "Primarion" not in captured.out
    assert "  0x62  MP2955A" in captured.out
    assert "1 address could not be checked; run the scan again." in captured.out
    assert chip.page == 0x2F


def test_scan_never_writes_to_a_device_that_is_not_pmbus(capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    eeprom = FakeEEPROM()
    device = FakeDevice({0x50: eeprom, 0x60: FakePXE1610C()})
    assert vrm.scan(vrm.Bus(device, vrm.ATTEMPTS), 0x50, 0x60) == vrm.EXIT_OK
    out = capsys.readouterr().out
    assert "  0x50  not PMBus" in out
    assert "  0x60  PXE1610C" in out
    assert eeprom.stores == []
    assert eeprom.memory == bytearray(range(256))


@pytest.mark.parametrize("revision, pmbus", [(0x33, True), (0x22, True), (0x00, False), (0xFF, False)])
def test_pmbus_revision_must_be_a_valid_value(revision, pmbus):
    chip = FakeChip({(None, 0x98): bytes([revision])})
    _, bus = setup(chip)
    assert vrm.is_pmbus(bus, ADDR) is pmbus


def test_pmbus_check_recognises_primarion_without_pmbus_revision():
    _, bus = setup(FakePXE1610C())
    assert vrm.is_pmbus(bus, ADDR)


def test_pmbus_check_writes_nothing():
    chip = FakePXE1610C()
    device, bus = setup(chip)
    vrm.is_pmbus(bus, ADDR)
    assert device.writes(ADDR) == []


def test_scan_nothing_found(capsys):
    vrm.scan(vrm.Bus(FakeDevice({}), vrm.ATTEMPTS), 0x03, 0x05)
    assert "No devices found." in capsys.readouterr().out


# --- adapter open/reset ---


RESET_REPORT = [0, 0x70, 0xAB, 0xCD, 0xEF] + [0] * 60


def test_reset_adapter_releases_library_handle_reboots_and_reopens(usb):
    stuck = FakeDevice({})
    assert vrm.reset_adapter(stuck, 0, 400_000) is usb.opened
    assert stuck.hidhandler.closed
    assert usb.resets() == [RESET_REPORT]


def test_open_adapter_reboots_a_locked_adapter_before_opening_it(usb, capsys):
    vrm.setup_logging(verbose=False, quiet=False)
    usb.sda_low = True
    assert vrm.open_adapter(0, 400_000) is usb.opened
    assert usb.resets() == [RESET_REPORT]
    assert "resetting the MCP2221A" in capsys.readouterr().err


def test_open_adapter_checks_lines_without_rebooting_a_healthy_adapter(usb):
    assert vrm.open_adapter(0, 400_000) is usb.opened
    assert usb.resets() == []
    assert usb.reports[0][:2] == [0, 0x10]  # a status poll, no I2C traffic


def test_open_adapter_waits_out_brief_held_low_errors(usb):
    usb.open_errors = [mcp_exc.LowSDAError("SDA is low.")] * 10
    assert vrm.open_adapter(0, 400_000) is usb.opened
    assert usb.resets() == []


def test_open_adapter_missing_device_fails_at_once(usb, clock):
    usb.open_errors = [RuntimeError("No devices found with VID 04D8 and PID 00DD.")]
    with pytest.raises(vrm.AdapterError, match="No devices found"):
        vrm.open_adapter(0, 400_000)
    assert clock[0] == 0


def test_open_adapter_without_adapter(usb):
    usb.count = 0
    with pytest.raises(vrm.AdapterError, match="^MCP2221A #0 is not connected$"):
        vrm.open_adapter(0, 400_000)


def test_reboot_fails_when_adapter_does_not_reconnect(usb):
    usb.failed_opens_after_reset = 1000
    with pytest.raises(vrm.AdapterError, match="^the MCP2221A did not reconnect after a reset$"):
        vrm.reboot_adapter(0)


def test_reboot_fails_when_bus_stays_held_low(usb):
    usb.sda_low_after_reset = True
    with pytest.raises(vrm.AdapterError, match="^I2C bus stuck: still held low after resetting the MCP2221A$"):
        vrm.reboot_adapter(0)


# --- command line ---


@pytest.mark.parametrize("text, addr", [("60", 0x60), ("0x60", 0x60), ("0X60", 0x60), ("60h", 0x60), ("3", 0x03), ("77", 0x77)])
def test_address_parsing(text, addr):
    assert vrm.i2c_address(text) == addr


@pytest.mark.parametrize("text", ["0x02", "78", "zz", "", "0x", "0x60h", "100"])
def test_address_rejected(text):
    with pytest.raises(argparse.ArgumentTypeError):
        vrm.i2c_address(text)


@pytest.mark.parametrize("argv, start, end", [
    (["scan"], 0x03, 0x77),
    (["scan", "40"], 0x40, 0x40),
    (["scan", "0x20", "0x6f"], 0x20, 0x6F),
])
def test_scan_range_arguments(argv, start, end):
    args = vrm.parse_args(argv)
    assert (args.start, args.end) == (start, end)


def test_scan_range_reversed_is_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        vrm.parse_args(["scan", "0x60", "0x20"])
    assert exit_info.value.code == 2


def test_chip_name_is_case_insensitive():
    assert vrm.parse_args(["set", "pxe1610c", "60", "--yes"]).chip == "PXE1610C"


def test_set_without_terminal_requires_yes(monkeypatch):
    monkeypatch.setattr(vrm.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exit_info:
        vrm.parse_args(["set", "mp2955a", "60"])
    assert exit_info.value.code == 2


@pytest.fixture
def adapter(monkeypatch):
    def install(chips):
        device = FakeDevice(chips)
        monkeypatch.setattr(vrm, "open_adapter", lambda devnum, speed: device)
        return device
    return install


def test_main_success(adapter, capsys):
    adapter({0x60: fake_mp2955a(), 0x62: fake_mp2955a(icc_max=0xFF)})
    assert vrm.main(["set", "mp2955a", "60", "62", "--yes"]) == vrm.EXIT_OK
    out = capsys.readouterr().out
    assert "  0x60  MP2955A  updated       0xB4 (180 A) -> 0xFF (255 A)" in out
    assert "  0x62  MP2955A  already set   0xFF (255 A)" in out


def test_main_failure_wins_and_is_reported_on_stderr(adapter, capsys):
    adapter({0x60: fake_mp2955a()})
    assert vrm.main(["set", "mp2955a", "60", "61", "--yes"]) == vrm.EXIT_FAILED
    assert "error: MP2955A @ 0x61: no device responds" in capsys.readouterr().err


def test_main_skipped(adapter, monkeypatch):
    adapter({0x60: fake_mp2955a()})
    monkeypatch.setattr(vrm.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert vrm.main(["set", "mp2955a", "60"]) == vrm.EXIT_ABORTED


def test_main_adapter_error(monkeypatch, capsys):
    def fail(devnum, speed):
        raise vrm.AdapterError("cannot open the MCP2221A: No devices found")
    monkeypatch.setattr(vrm, "open_adapter", fail)
    assert vrm.main(["scan"]) == vrm.EXIT_ADAPTER
    assert "error: cannot open the MCP2221A: No devices found" in capsys.readouterr().err


def test_main_duplicate_addresses_processed_once(adapter, capsys):
    device = adapter({0x60: fake_mp2955a()})
    vrm.main(["set", "mp2955a", "60", "0x60", "--yes"])
    assert device.chips[0x60].commits == 1
    assert "warning: duplicate addresses ignored" in capsys.readouterr().err


def test_main_quiet_prints_only_results(adapter, capsys):
    adapter({0x60: fake_mp2955a()})
    vrm.main(["set", "mp2955a", "60", "--yes", "-q"])
    assert capsys.readouterr().out.splitlines() == ["Summary", "  0x60  MP2955A  updated       0xB4 (180 A) -> 0xFF (255 A)"]


def test_main_verbose_logs_transactions(adapter, capsys):
    adapter({0x60: fake_mp2955a()})
    vrm.main(["set", "mp2955a", "60", "--yes", "-v"])
    assert "    0x60  R  page 0x00 cmd 0xEF -> B4" in capsys.readouterr().out
