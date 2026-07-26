# TODO

- [ ] **Widget PV Overview** — create a per-device widget showing current power, daily/total production, dynamic PV1-PV4 comparison, and SOC when available. Show only existing capabilities, support all 10 locales, dark/light mode, and pause updates when off-screen. Update the changelog only after the widget is implemented and validated on Homey.

## gpm.hoymiles.solarpanel backlog

### Power ratio per device

- Add a `nominal_power` (W) setting in `driver.compose.json`.
- Add a `measure_power_ratio` (%) capability calculated on each poll: `(solar / nominal_power) * 100`.
- Useful for flows: e.g. "if production < 10% of nominal capacity → action".

### DTU rebuild only on exception, not on no-data

- The current 20-minute no-data rebuild can fire before sunrise unnecessarily.
- Rebuild should be triggered only on actual exceptions, not when the DTU simply returns `None` (inverter sleeping).

### Night backoff ends before sunrise

- There is a ~20-minute window before sunrise where the app still polls and logs noise.
- Small issue, but generates unnecessary log traffic.

### Warning number logging and `test_dtu.py`

- [ ] Add `async_get_alarm_list()` to `test_dtu.py`.
- [ ] Document known warning codes from OpenDTU / S-Miles Installer `warn_code.json`:

  | Code | Description |
  |------|-------------|
  | 11   | Grid voltage surge |
  | 12   | Grid voltage sharp drop |
  | 13   | Grid frequency mutation |
  | 14   | Grid phase mutation |
  | 15   | Grid transient fluctuation |
  | 36   | INV overvoltage or overcurrent |
  | 46   | FB overvoltage |
  | 47   | FB overcurrent |
  | 71   | Grid overvoltage load reduction (VW) |
  | 72   | Grid over-frequency load reduction (FW) |
  | 73   | Over-temperature load reduction (TW) |
