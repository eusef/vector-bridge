"""PiSugar battery: board detection over a fake I2C bus, the voltage curve, and the home page hook."""

import pytest

from vector_bridge import battery
from vector_bridge.tuner import HOME, TunerApp


def bus(regs):
    """A read_byte(addr, reg) over {(addr, reg): value}; an address with no entries does not ACK."""
    addrs = {a for a, _ in regs}

    def read_byte(addr, reg):
        if addr not in addrs:
            raise OSError(121, "Remote I/O error")
        return regs.get((addr, reg), 0)
    return read_byte


def test_pisugar3_reports_its_own_percentage_and_plugged_state():
    got = battery.probe(bus({(0x57, 0x2A): 87, (0x57, 0x02): 0x80}))
    assert got == {"ok": True, "present": True, "model": "PiSugar 3", "percent": 87, "plugged": True}


def test_pisugar3_on_battery_is_not_plugged():
    assert battery.probe(bus({(0x57, 0x2A): 40, (0x57, 0x02): 0x40}))["plugged"] is False


def test_pisugar2_percentage_comes_from_voltage_through_the_ip5209_curve():
    # 0x13A3 counts is 3.950 V, between 3.92 V (65%) and 4.00 V (80%).
    got = battery.probe(bus({(0x75, 0xA2): 0xA3, (0x75, 0xA3): 0x13}))
    assert (got["model"], got["percent"], got["plugged"]) == ("PiSugar 2", 71, None)


def test_pisugar2_pro_is_read_from_the_ip5312_registers_when_ip5209_reads_are_not_a_cell():
    # 0x1174 counts is 3.800 V: 77% on the IP5312 curve.
    got = battery.probe(bus({(0x75, 0xD0): 0x74, (0x75, 0xD1): 0x11}))
    assert (got["model"], got["percent"]) == ("PiSugar 2 Pro", 77)


def test_nothing_on_the_bus_is_absent():
    assert battery.probe(bus({})) == {"ok": True, "present": False}


def test_a_missing_bus_device_is_absent(tmp_path):
    assert battery.read(str(tmp_path / "i2c-1")) == {"ok": True, "present": False}


@pytest.mark.parametrize("volts, percent", [(4.20, 100.0), (3.96, 72.5), (3.0, 0.0)])
def test_level_from_voltage_interpolates_like_pisugar_core(volts, percent):
    assert battery.level_from_voltage(volts, battery.IP5209_CURVE) == pytest.approx(percent)


def test_home_page_polls_the_battery_route_and_starts_hidden():
    a = TunerApp(lambda req: None, battery_fn=lambda: {"ok": True, "present": False})
    assert a.battery() == (200, {"ok": True, "present": False})
    assert '<span id="batt" hidden>' in HOME and "/api/battery" in HOME


def watch(volts):
    """A PlugWatch fed PiSugar 2 readings in turn; None stands for a board that stopped answering."""
    readings = iter(volts)

    def read():
        v = next(readings, None)
        return dict(battery.ABSENT) if v is None else {"ok": True, "present": True, "model": "PiSugar 2",
                                                       "percent": 70, "plugged": None, "volts": v}
    return battery.PlugWatch(read_fn=read, window=5)


def test_plug_watch_is_unknown_on_a_flat_voltage():
    w = watch([3.9371, 3.9393, 3.9395, 3.9403, 3.9390, 3.9395, 3.9376, 3.9385])  # bench trace, on battery
    for _ in range(8):
        w.sample()
    assert w.plugged is None


def test_plug_watch_flips_on_the_bench_plug_and_unplug_steps():
    w = watch([3.9360, 3.9355, 3.9360, 3.9360, 3.9360, 4.0056,          # USB on: +70 mV
                  4.0120, 4.0147, 4.0147, 4.0182, 4.0190, 3.9422])         # USB off: -71 mV
    for n in range(12):
        w.sample()
        if n == 5:
            assert w.plugged is True
        if n == 10:
            assert w.plugged is True  # still charging: the slow rise after the step is not another step
    assert w.plugged is False


def test_plug_watch_fills_only_a_missing_plugged_state():
    w = watch([4.0])
    w.plugged = True
    assert w.report()["plugged"] is True
    pisugar3 = battery.PlugWatch(read_fn=lambda: {"ok": True, "present": True, "model": "PiSugar 3",
                                                  "percent": 50, "plugged": False})
    pisugar3.plugged = True
    assert pisugar3.report()["plugged"] is False
    assert battery.PlugWatch(read_fn=lambda: dict(battery.ABSENT)).report() == {"ok": True, "present": False}


def test_plug_watch_sees_the_smaller_unplug_step_of_a_full_cell():
    w = watch([4.183, 4.183, 4.182, 4.183, 4.183, 4.155])  # bench, full: -28 mV
    w.plugged = True
    for _ in range(6):
        w.sample()
    assert w.plugged is False


@pytest.mark.parametrize("percent, plugged, level", [
    (21, False, "ok"), (20, False, "low"), (11, None, "low"), (10, False, "critical"),
    (7, False, "critical"), (6, False, "tx_locked"), (3, None, "tx_locked"), (5, True, "ok")])
def test_thresholds_warn_at_20_and_10_and_lock_transmit_at_6_on_battery(percent, plugged, level):
    r = battery.assess({"ok": True, "present": True, "percent": percent, "plugged": plugged})
    assert r["level"] == level and r["tx_locked"] is (level == "tx_locked")
