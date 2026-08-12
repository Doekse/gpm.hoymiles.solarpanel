"""Hoymiles device — polls DTU and updates Homey capabilities."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from astral import LocationInfo
from astral.sun import sun

from app.app import DEBUG_LOG as _DEBUG_LOG
from app.drivers.hoymiles.driver import capability_title, _ASSETS
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
# warning_number is a single code (not a bitfield, unlike Deye).
#
# Primary source: the official Hoymiles "S-Miles Enduser" iOS app
# (hemaiEnduser.app, en/de/es/fr/it/nl/pl .lproj/Localizable.strings,
# keys "1007_<code>"). Device-type prefix "1007" was chosen because its
# code ranges and text line up with both directly observed warning_number
# values (1, 2 seen live during this app's development) and the
# community-sourced OpenDTU table below. Not exhaustive on its own — the
# few gaps and the whole 5000+/8000+/9000 range aren't covered by "1007"
# in this app version, so those are filled in (English only, no official
# translation available) from OpenDTU's AlarmLogParser.cpp
# (github.com/tbnobody/OpenDTU), itself derived from an older version of
# the same Hoymiles app.
#
# {code: {lang: text}} — lang is a 2-char Homey language code. Falls back
# to "en" in _decode_warning() if the device's language isn't covered for
# a given code (many codes only have "en" from the OpenDTU-only fallback).
_WARNING_MESSAGES: dict[int, dict[str, str]] = {
    1: {"en": "Reset", "de": "Zurücksetzen", "es": "Reset", "fr": "Réinitialiser", "it": "Reset", "nl": "Resetten", "pl": "Resetuj"},
    2: {"en": "Time calibration", "de": "Kalibrierung der Zeit", "es": "Time calibration", "fr": "Étalonnage du temps", "it": "Time calibration", "nl": "Tijdkalibratie", "pl": "Kalibracja czasu"},
    3: {"en": "EEPROM reading and writing error", "de": "EEPROM-Lese- und -Schreibfehler", "es": "EEPROM reading and writing error", "fr": "Erreur de lecture et d’écriture EEPROM", "it": "EEPROM reading and writing error", "nl": "Lees- en schrijffout EEPROM", "pl": "Błąd odczytu i zapisu EEPROM"},
    4: {"en": "SCI Fault Reset", "de": "SCI-Fehler zurücksetzen", "es": "SCI Fault Reset", "fr": "Réinitialisation de l’anomalie SCI", "it": "SCI Fault Reset", "nl": "Resetten SCI-fout", "pl": "Resetowanie błędu SCI"},
    5: {"en": "CLA task error", "de": "CLA-Task-Fehler", "es": "CLA task error", "fr": "Erreur de tâche CLA", "it": "CLA task error", "nl": "CLA-taakfout", "pl": "Błąd zadania CLA"},
    6: {"en": "Sleep Event", "de": "Ereignis im Ruhemodus", "es": "Sleep Event", "fr": "Événement de mise en veille", "it": "Sleep Event", "nl": "Slaapgebeurtenis", "pl": "Uśpienie"},
    7: {"en": "BMS WD Reset", "de": "BMS WD zurücksetzen", "es": "BMS WD Reset", "fr": "Réinitialisation BMS WD", "it": "BMS WD Reset", "nl": "BMS WD Reset", "pl": "Reset WD BMS"},
    8: {"en": "EPS", "de": "EPS", "es": "EPS", "fr": "EPS", "it": "EPS", "nl": "EPS", "pl": "EPS"},
    11: {"en": "Grid voltage surge", "de": "Netzüberspannung", "es": "Grid voltage surge", "fr": "Surtension du réseau", "it": "Grid voltage surge", "nl": "Netspanningsstoot", "pl": "Gwałtowny wzrost napięcia sieci"},
    12: {"en": "Grid voltage sharp drop", "de": "Starker Abfall der Netzspannung", "es": "Grid voltage sharp drop", "fr": "Forte chute de la tension du réseau", "it": "Grid voltage sharp drop", "nl": "Scherpe daling netspanning", "pl": "Gwałtowny spadek napięcia sieci"},
    13: {"en": "Grid voltage spike", "de": "Netzspannungsspitze", "es": "Grid voltage spike", "fr": "Pointe de tension du réseau", "it": "Grid voltage spike", "nl": "Netspanningspiek", "pl": "Impuls napięcia sieci"},
    14: {"en": "Grid phase mutation", "de": "Veränderung der Netzphasen", "es": "Grid phase mutation", "fr": "Mutation de phase du réseau", "it": "Grid phase mutation", "nl": "Netfasemutatie", "pl": "Mutacja fazy sieci"},
    15: {"en": "Grid zero-crossing distortion", "de": "Netz-Nulldurchgangsstörung", "es": "Grid zero-crossing distortion", "fr": "Distorsion du passage à zéro du réseau", "it": "Grid zero-crossing distortion", "nl": "Nuldoorgangsvervorming elektriciteitsnet", "pl": "Zniekształcenie przejścia przez zero sieci"},
    16: {"en": "Abnormal grid polarity", "de": "Anormale Netzpolarität", "es": "Abnormal grid polarity", "fr": "Polarité du réseau anormale", "it": "Abnormal grid polarity", "nl": "Abnormale netpolariteit", "pl": "Nieprawidłowa polaryzacja sieci"},
    17: {"en": "Grid hardware overvoltage", "de": "Netz-Hardware-Überspannung", "es": "Grid hardware overvoltage", "fr": "Surtension matérielle du réseau", "it": "Grid hardware overvoltage", "nl": "Overspanning nethardware", "pl": "Nadnapięcie sprzętowe w sieci"},
    18: {"en": "Instant grid phase lock error", "de": "Sofortiger Netzphasenverriegelungsfehler", "es": "Instant grid phase lock error", "fr": "Erreur de verrouillage de phase instantanée du réseau", "it": "Instant grid phase lock error", "nl": "Instant elektriciteitsnetwerk-fasevergrendelingsfout", "pl": "Błąd chwilowej blokady fazy sieciowej"},
    19: {"en": "Instant off-grid overvoltage", "de": "Sofortige netzunabhängige Überspannung", "es": "Instant off-grid overvoltage", "fr": "Surtension instantanée hors réseau", "it": "Instant off-grid overvoltage", "nl": "Instant netwerk-onafhankelijke overspanning", "pl": "Chwilowe przepięcie w trybie wyspowym"},
    20: {"en": "Instant DC bus overvoltage", "de": "Sofortige Überspannung am DC-Bus", "es": "Instant DC bus overvoltage", "fr": "Surtension instantanée du bus CC", "it": "Instant DC bus overvoltage", "nl": "Instant DC-busoverspanning", "pl": "Chwilowe przepięcie magistrali DC"},
    21: {"en": "Instant DC bus undervoltage", "de": "Sofortige Unterspannung am DC-Bus", "es": "Instant DC bus undervoltage", "fr": "Sous-tension instantanée du bus CC", "it": "Instant DC bus undervoltage", "nl": "Instant DC-busonderspanning", "pl": "Natychmiastowe podnapięcie magistrali DC"},
    22: {"en": "BOOSTA overcurrent", "de": "BOOSTA-Überstrom", "es": "BOOSTA overcurrent", "fr": "Surintensité BOOSTA", "it": "BOOSTA overcurrent", "nl": "BOOSTA-overstroom", "pl": "Przetężenie BOOSTA"},
    23: {"en": "BOOSTB overcurrent", "de": "BOOSTB-Überstrom", "es": "BOOSTB overcurrent", "fr": "Surintensité BOOSTB", "it": "BOOSTB overcurrent", "nl": "BOOSTB-overstroom", "pl": "Przetężenie BOOSTB"},
    24: {"en": "Battery overcurrent", "de": "Akku-Überstrom", "es": "Battery overcurrent", "fr": "Surintensité de la batterie", "it": "Battery overcurrent", "nl": "Accu-overstroom", "pl": "Przetężenie akumulatora"},
    25: {"en": "Resonant cavity overcurrent", "de": "Überstrom des Resonanzhohlraums", "es": "Resonant cavity overcurrent", "fr": "Surintensité de la cavité résonnante", "it": "Resonant cavity overcurrent", "nl": "Resonantieholte-overstroom", "pl": "Przetężenie wnęki rezonansowej"},
    26: {"en": "Abnormal battery current limiting", "de": "Anormale Strombegrenzung des Akkus", "es": "Abnormal battery current limiting", "fr": "Limitation anormale du courant de la batterie", "it": "Abnormal battery current limiting", "nl": "Abnormale accustroom begrenzing", "pl": "Nieprawidłowe ograniczenie natężenia akumulatora"},
    27: {"en": "Off-grid instant voltage abnormality", "de": "Anomalie der sofortigen Spannung außerhalb des Netzes", "es": "Anomalía de voltaje instantáneo fuera de la red", "fr": "Anomalie de tension instantanée hors réseau", "it": "Off-grid instant voltage abnormality", "nl": "Netwerk-onafhankelijke instant spanningsabnormaliteit", "pl": "Nieprawidłowe napięcie chwilowe w trybie wyspowym"},
    28: {"en": "BOOSTC overcurrent", "de": "BOOSTC-Überstrom", "es": "BOOSTC overcurrent", "fr": "Surintensité BOOSTC", "it": "BOOSTC overcurrent", "nl": "BOOSTC-overstroom", "pl": "Przetężenie BOOSTC"},
    29: {"en": "BOOSTD overcurrent", "de": "BOOSTD-Überstrom", "es": "BOOSTD overcurrent", "fr": "Surintensité BOOSTD", "it": "BOOSTD overcurrent", "nl": "BOOSTD-overstroom", "pl": "Przetężenie BOOSTD"},
    36: {"en": "INV overvoltage or overcurrent"},
    46: {"en": "FB overvoltage"},
    47: {"en": "FB overcurrent"},
    48: {"en": "FB clamp overvoltage"},
    49: {"en": "FB clamp overvoltage"},
    61: {"en": "Calibration parameter error", "de": "Parameterfehler bei der Kalibrierung", "es": "Calibration parameter error", "fr": "Erreur de paramètre d’étalonnage", "it": "Calibration parameter error", "nl": "Fout in kalibratieparameter", "pl": "Błąd parametru kalibracji"},
    62: {"en": "System configuration parameter error", "de": "Parameterfehler bei der Systemkonfiguration", "es": "Error en parámetro de configuración del sistema", "fr": "Erreur de paramètre de configuration du système", "it": "System configuration parameter error", "nl": "Fout in systeemconfiguratieparameter", "pl": "Błąd parametru konfiguracji systemu"},
    63: {"en": "TW function enabled", "de": "TW-Funktion aktiviert", "es": "Función TW habilitada", "fr": "Fonction TW activée", "it": "TW function enabled", "nl": "TW-functie ingeschakeld", "pl": "Funkcja TW włączona"},
    64: {"en": "VW function enabled", "de": "VW-Funktion aktiviert", "es": "VW function enabled", "fr": "Fonction VW activée", "it": "VW function enabled", "nl": "VW-functie ingeschakeld", "pl": "Funkcja VW włączona"},
    65: {"en": "FW function enabled", "de": "FW-Funktion aktiviert", "es": "FW function enabled", "fr": "Fonction FW activée", "it": "FW function enabled", "nl": "FW-functie ingeschakeld", "pl": "Funkcja FW włączona"},
    66: {"en": "TOC function enabled", "de": "TOC-Funktion aktiviert", "es": "TOC function enabled", "fr": "Fonction TOC activée", "it": "TOC function enabled", "nl": "TOC-functie ingeschakeld", "pl": "Funkcja TOC włączona"},
    67: {"en": "Input current TOC function enabled", "de": "Eingangsstrom der TOC-Funktion aktiviert", "es": "Input current TOC function enabled", "fr": "Fonction TOC de courant d’entrée activée", "it": "Input current TOC function enabled", "nl": "Ingangsstroom TOC-functie ingeschakeld", "pl": "Włączono funkcję TOC prądu wejściowego"},
    68: {"en": "Input current TOC function enabled", "de": "Eingangsstrom der TOC-Funktion aktiviert", "es": "Input current TOC function enabled", "fr": "Fonction TOC de courant d’entrée activée", "it": "Input current TOC function enabled", "nl": "Ingangsstroom TOC-functie ingeschakeld", "pl": "Włączono funkcję TOC prądu wejściowego"},
    69: {"en": "Low irradiation", "de": "Niedrige Bestrahlung", "es": "Low irradiation", "fr": "Faible rayonnement", "it": "Low irradiation", "nl": "Lage straling", "pl": "Niskie napromienienie"},
    70: {"en": "Battery charge overcurrent alarm", "de": "Überstromalarm beim Laden des Akkus", "es": "Battery charge overcurrent alarm", "fr": "Alarme de surintensité de charge de la batterie", "it": "Battery charge overcurrent alarm", "nl": "Acculaad-overstroomalarm", "pl": "Alarm przetężenia ładowania akumulatora"},
    71: {"en": "Battery discharge overcurrent alarm", "de": "Überstromalarm beim Entladen des Akkus", "es": "Battery discharge overcurrent alarm", "fr": "Alarme de surintensité de décharge de la batterie", "it": "Battery discharge overcurrent alarm", "nl": "Accuontlaad-overstroomalarm", "pl": "Alarm przetężenia rozładowania akumulatora"},
    72: {"en": "Battery Overvoltage Alarm", "de": "Batterieüberspannungsalarm", "es": "Battery Overvoltage Alarm", "fr": "Alarme de surtension de la batterie", "it": "Battery Overvoltage Alarm", "nl": "Accu-overspanningsalarm", "pl": "Alarm przepięcia akum."},
    73: {"en": "Battery Undervoltage Alarm", "de": "Batterieunterspannungsalarm", "es": "Battery Undervoltage Alarm", "fr": "Alarme de sous-tension de la batterie", "it": "Battery Undervoltage Alarm", "nl": "Accu-onderspanningsalarm", "pl": "Alarm podnapięcia akum."},
    74: {"en": "High temperature alarm", "de": "Hochtemperaturalarm", "es": "Alarma de alta temperatura", "fr": "Alarme de température élevée", "it": "High temperature alarm", "nl": "Hoge-temperatuuralarm", "pl": "Alarm wysokiej temperatury"},
    75: {"en": "Low temperature charge alarm", "de": "Ladealarm bei Niedrigtemperatur", "es": "Alarma de carga a baja temperatura", "fr": "Alarme de charge à basse température", "it": "Low temperature charge alarm", "nl": "Lage temperatuur oplaadalarm", "pl": "Alarm ładowania w niskiej temperaturze"},
    76: {"en": "Low temperature discharge alarm", "de": "Entladealarm bei niedriger Temperatur", "es": "Alarma de descarga a baja temperatura", "fr": "Alarme de décharge à basse température", "it": "Low temperature discharge alarm", "nl": "Lage temperatuur ontlaadalarm", "pl": "Alarm rozładowania w niskiej temperaturze"},
    77: {"en": "Underfrequency FWUF function enabled", "de": "Unterfrequenz-FWUF-Funktion aktiviert", "es": "Underfrequency FWUF function enabled", "fr": "Fonction FWUF de sous-fréquence activée", "it": "Underfrequency FWUF function enabled", "nl": "Onderfrequentie FWUF-functie ingeschakeld", "pl": "Włączono funkcję FWUF podczęstotliwości"},
    78: {"en": "BMS Comprehensive Alarm", "de": "Umfassender BMS-Alarm", "es": "BMS Comprehensive Alarm", "fr": "Alarme globale du BMS", "it": "BMS Comprehensive Alarm", "nl": "Uitgebreid BMS-alarm", "pl": "Kompleksowy alarm BMS"},
    79: {"en": "Loss of Battery Charging Capability Under Non-full Charge", "de": "Verlust der Ladefähigkeit der Batterie bei unvollständiger Ladung", "es": "Loss of Battery Charging Capability Under Non-full Charge", "fr": "Perte de capacité de charge lorsque la batterie n’est pas complètement chargée", "it": "Loss of Battery Charging Capability Under Non-full Charge", "nl": "Verlies Van Accu-Oplaadcapaciteit Bij Niet-Volledig Opladen", "pl": "Utrata możliwości ładowania akumulatora przy niepełnym naładowaniu"},
    80: {"en": "Loss of Battery Discharging Capability Under Non-full Discharge", "de": "Verlust der Entladefähigkeit der Batterie bei unvollständiger Entladung", "es": "Loss of Battery Discharging Capability Under Non-full Discharge", "fr": "Perte de capacité de décharge lorsque la batterie n’est pas complètement déchargée", "it": "Loss of Battery Discharging Capability Under Non-full Discharge", "nl": "Verlies Van Accu-Ontlaadcapaciteit Bij Niet-Volledig Ontladen", "pl": "Utrata możliwości rozładowania akumulatora przy niepełnym rozładowaniu"},
    81: {"en": "Battery Charging Capability Limit", "de": "Grenzwert der Batterieladekapazität", "es": "Battery Charging Capability Limit", "fr": "Limite de capacité de charge de la batterie", "it": "Battery Charging Capability Limit", "nl": "Grenswaarde acculaadvermogen", "pl": "Limit możliwości ładowania akumulatora"},
    82: {"en": "Battery Discharging Capability Limit", "de": "Grenzwert der Batterieentladekapazität", "es": "Battery Discharging Capability Limit", "fr": "Limite de capacité de décharge de la batterie", "it": "Battery Discharging Capability Limit", "nl": "Accuontlaadvermogensgrenswaarde", "pl": "Limit możliwości rozładowania akumulatora"},
    83: {"en": "Battery Heating Under Low Voltage", "de": "Batterieheizung-Unterspannung", "es": "Battery Heating Under Low Voltage", "fr": "Chauffage de la batterie en cas de basse tension", "it": "Battery Heating Under Low Voltage", "nl": "Accuverwarming Bij Laagspanning", "pl": "Nagrzewanie się akum. przy niskim napięciu"},
    84: {"en": "Auxiliary Power Abnormality", "de": "Hilfsstromversorgung anormal", "es": "Auxiliary Power Abnormality", "fr": "Anomalie de l’alimentation auxiliaire", "it": "Auxiliary Power Abnormality", "nl": "Hulpvoedingsafwijking", "pl": "Nieprawidłowe zasilanie pomocnicze"},
    95: {"en": "PV-1: Module in suspected shadow"},
    96: {"en": "PV-2: Module in suspected shadow"},
    97: {"en": "PV-3: Module in suspected shadow"},
    98: {"en": "PV-4: Module in suspected shadow"},
    101: {"en": "Grid configuration parameter error", "de": "Parameterfehler bei der Konfiguration des Elektrizitätsnetzes", "es": "Error en parámetro de configuración de red", "fr": "Erreur de paramètre de configuration du réseau", "it": "Grid configuration parameter error", "nl": "Fout in netconfiguratieparameter", "pl": "Błąd parametru konfiguracji sieci"},
    102: {"en": "Firmware error", "de": "Firmwarefehler", "es": "Error de firmware", "fr": "Erreur du micrologiciel", "it": "Firmware error", "nl": "Firmwarefout", "pl": "Błąd oprogramowania układowego"},
    103: {"en": "Hardware configuration information error", "de": "Fehler in der Hardware-Konfigurationsinformation", "es": "Error de información de configuración del hardware", "fr": "Erreur d’information sur la configuration du matériel", "it": "Hardware configuration information error", "nl": "Hardwareconfiguratie gegevensfout", "pl": "Błąd informacji o konfiguracji sprzętu"},
    104: {"en": "Abnormal insulation impedance", "de": "Abnormale Isolationsimpedanz", "es": "Impedancia de aislamiento anormal", "fr": "Impédance d’isolement anormale", "it": "Abnormal insulation impedance", "nl": "Abnormale isolatie-impedantie", "pl": "Nieprawidłowa impedancja izolacji"},
    105: {"en": "Over temperature protection", "de": "Übertemperaturschutz", "es": "Protección contra exceso de temperatura", "fr": "Protection contre la surchauffe", "it": "Over temperature protection", "nl": "Bescherming tegen te hoge temperatuur", "pl": "Zabezpieczenie przed przekroczeniem temperatury"},
    106: {"en": "PV input parallel abnormality", "de": "Anomalie am parallelen PV-Eingang", "es": "Anomalía en conexión en paralelo de la entrada FV", "fr": "Anomalie parallèle de l’entrée photovoltaïque", "it": "PV input parallel abnormality", "nl": "PV-ingang parallel-abnormaliteit", "pl": "Odchylenie równoległego wejścia PV"},
    107: {"en": "Offset error", "de": "Offset-Fehler", "es": "Offset error", "fr": "Erreur de décalage", "it": "Offset error", "nl": "Offsetfout", "pl": "Błąd przesunięcia"},
    108: {"en": "Relay self-test abnormality", "de": "Abnormalität des Relais-Selbsttests", "es": "Relay self-test abnormality", "fr": "Anomalie de l’autodiagnostic du relais", "it": "Relay self-test abnormality", "nl": "Relais-zelftestabnormaliteit", "pl": "Błąd autotestu przekaźnika"},
    109: {"en": "Wi-Fi communication abnormality", "de": "Anomalie der WLAN-Kommunikation", "es": "Wi-Fi communication abnormality", "fr": "Anomalie de communication Wi-Fi", "it": "Wi-Fi communication abnormality", "nl": "WiFi-communicatie-abnormaliteit", "pl": "Błąd komunikacji Wi-Fi"},
    110: {"en": "BMS communication abnormality", "de": "Anomalie der BMS-Kommunikation", "es": "BMS communication abnormality", "fr": "Anomalie de communication BMS", "it": "BMS communication abnormality", "nl": "BMS-communicatie-abnormaliteit", "pl": "Błąd komunikacji BMS"},
    111: {"en": "Sub-1G communication abnormality", "de": "Fehler in der Sub-1G-Kommunikation", "es": "Sub-1G communication abnormality", "fr": "Anomalie de communication Sub-1G", "it": "Sub-1G communication abnormality", "nl": "Sub-1G communicatie-abnormaliteit", "pl": "Błąd komunikacji Sub-1G"},
    112: {"en": "DSP communication abnormality", "de": "DSP-Kommunikationsanomalie", "es": "DSP communication abnormality", "fr": "Anomalie de communication DSP", "it": "DSP communication abnormality", "nl": "DSP-communicatie-abnormaliteit", "pl": "Błąd komunikacji DSP"},
    113: {"en": "Shut down by remote control", "de": "Durch Fernbedienung abgeschaltet", "es": "Shut down by remote control", "fr": "Arrêté par la télécommande", "it": "Shut down by remote control", "nl": "Afsluiten via afstandsbediening", "pl": "Zamknięty przez zdalne sterowanie"},
    114: {"en": "Under Temperature Lock", "de": "Untertemperatursperre", "es": "Bloqueo por temperatura", "fr": "Verrouillage pour température trop basse", "it": "Under Temperature Lock", "nl": "Ondertemperatuurvergrendeling", "pl": "Blokada przy niskiej temp."},
    115: {"en": "LED Communication Abnormality", "de": "LED-Kommunikation anormal", "es": "LED Communication Abnormality", "fr": "Anomalie de communication LED", "it": "LED Communication Abnormality", "nl": "LED-communicatie-afwijking", "pl": "Nieprawidłowa komunikacja LED"},
    116: {"en": "Low Power", "de": "Geringe Leistung", "es": "Baja potencia", "fr": "Basse puissance", "it": "Low Power", "nl": "Laag vermogen", "pl": "Niska moc"},
    117: {"en": "CAN Communication Abnormality", "de": "CAN-Kommunikation anormal", "es": "CAN Communication Abnormality", "fr": "Anomalie de communication CAN", "it": "CAN Communication Abnormality", "nl": "CAN-communicatie-afwijking", "pl": "Nieprawidłowa komunikacja CAN"},
    118: {"en": "PV1 Over Temperature Protection", "de": "PV1-Übertemperaturschutz", "es": "Protección contra sobretemperatura de FV1", "fr": "Protection contre la surchauffe PV1", "it": "PV1 Over Temperature Protection", "nl": "PV1-overtemperatuurbeveiliging", "pl": "Ochrona przed przegrzaniem PV1"},
    119: {"en": "PV2 Over Temperature Protection", "de": "PV2-Übertemperaturschutz", "es": "Protección contra sobretemperatura de FV2", "fr": "Protection contre la surchauffe PV2", "it": "PV2 Over Temperature Protection", "nl": "PV2-overtemperatuurbeveiliging", "pl": "Ochrona przed przegrzaniem PV2"},
    120: {"en": "PV3 Over Temperature Protection", "de": "PV3-Übertemperaturschutz", "es": "Protección contra sobretemperatura de FV3", "fr": "Protection contre la surchauffe PV3", "it": "PV3 Over Temperature Protection", "nl": "PV3-overtemperatuurbeveiliging", "pl": "Ochrona przed przegrzaniem PV3"},
    121: {"en": "PV4 Over Temperature Protection", "de": "PV4-Übertemperaturschutz", "es": "Protección contra sobretemperatura de FV4", "fr": "Protection contre la surchauffe PV4", "it": "PV4 Over Temperature Protection", "nl": "PV4-overtemperatuurbeveiliging", "pl": "Ochrona przed przegrzaniem PV4"},
    122: {"en": "VAN Communication Busoff", "de": "VAN Communication Busoff", "es": "VAN Communication Busoff", "fr": "VAN Communication Busoff", "it": "VAN Communication Busoff", "nl": "VAN Communication Busoff", "pl": "VAN Communication Busoff"},
    123: {"en": "Locked by remote control"},
    124: {"en": "Shut down by remote control"},
    126: {"en": "Software error code 126"},
    127: {"en": "Firmware error"},
    128: {"en": "Hardware configuration error"},
    129: {"en": "Abnormal bias"},
    130: {"en": "Offline"},
    141: {"en": "Grid overvoltage", "de": "Netzüberspannung", "es": "Sobretensión en la red", "fr": "Surtension du réseau", "it": "Grid overvoltage", "nl": "Netoverspanning", "pl": "Nadnapięcie w sieci"},
    142: {"en": "Grid overvoltage (10 min mean value)", "de": "Netzüberspannung (10-min. Mittelwert)", "es": "Sobretensión de red (valor medio de 10 minutos)", "fr": "Surtension du réseau (valeur moyenne sur 10 minutes)", "it": "Grid overvoltage (10 min mean value)", "nl": "Netoverspanning (10 min. gemiddelde waarde)", "pl": "Nadnapięcie sieci (wartość średnia, 10 min)"},
    143: {"en": "Grid undervoltage", "de": "Netzunterspannung", "es": "Subtensión de la red", "fr": "Sous-tension du réseau", "it": "Grid undervoltage", "nl": "Netonderspanning", "pl": "Podnapięcie w sieci"},
    144: {"en": "Grid overfrequency", "de": "Netzüberfrequenz", "es": "Sobrefrecuencia de la red", "fr": "Surfréquence du réseau", "it": "Grid overfrequency", "nl": "Netoverfrequentie", "pl": "Nadczęstotliwość w sieci"},
    145: {"en": "Grid underfrequency", "de": "Netzunterfrequenz", "es": "Subfrecuencia de la red", "fr": "Sous-fréquence du réseau", "it": "Grid underfrequency", "nl": "Netonderfrequentie", "pl": "Podczęstotliwość w sieci"},
    146: {"en": "Excessive grid frequency change rate", "de": "Überhöhte Netzfrequenzänderungsrate", "es": "Tasa de cambio de frecuencia de la red excesiva", "fr": "Taux de changement de fréquence du réseau excessif", "it": "Excessive grid frequency change rate", "nl": "Excessieve netwerkfrequentie-veranderingssnelheid", "pl": "Nadmierne tempo zmiany częstotliwości sieci"},
    147: {"en": "No voltage on grid port", "de": "Keine Spannung am Netzanschluss", "es": "No hay voltaje en el puerto de red", "fr": "Pas de tension sur le port du réseau", "it": "No voltage on grid port", "nl": "Geen spanning op netpoort", "pl": "Brak napięcia w porcie sieci"},
    148: {"en": "Grid disconnection", "de": "Netztrennung", "es": "Desconexión de la red", "fr": "Déconnexion du réseau", "it": "Grid disconnection", "nl": "Netontkoppeling", "pl": "Odłączanie sieci"},
    149: {"en": "Island detected", "de": "Inselbetrieb festgestellt", "es": "Aislamiento (Isla) detectado", "fr": "Îlotage détecté", "it": "Island detected", "nl": "Eiland gedetecteerd", "pl": "Wykryto wyspę"},
    150: {"en": "DCI exceeded", "de": "DCI überschritten", "es": "DCI exceeded", "fr": "DCI dépassé", "it": "DCI exceeded", "nl": "DCI overschreden", "pl": "DCI przekroczone"},
    151: {"en": "Severe grid wave distortion", "de": "Starke Netzwellenverzerrung", "es": "Severe grid wave distortion", "fr": "Forte distorsion des ondes du réseau", "it": "Severe grid wave distortion", "nl": "Ernstige netgolfvervorming", "pl": "Znaczące zniekształcenie fali sieci"},
    152: {"en": "Grid Phase Lock Error", "de": "Netzphasenverriegelungsfehler", "es": "Grid Phase Lock Error", "fr": "Erreur de verrouillage de phase du réseau", "it": "Grid Phase Lock Error", "nl": "Vergrendelingsfout netfase", "pl": "Błąd blokady fazy sieci"},
    153: {"en": "Grid: island detected (variant)"},
    161: {"en": "Overload", "de": "Überlast", "es": "Sobrecarga", "fr": "Surcharge", "it": "Overload", "nl": "Overbelasting", "pl": "Przeciążenie"},
    162: {"en": "Overload locked", "de": "Überlast blockiert", "es": "Sobrecarga bloqueada", "fr": "Surcharge verrouillée", "it": "Overload locked", "nl": "Overbelasting vergrendeld", "pl": "Zablokowane ze względu na przeciążenie"},
    163: {"en": "Longterm overvoltage", "de": "Langfristige Überspannung", "es": "Sobretensión a largo plazo", "fr": "Surtension à long terme", "it": "Longterm overvoltage", "nl": "Langdurige overspanning", "pl": "Długoterminowe przepięcie"},
    164: {"en": "Undervoltage", "de": "Unterspannung", "es": "Subtensión", "fr": "Sous-tension", "it": "Undervoltage", "nl": "Onderspanning", "pl": "Podnapięcie"},
    165: {"en": "Off-Grid Port Overcurrent", "de": "Überstrom am netzunabhängigen Anschluss", "es": "Sobrecorriente en el puerto fuera de la red", "fr": "Surintensité au port hors réseau", "it": "Off-Grid Port Overcurrent", "nl": "Overstroom op off-gridpoort", "pl": "Przetężenie portu wyspowego"},
    166: {"en": "Off-grid port relay disconnected", "de": "Relais des netzfernen Anschlusses abgeklemmt", "es": "Off-grid port relay disconnected", "fr": "Relais du port hors réseau déconnecté", "it": "Off-grid port relay disconnected", "nl": "Netwerk-onafhankelijke poortrelais ontkoppeld", "pl": "Rozłączony przekaźnik portu wyspowego"},
    167: {"en": "Off-grid Port Device Mismatch", "de": "Fehlanpassung von Gerät und netzunabhängigem Anschluss", "es": "Discrepancia en el dispositivo de puerto fuera de la red", "fr": "Incompatibilité de l’appareil du port hors réseau", "it": "Off-grid Port Device Mismatch", "nl": "Netwerk-onafhankelijk poortapparaat komt niet overeen", "pl": "Niezgodność urządzenia portu wyspowego"},
    168: {"en": "Off-grid instant voltage abnormality", "de": "Anomalie der sofortigen Spannung außerhalb des Netzes", "es": "Anomalía de voltaje instantáneo fuera de la red", "fr": "Anomalie de tension instantanée hors réseau", "it": "Off-grid instant voltage abnormality", "nl": "Netwerk-onafhankelijke instant spanningsabnormaliteit", "pl": "Nieprawidłowe napięcie chwilowe w trybie wyspowym"},
    169: {"en": "Off-grid CYC fault lock", "de": "Netzunabhängige CYC-Fehlersperre", "es": "Bloqueo por falla CYC fuera de red", "fr": "Verrouillage des anomalies CYC hors réseau", "it": "Off-grid CYC fault lock", "nl": "Netwerk-onafhankelijke CYC-foutvergrendeling", "pl": "Blokada błędu CYC trybu wyspowego"},
    170: {"en": "Off-grid End-of-Discharge/Charge", "de": "Entladeschluss im netzunabhängigen Betrieb/Laden", "es": "Fin de carga/descarga fuera de la red", "fr": "Fin de décharge/charge hors réseau", "it": "Off-grid End-of-Discharge/Charge", "nl": "Off-grid einde-ontlading/-lading", "pl": "Końcowy poziom rozładowania / ładowania w trybie wyspowym"},
    171: {"en": "Grid: abnormal phase difference between phase to phase"},
    181: {"en": "Abnormal insulation impedance"},
    182: {"en": "Abnormal grounding"},
    202: {"en": "Battery charge overcurrent protection", "de": "Überstromschutz beim Laden des Akkus", "es": "Battery charge overcurrent protection", "fr": "Protection contre les surintensités en cas de charge de la batterie", "it": "Battery charge overcurrent protection", "nl": "Accu-laad-overstroombeveiliging", "pl": "Zabezpieczenie przetężeniowe ładowania akumulatora"},
    203: {"en": "Battery discharge overcurrent protection", "de": "Überstromschutz beim Entladen des Akkus", "es": "Battery discharge overcurrent protection", "fr": "Protection contre les surintensités en cas de décharge de la batterie", "it": "Battery discharge overcurrent protection", "nl": "Accu-ontlaad-overstroombeveiliging", "pl": "Zabezpieczenie przetężeniowe rozładowania akumulatora"},
    204: {"en": "Battery overvoltage protection", "de": "Akkuüberspannungsschutz", "es": "Protección contra sobretensión de la batería", "fr": "Protection contre les surtensions de la batterie", "it": "Battery overvoltage protection", "nl": "Accu-overspanningsbeveiliging", "pl": "Zabezpieczenie przepięciowe akumulatora"},
    205: {"en": "Battery undervoltage protection", "de": "Akkuunterspannungsschutz", "es": "Protección contra subtensión de la batería", "fr": "Protection contre les sous-tensions de la batterie", "it": "Battery undervoltage protection", "nl": "Accu-onderspanningsbeveiliging", "pl": "Zabezpieczenie podnapięciowe akumulatora"},
    206: {"en": "High temperature protection", "de": "Hochtemperaturschutz", "es": "Protección contra altas temperaturas", "fr": "Protection contre les températures élevées", "it": "High temperature protection", "nl": "Hoge-temperatuurbeveiliging", "pl": "Ochrona przed wysoką temperaturą"},
    207: {"en": "Low temperature charge protection", "de": "Niedrigtemperatur-Ladeschutz", "es": "Protección contra la carga a baja temperatura", "fr": "Protection de charge aux basses températures", "it": "Low temperature charge protection", "nl": "Lage-temperatuuroplaadbeveiliging", "pl": "Ochrona przed ładowaniem w niskiej temperaturze"},
    208: {"en": "Low temperature discharge protection", "de": "Niedrigtemperatur-Entladeschutz", "es": "Protección contra descarga a baja temperatura", "fr": "Protection de décharge aux basses températures", "it": "Low temperature discharge protection", "nl": "Lage-temperatuur ontlaadbeveiliging", "pl": "Ochrona przed rozładowaniem w niskiej temperaturze"},
    209: {"en": "Battery port overvoltage", "de": "Überspannung am Akkuanschluss", "es": "Battery port overvoltage", "fr": "Surtension du port de la batterie", "it": "Battery port overvoltage", "nl": "Accupoort-overspanning", "pl": "Przepięcie na porcie akumulatora"},
    210: {"en": "Battery port undervoltage", "de": "Unterspannung am Akkuanschluss", "es": "Battery port undervoltage", "fr": "Sous-tension du port de la batterie", "it": "Battery port undervoltage", "nl": "Accupoort-onderspanning", "pl": "Podnapięcie na porcie akumulatora"},
    211: {"en": "Longterm DC bus overvoltage", "de": "Langfristige Überspannung am DC-Bus", "es": "Longterm DC bus overvoltage", "fr": "Surtension à long terme du bus CC", "it": "Longterm DC bus overvoltage", "nl": "Langdurige DC-busoverspanning", "pl": "Długotrwałe przepięcie magistrali DC"},
    212: {"en": "Longterm DC bus undervoltage", "de": "Langfristige Unterspannung am DC-Bus", "es": "Longterm DC bus undervoltage", "fr": "Sous-tension à long terme du bus CC", "it": "Longterm DC bus undervoltage", "nl": "Langdurige DC-busonderspanning", "pl": "Długotrwałe podnapięcie magistrali DC"},
    213: {"en": "PV1 no input", "de": "PV1 ohne Eingang", "es": "Sin entrada en el puerto FV1", "fr": "Aucune entrée PV1", "it": "PV1 no input", "nl": "Geen invoer voor PV1", "pl": "Brak wejścia PV1"},
    214: {"en": "PV2 no input", "de": "PV2 ohne Eingang", "es": "Sin entrada en el puerto FV2", "fr": "Aucune entrée PV2", "it": "PV2 no input", "nl": "Geen invoer voor PV2", "pl": "Brak wejścia PV2"},
    215: {"en": "Port 1 wiring abnormality", "de": "Anomalie in der Verkabelung von Anschluss 1", "es": "Anomalía en el cableado del puerto 1", "fr": "Anomalie de câblage du port 1", "it": "Port 1 wiring abnormality", "nl": "Poort 1 bedradingsafwijking", "pl": "Błąd okablowania portu 1"},
    216: {"en": "Port 2 wiring abnormality", "de": "Anomalie in der Verkabelung von Anschluss 2", "es": "Anomalía en el cableado del puerto 2", "fr": "Anomalie de câblage du port 2", "it": "Port 2 wiring abnormality", "nl": "Poort 2 bedradingsafwijking", "pl": "Błąd okablowania portu 2"},
    217: {"en": "PV1 input overvoltage", "de": "Eingangsüberspannung bei PV1", "es": "Sobretensión de entrada en FV1", "fr": "Surtension de l’entrée PV1", "it": "PV1 input overvoltage", "nl": "Overspanning PV1-ingang", "pl": "Nadnapięcie wejściowe PV1"},
    218: {"en": "PV1 input undervoltage", "de": "Eingangsunterspannung bei PV1", "es": "Subtensión de entrada en FV1", "fr": "Sous-tension de l’entrée PV1", "it": "PV1 input undervoltage", "nl": "Onderspanning PV1-ingang", "pl": "Podnapięcie wejściowe PV1"},
    219: {"en": "PV2 input overvoltage", "de": "Eingangsüberspannung bei PV2", "es": "Sobretensión de entrada en FV2", "fr": "Surtension de l’entrée PV2", "it": "PV2 input overvoltage", "nl": "Overspanning PV2-ingang", "pl": "Nadnapięcie wejściowe PV2"},
    220: {"en": "PV2 input undervoltage", "de": "Eingangsunterspannung bei PV2", "es": "Subtensión de entrada en FV2", "fr": "Sous-tension de l’entrée PV2", "it": "PV2 input undervoltage", "nl": "Onderspanning PV2-ingang", "pl": "Podnapięcie wejściowe PV2"},
    221: {"en": "MOS failure", "de": "MOS-Fehler", "es": "MOS failure", "fr": "Anomalie MOS", "it": "MOS failure", "nl": "MOS-storing", "pl": "Błąd MOS"},
    222: {"en": "Battery Over Temperature", "de": "Batterieübertemperatur", "es": "Battery Over Temperature", "fr": "Surchauffe batterie", "it": "Battery Over Temperature", "nl": "Accu overtemperatuur", "pl": "Nadmierna temp. akum."},
    223: {"en": "BMS Hardware Fault", "de": "BMS-Hardwarefehler", "es": "BMS Hardware Fault", "fr": "Panne matérielle du BMS", "it": "BMS Hardware Fault", "nl": "BMS-hardwarestoring", "pl": "Błąd sprzętowy BMS"},
    224: {"en": "BMS System Fault", "de": "BMS-Systemfehler", "es": "BMS System Fault", "fr": "Panne système du BMS", "it": "BMS System Fault", "nl": "BMS-systeemstoring", "pl": "Błąd systemu BMS"},
    225: {"en": "BMS Information Mismatch", "de": "BMS-Informationsfehlanpassung", "es": "BMS Information Mismatch", "fr": "Non-concordance des informations du BMS", "it": "BMS Information Mismatch", "nl": "BMS-gegevens komen niet overeen", "pl": "Niezgodność informacji BMS"},
    226: {"en": "BMS Comprehensive Protection", "de": "Umfassender BMS-Schutz", "es": "BMS Comprehensive Protection", "fr": "Protection globale du BMS", "it": "BMS Comprehensive Protection", "nl": "Uitgebreide BMS-beveiliging", "pl": "Kompleksowa ochrona BMS"},
    227: {"en": "Off-Grid Battery Overcharge Protection", "de": "Überladeschutz für netzunabhängige Batterie", "es": "Off-Grid Battery Overcharge Protection", "fr": "Protection contre la surcharge de batterie hors réseau", "it": "Off-Grid Battery Overcharge Protection", "nl": "Off-grid Accu-overlaadbeveiliging", "pl": "Zabezp. przed przeładowaniem akum. przy pracy wyspowej"},
    228: {"en": "Cell Overvoltage Protection", "de": "Zellenüberspannungsschutz", "es": "Cell Overvoltage Protection", "fr": "Protection contre la surtension des cellules", "it": "Cell Overvoltage Protection", "nl": "Cel-overspanningsbeveiliging", "pl": "Zabezp. nadnapięciowe ogniw"},
    229: {"en": "PV3 no input", "de": "PV3 ohne Eingang", "es": "Sin entrada en el puerto FV3", "fr": "Aucune entrée PV3", "it": "PV3 no input", "nl": "Geen invoer voor PV3", "pl": "Brak wejścia PV3"},
    230: {"en": "PV4 no input", "de": "PV4 ohne Eingang", "es": "Sin entrada en el puerto FV4", "fr": "Aucune entrée PV4", "it": "PV4 no input", "nl": "Geen invoer voor PV4", "pl": "Brak wejścia PV4"},
    231: {"en": "Port 3 wiring abnormality", "de": "Anomalie in der Verkabelung von Anschluss 3", "es": "Anomalía en el cableado del puerto 3", "fr": "Anomalie de câblage du port 3", "it": "Port 3 wiring abnormality", "nl": "Poort 3 bedradingsafwijking", "pl": "Błąd okablowania portu 3"},
    232: {"en": "Port 4 wiring abnormality", "de": "Anomalie in der Verkabelung von Anschluss 4", "es": "Anomalía en el cableado del puerto 4", "fr": "Anomalie de câblage du port 4", "it": "Port 4 wiring abnormality", "nl": "Poort 4 bedradingsafwijking", "pl": "Błąd okablowania portu 4"},
    233: {"en": "PV3 input overvoltage", "de": "Eingangsüberspannung bei PV3", "es": "Sobretensión de entrada en FV3", "fr": "Surtension de l’entrée PV3", "it": "PV3 input overvoltage", "nl": "Overspanning PV3-ingang", "pl": "Nadnapięcie wejściowe PV3"},
    234: {"en": "PV3 input undervoltage", "de": "Eingangsunterspannung bei PV3", "es": "Subtensión de entrada en FV3", "fr": "Sous-tension de l’entrée PV3", "it": "PV3 input undervoltage", "nl": "Onderspanning PV3-ingang", "pl": "Podnapięcie wejściowe PV3"},
    235: {"en": "PV4 input overvoltage", "de": "Eingangsüberspannung bei PV4", "es": "Sobretensión de entrada en FV4", "fr": "Surtension de l’entrée PV4", "it": "PV4 input overvoltage", "nl": "Overspanning PV4-ingang", "pl": "Nadnapięcie wejściowe PV4"},
    236: {"en": "PV4 input undervoltage", "de": "Eingangsunterspannung bei PV4", "es": "Subtensión de entrada en FV4", "fr": "Sous-tension de l’entrée PV4", "it": "PV4 input undervoltage", "nl": "Onderspanning PV4-ingang", "pl": "Podnapięcie wejściowe PV4"},
    237: {"en": "Bus Voltage Deviation Abnormality", "de": "Busspannungsabweichung anormal", "es": "Bus Voltage Deviation Abnormality", "fr": "Anomalie de l’écart de tension du bus", "it": "Bus Voltage Deviation Abnormality", "nl": "Afwijkende busspanning", "pl": "Nieprawidłowe odchylenie napięcia magistrali"},
    239: {"en": "PV1 Port Reverse Connection", "de": "PV1-Anschluss verpolt", "es": "Conexión inversa del puerto PV1", "fr": "Connexion inversée du port PV1", "it": "PV1 Port Reverse Connection", "nl": "PV1-poort omgekeerde aansluiting", "pl": "Odwrotne podłączenie portu PV1"},
    240: {"en": "PV2 Port Reverse Connection", "de": "PV2-Anschluss verpolt", "es": "Conexión inversa del puerto PV2", "fr": "Connexion inversée du port PV2", "it": "PV2 Port Reverse Connection", "nl": "PV2-poort omgekeerde aansluiting", "pl": "Odwrotne podłączenie portu PV2"},
    241: {"en": "PV3 Port Reverse Connection", "de": "PV3-Anschluss verpolt", "es": "Conexión inversa del puerto PV3", "fr": "Connexion inversée du port PV3", "it": "PV3 Port Reverse Connection", "nl": "PV3-poort omgekeerde aansluiting", "pl": "Odwrotne podłączenie portu PV3"},
    242: {"en": "PV4 Port Reverse Connection", "de": "PV4-Anschluss verpolt", "es": "Conexión inversa del puerto PV4", "fr": "Connexion inversée du port PV4", "it": "PV4 Port Reverse Connection", "nl": "PV4-poort omgekeerde aansluiting", "pl": "Odwrotne podłączenie portu PV4"},
    301: {"en": "Resonant cavity overcurrent protection failure", "de": "Ausfall des Überstromschutzes im Resonanzraum", "es": "Resonant cavity overcurrent protection failure", "fr": "Anomalie de la protection contre les surintensités de la cavité résonnante", "it": "Resonant cavity overcurrent protection failure", "nl": "Resonantieholte overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego wnęki rezonansowej"},
    302: {"en": "BOOSTA internal short circuit failure", "de": "BOOSTA interner Kurzschlussfehler", "es": "BOOSTA internal short circuit failure", "fr": "Anomalie de la protection contre les courts-circuits internes BOOSTA", "it": "BOOSTA internal short circuit failure", "nl": "BOOSTA interne kortsluitingsstoring", "pl": "Błąd zwarcia wewnętrznego BOOSTA"},
    303: {"en": "BOOSTB internal short circuit failure", "de": "BOOSTB interner Kurzschlussfehler", "es": "BOOSTB internal short circuit failure", "fr": "Anomalie de la protection contre les courts-circuits internes BOOSTB", "it": "BOOSTB internal short circuit failure", "nl": "BOOSTB interne kortsluitingsstoring", "pl": "Błąd zwarcia wewnętrznego BOOSTB"},
    304: {"en": "BoostA Overcurrent Protection Failure", "de": "Ausfall des BOOSTA-Überstromschutzes", "es": "BoostA Overcurrent Protection Failure", "fr": "Anomalie de la protection contre les surintensités BOOSTA", "it": "BoostA Overcurrent Protection Failure", "nl": "BOOSTA overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego BOOSTA"},
    305: {"en": "BoostB Overcurrent Protection Failure", "de": "Ausfall des BOOSTB-Überstromschutzes", "es": "BoostB Overcurrent Protection Failure", "fr": "Anomalie de la protection contre les surintensités BOOSTB", "it": "BoostB Overcurrent Protection Failure", "nl": "BOOSTB overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego BOOSTB"},
    306: {"en": "Internal short circuit failure", "de": "Ausfall des internen Kurzschlusses", "es": "Internal short circuit failure", "fr": "Anomalie de la protection interne contre les courts-circuits", "it": "Internal short circuit failure", "nl": "Interne kortsluitingsstoring", "pl": "Błąd zwarcia wewnętrznego"},
    307: {"en": "Battery open current failure", "de": "Akku-Ruhestromfehler", "es": "Battery open current failure", "fr": "Anomalie de la protection contre les courants ouverts de la batterie", "it": "Battery open current failure", "nl": "Accu open stroomstoring", "pl": "Błąd obwodu otwartego akumulatora"},
    308: {"en": "Overcurrent protection failure", "de": "Ausfall des Überstromschutzes", "es": "Overcurrent protection failure", "fr": "Anomalie de la protection contre les surintensités", "it": "Overcurrent protection failure", "nl": "Overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego"},
    309: {"en": "DC bus short circuit failure", "de": "Ausfall des DC-Bus-Kurzschlusses", "es": "DC bus short circuit failure", "fr": "Anomalie de la protection contre les courts-circuits du bus CC", "it": "DC bus short circuit failure", "nl": "DC-bus kortsluitingsstoring", "pl": "Błąd zwarcia magistrali DC"},
    310: {"en": "Battery current limiting failure", "de": "Akku-Strombegrenzungsfehler", "es": "Battery current limiting failure", "fr": "Anomalie du limiteur du courant de la batterie", "it": "Battery current limiting failure", "nl": "Accustroombegrenzingsstoring", "pl": "Błąd ograniczenia natężenia akumulatora"},
    311: {"en": "BMS lock-up alarm", "de": "BMS-Überbrückungsalarm", "es": "Alarma de bloqueo de BMS", "fr": "Alarme de blocage du BMS", "it": "BMS lock-up alarm", "nl": "BMS-vergrendelingsalarm", "pl": "Błąd blokady BMS"},
    312: {"en": "Severe Battery Undervoltage", "de": "Erhebliche Batterieunterspannung", "es": "Subtensión grave de la batería", "fr": "Sous-tension grave de la batterie", "it": "Severe Battery Undervoltage", "nl": "Ernstige Accu-onderspanning", "pl": "Poważne podnapięcie akum."},
    313: {"en": "DCI Protection Failure", "de": "DCI-Schutzfehler", "es": "DCI Protection Failure", "fr": "Défaillance de la protection DCI", "it": "DCI Protection Failure", "nl": "DCI-beveiligingsstoring", "pl": "Błąd zabezp. DCI"},
    314: {"en": "Off-Grid Battery Overcharge Protection Failure", "de": "Fehler im Überladeschutz der netzunabhängigen Batterie", "es": "Falla de protección contra sobrecarga de la batería fuera de la red", "fr": "Échec de la protection contre la surcharge de batterie hors réseau", "it": "Off-Grid Battery Overcharge Protection Failure", "nl": "Off-grid Accu-overlaadbeveiligingsstoring", "pl": "Błąd zabezp. przed przeładowaniem akum. przy pracy wyspowej"},
    315: {"en": "Heating MOS Failure", "de": "Fehler bei Heizungs-MOS", "es": "Heating MOS Failure", "fr": "Défaillance du MOS de chauffage", "it": "Heating MOS Failure", "nl": "Verwarming MOS-storing", "pl": "Błąd MOS grzania"},
    316: {"en": "Bus Establishment Failure", "de": "Fehler bei Bus-Einrichtung", "es": "Bus Establishment Failure", "fr": "Échec de l’établissement du bus", "it": "Bus Establishment Failure", "nl": "Busopbouwstoring", "pl": "Błąd ustanowienia magistrali"},
    317: {"en": "Battery Current Out of Control", "de": "Batteriestrom außer Kontrolle", "es": "Battery Current Out of Control", "fr": "Courant de batterie incontrôlé", "it": "Battery Current Out of Control", "nl": "Accustroom Onbeheersbaar", "pl": "Prąd akumulatora poza kontrolą"},
    318: {"en": "BOOSTC internal short circuit failure", "de": "BOOSTC interner Kurzschlussfehler", "es": "BOOSTC internal short circuit failure", "fr": "Anomalie de la protection contre les courts-circuits internes BOOSTC", "it": "BOOSTC internal short circuit failure", "nl": "BOOSTC interne kortsluitingsstoring", "pl": "Błąd zwarcia wewnętrznego BOOSTC"},
    319: {"en": "BOOSTD internal short circuit failure", "de": "BOOSTD interner Kurzschlussfehler", "es": "BOOSTD internal short circuit failure", "fr": "Anomalie de la protection contre les courts-circuits internes BOOSTD", "it": "BOOSTD internal short circuit failure", "nl": "BOOSTD interne kortsluitingsstoring", "pl": "Błąd zwarcia wewnętrznego BOOSTD"},
    320: {"en": "BoostC Overcurrent Protection Failure", "de": "Ausfall des BOOSTC-Überstromschutzes", "es": "BoostC Overcurrent Protection Failure", "fr": "Anomalie de la protection contre les surintensités BOOSTC", "it": "BoostC Overcurrent Protection Failure", "nl": "BOOSTC overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego BOOSTC"},
    321: {"en": "BoostD Overcurrent Protection Failure", "de": "Ausfall des BOOSTD-Überstromschutzes", "es": "BoostD Overcurrent Protection Failure", "fr": "Anomalie de la protection contre les surintensités BOOSTD", "it": "BoostD Overcurrent Protection Failure", "nl": "BOOSTD overstroombeveiligingsstoring", "pl": "Błąd zabezpieczenia przetężeniowego BOOSTD"},
    322: {"en": "Stacked Terminal Temperature Failure", "de": "Stacked Terminal Temperature Failure", "es": "Stacked Terminal Temperature Failure", "fr": "Stacked Terminal Temperature Failure", "it": "Stacked Terminal Temperature Failure", "nl": "Stacked Terminal Temperature Failure", "pl": "Stacked Terminal Temperature Failure"},
    400: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    401: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    402: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    403: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    404: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    405: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    406: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    407: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    408: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    409: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    410: {"en": "Abnormal electrical connection", "de": "Anormaler elektrischer Anschluss", "es": "Conexión eléctrica anormal", "fr": "Connexion électrique anormale", "it": "Abnormal electrical connection", "nl": "Abnormale elektrische aansluiting", "pl": "Nieprawidłowe przyłącze elektryczne"},
    411: {"en": "System Networking failure", "de": "Ausfall der Systemvernetzung", "es": "Falló la conexión de red del sistema", "fr": "Panne de mise en réseau du système", "it": "System Networking failure", "nl": "Systeemnetwerkstoring", "pl": "Awaria sieci systemu"},
    412: {"en": "Abnormal master-slave communication", "de": "Anormale Master-Slave-Kommunikation", "es": "Comunicación maestro-esclavo anormal", "fr": "Communication maître-esclave anormale", "it": "Abnormal master-slave communication", "nl": "Abnormale master-slavecommunicatie", "pl": "Błąd komunikacji urządzenia głównego i podrzędnego"},
    413: {"en": "Software Version Mismatch", "de": "Softwareversion stimmt nicht überein", "es": "Incompatibilidad de versión de software", "fr": "Version du logiciel non conforme", "it": "Software Version Mismatch", "nl": "Mismatchsoftwareversie", "pl": "Niezgodność wersji oprogramowania"},
    414: {"en": "Abnormal master-slave system configuration", "de": "Abnormale Master-Slave-Systemkonfiguration", "es": "Configuración anormal del sistema maestro-esclavo", "fr": "Configuration système maître-esclave anormale", "it": "Abnormal master-slave system configuration", "nl": "Abnormale master-slavesysteemconfiguratie", "pl": "Nieprawidłowa konfiguracja systemu urz. główne-urz. podrzędne"},
    415: {"en": "Abnormal master-slave file configuration", "de": "Abnormale Master-Slave-Dateikonfiguration", "es": "Configuración anormal del archivo maestro-esclavo", "fr": "Configuration de fichier maître-esclave anormale", "it": "Abnormal master-slave file configuration", "nl": "Abnormale master-slavebestandsconfiguratie", "pl": "Nieprawidłowa konfiguracja pliku urz. główne-urz. podrzędne"},
    416: {"en": "Phase Setting Abnormality of the polyphase system", "de": "Phaseneinstellung des Mehrphasensystems anormal", "es": "Anomalía en la configuración de fase del sistema polifásico", "fr": "Anomalie de réglage de phase dans un système polyphasé", "it": "Phase Setting Abnormality of the polyphase system", "nl": "Fase-instellingsafwijking van het polyfasesysteem", "pl": "Nieprawidłowe ustawienie faz w układzie wielofazowym"},
    417: {"en": "Meter Addition Abnormality of the polyphase system", "de": "Zähleraddition des Mehrphasensystems anormal", "es": "Anomalía en la conexión del medidor del sistema polifásico", "fr": "Anomalie d’ajout de compteur dans un système polyphasé", "it": "Meter Addition Abnormality of the polyphase system", "nl": "Metertoevoegingsafwijking van het polyfasesysteem", "pl": "Nieprawidłowe dodanie licznika w układzie wielofazowym"},
    420: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    421: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    422: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    423: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    424: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    425: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    426: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    427: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    428: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    429: {"en": "Sys Comm Abnormality", "de": "Systemkommunikation anormal", "es": "Anomalía en la com. del sist.", "fr": "Anomalie comm. système", "it": "Sys Comm Abnormality", "nl": "Sys Comm-afwijking", "pl": "Nieprawidłowa kom. systemu"},
    501: {"en": "Partial Alarm Clearance", "de": "Teilweise Alarmlöschung", "es": "Partial Alarm Clearance", "fr": "Réinitialisation partielle d’alarme", "it": "Partial Alarm Clearance", "nl": "Gedeeltelijke Alarmopheffing", "pl": "Częściowe usunięcie alarmu"},
    1055: {"en": "Fan Abnormity", "de": "Lüfterstörung", "es": "Fan Abnormity", "fr": "Anomalie du ventilateur", "it": "Fan Abnormity", "nl": "Ventilatorafwijking", "pl": "Nieprawidłowe działanie wentylatora"},
    1111: {"en": "Repeater"},
    1202: {"en": "Software Version Mismatch", "de": "Softwareversion stimmt nicht überein", "es": "Incompatibilidad de versión de software", "fr": "Version du logiciel non conforme", "it": "Software Version Mismatch", "nl": "Mismatchsoftwareversie", "pl": "Niezgodność wersji oprogramowania"},
    1409: {"en": "Heating film short-circuit failure", "de": "Kurzschlussfehler der Heizfolie", "es": "Falla por cortocircuito de la película calefactora", "fr": "Défaillance due à un court-circuit du film chauffant", "it": "Heating film short-circuit failure", "nl": "Kortsluitingsstoring van de verwarmingsfolie", "pl": "Błąd zwarcia folii grzewczej"},
    2000: {"en": "Standby"},
    2001: {"en": "Standby"},
    2002: {"en": "Standby"},
    2003: {"en": "Standby"},
    2004: {"en": "Standby"},
    2101: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2102: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2103: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2104: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2105: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2106: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2107: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2108: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2109: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    2110: {"en": "Smart Device Offline", "de": "Intelligentes Gerät offline", "es": "Dispositivo inteligente fuera de línea", "fr": "Appareil intelligent hors ligne", "it": "Smart Device Offline", "nl": "Smart-apparaat is Offline", "pl": "Intel. urządz. offline"},
    3001: {"en": "Reset"},
    3002: {"en": "Reset"},
    3003: {"en": "Reset"},
    3004: {"en": "Reset"},
    5011: {"en": "PV-1: MOSFET overcurrent (II)"},
    5012: {"en": "PV-2: MOSFET overcurrent (II)"},
    5013: {"en": "PV-3: MOSFET overcurrent (II)"},
    5014: {"en": "PV-4: MOSFET overcurrent (II)"},
    5020: {"en": "H-bridge MOSFET overcurrent or H-bridge overvoltage"},
    5041: {"en": "PV-1: overcurrent (II)"},
    5042: {"en": "PV-2: overcurrent (II)"},
    5043: {"en": "PV-3: overcurrent (II)"},
    5044: {"en": "PV-4: overcurrent (II)"},
    5051: {"en": "PV-1: overvoltage/undervoltage"},
    5052: {"en": "PV-2: overvoltage/undervoltage"},
    5053: {"en": "PV-3: overvoltage/undervoltage"},
    5054: {"en": "PV-4: overvoltage/undervoltage"},
    5060: {"en": "Abnormal bias"},
    5070: {"en": "Over temperature protection"},
    5080: {"en": "Grid overvoltage/undervoltage"},
    5090: {"en": "Grid overfrequency/underfrequency"},
    5100: {"en": "Island detected"},
    5110: {"en": "GFDI failure"},
    5120: {"en": "EEPROM reading and writing error"},
    5141: {"en": "FB clamp overvoltage"},
    5142: {"en": "FB clamp overvoltage"},
    5143: {"en": "FB clamp overvoltage"},
    5144: {"en": "FB clamp overvoltage"},
    5150: {"en": "10 min value grid overvoltage"},
    5160: {"en": "Grid transient fluctuation"},
    5200: {"en": "Firmware error"},
    5511: {"en": "PV-1: MOSFET overcurrent-H"},
    5512: {"en": "PV-2: MOSFET overcurrent-H"},
    5513: {"en": "PV-3: MOSFET overcurrent-H"},
    5514: {"en": "PV-4: MOSFET overcurrent-H"},
    5520: {"en": "H-bridge MOSFET overcurrent or H-bridge overvoltage"},
    8310: {"en": "Shut down by remote control"},
    8320: {"en": "Locked by remote control"},
    9000: {"en": "Microinverter is suspected of being stolen"},
}


def _decode_warning(code: int, lang: str = "en") -> str:
    if not code:
        return "OK"
    entry = _WARNING_MESSAGES.get(code)
    if entry is None:
        return f"Warning {code}"
    return entry.get(lang) or entry.get("en") or f"Warning {code}"


_UNSET = object()  # sentinel distinguishing "no value passed" from an explicit None


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
        await self._ensure_structural_caps()
        try:
            # Best-effort — a bad first poll must not block on_init() from
            # completing, or the device would never start its recurring
            # poll loop below.
            await self._poll()
        except Exception as e:
            self.log(f"Initial poll failed (will retry via poll loop): {type(e).__name__}: {e}")
        self._start_polling()
        await self._sync_detail_caps("showPvDetail", self._pv_detail_caps())
        await self._sync_detail_caps("showAcDetail", self._ac_detail_caps())

    async def on_settings(self, old_settings, new_settings, changed_keys) -> None:
        if any(k in changed_keys for k in ("ip", "is_encrypted", "enc_rand")):
            await self._build_dtu()
        if any(k in changed_keys for k in (
            "polling_interval", "solar_latitude", "solar_longitude"
        )):
            self._start_polling()
        if "showPvDetail" in changed_keys:
            await self._sync_detail_caps("showPvDetail", self._pv_detail_caps(), new_settings.get("showPvDetail"))
        if "showAcDetail" in changed_keys:
            await self._sync_detail_caps("showAcDetail", self._ac_detail_caps(), new_settings.get("showAcDetail"))

    def _pv_detail_caps(self) -> dict[str, tuple[str, dict]]:
        """{capability_id: (title_key, title_kwargs)} for per-panel Voltage/Current."""
        panel_count = int(self.get_setting("panel_count") or 0)
        caps: dict[str, tuple[str, dict]] = {}
        for i in range(1, panel_count + 1):
            caps[f"measure_voltage.pv{i}"] = ("Panel Voltage", {"i": i})
            caps[f"measure_current.pv{i}"] = ("Panel Current", {"i": i})
        return caps

    def _ac_detail_caps(self) -> dict[str, tuple[str, dict]]:
        """{capability_id: (title_key, title_kwargs)} for the AC connection Voltage/Current."""
        if bool(self.get_setting("three_phase")):
            return {
                f"measure_voltage.phase_{ph}": ("Voltage Phase", {"label": label})
                for ph, label in (("a", "A"), ("b", "B"), ("c", "C"))
            } | {
                f"measure_current.phase_{ph}": ("Current Phase", {"label": label})
                for ph, label in (("a", "A"), ("b", "B"), ("c", "C"))
            }
        return {
            "measure_voltage": ("AC Voltage", {}),
            "measure_current": ("AC Current", {}),
        }

    async def _sync_detail_caps(self, setting_id: str, cap_titles: dict[str, tuple[str, dict]], value=_UNSET) -> None:
        """Add/remove a group of optional detail capabilities to match a device
        setting — no re-pairing needed.

        `value`, when given, is used as-is (the fresh value from on_settings'
        new_settings — self.get_setting() can still return the *previous*
        value at the moment on_settings fires, since Homey calls this hook
        before persisting the change; reading it there would react one
        toggle behind). When omitted (on_init, no settings-change event to
        read from), falls back to self.get_setting().
        """
        raw = self.get_setting(setting_id) if value is _UNSET else value
        # Devices paired before this setting existed have no stored value for
        # it — get_setting() returns None. Treat that as "show" (matches the
        # driver.compose.json default of true), not "hide": these capabilities
        # must never disappear from an existing device unless the user
        # explicitly unchecks the setting themselves.
        show = True if raw is None else bool(raw)
        for cap, (title_key, title_kwargs) in cap_titles.items():
            try:
                if show and not self.has_capability(cap):
                    await self.add_capability(cap)
                    # addCapability() at runtime doesn't inherit the per-device
                    # title set at pairing — it falls back to the capability's
                    # generic built-in title (every measure_voltage.* tile
                    # just says "Voltage"). Restore the specific one.
                    try:
                        await self.set_capability_options(cap, {"title": capability_title(title_key, **title_kwargs)})
                    except Exception as e:
                        _LOGGER.warning(f"Set title for {cap} failed: {e}")
                    self.log(f"Added detail capability {cap} ({setting_id})")
                elif not show and self.has_capability(cap):
                    await self.remove_capability(cap)
                    self.log(f"Removed detail capability {cap} ({setting_id})")
            except Exception as e:
                _LOGGER.warning(f"Sync detail capability {cap} ({setting_id}) failed: {e}")

    async def on_deleted(self) -> None:
        self._stop_polling()

    async def _ensure_structural_caps(self) -> None:
        """Add capabilities introduced after this device was originally paired.

        Only adds, never removes — safe to run on every init. Hybrid devices
        don't get fault_description: _poll_hybrid() aggregates warning_number
        across multiple inverter_sns, so there's no single code to decode.
        """
        if bool(self.get_setting("is_hybrid")):
            return
        if not self.has_capability("fault_description"):
            try:
                await self.add_capability("fault_description")
                self.log("Added missing fault_description cap (recovered from pre-1.1.22 pairing)")
            except Exception as e:
                _LOGGER.warning(f"Could not add fault_description: {e}")
                return
        # Runs every init, not just right after adding — a device that got
        # the capability from an earlier version of this method (before the
        # icon existed) would otherwise never pick it up, since the
        # `not has_capability(...)` branch above only runs once. Idempotent
        # and cheap, so no harm in re-applying it on an already-correct device.
        try:
            await self.set_capability_options(
                "fault_description", {"icon": f"{_ASSETS}/fault_alarm_detail.svg"}
            )
        except Exception as e:
            _LOGGER.warning(f"Set icon for fault_description failed: {e}")

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
        await self._set("fault_description",   _decode_warning(warn_raw, self._lang()))

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
        await self._set("fault_description",       _decode_warning(warn_raw, self._lang()))

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

    def _lang(self) -> str:
        """Homey's active 2-char language code, e.g. 'en', 'nl' — used to pick
        the right translation for fault_description. Falls back to English
        if the API call fails for any reason (must never break a poll)."""
        try:
            return self.homey.i18n.get_language() or "en"
        except Exception:
            return "en"

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
