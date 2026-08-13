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

### Flow cards for fault/warning codes

- The Deye app (`gpm.python.deye`) just got 3 flow cards built on top of its
  equivalent fault table — same pattern makes sense here once someone has
  time. Reference implementation there: `app.py` (`_register_flow_conditions`),
  `drivers/deye/device.py` (`_fire_flow_triggers`), `lib/fault_codes.py`
  (`known_fault_names`), and the 3 JSON files under
  `.homeycompose/flow/{triggers,conditions}/`.
  - **Trigger "Fault/warning detected"** — edge-triggered (fires once on the
    OK → fault transition, not every poll), `description` token from
    `decode_warning()`.
  - **Condition "Fault/warning is active"** — simple boolean, `fault_description != "OK"`.
  - **Condition "Warning is ..."** — `type: autocomplete` arg, listener
    registered with `register_argument_autocomplete_listener()`. List
    suggestions from `_WARNING_MESSAGES` (now in `lib/fault_codes.py`),
    filtered by the already-selected `device` arg's model — this driver has
    a "Hybrid inverter (battery storage)" model option
    (`driver.compose.json:173`), so battery-only codes (70-78: charge/
    discharge/voltage/temp/BMS alarms) should be hidden from the suggestion
    list for non-hybrid/non-battery devices, same reasoning as the Deye side.
  - Note: couldn't verify the exact Python callback signature for
    `register_argument_autocomplete_listener()` against this project's
    actual `homey` runtime binding (not inspectable locally, only found via
    the official — JS-oriented — docs at
    https://apps.developer.homey.app/the-basics/flow/arguments). The Deye
    implementation is marked experimental in its changelog for the same
    reason — test on a real device before assuming it works as coded.

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
