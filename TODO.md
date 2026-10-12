# TODO

## Open

### Energy meter support (`meter_data`)

- DTU-Pro with a Hoymiles meter (e.g. DTSU666) reports grid power per phase/total, import/export energy,
  voltage, current and power factor. The HA hoymiles_wifi integration already reads these (read-only).
- Would give grid import/export in Homey Energy.
- Needs a user with a meter to test.

### `test_dtu.py`

- Add `async_get_alarm_list()` to the diagnostic script.

## Decisions (don't redo)

- **No widget (2026-10-12)** — standard DTUs only have solar data, so a summary widget would repeat the
  device tile. The Deye "Energy Summary" widget was considered and left out.
- **Read-only by design** — no power limit / curtailment. Users who need to change inverter settings use the
  official S-Miles app.
- **"Lifetime Energy" title kept as-is (2026-08-24)** — SolarEdge's official HA wording; naming varies by
  vendor. If revisited: change the "en" string in `driver.py` and re-apply the title in on_init so paired
  devices pick it up.
- **No-data DTU rebuild stays** — the 20-min no-data rebuild also refreshes `enc_rand`, which is what recovers
  DTU WLite-S / Pro-S after the night (the old "restart the app every morning" problem). Rebuilding only on
  exceptions would bring that back.
- **Pre-sunrise polling** — the 30-min buffer before sunrise is intentional; routine log lines are throttled
  (15-min heartbeat), so the noise is already handled.

## Done

- Power ratio: `nominal_power` setting + `measure_power_ratio` (%) capability, added/removed with the setting.
- Fault/warning flow cards: trigger `fault_detected`, conditions `has_active_fault` and `fault_is` (`9927fcb`).
- Warning code table: official Hoymiles table, 264 codes, in `lib/fault_codes.py`.
