# Sources

Not packaged into the Homey app build (see `.homeyignore`) — reference material for maintainers.

## Fault/warning code table (`lib/fault_codes.py`)

`_WARNING_MESSAGES` — `warning_number` is a single code (not a bitfield, unlike the Deye app).

**Primary source**: the official Hoymiles "S-Miles Enduser" iOS app (`hemaiEnduser.app`, `en/de/es/fr/it/nl/pl.lproj/Localizable.strings`, keys `"1007_<code>"`). Not exhaustive on its own — gaps and the whole 5000+/8000+/9000 range are filled in (English only) from OpenDTU's [AlarmLogParser.cpp](https://github.com/tbnobody/OpenDTU) (community-sourced, derived from an older version of the same Hoymiles app).

**Correction (2026-08-12)**: codes 205-218 originally described battery/BMS conditions inherited from the primary source, which don't apply to the battery-less HMS microinverter family this app targets. Corrected against two official Hoymiles user manuals, cross-checked against each other:

- HMS-800/900/1000-2T (dual-MPPT): [user-manual_hms-8009001000-2t_global_en_v202302.pdf](https://energypower.gr/wp-content/uploads/2023/12/user-manual_hms-8009001000-2t_global_en_v202302.pdf) (hosted copy of the official Hoymiles manual)
- HMS-400/450/500-1T (single-MPPT): [Hoymiles_User-manual_HMS-350-400-450-500-1T_NA.pdf](https://www.solar-electric.com/lib/wind-sun/Hoymiles_User-manual_HMS-350-400-450-500-1T_NA.pdf) (hosted copy of the official Hoymiles manual)

Both manuals' "Troubleshooting List" (section 6.1) independently confirm the same MPPT-port over/under-voltage and no-input/wiring meanings at codes 205-218 (the single-MPPT manual only covers the subset applicable to its one PV port). Codes 202-204 and 219-220 remain unconfirmed either way — not covered by either manual.

**Compatibility check**: the `hoymiles-wifi` library (this app's dependency) lists its tested devices at [github.com/suaveolent/hoymiles-wifi](https://github.com/suaveolent/hoymiles-wifi) — HMS-400W-1T and HMS-800W-2T/HMS-1000W-2T/HMS-2000DW-4T are explicitly listed, which is why those two manuals (not the several other Hoymiles manuals gathered during this investigation — HYS hybrid inverters, HMS-300W-1T, HMS-500-1A, MIT three-phase micros) were the ones actually cross-checked: they're the only ones matching hardware this app can talk to.

Not exhaustive — unknown codes fall back to `f"Warning {code}"` rather than a guessed name.
