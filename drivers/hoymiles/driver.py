"""Hoymiles universal driver — auto-detects panel count, three-phase, hybrid and encryption."""

import asyncio
import json
import logging
import socket

from homey.driver import Driver
from hoymiles_wifi.dtu import DTU
from hoymiles_wifi.const import IS_ENCRYPTED_BIT_INDEX

_LOGGER = logging.getLogger(__name__)

_IS_ENCRYPTED_BIT = IS_ENCRYPTED_BIT_INDEX
_DTU_PORT = 10081

# Capability icon SVG paths (relative to app root — confirmed working via the
# Deye app's identical convention, distinct from the per-device pairing icon
# path, which is relative to the driver's own assets folder instead).
_ASSETS = "/drivers/hoymiles/assets"

_CAPABILITY_TITLES: dict[str, dict[str, str]] = {
    "Current Power": {
        "en": "Current Power", "nl": "Huidig Vermogen", "de": "Aktuelle Leistung",
        "fr": "Puissance Actuelle", "it": "Potenza Attuale", "sv": "Aktuell Effekt",
        "no": "Aktuell Effekt", "es": "Potencia Actual", "da": "Aktuel Effekt", "pl": "Aktualna Moc",
    },
    "Daily Energy": {
        "en": "Daily Energy", "nl": "Dagelijkse Energie", "de": "Tagesenergie",
        "fr": "Énergie Journalière", "it": "Energia Giornaliera", "sv": "Daglig Energi",
        "no": "Daglig Energi", "es": "Energía Diaria", "da": "Daglig Energi", "pl": "Dzienna Energia",
    },
    "Lifetime Energy": {
        "en": "Lifetime Energy", "nl": "Totale Energie", "de": "Gesamtenergie",
        "fr": "Énergie Totale", "it": "Energia Totale", "sv": "Total Energi",
        "no": "Total Energi", "es": "Energía Total", "da": "Total Energi", "pl": "Łączna Energia",
    },
    "Inverter Alarm": {
        "en": "Inverter Alarm", "nl": "Omvormer Alarm", "de": "Wechselrichter-Alarm",
        "fr": "Alarme Onduleur", "it": "Allarme Inverter", "sv": "Växelriktarlarm",
        "no": "Vekselretter-Alarm", "es": "Alarma Inversor", "da": "Vekselretter-Alarm",
        "pl": "Alarm Falownika",
    },
    "Battery State of Charge": {
        "en": "Battery State of Charge", "nl": "Accustand", "de": "Batterieladezustand",
        "fr": "État de Charge de la Batterie", "it": "Stato di Carica della Batteria",
        "sv": "Batteriladdningsstatus", "no": "Batteriladestatus",
        "es": "Estado de Carga de la Batería", "da": "Batteriopladningsstatus",
        "pl": "Stan Naładowania Baterii",
    },
    "Grid Frequency": {
        "en": "Grid Frequency", "nl": "Netfrequentie", "de": "Netzfrequenz",
        "fr": "Fréquence du Réseau", "it": "Frequenza di Rete", "sv": "Nätfrekvens",
        "no": "Nettfrekvens", "es": "Frecuencia de Red", "da": "Netfrekvens",
        "pl": "Częstotliwość Sieci",
    },
    "Inverter Temperature": {
        "en": "Inverter Temperature", "nl": "Omvormer Temperatuur", "de": "Wechselrichter-Temperatur",
        "fr": "Température de l'Onduleur", "it": "Temperatura Inverter",
        "sv": "Växelriktartemperatur", "no": "Vekselretter-Temperatur",
        "es": "Temperatura del Inversor", "da": "Vekselretter-Temperatur",
        "pl": "Temperatura Falownika",
    },
    "Voltage Phase": {
        "en": "Voltage Phase {label}", "nl": "Spanning Fase {label}",
        "de": "Spannung Phase {label}", "fr": "Tension Phase {label}",
        "it": "Tensione Fase {label}", "sv": "Spänning Fas {label}",
        "no": "Spenning Fase {label}", "es": "Tensión Fase {label}",
        "da": "Spænding Fase {label}", "pl": "Napięcie Faza {label}",
    },
    "Current Phase": {
        "en": "Current Phase {label}", "nl": "Stroom Fase {label}",
        "de": "Strom Phase {label}", "fr": "Courant Phase {label}",
        "it": "Corrente Fase {label}", "sv": "Ström Fas {label}",
        "no": "Strøm Fase {label}", "es": "Corriente Fase {label}",
        "da": "Strøm Fase {label}", "pl": "Prąd Faza {label}",
    },
    "AC Voltage": {
        "en": "AC Voltage", "nl": "AC-Spanning", "de": "AC-Spannung",
        "fr": "Tension AC", "it": "Tensione CA", "sv": "AC-Spänning",
        "no": "AC-Spenning", "es": "Tensión CA", "da": "AC-Spænding", "pl": "Napięcie AC",
    },
    "AC Current": {
        "en": "AC Current", "nl": "AC-Stroom", "de": "AC-Strom",
        "fr": "Courant AC", "it": "Corrente CA", "sv": "AC-Ström",
        "no": "AC-Strøm", "es": "Corriente CA", "da": "AC-Strøm", "pl": "Prąd AC",
    },
    "Panel Power": {
        "en": "Panel {i} Power", "nl": "Paneel {i} Vermogen", "de": "Panel {i} Leistung",
        "fr": "Panneau {i} Puissance", "it": "Pannello {i} Potenza",
        "sv": "Panel {i} Effekt", "no": "Panel {i} Effekt",
        "es": "Panel {i} Potencia", "da": "Panel {i} Effekt", "pl": "Panel {i} Moc",
    },
    "Panel Voltage": {
        "en": "Panel {i} Voltage", "nl": "Paneel {i} Spanning", "de": "Panel {i} Spannung",
        "fr": "Panneau {i} Tension", "it": "Pannello {i} Tensione",
        "sv": "Panel {i} Spänning", "no": "Panel {i} Spenning",
        "es": "Panel {i} Tensión", "da": "Panel {i} Spænding", "pl": "Panel {i} Napięcie",
    },
    "Panel Current": {
        "en": "Panel {i} Current", "nl": "Paneel {i} Stroom", "de": "Panel {i} Strom",
        "fr": "Panneau {i} Courant", "it": "Pannello {i} Corrente",
        "sv": "Panel {i} Ström", "no": "Panel {i} Strøm",
        "es": "Panel {i} Corriente", "da": "Panel {i} Strøm", "pl": "Panel {i} Prąd",
    },
}


def capability_title(key: str, **values) -> dict[str, str]:
    title_map = dict(_CAPABILITY_TITLES.get(key, {"en": key}))
    if not values:
        return title_map
    return {lang: title.format(**values) for lang, title in title_map.items()}


def _is_encrypted(dfs: int) -> bool:
    return bool((dfs >> _IS_ENCRYPTED_BIT) & 1)


def _local_subnet() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except Exception:
        pass
    return None


async def _tcp_open(ip: str, timeout: float) -> str | None:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, _DTU_PORT), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return ip
    except Exception:
        return None


async def _discover_dtus(timeout: float = 1.5) -> list[str]:
    subnet = _local_subnet()
    if not subnet:
        return []
    tasks = [_tcp_open(f"{subnet}.{i}", timeout) for i in range(1, 255)]
    results = await asyncio.gather(*tasks)
    return [ip for ip in results if ip]


def _build_capabilities(panel_count: int, three_phase: bool, is_hybrid: bool) -> list:
    if is_hybrid:
        # No daily energy field in the energy-storage data, so no meter_power.today.
        return [
            "measure_power",
            "meter_power",
            "measure_battery",
            "alarm_generic",
        ]

    caps = ["meter_power.today", "measure_power", "meter_power"]

    for i in range(1, panel_count + 1):
        caps.append(f"measure_power.pv{i}")
        caps.append(f"measure_voltage.pv{i}")
        caps.append(f"measure_current.pv{i}")

    if three_phase:
        for ph in ("a", "b", "c"):
            caps.append(f"measure_voltage.phase_{ph}")
            caps.append(f"measure_current.phase_{ph}")
    else:
        caps.append("measure_voltage")
        caps.append("measure_current")

    caps += ["measure_frequency", "measure_temperature", "alarm_generic", "fault_description"]
    return caps


def _build_capabilities_options(panel_count: int, three_phase: bool, is_hybrid: bool) -> dict:
    opts = {
        "measure_power": {
            "title": capability_title("Current Power"),
            "units": {"en": "W"},
        },
        "meter_power.today": {
            "title": capability_title("Daily Energy"),
            "units": {"en": "kWh"},
        },
        "meter_power": {
            "title": capability_title("Lifetime Energy"),
            "units": {"en": "kWh"},
        },
        "alarm_generic": {
            "title": capability_title("Inverter Alarm"),
        },
    }

    if is_hybrid:
        opts["measure_battery"] = {
            "title": capability_title("Battery State of Charge"),
        }
        return opts

    opts["measure_frequency"] = {
        "title": capability_title("Grid Frequency"),
    }
    opts["measure_temperature"] = {
        "title": capability_title("Inverter Temperature"),
    }
    opts["fault_description"] = {
        "icon": f"{_ASSETS}/fault_alarm_detail.svg",
    }

    if three_phase:
        for ph, label in (("a", "A"), ("b", "B"), ("c", "C")):
            opts[f"measure_voltage.phase_{ph}"] = {
                "title": capability_title("Voltage Phase", label=label),
            }
            opts[f"measure_current.phase_{ph}"] = {
                "title": capability_title("Current Phase", label=label),
            }
    else:
        opts["measure_voltage"] = {
            "title": capability_title("AC Voltage"),
        }
        opts["measure_current"] = {
            "title": capability_title("AC Current"),
        }

    for i in range(1, panel_count + 1):
        opts[f"measure_power.pv{i}"] = {
            "title": capability_title("Panel Power", i=i),
            "units": {"en": "W"},
        }
        opts[f"measure_voltage.pv{i}"] = {
            "title": capability_title("Panel Voltage", i=i),
            "units": {"en": "V"},
        }
        opts[f"measure_current.pv{i}"] = {
            "title": capability_title("Panel Current", i=i),
            "units": {"en": "A"},
        }

    return opts


class HoymilesDriver(Driver):

    async def on_init(self) -> None:
        self.log("HoymilesDriver (Python) init")

    async def on_pair(self, session) -> None:
        self.log("onPair started")
        found_dtus: list[dict] = []
        confirmed:  dict        = {}

        def _device_key(info: dict) -> str:
            return str(info.get("dtu_sn") or info.get("ip") or "")

        async def _query_dtu(ip: str, manual_sn: str = "") -> dict | None:
            """Query a single DTU — same detection logic as the HA integration."""
            dtu = DTU(ip, timeout=10)

            # 1. app_info — always available (DTU on even at night)
            try:
                app_info = await asyncio.wait_for(dtu.async_app_information_data(), timeout=10)
            except Exception:
                return None
            if not app_info:
                return None

            dtu_sn       = str(app_info.dtu_serial_number or "") or manual_sn
            is_encrypted = False
            enc_rand     = ""

            if app_info.dtu_info:
                di = app_info.dtu_info
                if di.dfs and _is_encrypted(di.dfs):
                    is_encrypted = True
                    enc_rand = di.enc_rand.hex() if di.enc_rand else ""
                    dtu = DTU(ip, timeout=10, is_encrypted=True,
                              enc_rand=bytes.fromhex(enc_rand))
                    await asyncio.sleep(2)

            # 2. real_data — detect three_phase and panel count
            # Use app_info.pv_number as baseline (available even when inverter sleeps)
            app_info_panels = int(app_info.pv_number or 0)
            panel_count     = app_info_panels or 2
            three_phase     = False
            is_hybrid       = False
            inverter_sns    = []
            source          = "app_info"

            # Try real_data up to 2 times — some DTUs respond slowly
            real_data = None
            for attempt in range(2):
                try:
                    real_data = await asyncio.wait_for(dtu.async_get_real_data_new(), timeout=8)
                    if real_data:
                        break
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(2)

            if real_data:
                pv_count = len(real_data.pv_data or [])
                # Use max so a sleeping panel doesn't reduce a known panel count
                if pv_count:
                    panel_count = max(app_info_panels, pv_count)
                three_phase = len(real_data.tgs_data or []) > 0
                dtu_sn      = str(real_data.device_serial_number or "") or dtu_sn
                source      = "real_data"
            else:
                # Inverter sleeping — try hybrid (gateway) path
                try:
                    gw = await asyncio.wait_for(dtu.async_get_gateway_info(), timeout=8)
                    if gw:
                        dtu_sn    = str(gw.serial_number or "") or dtu_sn
                        is_hybrid = True
                        source    = "gateway"
                        try:
                            registry = await asyncio.wait_for(
                                dtu.async_get_energy_storage_registry(
                                    dtu_serial_number=int(dtu_sn)
                                ), timeout=8
                            )
                            if registry:
                                inverter_sns = [
                                    str(inv.serial_number)
                                    for inv in (registry.inverters or [])
                                ]
                        except Exception:
                            pass
                        if not inverter_sns:
                            # Without inverter serials the device could never be polled.
                            self.log(f"DTU {ip} — hybrid but inverter registry unavailable")
                            return None
                except Exception:
                    pass

            self.log(
                f"DTU {ip} — SN:{dtu_sn} panels:{panel_count} 3ph:{three_phase} "
                f"hybrid:{is_hybrid} enc:{is_encrypted} source:{source}"
            )

            return {
                "ip":            ip,
                "dtu_sn":        dtu_sn,
                "panel_count":   panel_count,
                "three_phase":   three_phase,
                "is_hybrid":     is_hybrid,
                "inverter_sns":  inverter_sns,
                "is_encrypted":  is_encrypted,
                "enc_rand":      enc_rand,
                "source":        source,
            }

        async def on_login(data: dict) -> bool:
            nonlocal found_dtus
            host      = str(data.get("username", "")).strip()
            manual_sn = str(data.get("password", "")).strip()
            self.log(f"login — ip: '{host}' sn_fallback: '{manual_sn}'")

            if not host:
                self.log("No IP entered — scanning network for Hoymiles DTU...")
                ips = await _discover_dtus(timeout=1.5)
                self.log(f"Network scan found: {ips}")
                if not ips:
                    raise Exception(self.homey.translate("error.no_dtu_found"))
                results = await asyncio.gather(*[_query_dtu(ip) for ip in ips])
                found_dtus = [r for r in results if r]
            else:
                octets = host.split(".")
                if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
                    raise Exception(self.homey.translate("error.invalid_ip", ip=host))
                info = await _query_dtu(host, manual_sn)
                if not info:
                    raise Exception(self.homey.translate("error.cannot_reach", ip=host))
                found_dtus = [info]

            if not found_dtus:
                raise Exception(self.homey.translate("error.no_dtu_responded"))

            for d in found_dtus:
                self.log(f"login OK — {d['ip']} SN:{d['dtu_sn']} panels:{d['panel_count']} 3ph:{d['three_phase']} hybrid:{d['is_hybrid']}")
            return True

        async def on_get_detected(data=None) -> dict:
            if not found_dtus:
                raise Exception("No DTU detected — go back and scan again.")
            return {
                "dtu_count": len(found_dtus),
                "dtus": [dict(d) for d in found_dtus],
            }

        async def on_confirm_structure(data: dict) -> bool:
            nonlocal confirmed
            if not found_dtus:
                raise Exception("No DTU detected — go back and scan again.")

            overrides = data.get("devices")
            if not isinstance(overrides, list) or not overrides:
                # Backward-compatible single-device payload.
                overrides = [data]

            confirmed = {}
            for index, base in enumerate(found_dtus):
                incoming = overrides[index] if index < len(overrides) else {}
                count = int(incoming.get("panel_count", base["panel_count"]))
                if count < 1 or count > 8:
                    raise Exception("Panel count must be between 1 and 8.")

                merged = {
                    "ip":           base["ip"],
                    "dtu_sn":       base["dtu_sn"],
                    "panel_count":  count,
                    "three_phase":  bool(incoming.get("three_phase", base["three_phase"])),
                    "is_hybrid":    bool(incoming.get("is_hybrid",   base["is_hybrid"])),
                    "is_encrypted": bool(incoming.get("is_encrypted", base["is_encrypted"])),
                    "enc_rand":     str(incoming.get("enc_rand", base["enc_rand"]) or ""),
                    "inverter_sns": base["inverter_sns"],
                }
                confirmed[_device_key(base)] = merged
                self.log(
                    f"confirm_structure — {merged['ip']} SN:{merged['dtu_sn']} "
                    f"panels:{merged['panel_count']} 3ph:{merged['three_phase']} "
                    f"hybrid:{merged['is_hybrid']} enc:{merged['is_encrypted']}"
                )
            return True

        async def on_list_devices(data: dict = None) -> list:
            if not found_dtus:
                raise Exception(self.homey.translate("error.no_devices"))

            devices = []
            for d in found_dtus:
                ip     = d["ip"]
                sn     = d["dtu_sn"]
                selected = confirmed.get(_device_key(d), d)
                count        = selected["panel_count"]
                three        = selected["three_phase"]
                hybrid       = selected["is_hybrid"]
                is_encrypted = selected["is_encrypted"]
                enc_rand     = selected["enc_rand"]

                dev_id = f"hoymiles_{sn or ip.replace('.', '_')}"
                name   = f"Hoymiles DTU {sn}" if sn else f"Hoymiles DTU ({ip})"
                self.log(f"list_devices — {name} panels:{count} 3ph:{three} hybrid:{hybrid}")
                devices.append({
                    "name":                name,
                    "data":                {"id": dev_id},
                    "capabilities":        _build_capabilities(count, three, hybrid),
                    "capabilitiesOptions": _build_capabilities_options(count, three, hybrid),
                    "settings": {
                        "ip":               ip,
                        "dtu_sn":           sn,
                        "panel_count":      count,
                        "three_phase":      three,
                        "is_hybrid":        hybrid,
                        "inverter_sns":     json.dumps(d["inverter_sns"]),
                        "polling_interval": 60,
                        "is_encrypted":     is_encrypted,
                        "enc_rand":         enc_rand,
                    },
                })
            return devices

        session.set_handler("login",            on_login)
        session.set_handler("get_detected",     on_get_detected)
        session.set_handler("confirm_structure", on_confirm_structure)
        session.set_handler("list_devices",     on_list_devices)

    async def on_repair(self, session, device) -> None:
        """
        Repair flow — lets the user fix the DTU's IP address or refresh its
        encryption metadata (enc_rand) without removing and re-adding the
        device. Does NOT touch panel_count / three_phase / is_hybrid — those
        determine which capabilities the device has, and changing them still
        requires re-pairing.
        """
        self.log(f"onRepair started for device {device.get_id()}")

        def _current_ip() -> str:
            return str(device.get_setting("ip") or "").strip()

        def _current_dtu_sn() -> str:
            return str(device.get_setting("dtu_sn") or "").strip()

        async def on_get_current(data=None) -> dict:
            ip = _current_ip()
            dtu_sn = _current_dtu_sn()
            self.log(f"Repair current — ip:{ip or '<empty>'} dtu_sn:{dtu_sn or '<empty>'}")
            return {"ip": ip, "dtu_sn": dtu_sn}

        async def on_run_detection(data=None) -> dict:
            data = data or {}
            ip = str(data.get("ip") or _current_ip()).strip()
            if not ip:
                raise Exception("No IP configured — enter an IP address and save first.")
            parts = ip.split(".")
            if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                raise Exception(f"Invalid IP address: {ip}")

            self.log(f"Repair: querying DTU at {ip}")
            dtu = DTU(ip, timeout=10)
            try:
                app_info = await asyncio.wait_for(dtu.async_app_information_data(), timeout=10)
            except Exception as e:
                raise Exception(f"Cannot reach DTU at {ip}: {e}")
            if not app_info:
                raise Exception(f"DTU at {ip} did not respond with app info.")

            dtu_sn = str(app_info.dtu_serial_number or "") or _current_dtu_sn()
            is_encrypted = False
            enc_rand = ""
            if app_info.dtu_info and app_info.dtu_info.dfs and _is_encrypted(app_info.dtu_info.dfs):
                is_encrypted = True
                enc_rand = app_info.dtu_info.enc_rand.hex() if app_info.dtu_info.enc_rand else ""

            self.log(f"Repair detection: dtu_sn:{dtu_sn} encrypted:{is_encrypted}")
            return {"dtu_sn": dtu_sn, "is_encrypted": is_encrypted, "enc_rand": enc_rand}

        async def on_save_repair(data: dict) -> bool:
            current_ip = _current_ip()
            ip = str(data.get("ip") or current_ip).strip()
            new_settings: dict = {}
            if ip and ip != current_ip:
                parts = ip.split(".")
                if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    raise Exception(f"Invalid IP address: {ip}")
                new_settings["ip"] = ip
            if data.get("dtu_sn"):
                new_settings["dtu_sn"] = str(data["dtu_sn"])
            if "is_encrypted" in data:
                new_settings["is_encrypted"] = bool(data["is_encrypted"])
            if data.get("enc_rand"):
                new_settings["enc_rand"] = str(data["enc_rand"])
            if not new_settings:
                return True
            self.log(f"Repair: applying settings {new_settings}")
            await device.set_settings(new_settings)
            return True

        session.set_handler("get_current",   on_get_current)
        session.set_handler("run_detection", on_run_detection)
        session.set_handler("save_repair",   on_save_repair)


homey_export = HoymilesDriver
