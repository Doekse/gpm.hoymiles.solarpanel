"""Hoymiles device — polls DTU and updates Homey capabilities."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from astral import LocationInfo
from astral.sun import sun

from app.app import DEBUG_LOG as _DEBUG_LOG
from homey.device import Device
from hoymiles_wifi.dtu import DTU

_LOGGER = logging.getLogger(__name__)

_INFORMATIONAL_WARN_MAX = 255
_BACKOFF_NIGHT = 30 * 60   # seconds
_BACKOFF_DAY   =  5 * 60   # seconds
_NO_DATA_REBUILD_AFTER = 20 * 60  # seconds

# Diagnostic reports submitted from the Homey app only include the last ~100
# lines of stdout. Logging every single poll (every 35-60 s) or every single
# failure of a long outage fills that budget in under 2 hours, pushing the
# actual failure/onset out of the report. Routine/repeat lines are throttled
# to this interval; state transitions (error, recovery, night enter/exit)
# always log immediately regardless.
_HEARTBEAT_INTERVAL_S = 15 * 60

# ── Warning/fault code table ──────────────────────────────────────────────
# warning_number is a single code (not a bitfield, unlike Deye). Source:
# github.com/tbnobody/OpenDTU lib/Hoymiles/src/parser/AlarmLogParser.cpp,
# itself derived from the Hoymiles S-Miles Installer App's warn_code.json —
# not officially published by Hoymiles, but actively maintained by the
# OpenDTU project against real hardware. NOT exhaustive; an unrecognised code
# still shows as "Warning {N}" rather than being silently dropped.
_WARNING_MESSAGES: dict[int, str] = {
    1: "Inverter start",
    2: "Time calibration",
    3: "EEPROM reading and writing error during operation",
    4: "Offline",
    11: "Grid voltage surge",
    12: "Grid voltage sharp drop",
    13: "Grid frequency mutation",
    14: "Grid phase mutation",
    15: "Grid transient fluctuation",
    36: "INV overvoltage or overcurrent",
    46: "FB overvoltage",
    47: "FB overcurrent",
    48: "FB clamp overvoltage",
    49: "FB clamp overvoltage",
    61: "Calibration parameter error",
    62: "System configuration parameter error",
    63: "Abnormal power generation data",
    71: "Grid overvoltage load reduction (VW) function enable",
    72: "Power grid over-frequency load reduction (FW) function enable",
    73: "Over-temperature load reduction (TW) function enable",
    95: "PV-1: Module in suspected shadow",
    96: "PV-2: Module in suspected shadow",
    97: "PV-3: Module in suspected shadow",
    98: "PV-4: Module in suspected shadow",
    121: "Over temperature protection",
    122: "Microinverter is suspected of being stolen",
    123: "Locked by remote control",
    124: "Shut down by remote control",
    125: "Grid configuration parameter error",
    126: "Software error code 126",
    127: "Firmware error",
    128: "Hardware configuration error",
    129: "Abnormal bias",
    130: "Offline",
    141: "Grid overvoltage",
    142: "Grid: 10 min value grid overvoltage",
    143: "Grid undervoltage",
    144: "Grid overfrequency",
    145: "Grid underfrequency",
    146: "Grid: rapid grid frequency change rate",
    147: "Power grid outage",
    148: "Grid disconnection",
    149: "Grid: island detected",
    150: "DCI exceeded",
    152: "Grid: phase angle difference between two phases exceeded 5°, >10 times",
    171: "Grid: abnormal phase difference between phase to phase",
    181: "Abnormal insulation impedance",
    182: "Abnormal grounding",
    205: "MPPT-A: input overvoltage",
    206: "MPPT-B: input overvoltage",
    207: "MPPT-A: input undervoltage",
    208: "MPPT-B: input undervoltage",
    209: "PV-1: no input",
    210: "PV-2: no input",
    211: "PV-3: no input",
    212: "PV-4: no input",
    213: "MPPT-A: PV-1 & PV-2 abnormal wiring",
    214: "MPPT-B: PV-3 & PV-4 abnormal wiring",
    215: "PV-1 / MPPT-C: input overvoltage",
    216: "PV-1 / MPPT-C: input undervoltage",
    217: "PV-2: input overvoltage / PV-5: no input",
    218: "PV-2: input undervoltage / PV-6: no input",
    219: "PV-3: input overvoltage / MPPT-C: PV-5 & PV-6 abnormal wiring",
    220: "PV-3: input undervoltage",
    221: "PV-4: input overvoltage / abnormal wiring of grid neutral line",
    222: "PV-4: input undervoltage",
    301: "FB-A: internal short circuit failure",
    302: "FB-B: internal short circuit failure",
    303: "FB-A: overcurrent protection failure",
    304: "FB-B: overcurrent protection failure",
    305: "FB-A: clamp circuit failure",
    306: "FB-B: clamp circuit failure",
    307: "INV power device failure",
    308: "INV overcurrent or overvoltage protection failure",
    309: "Hardware error code 309",
    310: "Hardware error code 310",
    311: "Hardware error code 311",
    312: "Hardware error code 312",
    313: "Hardware error code 313",
    314: "Hardware error code 314",
    1111: "Repeater",
    5020: "H-bridge MOSFET overcurrent or H-bridge overvoltage",
    5060: "Abnormal bias",
    5070: "Over temperature protection",
    5080: "Grid overvoltage/undervoltage",
    5090: "Grid overfrequency/underfrequency",
    5100: "Island detected",
    5110: "GFDI failure",
    5120: "EEPROM reading and writing error",
    5150: "10 min value grid overvoltage",
    5160: "Grid transient fluctuation",
    5200: "Firmware error",
    5520: "H-bridge MOSFET overcurrent or H-bridge overvoltage",
    8310: "Shut down by remote control",
    8320: "Locked by remote control",
    9000: "Microinverter is suspected of being stolen",
    **{c: "PV-1: MOSFET overcurrent (II)" for c in (5011,)},
    **{c: "PV-2: MOSFET overcurrent (II)" for c in (5012,)},
    **{c: "PV-3: MOSFET overcurrent (II)" for c in (5013,)},
    **{c: "PV-4: MOSFET overcurrent (II)" for c in (5014,)},
    **{2000 + i: "Standby" for i in range(5)},
    **{3001 + i: "Reset" for i in range(4)},
    **{5041 + i: f"PV-{i + 1}: overcurrent (II)" for i in range(4)},
    **{5051 + i: f"PV-{i + 1}: overvoltage/undervoltage" for i in range(4)},
    **{5141 + i: "FB clamp overvoltage" for i in range(4)},
    **{5511 + i: f"PV-{i + 1}: MOSFET overcurrent-H" for i in range(4)},
}


def _decode_warning(code: int) -> str:
    if not code:
        return "OK"
    return _WARNING_MESSAGES.get(code, f"Warning {code}")


class HoymilesDevice(Device):

    _dtu: DTU | None = None
    _poll_task: asyncio.Task | None = None
    _consecutive_errors: int = 0
    _ERROR_THRESHOLD: int = 5
    _backoff_until: float = 0.0
    _first_no_data_at: float | None = None
    _last_poll_ok_at: float | None = None
    _last_poll_ok_power: float | None = None
    _last_poll_ok_daily: float | None = None
    _last_poll_ok_date = None
    _last_poll_ok_warning: int | None = None
    _dtu_rebuilds: int = 0
    _sun_cache: tuple | None = None   # (cache_date, sunrise_utc, sunset_utc)
    _was_producing: bool | None = None  # None = first poll, state not yet known
    _was_night: bool = False          # last poll's night/day state, for transition logging
    _last_heartbeat_at: float = 0.0   # monotonic time of the last routine log line
    _first_error_reason: str | None = None  # captured at the start of the current outage

    def _heartbeat_due(self) -> bool:
        """Throttle routine (non-transition) log lines to _HEARTBEAT_INTERVAL_S apart."""
        now = time.monotonic()
        if now - self._last_heartbeat_at >= _HEARTBEAT_INTERVAL_S:
            self._last_heartbeat_at = now
            return True
        return False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_init(self) -> None:
        self.log(f"Device init — {self.get_setting('ip')}")
        await self._build_dtu()
        await self._refresh_info()
        try:
            # Best-effort — a bad first poll must not block on_init() from
            # completing, or the device would never start its recurring
            # poll loop below.
            await self._poll()
        except Exception as e:
            self.log(f"Initial poll failed (will retry via poll loop): {type(e).__name__}: {e}")
        self._start_polling()

    async def on_settings(self, old_settings, new_settings, changed_keys) -> None:
        if any(k in changed_keys for k in ("ip", "is_encrypted", "enc_rand")):
            await self._build_dtu()
        if any(k in changed_keys for k in (
            "polling_interval", "solar_latitude", "solar_longitude"
        )):
            self._start_polling()

    async def on_deleted(self) -> None:
        self._stop_polling()

    # ── DTU instance ──────────────────────────────────────────────────────────

    def _dtu_config(self) -> tuple[str, bool, dict]:
        ip           = self.get_setting("ip") or ""
        is_encrypted = bool(self.get_setting("is_encrypted"))
        enc_rand_hex = self.get_setting("enc_rand") or ""

        kwargs: dict = {"timeout": 20}
        if is_encrypted and enc_rand_hex:
            try:
                kwargs["is_encrypted"] = True
                kwargs["enc_rand"]     = bytes.fromhex(enc_rand_hex)
            except ValueError:
                # Corrupted setting (manual edit / partial write) — fall back to
                # unencrypted rather than crash on_init()/on_settings() forever.
                self.log(f"Ignoring invalid enc_rand setting: {enc_rand_hex!r}")
                is_encrypted = False
                kwargs.pop("is_encrypted", None)
                kwargs.pop("enc_rand", None)

        return ip, is_encrypted, kwargs

    async def _build_dtu(self) -> None:
        try:
            ip, is_encrypted, kwargs = self._dtu_config()
            self._dtu = DTU(ip, **kwargs)
            self.log(
                f"DTU built — ip:{ip} encrypted:{is_encrypted} "
                f"enc_rand:{'yes' if kwargs.get('enc_rand') else 'no'}"
            )
        except Exception as e:
            self._dtu = None
            self.log(f"_build_dtu failed: {type(e).__name__}: {e}")

    async def _probe_dtu_info(self, reason: str) -> None:
        """Log a fresh DTU app_info probe for diagnostics."""
        ip, is_encrypted, kwargs = self._dtu_config()
        try:
            probe = DTU(ip, **kwargs)
            app_info = await asyncio.wait_for(
                probe.async_app_information_data(), timeout=10
            )
            if not app_info:
                self.log(f"DTU probe ({reason}): no app_info")
                return

            dtu_info = app_info.dtu_info
            sw = ""
            signal = None
            dfs = None
            if dtu_info:
                sw = dtu_info.wifi_version or dtu_info.dtu_sw_version or ""
                signal = dtu_info.signal_strength
                dfs = dtu_info.dfs

            self.log(
                f"DTU probe ({reason}): ok ip={ip} encrypted={is_encrypted} "
                f"sn={app_info.dtu_serial_number or '—'} "
                f"panels={app_info.pv_number or 0} "
                f"sw={sw or '—'} signal={signal if signal is not None else '—'} "
                f"dfs={dfs if dfs is not None else '—'}"
            )
        except Exception as e:
            self.log(f"DTU probe ({reason}) failed: {type(e).__name__}: {e}")

    async def _refresh_enc_rand_from_app_info(self, reason: str) -> bool:
        """Refresh encrypted DTU metadata from app_info.

        DTU-Pro-S / WLite-S devices expose enc_rand through app_information_data().
        Re-pairing works because it reads this metadata before polling real data.
        During recovery, mirror that behaviour so an installed device can repair
        stale encryption settings without removing/re-adding it.
        """
        ip, is_encrypted, kwargs = self._dtu_config()
        attempts = [("configured", kwargs)]
        if is_encrypted:
            attempts.append(("plain", {"timeout": kwargs.get("timeout", 20)}))

        for label, attempt_kwargs in attempts:
            try:
                probe = DTU(ip, **attempt_kwargs)
                app_info = await asyncio.wait_for(
                    probe.async_app_information_data(), timeout=10
                )
            except Exception as e:
                self.log(
                    f"enc_rand refresh ({reason}, {label}) failed: "
                    f"{type(e).__name__}: {e}"
                )
                continue

            if not app_info or not app_info.dtu_info:
                self.log(f"enc_rand refresh ({reason}, {label}): no app_info")
                continue

            di = app_info.dtu_info
            updates = {
                "signal_strength": str(di.signal_strength or ""),
                "sw_version":      str(di.wifi_version   or di.dtu_sw_version or ""),
                "hw_version":      str(di.dtu_hw_version or ""),
            }

            new_enc_rand = di.enc_rand.hex() if di.enc_rand else ""
            old_enc_rand = self.get_setting("enc_rand") or ""
            if new_enc_rand:
                updates["is_encrypted"] = True
                updates["enc_rand"] = new_enc_rand

            try:
                await self.set_settings(updates)
            except Exception as e:
                self.log(
                    f"enc_rand refresh ({reason}, {label}): settings save failed: "
                    f"{type(e).__name__}: {e}"
                )
                return False

            if new_enc_rand and new_enc_rand != old_enc_rand:
                self.log(
                    f"enc_rand refresh ({reason}, {label}): updated "
                    f"{old_enc_rand[:8] or 'empty'}… → {new_enc_rand[:8]}…"
                )
                return True

            if new_enc_rand:
                self.log(
                    f"enc_rand refresh ({reason}, {label}): unchanged "
                    f"{new_enc_rand[:8]}…"
                )
            else:
                self.log(f"enc_rand refresh ({reason}, {label}): no enc_rand")
            return False

        return False

    # ── One-time startup info ─────────────────────────────────────────────────

    async def _refresh_info(self) -> None:
        if not self._dtu:
            return
        try:
            app_info = await self._dtu.async_app_information_data()
            if app_info:
                pv_num = int(app_info.pv_number or 0)
                if pv_num > 0 and int(self.get_setting("panel_count") or 0) != pv_num:
                    await self.set_settings({"panel_count": pv_num})
                if app_info.dtu_info:
                    di = app_info.dtu_info
                    if di.enc_rand:
                        new_hex = di.enc_rand.hex()
                        if new_hex != (self.get_setting("enc_rand") or ""):
                            await self.set_settings({"enc_rand": new_hex})
                            self._dtu.enc_rand = di.enc_rand
                    await self.set_settings({
                        "signal_strength": str(di.signal_strength or ""),
                        "sw_version":      str(di.wifi_version   or di.dtu_sw_version or ""),
                        "hw_version":      str(di.dtu_hw_version or ""),
                    })

            config = await self._dtu.async_get_config()
            if config:
                await self.set_settings({
                    "wifi_ssid":   config.wifi_ssid    or "",
                    "dtu_ap_ssid": config.dtu_ap_ssid or "",
                })
        except Exception as e:
            self.log(f"_refresh_info error: {e}")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _start_polling(self) -> None:
        self._stop_polling()
        interval = max(35, int(self.get_setting("polling_interval") or 60))
        self._poll_task = asyncio.ensure_future(self._poll_loop(interval))

    def _stop_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    async def _poll_loop(self, interval: int) -> None:
        self.log(f"Polling started — every {interval}s")
        while True:
            remaining = self._backoff_until - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, interval))
                continue
            try:
                await self._poll()
            except Exception as e:
                self.log(f"Poll loop error: {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    async def _poll(self) -> None:
        if not self._dtu:
            return
        if bool(self.get_setting("is_hybrid")):
            await self._poll_hybrid()
        else:
            await self._poll_standard()

    # ── Standard polling (sgs / tgs / pv) ────────────────────────────────────

    async def _poll_standard(self) -> None:
        sun_times = self._get_sunrise_sunset()
        if sun_times is not None and self._is_night_time_from(sun_times):
            self._first_no_data_at = None
            await self._apply_night_backoff(sun_times)
            return

        try:
            data = await self._dtu.async_get_real_data_new()
        except Exception as e:
            sun_times = self._get_sunrise_sunset()
            if sun_times is not None and self._is_night_time_from(sun_times):
                self._first_no_data_at = None
                await self._apply_night_backoff(sun_times, e)
            else:
                self._first_no_data_at = None
                reason = f"{type(e).__name__}: {e}"
                self._consecutive_errors += 1
                if self._consecutive_errors == 1:
                    self._first_error_reason = reason
                # Log immediately on the first failure and exactly when it crosses the
                # unavailable threshold; otherwise throttle so a long outage doesn't
                # burn through the ~100-line diagnostic report with identical lines.
                if (self._consecutive_errors == 1
                        or self._consecutive_errors == self._ERROR_THRESHOLD
                        or self._heartbeat_due()):
                    self.log(f"Poll error ({self._consecutive_errors}): {reason} "
                             f"| first={self._first_error_reason}")
                if self._consecutive_errors >= self._ERROR_THRESHOLD:
                    await self.set_unavailable(str(e))
            return

        if not data:
            now = time.monotonic()
            just_started = self._first_no_data_at is None
            if just_started:
                self._first_no_data_at = now
            no_data_for = now - self._first_no_data_at
            last_ok = (
                "never" if self._last_poll_ok_at is None
                else f"{(now - self._last_poll_ok_at) / 60:.0f} min ago"
            )
            # Log immediately when the "no data" streak starts, then throttle —
            # this repeats every poll (35-60 s) while the inverter sleeps, which
            # would otherwise fill the diagnostic report's ~100-line budget fast.
            if just_started or self._heartbeat_due():
                self.log(
                    f"Poll: no data — inverter sleeping, keeping last values "
                    f"({no_data_for / 60:.0f} min, last_ok={last_ok}, "
                    f"last_warn={self._last_poll_ok_warning}, "
                    f"rebuilds={self._dtu_rebuilds})"
                )
            if no_data_for >= _NO_DATA_REBUILD_AFTER:
                self.log(
                    f"Poll: no data persisted — rebuilding DTU connection "
                    f"(last_power={self._last_poll_ok_power}W "
                    f"last_daily={self._last_poll_ok_daily}kWh "
                    f"last_warn={self._last_poll_ok_warning})"
                )
                await self._probe_dtu_info("before rebuild")
                enc_refreshed = await self._refresh_enc_rand_from_app_info(
                    "no data recovery"
                )
                if enc_refreshed:
                    self.log("Poll: DTU encryption metadata refreshed")
                self._dtu_rebuilds += 1
                await self._build_dtu()
                self._first_no_data_at = None
            return

        # A transition (day resumed / recovered from failures) always forces the
        # upcoming heartbeat line through immediately, bypassing the throttle,
        # so the report shows fresh values right when something changed.
        force_heartbeat = self._was_night or self._consecutive_errors > 0
        if self._was_night:
            self.log("day resumed — polling normally")
            self._was_night = False
        if self._consecutive_errors > 0:
            self.log(f"poll recovered after {self._consecutive_errors} failed attempt(s) "
                     f"| first failure was: {self._first_error_reason}")
            self._first_error_reason = None
        self._first_no_data_at = None
        self._consecutive_errors = 0
        await self.set_available()
        panel_count = int(self.get_setting("panel_count") or 0)
        three_phase = bool(self.get_setting("three_phase"))

        pv_list = data.pv_data or []
        # Some DTUs (e.g. DTU-WLite-S with HMS-1600-4T) report dtu_power and
        # dtu_daily_energy as 0 even when individual PV data is correct.
        # Fall back to summing per-PV values so the main card always shows data.
        dtu_w     = (data.dtu_power       or 0) / 10
        dtu_today = (data.dtu_daily_energy or 0) / 1000
        pv_w      = sum((pv.power         or 0) for pv in pv_list) / 10
        pv_today  = sum((pv.energy_daily  or 0) for pv in pv_list) / 1000
        power_w   = dtu_w     if dtu_w     > 0 else pv_w
        today_raw = dtu_today if dtu_today > 0 else pv_today
        today_date = datetime.now(timezone.utc).date()
        today_kwh = today_raw
        if (
            today_raw == 0
            and power_w <= 5
            and self._last_poll_ok_daily
            and self._last_poll_ok_daily > 0
            and self._last_poll_ok_date == today_date
        ):
            today_kwh = self._last_poll_ok_daily
            self.log(
                f"Daily energy reported zero at low power — keeping last value "
                f"{today_kwh:.2f}kWh for today"
            )
        warn_raw = self._warning_number(data)
        self._last_poll_ok_at = time.monotonic()
        self._last_poll_ok_power = round(power_w, 1)
        self._last_poll_ok_daily = round(today_kwh, 3)
        self._last_poll_ok_date = today_date
        self._last_poll_ok_warning = warn_raw
        await self._set("measure_power",     power_w)
        await self._set("meter_power.today", today_kwh)

        total_wh = sum(pv.energy_total or 0 for pv in pv_list)
        await self._set("meter_power", total_wh / 1000)

        # Each block below is independently guarded: an unexpected field on one
        # DTU response (e.g. an attribute missing after a library update) must
        # not skip the remaining capability updates or the flow triggers.
        try:
            if three_phase:
                await self._poll_three_phase(data)
            else:
                await self._poll_single_phase(data)
        except Exception as e:
            _LOGGER.debug(f"Phase capability update failed: {e}")

        try:
            for idx, pv in enumerate((data.pv_data or []), start=1):
                if idx > panel_count:
                    break
                sfx = f"pv{idx}"
                await self._set(f"measure_power.{sfx}",   (pv.power   or 0) / 10)
                await self._set(f"measure_voltage.{sfx}", (pv.voltage or 0) / 10)
                await self._set(f"measure_current.{sfx}", (pv.current or 0) / 100)
        except Exception as e:
            _LOGGER.debug(f"Per-panel capability update failed: {e}")

        # ── Flow triggers ──────────────────────────────────────────────────────
        try:
            await self._fire_flow_triggers(power_w, today_kwh)
        except Exception as e:
            _LOGGER.debug(f"Flow trigger evaluation failed: {e}")

        # Throttled to _HEARTBEAT_INTERVAL_S for the same reason as the other
        # routine log lines above — force_heartbeat bypasses it right after a
        # transition so the report shows it happened.
        if force_heartbeat or self._heartbeat_due():
            self.log(
                f"poll ok | solar={power_w:.0f}W daily={today_kwh:.2f}kWh "
                f"raw_daily={today_raw:.2f}kWh warn={warn_raw} "
                f"failures={self._consecutive_errors}"
            )

    def _warning_number(self, data) -> int:
        if data.tgs_data:
            return (data.tgs_data[0].warning_number or 0)
        if data.sgs_data:
            return (data.sgs_data[0].warning_number or 0)
        return 0

    async def _poll_single_phase(self, data) -> None:
        sgs = (data.sgs_data or [None])[0]
        if not sgs:
            return
        warn_raw = sgs.warning_number or 0
        await self._set("measure_voltage",     (sgs.voltage   or 0) / 10)
        await self._set("measure_current",     (sgs.current   or 0) / 100)
        await self._set("measure_frequency",   (sgs.frequency or 0) / 100)
        await self._set("measure_temperature", self._decode_temp(sgs.temperature or 0))
        await self._set("alarm_generic",       warn_raw > _INFORMATIONAL_WARN_MAX)
        await self._set("fault_description",   _decode_warning(warn_raw))

    async def _poll_three_phase(self, data) -> None:
        tgs = (data.tgs_data or [None])[0]
        if not tgs:
            return
        warn_raw = tgs.warning_number or 0
        await self._set("measure_voltage.phase_a", (tgs.voltage_phase_A or 0) / 10)
        await self._set("measure_voltage.phase_b", (tgs.voltage_phase_B or 0) / 10)
        await self._set("measure_voltage.phase_c", (tgs.voltage_phase_C or 0) / 10)
        await self._set("measure_current.phase_a", (tgs.current_phase_A or 0) / 100)
        await self._set("measure_current.phase_b", (tgs.current_phase_B or 0) / 100)
        await self._set("measure_current.phase_c", (tgs.current_phase_C or 0) / 100)
        await self._set("measure_frequency",       (tgs.frequency       or 0) / 100)
        await self._set("measure_temperature",     self._decode_temp(tgs.temperature or 0))
        await self._set("alarm_generic",           warn_raw > _INFORMATIONAL_WARN_MAX)
        await self._set("fault_description",       _decode_warning(warn_raw))

    # ── Hybrid polling (energy storage) ──────────────────────────────────────

    async def _poll_hybrid(self) -> None:
        dtu_sn_str   = self.get_setting("dtu_sn") or ""
        inverter_sns = json.loads(self.get_setting("inverter_sns") or "[]")

        if not dtu_sn_str or not inverter_sns:
            # Throttled — a persistent misconfiguration would otherwise repeat this
            # line on every poll (35-60 s) and fill the diagnostic report with it.
            if self._heartbeat_due():
                self.log("Hybrid poll: missing dtu_sn or inverter_sns")
            return

        try:
            dtu_sn_int = int(dtu_sn_str)
        except ValueError:
            self.log(f"Hybrid poll: invalid dtu_sn '{dtu_sn_str}'")
            return

        total_power = 0.0
        total_soc   = None
        any_alarm   = False
        responses   = 0

        for inv_sn in inverter_sns:
            try:
                storage = await self._dtu.async_get_energy_storage_data(
                    dtu_serial_number=dtu_sn_int,
                    inverter_serial_number=int(inv_sn),
                )
            except Exception as e:
                # Throttled — a down inverter would otherwise repeat this every poll
                # and fill the diagnostic report with identical lines.
                if self._consecutive_errors == 0 or self._heartbeat_due():
                    self.log(f"Hybrid poll error ({inv_sn}): {type(e).__name__}: {e}")
                continue

            if not storage:
                continue

            responses += 1

            if storage.production:
                total_power += (storage.production.active_power or 0) / 10

            if storage.battery_management:
                soc = storage.battery_management.state_of_charge
                if soc is not None:
                    total_soc = soc if total_soc is None else (total_soc + soc) // 2

            if storage.warning_number and storage.warning_number > _INFORMATIONAL_WARN_MAX:
                any_alarm = True

        if responses == 0:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._ERROR_THRESHOLD:
                await self.set_unavailable("No response from hybrid inverter")
            return

        self._consecutive_errors = 0
        await self.set_available()
        await self._set("measure_power", total_power)
        if total_soc is not None:
            await self._set("measure_battery", total_soc)
        await self._set("alarm_generic", any_alarm)

    # ── Night-time / solar window ─────────────────────────────────────────────

    def _get_sunrise_sunset(self) -> tuple[float, float] | None:
        """Returns (sunrise_utc, sunset_utc) as decimal UTC hours using astral.

        Result is cached per calendar day — computed and logged only once per day.
        Everything is in UTC — no timezone conversion needed.
        Priority: manual solar_latitude/longitude → Homey geolocation."""
        try:
            today = datetime.now(timezone.utc).date()
            if self._sun_cache and self._sun_cache[0] == today:
                return (self._sun_cache[1], self._sun_cache[2])

            lat = self._get_float_setting("solar_latitude")
            lng = self._get_float_setting("solar_longitude")

            from_geolocation = False
            if lat is None or lat == 0.0:
                lat = self.homey.geolocation.get_latitude()
                from_geolocation = True
            if lng is None or lng == 0.0:
                lng = self.homey.geolocation.get_longitude()
                from_geolocation = True

            if lat is None or lng is None or (lat == 0.0 and lng == 0.0):
                self.log("Night backoff disabled — location not available (Homey location not configured?)")
                return None

            # Back-fill settings so the user can see which coordinates are in use.
            # Only written once per day (sun_cache miss), only when fields are still
            # at default 0 values, and only when geolocation returned a real fix.
            if from_geolocation and (lat != 0.0 or lng != 0.0):
                cur_lat = self._get_float_setting("solar_latitude")
                cur_lng = self._get_float_setting("solar_longitude")
                if (cur_lat is None or cur_lat == 0.0) and (cur_lng is None or cur_lng == 0.0):
                    async def _save_location(la: float, lo: float) -> None:
                        try:
                            await self.set_settings({
                                "solar_latitude":  round(la, 6),
                                "solar_longitude": round(lo, 6),
                            })
                        except Exception as exc:
                            _LOGGER.debug(f"Back-fill location settings failed: {exc}")
                    asyncio.create_task(_save_location(lat, lng))

            loc = LocationInfo(latitude=lat, longitude=lng)
            s   = sun(loc.observer, date=today, tzinfo=timezone.utc)
            sr  = s["sunrise"].hour + s["sunrise"].minute / 60
            ss  = s["sunset"].hour  + s["sunset"].minute  / 60
            self._sun_cache = (today, sr, ss)
            self.log(f"Sun times (UTC): sunrise={sr:.2f}h sunset={ss:.2f}h lat={lat:.4f} lng={lng:.4f}")
            return (sr, ss)
        except Exception as e:
            self.log(f"Night backoff disabled — sun calculation failed: {e}")
            return None

    def _is_night_time(self) -> bool:
        """True when outside solar window. Convenience wrapper."""
        return self._is_night_time_from(self._get_sunrise_sunset())

    def _is_night_time_from(self, sun_times: tuple[float, float] | None) -> bool:
        """True when outside solar window with a 30-minute buffer on each side.

        Compares UTC current time against UTC sunrise/sunset. For locations west
        of the UTC meridian (e.g. UTC-6), the sunset returned by astral can be on
        the next calendar day in UTC (sunset < sunrise). In that case the solar day
        wraps past midnight UTC and the night window is the inverse interval.
        """
        if sun_times is None:
            return False
        sunrise, sunset = sun_times
        try:
            now      = datetime.now(timezone.utc)
            utc_hour = now.hour + now.minute / 60
            start = sunrise - 0.5
            end   = sunset  + 0.5

            if start < end:
                # Solar window falls within the same UTC day.
                return utc_hour < start or utc_hour >= end
            # Solar window crosses midnight UTC.
            return end <= utc_hour < start
        except Exception as e:
            self.log(f"Night time check failed ({e}) — assuming daytime")
            return False

    async def _apply_night_backoff(
        self,
        sun_times: tuple[float, float],
        reason: Exception | None = None,
    ) -> None:
        """Zero instantaneous values, mark device available, and sleep 30 min."""
        self._consecutive_errors = 0
        await self._apply_zeros()
        await self.set_available()
        self._backoff_until = time.monotonic() + _BACKOFF_NIGHT
        sunrise, sunset = sun_times
        suffix = f" | {reason}" if reason else ""
        # Log immediately on entering night (transition) or every _HEARTBEAT_INTERVAL_S
        # while it persists — every single 30-min backoff cycle would burn through the
        # ~100-line diagnostic report window in under 2 days of normal operation.
        if not self._was_night or self._heartbeat_due():
            self.log(
                f"poll: night — backing off 30 min | "
                f"sunrise≈{sunrise:.2f}h sunset≈{sunset:.2f}h (UTC) "
                f"last_power={self._last_poll_ok_power}W "
                f"last_daily={self._last_poll_ok_daily}kWh "
                f"last_warn={self._last_poll_ok_warning}{suffix}"
            )
        self._was_night = True

    # ── Apply zeros ───────────────────────────────────────────────────────────

    async def _apply_zeros(self) -> None:
        """Zero out instantaneous capabilities. Preserves meter_power (lifetime)."""
        panel_count = int(self.get_setting("panel_count") or 0)
        three_phase = bool(self.get_setting("three_phase"))

        await self._set("measure_power",     0)
        await self._set("alarm_generic",     False)
        await self._set("measure_frequency", 0)
        await self._set("measure_temperature", 0)

        if three_phase:
            for ph in ("a", "b", "c"):
                await self._set(f"measure_voltage.phase_{ph}", 0)
                await self._set(f"measure_current.phase_{ph}", 0)
        else:
            await self._set("measure_voltage", 0)
            await self._set("measure_current", 0)

        for i in range(1, panel_count + 1):
            sfx = f"pv{i}"
            await self._set(f"measure_power.{sfx}",   0)
            await self._set(f"measure_voltage.{sfx}", 0)
            await self._set(f"measure_current.{sfx}", 0)

    # ── Flow triggers ─────────────────────────────────────────────────────────

    async def _fire_flow_triggers(self, power_w: float, today_kwh: float) -> None:
        """Fire flow triggers based on state transitions detected in poll values."""
        is_producing = power_w > 5.0

        if self._was_producing is None:
            # First poll after startup — record state silently, no transition to fire.
            self._was_producing = is_producing
        elif is_producing and not self._was_producing:
            self._was_producing = True
            self.log(f"production started — {power_w:.0f}W")
            await self._trigger("solar_production_started", {"power": power_w})
        elif not is_producing and self._was_producing:
            self._was_producing = False
            self.log(f"production stopped — daily {today_kwh:.2f}kWh")
            await self._trigger("solar_production_stopped", {})
            await self._trigger("daily_data_updated", {"daily_production": today_kwh})

        await self._trigger("data_updated", {"power": power_w, "daily_production": today_kwh})

    async def _trigger(self, card_id: str, tokens: dict) -> None:
        """Fire a flow trigger card."""
        try:
            card = self.homey.flow.get_trigger_card(card_id)
            await card.trigger(self, tokens, {})
        except Exception as e:
            _LOGGER.debug(f"Flow trigger '{card_id}' failed: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_float_setting(self, key: str) -> float | None:
        """Return a device setting as float, or None if absent/invalid."""
        raw = self.get_setting(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            self.log(f"Ignoring invalid numeric setting {key}={raw!r}")
            return None

    async def _set(self, cap: str, value) -> None:
        try:
            await self.set_capability_value(cap, value)
        except Exception:
            pass

    @staticmethod
    def _decode_temp(raw: int) -> float:
        if raw > 32767:
            raw -= 65536
        return raw * 0.1


homey_export = HoymilesDevice
