"""Logic checks for device.py / driver.py, run outside Homey:

    python3 tests/test_device_logic.py

Stubs the Homey SDK and the DTU network client; uses the real hoymiles_wifi
protobufs from python_packages (no network, no hardware).
"""

import asyncio
import glob
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
SP = glob.glob("python_packages/*/.venv/lib/python3*/site-packages")[0]
sys.path.insert(0, SP)
# Stub the Homey SDK and app package imports so device.py loads outside Homey.
m = types.ModuleType("hoymiles_wifi.dtu")
m.DTU = object
sys.modules["hoymiles_wifi.dtu"] = m
for name in (
    "homey",
    "homey.device",
    "homey.driver",
    "homey.app",
    "astral",
    "astral.sun",
):
    sys.modules[name] = types.ModuleType(name)
sys.modules["homey.device"].Device = object
sys.modules["homey.driver"].Driver = object
sys.modules["homey.app"].App = object
sys.modules["astral"].LocationInfo = object
sys.modules["astral.sun"].sun = None


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


for pkg in ("app", "app.drivers", "app.drivers.hoymiles", "app.lib"):
    sys.modules[pkg] = types.ModuleType(pkg)
load("app.lib.fault_codes", "lib/fault_codes.py")
load("app.app", "app.py")
drv = load("app.drivers.hoymiles.driver", "drivers/hoymiles/driver.py")
dev = load("app.drivers.hoymiles.device", "drivers/hoymiles/device.py")
from hoymiles_wifi.protobuf import ESData_pb2  # noqa: E402 (after sys.path setup)


def es(powers, energies, soc):
    m = ESData_pb2.ESDataReqDTO()
    for p, e in zip(powers, energies):
        pv = m.pv_panels.add()
        pv.power = p
        pv.energy = e
    if soc is not None:
        m.battery_management.state_of_charge = soc
    return m


class FakeDTU:
    def __init__(self, replies):
        self.replies = replies

    async def async_get_energy_storage_data(
        self, dtu_serial_number, inverter_serial_number
    ):
        r = self.replies[inverter_serial_number]
        if isinstance(r, Exception):
            raise r
        return r


class D(dev.HoymilesDevice):
    def __init__(self, settings, replies):
        self.s = settings
        self.vals = {}
        self._dtu = FakeDTU(replies)

    def get_setting(self, k):
        return self.s.get(k)

    async def set_capability_value(self, c, v):
        self.vals[c] = v

    async def set_available(self):
        pass

    async def set_unavailable(self, msg):
        self.vals["unavailable"] = msg

    def log(self, *a):
        pass


settings = {
    "is_hybrid": True,
    "dtu_sn": "123",
    "inverter_sns": json.dumps(["1", "2", "3"]),
}
d = D(
    settings,
    {1: es([1000, 500], [100, 50], 90), 2: es([200], [30], 90), 3: es([0], [20], 30)},
)
asyncio.run(d._poll_hybrid())
assert d.vals["measure_power"] == 1700, d.vals
assert abs(d.vals["meter_power"] - 20.0) < 1e-9, d.vals  # 200 * 0.1 kWh
assert d.vals["measure_battery"] == 70, d.vals  # (90+90+30)/3
assert d.vals["alarm_generic"] is False

# One inverter down: power still shown, lifetime NOT written (would understate total).
d.vals.clear()
d._dtu = FakeDTU({1: es([100], [101], 50), 2: OSError("down"), 3: es([0], [20], 50)})
asyncio.run(d._poll_hybrid())
assert d.vals["measure_power"] == 100 and "meter_power" not in d.vals, d.vals

# Build list: hybrid has no daily tile.
assert "meter_power.today" not in drv._build_capabilities(2, False, True)
print("hybrid checks OK")


class R(D):
    def __init__(self, settings, caps):
        super().__init__(settings, {})
        self.caps = set(caps)
        self.opts = {}

    def has_capability(self, c):
        return c in self.caps

    async def add_capability(self, c):
        self.caps.add(c)

    async def set_capability_options(self, c, o):
        self.opts[c] = o

    async def set_settings(self, s):
        self.s.update(s)


# Paired with 2 panels, live data shows 4, PV detail hidden: adds pv3/pv4 power only.
r = R(
    {"panel_count": 2, "three_phase": False, "showPvDetail": False},
    drv._build_capabilities(2, False, False),
)
r._last_pv_count = 4
added = asyncio.run(r.restore_capabilities())
assert set(added) == {"measure_power.pv3", "measure_power.pv4"}, added
assert (
    r.s["panel_count"] == 4
    and r.opts["measure_power.pv3"]["title"]["en"] == "Panel 3 Power"
)
# on_settings path: new three_phase value passed in, adds phase caps.
added = asyncio.run(r.restore_capabilities({"three_phase": True}))
assert "measure_voltage.phase_a" in added, added
print("restore checks OK")


# Night window crossing midnight UTC (e.g. UTC-6: sunrise 12.5h, sunset 1.0h UTC next day).
class N(D):
    def __init__(self):
        pass


n = N()


def night_at(hour, times):
    real = dev.datetime

    class Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 6, 1, int(hour), int(hour % 1 * 60), tzinfo=timezone.utc)

    dev.datetime = Fixed
    try:
        return n._is_night_time_from(times)
    finally:
        dev.datetime = real


assert night_at(6.0, (12.5, 1.0)) is True  # 00:00 local
assert night_at(18.0, (12.5, 1.0)) is False  # 12:00 local
assert night_at(3.0, (5.0, 20.0)) is True and night_at(12.0, (5.0, 20.0)) is False


# W100 legacy rows: one grid row per inverter, values aggregated.
def row(sn, port, gv, gp):
    return types.SimpleNamespace(
        pv_sn=sn,
        pv_port=port,
        pv_power=100,
        pv_vol=300,
        pv_cur=50,
        pv_energy_total=1000,
        pv_energy=20,
        pv_fault_num=0,
        grid_vol=gv,
        grid_freq=5000 if gv else 0,
        grid_p=gp,
        grid_i=10 if gv else 0,
        pv_temp=250,
    )


legacy = types.SimpleNamespace(
    dtu_sn="10D3",
    pv_data=[row("A", 1, 2300, 500), row("A", 2, 0, 0), row("B", 1, 2310, 400)],
)
out = dev.HoymilesDevice._normalize_legacy_real_data(legacy)
assert (
    out.dtu_power == 900 and len(out.pv_data) == 3 and out.sgs_data[0].voltage == 2305
), out


# A night must end the "same day" for the daily-zero guard.
class Night(D):
    def __init__(self):
        self.vals = {}
        self.s = {}

    async def _apply_zeros(self):
        pass


nd = Night()
nd._last_poll_ok_date = datetime.now(timezone.utc).date()
asyncio.run(nd._apply_night_backoff((5.0, 20.0)))
assert nd._last_poll_ok_date is None
print("night/legacy checks OK")


# Power ratio: tile follows nominal_power; value = power / nominal.
class P(R):
    async def remove_capability(self, c):
        self.caps.discard(c)


p = P({"nominal_power": 1600}, ["measure_power"])
asyncio.run(p._sync_power_ratio_cap())
assert "measure_power_ratio" in p.caps
asyncio.run(p._set_power(400.0))
assert p.vals["measure_power"] == 400.0 and p.vals["measure_power_ratio"] == 25, p.vals
asyncio.run(p._sync_power_ratio_cap(0))  # on_settings passes the new value
assert "measure_power_ratio" not in p.caps
print("power ratio checks OK")

# Regression (10-13 dev log): devices paired before nominal_power existed have no
# stored value; on_init must not crash and must not add the tile.
old = P({}, ["measure_power"])
asyncio.run(old._sync_power_ratio_cap())
assert "measure_power_ratio" not in old.caps
# Hidden PV detail must stay hidden on init (no sentinel coercing it to "show").
hid = R({"panel_count": 1, "showPvDetail": False}, ["measure_power"])
asyncio.run(hid._sync_detail_caps("showPvDetail", hid._pv_detail_caps()))
assert "measure_voltage.pv1" not in hid.caps, hid.caps
print("init default checks OK")


# Repair (10-13 log): saving a new IP must rebuild the DTU right away —
# set_settings() from the app doesn't fire on_settings.
class Session:
    def __init__(self):
        self.handlers = {}

    def set_handler(self, name, fn):
        self.handlers[name] = fn


class RepairDev(R):
    built = 0

    def get_id(self):
        return "dev"

    async def _build_dtu(self):
        self.built += 1


class Drv(drv.HoymilesDriver):
    def __init__(self):
        pass

    def log(self, *a):
        pass


rd = RepairDev({"ip": "192.168.1.201"}, [])
sess = Session()
asyncio.run(Drv().on_repair(sess, rd))
asyncio.run(sess.handlers["save_repair"]({"ip": "10.10.10.201"}))
assert rd.s["ip"] == "10.10.10.201" and rd.built == 1, (rd.s, rd.built)
print("repair checks OK")


# ── Flow triggers use the Python SDK device-card API ─────────────────────────
# Python SDK: get_device_trigger_card(id).trigger(device, tokens). The app-level
# get_trigger_card(id).trigger() takes no device — the JS-style call raised a
# TypeError that was swallowed, so no trigger ever fired.
class DeviceCard:
    calls = []

    async def trigger(self, device, tokens=None, **kwargs):
        DeviceCard.calls.append((device, tokens))


class AppCard:
    async def trigger(self, tokens=None, **kwargs):
        pass


class Flow:
    def get_device_trigger_card(self, card_id):
        return DeviceCard()

    def get_trigger_card(self, card_id):
        return AppCard()


td = D({}, [])
td.homey = types.SimpleNamespace(flow=Flow())
asyncio.run(td._trigger("solar_production_started", {"power": 120.0}))
assert DeviceCard.calls == [(td, {"power": 120.0})], DeviceCard.calls
print("flow trigger checks OK")
