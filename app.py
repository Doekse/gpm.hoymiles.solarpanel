"""Hoymiles Wi-Fi Solar Panel — Python runtime."""

import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

from homey.app import App

from app.lib.fault_codes import known_fault_names

# ── Debug verbosity ───────────────────────────────────────────────────────────
# True  → every DEBUG line from this app (app.*) and from the hoymiles_wifi
#         library (each DTU request in hex, read lengths, frame format), plus one
#         "poll ok" line per poll — view with `homey app run --remote`.
#         hoymiles_wifi's crypt_util also logs the encryption key and nonce; those
#         records are filtered out below.
# False → production. Never publish with True.
DEBUG_LOG = False


class _NoCryptDetails(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.module != "crypt_util"


def _app_version() -> str:
    try:
        manifest = os.path.join(os.path.dirname(__file__), "app.json")
        with open(manifest, encoding="utf-8") as f:
            return json.load(f).get("version", "unknown")
    except Exception:
        return "unknown"


class HoymilesApp(App):

    async def on_init(self) -> None:
        await super().on_init()
        if DEBUG_LOG:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("[debug] %(name)s: %(message)s"))
            handler.addFilter(_NoCryptDetails())
            for name in ("app", "hoymiles_wifi"):
                logger = logging.getLogger(name)
                logger.setLevel(logging.DEBUG)
                logger.addHandler(handler)
                logger.propagate = False
        self.log(f"gpm.hoymiles.solarpanel v{_app_version()} started"
                 f"{' — DEBUG_LOG on' if DEBUG_LOG else ''}")
        self._register_flow_conditions()

    async def on_uninit(self) -> None:
        self.log("App stopped")

    def _register_flow_conditions(self) -> None:
        """Register run listeners for condition flow cards."""

        async def _is_solar_producing(
            card_arguments: Mapping[str, Any], **kwargs: Any
        ) -> bool:
            device = card_arguments.get("device")
            if not device:
                return False
            power = device.get_capability_value("measure_power") or 0
            return float(power) > 5.0

        async def _has_active_fault(
            card_arguments: Mapping[str, Any], **kwargs: Any
        ) -> bool:
            device = card_arguments.get("device")
            if not device:
                return False
            current = device.get_capability_value("fault_description") or "OK"
            return current != "OK"

        async def _fault_is(card_arguments: Mapping[str, Any], **kwargs: Any) -> bool:
            device = card_arguments.get("device")
            fault = card_arguments.get("fault")
            if not device or not fault:
                return False
            name = fault.get("name") if isinstance(fault, dict) else fault
            current = device.get_capability_value("fault_description") or "OK"
            return name == current

        async def _fault_autocomplete(query: str, **kwargs: Any) -> list[dict]:
            device = kwargs.get("device")
            lang = device._lang() if device else "en"
            names = known_fault_names(lang)
            if not device or not bool(device.get_setting("is_hybrid")):
                # No battery on non-hybrid devices — never suggest
                # battery/BMS-specific faults for them.
                names = [n for n in names if "battery" not in n.lower() and "bms" not in n.lower()]
            query_lower = (query or "").lower()
            return [{"name": n} for n in names if query_lower in n.lower()]

        try:
            self.homey.flow.get_condition_card("is_solar_producing") \
                .register_run_listener(_is_solar_producing)
            self.homey.flow.get_condition_card("has_active_fault") \
                .register_run_listener(_has_active_fault)
            fault_is_card = self.homey.flow.get_condition_card("fault_is")
            fault_is_card.register_run_listener(_fault_is)
            fault_is_card.register_argument_autocomplete_listener("fault", _fault_autocomplete)
        except Exception as e:
            self.log(f"Flow condition registration failed: {e}")


homey_export = HoymilesApp
