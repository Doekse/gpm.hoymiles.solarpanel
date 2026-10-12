# TODO

## Open

### Widget PV Overview

- Per-device widget: current power, daily/total production, PV1–PVn comparison, SOC when available.
- Show only existing capabilities, all 10 locales, dark/light mode, pause updates when off-screen.
- Update the changelog only after it is validated on Homey.
- The Deye "Energy Summary" widget (`gpm.deye.solarpanel.tcp/widgets/energy-summary`) is not a fit yet:
  without grid/load data it would only show solar power. Revisit after meter support (below).

### Energy meter support (`meter_data`)

- DTU-Pro with a Hoymiles meter (e.g. DTSU666) reports grid power per phase/total, import/export energy,
  voltage, current and power factor. The HA hoymiles_wifi integration already reads these (read-only).
- Would give grid import/export in Homey Energy and make an energy-flow widget worthwhile.
- Needs a user with a meter to test.

### Power ratio per device

- Add a `nominal_power` (W) setting in `driver.compose.json`.
- Add a `measure_power_ratio` (%) capability calculated on each poll: `(solar / nominal_power) * 100`.
- Useful for flows: e.g. "if production < 10% of nominal capacity → action".

### `test_dtu.py`

- Add `async_get_alarm_list()` to the diagnostic script.

## Decisions (don't redo)

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

- Fault/warning flow cards: trigger `fault_detected`, conditions `has_active_fault` and `fault_is` (`9927fcb`).
- Warning code table: official Hoymiles table, 264 codes, in `lib/fault_codes.py`.
