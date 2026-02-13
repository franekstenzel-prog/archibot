from __future__ import annotations

import os
import json
import hmac
import time
import html
import base64
import hashlib
import secrets
import re
import datetime
import ssl
import socket
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware

# Optional deps – app ma działać bez konfiguracji
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

try:
    import stripe
except Exception:
    stripe = None  # type: ignore


# =========================
# 0) KONFIG – ENV (Render)
# =========================

APP_NAME = "ArchiBot"
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# Base URL (Render): https://archibot.onrender.com
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Sesje (Render ENV ma: SESSION_SECRET)
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

# Email (Gmail SMTP często NIE działa na hostingu przez blokadę egress SMTP)
BOT_EMAIL = os.getenv("BOT_EMAIL", "twoj.bot.architektoniczny@gmail.com").strip()
# App password od Google może mieć spacje -> usuwamy
BOT_EMAIL_PASSWORD = (os.getenv("BOT_EMAIL_PASSWORD", "") or "").strip().replace(" ", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Email przez HTTPS API (ZALECANE na Render) – Resend
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM = os.getenv("RESEND_FROM", "").strip()  # np. "ArchiBot <onboarding@resend.dev>" albo domena po weryfikacji

# Stripe (Render ENV ma: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID_MONTHLY, STRIPE_PRICE_ID_YEARLY)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY", "").strip()
STRIPE_PRICE_ID_YEARLY = os.getenv("STRIPE_PRICE_ID_YEARLY", "").strip()

# DEV bypass (Render ENV ma: DEV_BYPASS_SUBSCRIPTION)
DEV_BYPASS_SUBSCRIPTION = (os.getenv("DEV_BYPASS_SUBSCRIPTION", "false").lower() in ("1", "true", "yes", "y", "on"))


# =========================
# 1) KOSZT BUDOWY (V1: tabela założeń) – (pozostawiam bez zmian)
# =========================

BUILD_COST_M2_PLN = {
    "Ekonomiczny": 4500,
    "Standard": 6000,
    "Premium": 8000,
}

REGION_MULTIPLIER = {
    "Duże miasto (Warszawa/Kraków/Wrocław/Gdańsk/Poznań)": 1.15,
    "Miasto 100k+": 1.05,
    "Mniejsze miasto / okolice": 1.00,
    "Wieś": 0.95,
}



# =========================
# 1B) AI – Structured Outputs schema (raport przemysłowy, uporządkowany)
# =========================

# Wymusza porządek odpowiedzi: AI zwraca JSON wg schematu, a aplikacja renderuje raport deterministycznie.
REPORT_SCHEMA = {
    "name": "industrial_architect_report_v1",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "meta": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_name": {"type": "string"},
                    "client_company": {"type": "string"},
                    "site_location": {"type": "string"},
                },
                "required": ["project_name", "client_company", "site_location"],
            },

            # Twarde fakty z formularza (bez dopowiadania)
            "facts": {
                "type": "array",
                "minItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string"},
                        "field": {"type": "string"},
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                        "source": {"type": "string", "enum": ["client_form", "assumption"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["section", "field", "label", "value", "source", "confidence"],
                },
            },

            "questions": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "blockers": {"type": "array", "minItems": 4, "items": {"type": "string"}},
                    "important": {"type": "array", "minItems": 6, "items": {"type": "string"}},
                    "optional": {"type": "array", "minItems": 4, "items": {"type": "string"}},
                },
                "required": ["blockers", "important", "optional"],
            },

            "missing_docs": {"type": "array", "items": {"type": "string"}},

            "fee_estimate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "currency": {"type": "string", "enum": ["PLN"]},
                    "total_low_pln": {"type": "number", "minimum": 0},
                    "total_high_pln": {"type": "number", "minimum": 0},
                    "pricing_basis": {"type": "string"},
                    "calc_table": {
                        "type": "array",
                        "minItems": 4,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "item": {"type": "string"},
                                "basis": {"type": "string"},
                                "qty": {"type": "number"},
                                "unit": {"type": "string"},
                                "unit_price_pln": {"type": "number"},
                                "amount_pln": {"type": "number"},
                                "source": {"type": "string", "enum": ["pricing_text", "assumption"]},
                                "justification": {"type": "string"},
                            },
                            "required": ["item", "basis", "qty", "unit", "unit_price_pln", "amount_pln", "source", "justification"],
                        },
                    },
                    "included_scope": {"type": "array", "minItems": 4, "items": {"type": "string"}},
                    "excluded_scope": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["currency", "total_low_pln", "total_high_pln", "pricing_basis", "calc_table", "included_scope", "excluded_scope"],
            },

            "build_cost_estimate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "standard": {"type": "string"},
                    "region": {"type": "string"},
                    "unit_cost_low_pln_m2": {"type": "number", "minimum": 0},
                    "unit_cost_mid_pln_m2": {"type": "number", "minimum": 0},
                    "unit_cost_high_pln_m2": {"type": "number", "minimum": 0},
                    "total_low_pln": {"type": "number", "minimum": 0},
                    "total_mid_pln": {"type": "number", "minimum": 0},
                    "total_high_pln": {"type": "number", "minimum": 0},
                    "drivers": {"type": "array", "minItems": 5, "items": {"type": "string"}},
                },
                "required": [
                    "standard", "region",
                    "unit_cost_low_pln_m2", "unit_cost_mid_pln_m2", "unit_cost_high_pln_m2",
                    "total_low_pln", "total_mid_pln", "total_high_pln",
                    "drivers"
                ],
            },

            "risks": {
                "type": "array",
                "minItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "area": {"type": "string", "enum": ["PPOŻ", "BHP", "Technologia", "Logistyka", "Media", "Konstrukcja", "Formalne", "Środowisko"]},
                        "risk": {"type": "string"},
                        "impact": {"type": "string"},
                        "mitigation": {"type": "string"},
                        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    },
                    "required": ["area", "risk", "impact", "mitigation", "priority"],
                },
            },

            "assumptions": {"type": "array", "minItems": 6, "items": {"type": "string"}},
            "next_steps": {"type": "array", "minItems": 6, "items": {"type": "string"}},

            # Gotowy email do klienta (do skopiowania)
            "client_email": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "body_text": {"type": "string"},
                },
                "required": ["subject", "body_text"],
            },
        },
        "required": [
            "meta", "facts", "questions", "missing_docs",
            "fee_estimate", "build_cost_estimate",
            "risks", "assumptions", "next_steps",
            "client_email"
        ],
    },
}

# =========================
# 2) FORMULARZ – BUDYNKI PRZEMYSŁOWE (wersja rozszerzona)
# =========================

Field = Dict[str, Any]
Section = Tuple[str, List[Field]]

def _yn_unknown() -> List[str]:
    return ["Tak", "Nie", "Nie wiem"]

def _priority() -> List[str]:
    return ["Czas", "Budżet", "Jakość", "Elastyczność (rozbudowa/zmiana procesu)", "Zgodność / audyty (PPOŻ/BHP/ESG)"]

def _project_type() -> List[str]:
    return ["Nowy obiekt", "Rozbudowa", "Przebudowa/modernizacja", "Adaptacja istniejącego budynku", "Etapowanie (wielofazowo)"]

def _object_type() -> List[str]:
    return [
        "Hala produkcyjna",
        "Hala magazynowa",
        "Centrum logistyczne (cross-dock)",
        "Zakład przemysłowy (złożony)",
        "Hala montażowa",
        "Chłodnia",
        "Mroźnia",
        "Obiekt ATEX / strefy wybuchowe",
        "Laboratoria / R&D",
        "Inne (opisz)",
    ]

def _roof_type() -> List[str]:
    return ["Płaski", "Jednospadowy", "Dwuspadowy", "Inny", "Nie wiem"]

def _construction_pref() -> List[str]:
    return ["Stal", "Żelbet", "Prefabrykat", "Mieszana", "Nie wiem"]

def _flooring_types() -> List[str]:
    return [
        "Posadzka przemysłowa (standard)",
        "Posadzka o podwyższonej nośności",
        "Posadzka antyelektrostatyczna (ESD)",
        "Posadzka chemoodporna",
        "Posadzka spożywcza (HACCP – specjalne wykończenie)",
        "Inna / strefowana (opisz)",
    ]

def _forklift_types() -> List[str]:
    return ["Elektryczne", "Spalinowe LPG", "Diesel", "Wózki wysokiego składowania", "Wózki systemowe/VNA", "AGV/AMR", "Nie dotyczy / nie wiem"]

def _audit_standards() -> List[str]:
    return ["Brak", "ISO 9001", "ISO 14001", "ISO 45001", "HACCP", "BRC", "IFS", "GMP", "FDA", "ATEX", "Inne (opisz)"]

def _sprinkler() -> List[str]:
    return ["Wymagane", "Niewymagane", "Nie wiem", "Do potwierdzenia przez rzeczoznawcę"]

def _docks() -> List[str]:
    return ["Brak", "1–2", "3–5", "6–10", "11–20", "20+", "Nie wiem"]

def _shifts() -> List[str]:
    return ["1 zmiana", "2 zmiany", "3 zmiany", "Ruch ciągły 24/7", "Sezonowo", "Nie wiem"]

def _power_supply() -> List[str]:
    return ["Z sieci (operator)", "Własna stacja trafo", "Agregat", "UPS (krytyczne)", "Mieszane", "Nie wiem"]

def _water_sources() -> List[str]:
    return ["Sieć", "Studnia", "Mieszane", "Nie wiem"]

def _sewage() -> List[str]:
    return ["Kanalizacja sanitarna", "Zbiornik bezodpływowy", "Oczyszczalnia", "Mieszane", "Nie wiem"]

def _rainwater() -> List[str]:
    return ["Kanalizacja deszczowa", "Retencja + rozsączanie", "Zbiornik retencyjny", "Wykorzystanie technologiczne", "Nie wiem"]

def _heating() -> List[str]:
    return ["Gaz", "Pompa ciepła", "Sieć ciepłownicza", "Nagrzewnice (np. gazowe)", "Odzysk ciepła z procesu", "Elektryczne", "Nie wiem"]

def _ventilation() -> List[str]:
    return ["Grawitacyjna", "Mechaniczna", "Mechaniczna z odzyskiem", "Wentylacja technologiczna (opisz)", "Nie wiem"]

def _security_level() -> List[str]:
    return ["Standard", "Podwyższony (CCTV/KD/SSWiN)", "Wysoki (strefy krytyczne)", "Nie wiem"]

def _ownership() -> List[str]:
    return ["Własność inwestora", "W trakcie nabycia", "Dzierżawa", "Najem", "Nie wiem"]

def _soil() -> List[str]:
    return ["Piaski", "Glina", "Iły", "Nasypy", "Mieszany", "Nie wiem"]

def _groundwater() -> List[str]:
    return ["< 1 m p.p.t.", "1–2 m p.p.t.", "2–5 m p.p.t.", "> 5 m p.p.t.", "Nie wiem"]
    

def _flood() -> List[str]:
    return ["Tak", "Nie", "Nie wiem"]

def _mpzp() -> List[str]:
    return ["MPZP", "WZ", "Nie wiem", "W trakcie"]

def _access_road() -> List[str]:
    return ["Bezpośredni", "Służebność", "Droga wewnętrzna", "Nie wiem"]

def _load_zone() -> List[str]:
    return ["Brak", "Rampa", "Doki", "Rampa + doki", "Brak danych / do ustalenia"]

def _parking_req() -> List[str]:
    return ["Zgodnie z MPZP/WZ", "Minimalny", "Zwiększony (dużo pracowników)", "Nie wiem"]

def _delivery_windows() -> List[str]:
    return ["Dzień (6–18)", "Noc (18–6)", "24/7", "Sezonowo", "Nie wiem"]

def _noise() -> List[str]:
    return ["< 50 dB(A)","50-70 dB(A)", "70–80 dB(A)", "80–90 dB(A)", "> 90 dB(A)", "Punktowo/impulsowo > 90 dB(A)", "Nie wiem"]
    
def _dust() -> List[str]:
    return ["Brak", "Niskie (< 1 mg/m³)", "Średnie (1–5 mg/m³)", "Wysokie (> 5 mg/m³)", "Pyły palne/ATEX", "Nie wiem"]

def _hazards() -> List[str]:
    return ["Brak", "Chemikalia", "Materiały łatwopalne", "ATEX", "Wysokie temperatury", "Niskie temperatury", "Inne (opisz)", "Nie wiem"]

def _office_standard() -> List[str]:
    return ["Podstawowy", "Standard", "Wysoki", "Reprezentacyjny", "Nie wiem"]

def _bim() -> List[str]:
    return ["Tak (BIM wymagany)", "Opcjonalnie", "Nie", "Nie wiem"]

def _procurement() -> List[str]:
    return ["Generalny wykonawca", "Pakietowanie (branże)", "Zaprojektuj i wybuduj (D&B)", "Inwestor prowadzi przetarg", "Nie wiem"]

def _contract_model() -> List[str]:
    return ["Ryczałt", "Kosztorysowe", "GMP", "Mieszane", "Nie wiem"]

def _fire_load() -> List[str]:
    return ["Q <= 500", "500 < Q <= 1000", "1000 < Q <= 2000", "2000 < Q <= 4000", "Q > 4000", "Nie wiem"]

def _process_temp() -> List[str]:
    return ["Temperatura standardowa", "Kontrola temperatury", "Chłodnia", "Mroźnia", "Podwyższone temperatury", "Nie wiem"]


FORM_SCHEMA: List[Section] = [
    ("A. Inwestor i struktura decyzyjna", [
        {"name": "investor_company", "label": "Nazwa inwestora / spółki", "type": "text", "ph": "np. XYZ Sp. z o.o."},
        {"name": "investor_legal_form", "label": "Forma prawna", "type": "text", "ph": "np. sp. z o.o."},
        {"name": "investor_contact_name", "label": "Osoba kontaktowa (imię i nazwisko)", "type": "text"},
        {"name": "investor_contact_role", "label": "Rola / stanowisko osoby kontaktowej", "type": "text", "ph": "np. Project Manager / Dyrektor Techniczny"},
        {"name": "investor_email", "label": "Email kontaktowy", "type": "email"},
        {"name": "investor_phone", "label": "Telefon", "type": "text"},
        {"name": "decision_maker", "label": "Kto podejmuje decyzje projektowe? (opis)", "type": "textarea", "ph": "np. zarząd + centrala; akceptacje etapowe; terminy"},
        {"name": "approval_workflow", "label": "Proces akceptacji i czas decyzyjny (opis)", "type": "textarea", "ph": "np. spotkania co tydzień; akceptacje w 72h; odbiory wewnętrzne"},
        {"name": "stakeholders", "label": "Interesariusze (BHP, ppoż, technologia, IT, FM, audyt) – kto jest po stronie inwestora?", "type": "textarea"},
        {"name": "previous_projects", "label": "Doświadczenia z wcześniejszych inwestycji (co działało / co nie)", "type": "textarea"},
    ]),

    ("B. Podstawowe dane inwestycji", [
        {"name": "investment_name", "label": "Nazwa inwestycji / projekt roboczy", "type": "text"},
        {"name": "project_type", "label": "Charakter inwestycji", "type": "select", "options": _project_type()},
        {"name": "object_type", "label": "Typ obiektu (główna funkcja)", "type": "select", "options": _object_type()},
        {"name": "object_type_other", "label": "Jeśli 'Inne' – doprecyzuj typ obiektu", "type": "text"},
        {"name": "business_goal", "label": "Cel inwestycji (opis szczegółowy)", "type": "textarea", "ph": "Co ma umożliwić obiekt? jakie KPI? jakie ograniczenia?"},
        {"name": "horizon_years", "label": "Horyzont użytkowania (lata) – założenie inwestora", "type": "number", "min": 0},
        {"name": "future_expansion", "label": "Czy przewidujesz rozbudowę/etapowanie?", "type": "select", "options": ["Tak – rozbudowa w przyszłości", "Tak – etapowanie od startu", "Nie", "Nie wiem"]},
        {"name": "flexibility_priority", "label": "Elastyczność procesu / możliwość zmiany układu w przyszłości", "type": "select", "options": ["Wysoka", "Średnia", "Niska", "Nie wiem"]},
        {"name": "critical_failure", "label": "Co byłoby porażką inwestycji nawet jeśli obiekt powstanie? (opis)", "type": "textarea"},
        {"name": "must_secrets", "label": "Poufność / NDA / ograniczenia publikacji (opis)", "type": "textarea"},
    ]),

    ("C. Lokalizacja i działka", [
        {"name": "plot_address", "label": "Adres / lokalizacja / park przemysłowy", "type": "text"},
        {"name": "plot_ewidencyjny", "label": "Numer(y) działek ewidencyjnych", "type": "text"},
        {"name": "plot_pow_m2", "label": "Powierzchnia działki [m²]", "type": "number", "min": 0},
        {"name": "ownership_status", "label": "Status własności", "type": "select", "options": _ownership()},
        {"name": "plot_shape", "label": "Kształt działki (opis)", "type": "textarea", "ph": "np. nieregularna, wąska; wjazd od..."},
        {"name": "plot_slope", "label": "Ukształtowanie terenu (opis)", "type": "textarea", "ph": "płasko/spadek; kierunek spadku; niwelety jeśli znane"},
        {"name": "world_sides", "label": "Orientacja stron świata / wjazd / ekspozycja (jeśli znana)", "type": "textarea"},
        {"name": "neighbors_notes", "label": "Sąsiedztwo i potencjalne kolizje (hałas, dojazd, ograniczenia)", "type": "textarea"},
        {"name": "environmental_history", "label": "Historia terenu (zabudowa, zanieczyszczenia, rekultywacja) – opis", "type": "textarea"},
        {"name": "trees_inventory", "label": "Zieleń / drzewa do zachowania lub usunięcia – opis", "type": "textarea"},
        {"name": "site_constraints", "label": "Ograniczenia i odległości (las, linie, gazociągi, drogi, zalewy, linia brzegowa) – opis", "type": "textarea"},
        {"name": "flood_risk", "label": "Ryzyko zalewowe / podmokły teren", "type": "select", "options": _flood()},
    ]),

    ("D. Grunt i geotechnika", [
        {"name": "geotech_opinion", "label": "Opinia geotechniczna – posiadam", "type": "checkbox"},
        {"name": "soil_type", "label": "Rodzaj gruntu (jeśli znany)", "type": "select", "options": _soil()},
        {"name": "groundwater_level", "label": "Poziom wód gruntowych", "type": "select", "options": _groundwater()},
        {"name": "bearing_capacity", "label": "Nośność gruntu / problemy geotechniczne (opis)", "type": "textarea"},
        {"name": "earthworks_limits", "label": "Ograniczenia robót ziemnych (np. nasypy, skarpy, wymiana gruntu) – opis", "type": "textarea"},
        {"name": "foundation_preference", "label": "Preferencja posadowienia (jeśli jest)", "type": "select", "options": ["Ławy/stopy", "Płyta fundamentowa", "Posadowienie specjalne", "Nie wiem"]},
    ]),

    ("E. Formalności, plan miejscowy, decyzje", [
        {"name": "mpzp_or_wz", "label": "Podstawa planistyczna", "type": "select", "options": _mpzp()},
        {"name": "mpzp_wz_extract", "label": "Wypis i wyrys MPZP / decyzja WZ – posiadam", "type": "checkbox"},
        {"name": "kw_number", "label": "Numer księgi wieczystej (jeśli jest)", "type": "text"},
        {"name": "land_register_extract", "label": "Wypis z rejestru gruntów – posiadam", "type": "checkbox"},
        {"name": "right_to_dispose", "label": "Oświadczenie o prawie do dysponowania nieruchomością – posiadam", "type": "checkbox"},
        {"name": "environment_decision", "label": "Decyzja środowiskowa – posiadam / wymagana?", "type": "select", "options": ["Posiadam", "Wymagana – w trakcie", "Nie jest wymagana", "Nie wiem"]},
        {"name": "agri_exclusion", "label": "Wyłączenie z produkcji rolnej – czy dotyczy / status (opis)/ klasa gruntu", "type": "textarea"},
        {"name": "water_law_permit", "label": "Operat wodnoprawny / pozwolenie wodnoprawne - opis", "type": "textarea"},
        {"name": "heritage_protection", "label": "Ochrona konserwatorska / strefy ochrony / stanowiska archeologiczne – status (opis)", "type": "textarea"},
        {"name": "legal_constraints", "label": "Inne ograniczenia prawne (służebności, strefy, sieci) – opis", "type": "textarea"},
        {"name": "access_road", "label": "Dostęp do drogi publicznej", "type": "select", "options": _access_road()},
        {"name": "driveway_consent", "label": "Zgoda/warunki zjazdu z drogi publicznej – posiadam", "type": "checkbox"},
    ]),

    ("F. Media i przyłącza (stan i wymagania)", [
        {"name": "power_conditions", "label": "Warunki przyłączenia energii elektrycznej – posiadam", "type": "checkbox"},
        {"name": "water_conditions", "label": "Warunki przyłączenia wody – posiadam", "type": "checkbox"},
        {"name": "sewage_conditions", "label": "Warunki kanalizacji sanitarnej – posiadam", "type": "checkbox"},
        {"name": "rainwater_conditions", "label": "Warunki kanalizacji deszczowej / deszczówka – posiadam", "type": "checkbox"},
        {"name": "gas_conditions", "label": "Warunki przyłączenia gazu – posiadam (jeśli dotyczy)", "type": "checkbox"},
        {"name": "mec_conditions", "label": "Przyłącze do sieci ciepłowniczej / MEC – posiadam (jeśli dotyczy)", "type": "checkbox"},
        {"name": "power_supply", "label": "Zasilanie energią – preferencja", "type": "select", "options": _power_supply()},
        {"name": "power_kw_now", "label": "Moc przyłączeniowa – wymagana dziś [kW] (jeśli znana)", "type": "number", "min": 0},
        {"name": "power_kw_future", "label": "Moc przyłączeniowa – docelowo (rezerwa) [kW] (jeśli znana)", "type": "number", "min": 0},
        {"name": "water_solution", "label": "Źródło wody", "type": "select", "options": _water_sources()},
        {"name": "water_m3_day", "label": "Zużycie wody (szacunek) [m³/dobę] – jeśli znane", "type": "number", "min": 0},
        {"name": "sewage_solution", "label": "Ścieki sanitarne – rozwiązanie", "type": "select", "options": _sewage()},
        {"name": "tech_wastewater", "label": "Ścieki technologiczne – czy występują? (opis składu / ilości)", "type": "textarea"},
        {"name": "rainwater_handling", "label": "Wody opadowe – rozwiązanie", "type": "select", "options": _rainwater()},
        {"name": "rainwater_notes", "label": "Wody opadowe – wymagania/ograniczenia (opis)", "type": "textarea"},
        {"name": "internet_fiber", "label": "Światłowód / Internet", "type": "select", "options": ["Jest", "Brak", "Nie wiem"]},
    ]),

    ("G. Program funkcjonalny – strefy i powierzchnie", [
        {"name": "program_overview", "label": "Opis funkcji i stref (jak ma działać obiekt) – opis", "type": "textarea"},
        {"name": "usable_area_m2", "label": "Docelowa powierzchnia użytkowa (łącznie) [m²]", "type": "number", "min": 0},
        {"name": "production_area_m2", "label": "Strefa produkcji [m²] (jeśli dotyczy)", "type": "number", "min": 0},
        {"name": "warehouse_area_m2", "label": "Strefa magazynu [m²] (jeśli dotyczy)", "type": "number", "min": 0},
        {"name": "shipping_area_m2", "label": "Strefa wysyłki/kompletacji [m²] (jeśli dotyczy)", "type": "number", "min": 0},
        {"name": "office_area_m2", "label": "Biura [m²] (jeśli dotyczy)", "type": "number", "min": 0},
        {"name": "social_area_m2", "label": "Zaplecze socjalne [m²] (szatnie, jadalnia, sanitariaty)", "type": "number", "min": 0},
        {"name": "tech_area_m2", "label": "Pomieszczenia techniczne [m²] (rozdzielnia, sprężarkownia itp.)", "type": "number", "min": 0},
        {"name": "special_zones", "label": "Strefy specjalne (laboratoria, clean room, chłodnia, ATEX) – opis", "type": "textarea"},
        {"name": "mezzanine", "label": "Antresola / piętro technologiczne – czy przewidujesz? (opis)", "type": "textarea"},
    ]),

    ("H. Proces technologiczny (to determinuje projekt)", [
        {"name": "process_description", "label": "Opis procesu krok po kroku (wejście materiałów → proces → wyjście) – opis", "type": "textarea"},
        {"name": "materials_in", "label": "Materiały wejściowe: rodzaj, ilości, forma dostaw (opis)", "type": "textarea"},
        {"name": "products_out", "label": "Produkty wyjściowe: rodzaj, ilości, forma wysyłek (opis)", "type": "textarea"},
        {"name": "process_temp", "label": "Wymagania temperaturowe procesu", "type": "select", "options": _process_temp()},
        {"name": "process_humidity", "label": "Wymagania dot. wilgotności (jeśli dotyczy) – opis", "type": "textarea"},
        {"name": "noise_level", "label": "Hałas procesu (szacunek)", "type": "select", "options": _noise()},
        {"name": "dust_level", "label": "Pylenie / emisje (szacunek)", "type": "select", "options": _dust()},
        {"name": "hazards", "label": "Zagrożenia (chemikalia, ATEX, łatwopalne, temperatury) – wybierz", "type": "select", "options": _hazards()},
        {"name": "hazards_notes", "label": "Zagrożenia – szczegóły (substancje, ilości, SDS/MSDS, klasy) – opis", "type": "textarea"},
        {"name": "equipment_list", "label": "Urządzenia / linie technologiczne (lista + gabaryty + masa + media) – opis", "type": "textarea"},
        {"name": "process_changes", "label": "Czy proces może się zmieniać (modernizacje / nowe linie)? – opis", "type": "textarea"},
        {"name": "downtime_constraints", "label": "Ograniczenia przestojów (ciągłość pracy, redundancje) – opis", "type": "textarea"},
    ]),

    ("I. Wymiary hali i parametry przestrzenne", [
        {"name": "building_length_m", "label": "Długość budynku [m] (jeśli znana)", "type": "number", "min": 0},
        {"name": "building_width_m", "label": "Szerokość budynku [m] (jeśli znana)", "type": "number", "min": 0},
        {"name": "clear_height_m", "label": "Wysokość do spodu konstrukcji (clear height) [m] – jeśli znana", "type": "number", "min": 0},
        {"name": "building_height_m", "label": "Wysokość całkowita budynku [m] (jeśli wymagana/znana)", "type": "number", "min": 0},
        {"name": "storeys", "label": "Kondygnacje (opis)", "type": "textarea", "ph": "np. hala 1 kond., biura 2 kond."},
        {"name": "roof_type", "label": "Typ dachu", "type": "select", "options": _roof_type()},
        {"name": "roof_area_m2", "label": "Szacowana powierzchnia dachu [m²] (jeśli znana)", "type": "number", "min": 0},
        {"name": "daylight", "label": "Doświetlenie: okna / świetliki / pasma – wymagania (opis)", "type": "textarea"},
        {"name": "crane_needed", "label": "Suwnica – czy przewidujesz?", "type": "select", "options": ["Tak", "Nie", "Nie wiem"]},
        {"name": "crane_params", "label": "Suwnica – parametry (udźwig, rozpiętość, wysokość podnoszenia, ilość) – opis", "type": "textarea"},
    ]),

    ("J. Posadzki, obciążenia, regały", [
        {"name": "flooring_type", "label": "Typ posadzki (preferencja)", "type": "select", "options": _flooring_types()},
        {"name": "floor_load_kn_m2", "label": "Obciążenia na posadzkę [kN/m²] (jeśli znane)", "type": "number", "min": 0},
        {"name": "point_loads", "label": "Obciążenia skupione (maszyny/regaty/słupy) – opis", "type": "textarea"},
        {"name": "racking_system", "label": "System składowania (regały, automatyka, wysokości) – opis", "type": "textarea"},
        {"name": "floor_flatness", "label": "Wymagana płaskość posadzki (FF/FL / VNA) – opis", "type": "textarea"},
        {"name": "internal_plinths", "label": "Cokoły wewnętrzne / odboje / zabezpieczenia ścian – opis", "type": "textarea"},
    ]),

    ("K. Bramy, doki, rampy, komunikacja logistyczna", [
        {"name": "loading_zone", "label": "Strefa załadunku/rozładunku", "type": "select", "options": _load_zone()},
        {"name": "docks_count", "label": "Liczba doków", "type": "select", "options": _docks()},
        {"name": "dock_notes", "label": "Doki – typy, wymagania, obciążenia, wyposażenie (opis)", "type": "textarea"},
        {"name": "ramps", "label": "Rampy – czy wymagane? (opis)", "type": "textarea"},
        {"name": "gates_types", "label": "Bramy – typy (segmentowe/rolowane/przesuwne/szybkobieżne) – opis", "type": "textarea"},
        {"name": "gates_dimensions", "label": "Bramy – wymiary (światło przejazdu) i ilość – opis", "type": "textarea"},
        {"name": "delivery_windows", "label": "Okna czasowe dostaw", "type": "select", "options": _delivery_windows()},
        {"name": "truck_yard", "label": "Plac manewrowy (TIR) – wymagania, ilość stanowisk, promienie – opis", "type": "textarea"},
        {"name": "internal_transport", "label": "Transport wewnętrzny (wózki/AGV/suwnice) – opis", "type": "textarea"},
        {"name": "forklift_types", "label": "Rodzaj wózków widłowych (dominujący)", "type": "select", "options": _forklift_types()},
        {"name": "forklift_count", "label": "Liczba wózków / urządzeń transportu wewnętrznego (szacunek)", "type": "number", "min": 0},
        {"name": "pedestrian_separation", "label": "Separacja ruchu pieszego i kołowego (wymagania) – opis", "type": "textarea"},
    ]),

    ("L. Konstrukcja i obudowa", [
        {"name": "construction_pref", "label": "Preferowana technologia konstrukcyjna", "type": "select", "options": _construction_pref()},
        {"name": "column_grid", "label": "Rozstaw osi słupów / siatka konstrukcyjna (jeśli narzucona) – opis", "type": "textarea"},
        {"name": "envelope_materials", "label": "Obudowa/elewacje (płyta warstwowa, prefabrykat, inne) – opis", "type": "textarea"},
        {"name": "roof_covering", "label": "Pokrycie dachu / wymagania (membrana, płyta, klapy) – opis", "type": "textarea"},
        {"name": "thermal_requirements", "label": "Wymagania termiczne (U, szczelność, mostki) – opis", "type": "textarea"},
        {"name": "acoustic_requirements", "label": "Wymagania akustyczne (wewn./zewn.) – opis", "type": "textarea"},
        {"name": "durability_requirements", "label": "Wymagania trwałości/odporności (uderzenia, chemia, korozja) – opis", "type": "textarea"},
    ]),

    ("M. Instalacje – wymagania szczegółowe", [
        {"name": "heating", "label": "Ogrzewanie – preferowane źródło", "type": "select", "options": _heating()},
        {"name": "ventilation", "label": "Wentylacja – preferencja", "type": "select", "options": _ventilation()},
        {"name": "process_exhaust", "label": "Wyciągi/odpylanie/filtracja – wymagania (opis)", "type": "textarea"},
        {"name": "compressed_air", "label": "Sprężone powietrze – czy wymagane? parametry (opis)", "type": "textarea"},
        {"name": "compressor_room", "label": "Sprężarkownia – czy przewidujesz? lokalizacja/hałas/serwis (opis)", "type": "textarea"},
        {"name": "steam", "label": "Para technologiczna – czy wymagana? (opis)", "type": "textarea"},
        {"name": "cooling", "label": "Chłodzenie technologiczne / woda lodowa – wymagania (opis)", "type": "textarea"},
        {"name": "gas_usage", "label": "Gaz – czy używany w procesie? (opis)", "type": "textarea"},
        {"name": "water_tech", "label": "Woda technologiczna – parametry jakościowe / uzdatnianie (opis)", "type": "textarea"},
        {"name": "drain_tech", "label": "Odwodnienia technologiczne, separatory, neutralizacja (opis)", "type": "textarea"},
        {"name": "electrical_critical", "label": "Zasilanie krytyczne (UPS/agregat/redundancja) – opis", "type": "textarea"},
        {"name": "lighting_requirements", "label": "Oświetlenie (lux, strefy, automatyka, awaryjne) – opis", "type": "textarea"},
        {"name": "bms", "label": "BMS / automatyka budynkowa – zakres (opis)", "type": "textarea"},
    ]),

    ("N. PPOŻ – kluczowe dane wejściowe", [
        {"name": "fire_water_availability", "label": "Dostępność wody do gaszenia pożaru (hydranty, zbiorniki, wydajność) – opis", "type": "textarea"},
        {"name": "sprinkler", "label": "Instalacja tryskaczowa – status", "type": "select", "options": _sprinkler()},
        {"name": "fire_load", "label": "Obciążenie ogniowe Q(MJ/m²)", "type": "select", "options": _fire_load()},
        {"name": "stored_materials", "label": "Rodzaj magazynowanego materiału + ilości + sposób składowania (opis)", "type": "textarea"},
        {"name": "fire_zones", "label": "Podział na strefy pożarowe (jeśli narzucony) – opis", "type": "textarea"},
        {"name": "smoke_exhaust", "label": "Oddymianie / klapy dymowe / pasma – wymagania (opis)", "type": "textarea"},
        {"name": "fire_alarm", "label": "SSP/DSO/monitoring pożarowy – wymagania inwestora (opis)", "type": "textarea"},
        {"name": "fire_consultant", "label": "Czy inwestor ma rzeczoznawcę ppoż / standard korporacyjny? (opis)", "type": "textarea"},
    ]),

    ("O. BHP / ergonomia / ryzyka operacyjne", [
        {"name": "hse_risks", "label": "Ryzyka BHP (chemia, hałas, pyły, ruch pojazdów) – opis", "type": "textarea"},
        {"name": "ppe_zones", "label": "Strefy wymagające ŚOI / procedury wejścia – opis", "type": "textarea"},
        {"name": "emergency_procedures", "label": "Procedury awaryjne (ewakuacja, wycieki, awaria zasilania) – opis", "type": "textarea"},
        {"name": "safety_barriers", "label": "Wymagania barier/odbojów/siatek/kurtyn – opis", "type": "textarea"},
        {"name": "cleanliness", "label": "Wymagania czystości (brudna/czysta, śluzy, strefowanie) – opis", "type": "textarea"},
    ]),

    ("P. Zatrudnienie, zmiany, socjal", [
        {"name": "shifts", "label": "Tryb pracy", "type": "select", "options": _shifts()},
        {"name": "workers_total", "label": "Liczba pracowników (łącznie) – szacunek", "type": "number", "min": 0},
        {"name": "workers_per_shift", "label": "Liczba pracowników na zmianie – szacunek", "type": "number", "min": 0},
        {"name": "office_staff", "label": "Liczba pracowników biurowych – szacunek", "type": "number", "min": 0},
        {"name": "gender_split", "label": "Struktura (opcjonalnie): kobiety/mężczyźni (dla szatni/sanitariatów) – opis", "type": "textarea"},
        {"name": "social_requirements", "label": "Zaplecze socjalne (szatnie czysta/brudna, prysznice, jadalnia) – opis", "type": "textarea"},
        {"name": "office_standard", "label": "Standard części biurowej", "type": "select", "options": _office_standard()},
        {"name": "visitor_flow", "label": "Ruch gości / recepcja / strefy reprezentacyjne – opis", "type": "textarea"},
    ]),

    ("Q. IT / bezpieczeństwo / ochrona", [
        {"name": "security_level", "label": "Poziom zabezpieczeń", "type": "select", "options": _security_level()},
        {"name": "cctv", "label": "CCTV – zakres (strefy, retencja nagrań) – opis", "type": "textarea"},
        {"name": "access_control", "label": "Kontrola dostępu (KD) – zakres (strefy, uprawnienia) – opis", "type": "textarea"},
        {"name": "intrusion", "label": "SSWiN / ochrona fizyczna – opis", "type": "textarea"},
        {"name": "it_rooms", "label": "Serwerownia / teletechnika – wymagania (klima, UPS, redundancja) – opis", "type": "textarea"},
        {"name": "network_requirements", "label": "Sieć (Wi-Fi przemysłowe, IoT, OT/SCADA) – opis", "type": "textarea"},
    ]),

    ("R. Standardy, audyty, wymagania korporacyjne", [
        {"name": "audit_standards", "label": "Standardy / audyty (wybierz)", "type": "select", "options": _audit_standards()},
        {"name": "audit_notes", "label": "Wymagania audytowe i korporacyjne (materiały, procedury, układ) – opis", "type": "textarea"},
        {"name": "bim_requirement", "label": "BIM", "type": "select", "options": _bim()},
        {"name": "design_guidelines", "label": "Wytyczne inwestora (brandbook, standard obiektów) – opis", "type": "textarea"},
    ]),

    ("S. Zewnętrzne zagospodarowanie terenu", [
        {"name": "parking_policy", "label": "Parkingi – założenia", "type": "select", "options": _parking_req()},
        {"name": "parking_counts", "label": "Liczba miejsc: osobowe / ciężarowe / rowery (jeśli znane) – opis", "type": "textarea"},
        {"name": "roads_internal", "label": "Drogi wewnętrzne, place, nawierzchnie – wymagania (opis)", "type": "textarea"},
        {"name": "fence", "label": "Ogrodzenie – czy wymagane?", "type": "select", "options": ["Tak", "Nie", "Nie wiem"]},
        {"name": "gatehouse", "label": "Portiernia / wjazd kontrolowany – wymagania (opis)", "type": "textarea"},
        {"name": "stormwater_retention", "label": "Retencja / zbiorniki / zrzut deszczówki – wymagania (opis)", "type": "textarea"},
        {"name": "external_storage", "label": "Składowanie zewnętrzne (kontenery, odpady, palety) – opis", "type": "textarea"},
        {"name": "waste_management", "label": "Gospodarka odpadami (rodzaje, ilości, lokalizacja, segregacja) – opis", "type": "textarea"},
    ]),

    ("T. Budżet, harmonogram, ryzyka", [
        {"name": "budget_total", "label": "Budżet całej inwestycji [PLN] (jeśli jest)", "type": "number", "min": 0},
        {"name": "budget_build_only", "label": "Budżet budowy (bez gruntu) [PLN] (jeśli jest)", "type": "number", "min": 0},
        {"name": "timeline_start", "label": "Planowany start prac (projekt/budowa) – opis", "type": "textarea"},
        {"name": "timeline_deadline", "label": "Termin oddania / uruchomienia (czy jest krytyczny?) – opis", "type": "textarea"},
        {"name": "priority", "label": "Priorytet projektu", "type": "select", "options": _priority()},
        {"name": "risk_register", "label": "Ryzyka zidentyfikowane przez inwestora (formalno-prawne, techniczne, operacyjne) – opis", "type": "textarea"},
        {"name": "no_go", "label": "Warunki 'no-go' (czego nie wolno przekroczyć) – opis", "type": "textarea"},
        {"name": "unknowns", "label": "Obszary nieustalone (co wymaga doprecyzowania) – opis", "type": "textarea"},
        {"name": "must_have", "label": "Must-have (wymagania bezwzględne) – opis", "type": "textarea"},
        {"name": "nice_to_have", "label": "Nice-to-have (mile widziane) – opis", "type": "textarea"},
        {"name": "dont_want", "label": "Czego na pewno nie chcesz – opis", "type": "textarea"},
    ]),

    ("U. Załączniki (opcjonalnie)", [
        {"name": "attachments", "label": "Pliki (MPZP/WZ, mapa, geotechnika, warunki przyłączy, proces/technologia, szkice)", "type": "file", "multiple": True},
    ]),
]


# =========================
# 3) Prosta baza danych (JSON)
# =========================

def _load_db() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"companies": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"companies": {}}

def _save_db(db: Dict[str, Any]) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def _now_ts() -> int:
    return int(time.time())


# =========================
# Limity: formularze / miesiąc (per firma)
# =========================

FORMS_PER_MONTH_LIMIT = 100

# Free/Beta plan (mozesz wylaczyc w ENV: ENABLE_FREE_PLAN=false)
ENABLE_FREE_PLAN = (os.getenv("ENABLE_FREE_PLAN", "true").lower() in ("1", "true", "yes", "y", "on"))
FREE_FORMS_PER_MONTH_LIMIT = int(os.getenv("FREE_FORMS_PER_MONTH_LIMIT", "3"))

MAX_REPORTS_PER_COMPANY = int(os.getenv("MAX_REPORTS_PER_COMPANY", "50"))

PLAN_LABELS = {
    "free": "Beta 0 zł",
    "monthly": "Miesięczny",
    "yearly": "Roczny",
    "none": "Brak dostępu",
}

def _company_plan(company: dict) -> str:
    """Zwraca plan firmy. Wspiera wsteczna kompatybilnosc ze starymi rekordami."""
    p = str(company.get("plan") or "").strip().lower()
    if p in ("free", "monthly", "yearly", "none"):
        return p
    # Stare rekordy bez pola `plan`: jesli Stripe aktywny -> traktuj jako plan platny
    st = (company.get("stripe") or {}).get("status") or ""
    if st in ("active", "trialing"):
        return "monthly"
    return "free" if ENABLE_FREE_PLAN else "none"

def _forms_limit(company: dict) -> int:
    plan = _company_plan(company)
    if plan == "free":
        return FREE_FORMS_PER_MONTH_LIMIT if ENABLE_FREE_PLAN else 0
    if plan in ("monthly", "yearly"):
        return FORMS_PER_MONTH_LIMIT
    return 0

def _period_key(ts: Optional[int] = None) -> str:
    dt = datetime.datetime.utcfromtimestamp(ts or _now_ts())
    return f"{dt.year:04d}-{dt.month:02d}"

def _ensure_usage_period(company: Dict[str, Any]) -> None:
    usage = company.get("usage") or {}
    pk = _period_key()
    if usage.get("period") != pk:
        usage = {"period": pk, "forms_sent": 0}
        company["usage"] = usage

def _forms_remaining(company: Dict[str, Any]) -> int:
    _ensure_usage_period(company)
    sent = int((company.get("usage") or {}).get("forms_sent") or 0)
    limit = _forms_limit(company)
    return max(0, limit - sent)

def _increment_forms_sent(db: Dict[str, Any], company_id: str) -> None:
    c = db["companies"][company_id]
    _ensure_usage_period(c)
    c["usage"]["forms_sent"] = int(c["usage"].get("forms_sent") or 0) + 1

def _ensure_reports(company: Dict[str, Any]) -> None:
    if "reports" not in company or not isinstance(company.get("reports"), list):
        company["reports"] = []

def _pick_title_from_form(form_clean: Dict[str, Any]) -> str:
    # Próbujemy znaleźć sensowny tytuł bez zależności od konkretnego schematu pól
    keys = [
        "project_name", "investment_name", "project", "investment_title",
        "client_project", "name_of_investment",
        "investor_company", "company_name", "client_company",
        "location", "site_location",
    ]
    for k in keys:
        v = str(form_clean.get(k) or "").strip()
        if v:
            return v[:80]
    return "Brief inwestorski"

def _store_report(
    db: Dict[str, Any],
    company_id: str,
    *,
    report_text: str,
    form_clean: Dict[str, Any],
    architect: Dict[str, Any],
    delivery_id: str,
    email_sent: bool,
) -> str:
    if company_id not in db.get("companies", {}):
        return ""
    c = db["companies"][company_id]
    _ensure_reports(c)

    rid = _new_id("rep")
    title = _pick_title_from_form(form_clean)
    item = {
        "id": rid,
        "created_at": _now_ts(),
        "title": title,
        "architect_id": architect.get("id", ""),
        "architect_name": architect.get("name", ""),
        "architect_email": architect.get("email", ""),
        "delivery_id": delivery_id,
        "email_sent": bool(email_sent),
        "report": report_text,
    }

    # najnowsze na górze + twardy limit, żeby JSON nie puchł bez końca
    c["reports"] = [item] + list(c.get("reports") or [])
    c["reports"] = c["reports"][:max(1, int(MAX_REPORTS_PER_COMPANY or 50))]
    return rid


def _new_submit_token() -> str:
    return secrets.token_urlsafe(16)

def _mark_submit_token_used(db: Dict[str, Any], token: str, ttl_seconds: int = 6 * 60 * 60) -> bool:
    meta = db.setdefault("submit_tokens", {})
    now = _now_ts()
    try:
        for k, ts in list(meta.items()):
            if now - int(ts) > ttl_seconds:
                meta.pop(k, None)
    except Exception:
        pass

    if token in meta:
        return True
    meta[token] = now
    return False

def _hash_password(password: str, salt_b64: Optional[str] = None) -> str:
    salt = base64.b64decode(salt_b64) if salt_b64 else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split("$", 1)
        check = _hash_password(password, salt_b64=salt_b64).split("$", 1)[1]
        return hmac.compare_digest(check, dk_b64)
    except Exception:
        return False

def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"

def _clean_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v

def _clean_form_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in d.items():
        v2 = _clean_value(v)
        if v2 is None:
            continue
        out[k] = v2
    return out


# =========================
# 4) UI helpers
# =========================

def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)

def badge(label: str, ok: bool) -> str:
    cls = "badge ok" if ok else "badge bad"
    return f'<span class="{cls}">{esc(label)}</span>'

def layout(title: str, body: str, *, nav: str = "", request: Optional[Request] = None, page: str = "") -> str:
    """Globalny layout UI (jeden plik). Kolory pozostają zgodne z ikoną / tłem.
    `page` pozwala dodać lekkie różnice (np. home ma splash).
    """
    logged_in = False
    company_name = ""
    if request is not None:
        try:
            c = get_company(request)
            if c:
                logged_in = True
                company_name = str(c.get("name") or "")
        except Exception:
            pass

    if logged_in:
        cta = '''
          <a class="btn ghost" href="/demo">Podgląd briefu</a>
          <a class="btn" href="/dashboard">Panel</a>
          <a class="btn gold" href="/logout">Wyloguj</a>
        '''
    else:
        cta = '''
          <a class="btn ghost" href="/demo">Podgląd briefu</a>
          <a class="btn" href="/login">Zaloguj</a>
          <a class="btn gold" href="/register">Załóż konto</a>
        '''

    menu = nav or ""
    page_class = f"page-{page}" if page else ""

    return f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{esc(title)} • {APP_NAME}</title>
  <link rel="icon" href="/logo_arch.png" type="image/png"/>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #070B16;
      --panel: rgba(255,255,255,0.06);
      --panel2: rgba(255,255,255,0.09);
      --stroke: rgba(255,255,255,0.12);
      --text: #EEF2FF;
      --muted: rgba(238,242,255,0.70);
      --gold: #D6B36A;
      --gold2: #B89443;
      --danger: #ff5b5b;
      --ok: #49d17d;
      --shadow: 0 12px 40px rgba(0,0,0,0.40);
      --r: 22px;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      font-family: "Syne", system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background:
        radial-gradient(900px 480px at 18% 8%, rgba(214,179,106,0.12), transparent 55%),
        radial-gradient(920px 460px at 82% 20%, rgba(255,255,255,0.07), transparent 55%),
        radial-gradient(900px 640px at 50% 92%, rgba(214,179,106,0.09), transparent 58%),
        var(--bg);
      color: var(--text);
      overflow-x: hidden;
    }}
    body.no-scroll {{ overflow: hidden; }}
    a {{ color: inherit; text-decoration: none; }}
    .wrap {{ width: min(1120px, calc(100% - 40px)); margin: 0 auto; }}
    .topbar {{
      position: sticky; top: 0; z-index: 60;
      backdrop-filter: blur(10px);
      background: rgba(7,11,22,0.55);
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    .nav {{
      display:flex; align-items:center; justify-content:space-between;
      padding: 14px 0;
      gap: 12px;
    }}
    .brand {{
      display:flex; align-items:center; gap:10px; font-weight:800; letter-spacing: 0.2px;
      min-width: 160px;
    }}
    .logo {{
      width:34px; height:34px; border-radius: 12px;
      background: url('/logo_arch.png') center/contain no-repeat;
      box-shadow: 0 10px 30px rgba(214,179,106,0.20);
    }}
    .menu {{ display:flex; align-items:center; gap:6px; color: var(--muted); font-weight:700; flex-wrap: wrap; justify-content:center; }}
    .menu a {{ padding: 8px 10px; border-radius: 12px; }}
    .menu a:hover {{ background: rgba(255,255,255,0.06); color: var(--text); }}
    .cta {{ display:flex; align-items:center; gap:10px; flex-wrap: wrap; justify-content:flex-end; }}
    .btn {{
      display:inline-flex; align-items:center; justify-content:center;
      gap:10px;
      padding: 11px 14px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
      color: var(--text);
      font-weight: 800;
      transition: transform .15s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
      user-select:none;
      cursor:pointer;
    }}
    .btn:hover {{ transform: translateY(-1px); background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.18); }}
    .btn:active {{ transform: translateY(0); opacity: .92; }}
    .btn.gold {{
      background: linear-gradient(180deg, rgba(214,179,106,1), rgba(184,148,67,1));
      color: #0b0f1a;
      border-color: rgba(214,179,106,0.85);
      box-shadow: 0 14px 40px rgba(214,179,106,0.18);
    }}
    .btn.ghost {{ background: transparent; }}
    .badge {{ padding: 6px 10px; border-radius: 999px; font-weight: 900; font-size: 12px; border:1px solid rgba(255,255,255,0.12); }}
    .badge.ok {{ color: var(--ok); border-color: rgba(73,209,125,0.35); background: rgba(73,209,125,0.08); }}
    .badge.bad {{ color: var(--danger); border-color: rgba(255,91,91,0.35); background: rgba(255,91,91,0.08); }}
    .muted {{ color: var(--muted); font-weight: 700; }}
    .lead {{ color: rgba(238,242,255,0.82); font-weight: 750; line-height: 1.55; }}
    .panel {{
      border-radius: var(--r);
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow: 0 18px 50px rgba(0,0,0,0.28);
    }}
    .card {{ padding: 18px; }}
    .divider {{ height:1px; background: rgba(255,255,255,0.08); margin: 16px 0; }}
    .grid {{ display:grid; gap: 14px; }}
    .grid2 {{ display:grid; gap: 14px; grid-template-columns: 1fr 1fr; }}
    .grid3 {{ display:grid; gap: 14px; grid-template-columns: repeat(3, 1fr); }}
    .stat {{
      padding: 16px; border-radius: 18px; background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .k {{ color: rgba(238,242,255,0.55); font-weight: 900; letter-spacing: .12em; font-size: 12px; }}
    .n {{ font-weight: 900; font-size: 20px; margin-top: 6px; }}
    .t {{ color: var(--muted); font-weight: 750; margin-top: 6px; line-height: 1.5; }}
    .notice {{
      padding: 12px 14px; border-radius: 18px; border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.06);
    }}
    .formwrap {{ padding: 34px 0 60px; }}
    .fields {{ display:grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .field {{ display:flex; flex-direction:column; gap: 8px; }}
    .field.full {{ grid-column: 1 / -1; }}
    label {{ font-weight: 900; color: rgba(238,242,255,0.82); }}
    input, select, textarea {{
      width:100%;
      padding: 12px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(0,0,0,0.18);
      color: var(--text);
      outline: none;
      font-weight: 750;
    }}
    textarea {{ min-height: 140px; resize: vertical; }}
    input:focus, textarea:focus, select:focus {{ border-color: rgba(214,179,106,0.55); box-shadow: 0 0 0 4px rgba(214,179,106,0.10); }}
    .actions {{ display:flex; gap: 10px; flex-wrap: wrap; align-items:center; justify-content:flex-start; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .codebox {{ white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; line-height: 1.55; color: rgba(238,242,255,0.85); }}
    .tag {{ display:inline-flex; align-items:center; gap:8px; padding: 7px 10px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.06); color: rgba(238,242,255,0.78); font-weight: 900; font-size: 12px; }}
    .tag .dot {{ width: 8px; height: 8px; border-radius: 999px; background: var(--gold); box-shadow: 0 0 0 4px rgba(214,179,106,0.18); }}
    .reveal {{ opacity: 0; transform: translateY(14px); filter: blur(6px); transition: opacity .7s ease, transform .7s ease, filter .7s ease; }}
    .reveal.in {{ opacity: 1; transform: translateY(0); filter: blur(0); }}

    /* DASH */
    .dash {{
      display:grid;
      grid-template-columns: 260px 1fr;
      gap: 16px;
      align-items: start;
      padding: 26px 0 60px;
    }}
    .side {{
      position: sticky; top: 82px;
      padding: 14px;
    }}
    .side .title {{
      font-weight: 950; font-size: 14px; letter-spacing: .10em; color: rgba(238,242,255,0.55);
      margin-bottom: 10px;
    }}
    .navitem {{
      display:flex; align-items:center; justify-content:space-between;
      padding: 10px 12px;
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: rgba(238,242,255,0.78);
      font-weight: 850;
      margin: 8px 0;
    }}
    .navitem:hover {{ background: rgba(255,255,255,0.07); border-color: rgba(255,255,255,0.14); }}
    .navitem.active {{
      background: rgba(214,179,106,0.10);
      border-color: rgba(214,179,106,0.35);
      color: rgba(238,242,255,0.95);
    }}
    .main {{
      padding: 14px;
    }}
    .headrow {{
      display:flex; align-items:flex-start; justify-content:space-between; gap: 14px; flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .h1 {{ font-size: 28px; font-weight: 950; margin: 0; }}
    .sub {{ margin: 6px 0 0; }}
    .table {{
      width:100%;
      border-collapse: collapse;
      overflow:hidden;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
    }}
    .table th, .table td {{
      padding: 12px 12px;
      text-align: left;
      border-bottom: 1px solid rgba(255,255,255,0.07);
      vertical-align: top;
      font-weight: 750;
    }}
    .table th {{ color: rgba(238,242,255,0.65); font-weight: 900; font-size: 12px; letter-spacing: .10em; }}
    .table tr:last-child td {{ border-bottom: none; }}
    .pill {{
      display:inline-flex; align-items:center; gap:8px;
      padding: 6px 10px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.05);
      font-weight: 900; font-size: 12px;
    }}
    .pill.ok {{ border-color: rgba(73,209,125,0.35); background: rgba(73,209,125,0.08); color: var(--ok); }}
    .pill.bad {{ border-color: rgba(255,91,91,0.35); background: rgba(255,91,91,0.08); color: var(--danger); }}

    /* HOME SPLASH */
    .splash {{
      position: fixed; inset: 0; z-index: 9999;
      background:
        radial-gradient(900px 420px at 50% 40%, rgba(214,179,106,0.13), transparent 55%),
        radial-gradient(900px 520px at 50% 70%, rgba(255,255,255,0.06), transparent 62%),
        var(--bg);
      display:flex; align-items:center; justify-content:center;
    }}
    .splash .box {{
      width: min(920px, calc(100% - 40px));
      text-align:center;
    }}
    .splash svg {{ width: min(860px, 100%); height: 200px; }}
    .splash .hint {{
      margin-top: 10px; color: rgba(238,242,255,0.55); font-weight: 850; letter-spacing: .16em; font-size: 12px;
    }}
    .splash.hide {{ animation: splashOut .9s ease forwards; }}
    @keyframes splashOut {{
      to {{ opacity: 0; transform: scale(1.02); filter: blur(8px); pointer-events:none; }}
    }}

    /* HOME STEPS */
    .heroHome {{
      padding: 46px 0 12px;
    }}
    .heroGrid {{
      display:grid; gap: 18px;
      grid-template-columns: 1.2fr .8fr;
      align-items: start;
    }}
    .titleBig {{
      font-size: 54px;
      line-height: 1.02;
      margin: 0 0 12px;
      font-weight: 950;
      letter-spacing: -0.02em;
    }}
    .titleBig .gold {{ color: var(--gold); }}
    .heroCard {{
      padding: 16px; border-radius: var(--r);
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.05);
    }}
    .howShow {{
      padding: 26px 0 10px;
    }}
    .howShell {{
      position: relative;
      height: 340vh; /* scroll space */
    }}
    .howSticky {{
      position: sticky; top: 86px;
      border-radius: 26px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      padding: 18px;
      display:grid;
      grid-template-columns: .45fr .55fr;
      gap: 16px;
      overflow:hidden;
    }}
    .stepList .item {{
      padding: 14px 14px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      margin-bottom: 10px;
      cursor:pointer;
    }}
    .stepList .item.active {{
      border-color: rgba(214,179,106,0.40);
      background: rgba(214,179,106,0.10);
    }}
    .stepList .item .num {{ font-weight: 950; letter-spacing:.14em; color: rgba(238,242,255,0.58); font-size: 12px; }}
    .stepList .item .ttl {{ font-weight: 950; font-size: 18px; margin-top: 6px; }}
    .stepList .item .txt {{ color: rgba(238,242,255,0.72); font-weight: 750; margin-top: 6px; line-height: 1.5; }}
    .scene {{
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,0.10);
      background:
        radial-gradient(700px 340px at 30% 20%, rgba(214,179,106,0.10), transparent 55%),
        rgba(0,0,0,0.20);
      padding: 18px;
      min-height: 360px;
      position: relative;
      overflow:hidden;
    }}
    .scene .frame {{
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.05);
      padding: 14px;
      box-shadow: 0 20px 70px rgba(0,0,0,0.35);
      transform: translateY(8px);
      opacity: 0;
      transition: opacity .45s ease, transform .45s ease;
      position: absolute;
      inset: 18px;
    }}
    .scene .frame.show {{
      opacity: 1;
      transform: translateY(0);
    }}
    .frame .hdr {{
      display:flex; align-items:center; justify-content:space-between;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .dots {{ display:flex; gap: 6px; }}
    .dot {{ width:10px; height:10px; border-radius: 999px; background: rgba(255,255,255,0.16); }}
    .dot.g {{ background: rgba(214,179,106,0.50); }}
    .mini {{
      border-radius: 14px;
      border: 1px dashed rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.03);
      padding: 12px;
      color: rgba(238,242,255,0.78);
      font-weight: 750;
      line-height: 1.55;
    }}

    .foot {{
      padding: 28px 0 40px;
      color: rgba(238,242,255,0.55);
      font-weight: 800;
      border-top: 1px solid rgba(255,255,255,0.08);
      margin-top: 40px;
    }}
    .foot a {{ color: rgba(238,242,255,0.70); }}
    .foot a:hover {{ color: rgba(238,242,255,0.92); }}

    @media (max-width: 980px) {{
      .heroGrid {{ grid-template-columns: 1fr; }}
      .titleBig {{ font-size: 42px; }}
      .howSticky {{ grid-template-columns: 1fr; top: 74px; }}
      .dash {{ grid-template-columns: 1fr; }}
      .side {{ position: relative; top: 0; }}
      .grid2 {{ grid-template-columns: 1fr; }}
      .grid3 {{ grid-template-columns: 1fr; }}
      .fields {{ grid-template-columns: 1fr; }}
      .scene .frame {{ position: relative; inset: auto; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .reveal {{ opacity: 1; transform: none; filter: none; }}
      .splash.hide {{ animation: none; opacity: 0; }}
      .scene .frame {{ transition: none; }}
    }}
  </style>
</head>
<body class="{page_class}">
  <div class="topbar">
    <div class="wrap">
      <div class="nav">
        <div class="brand">
          <div class="logo"></div>
          <div style="display:flex;flex-direction:column;line-height:1">
            <div style="font-weight:950">{esc(APP_NAME)}</div>
            <div style="font-size:12px;color:rgba(238,242,255,0.55);font-weight:850">{esc(company_name) if company_name else "Brief → Raport → Wycena"}</div>
          </div>
        </div>
        <div class="menu">{menu}</div>
        <div class="cta">{cta}</div>
      </div>
    </div>
  </div>
  {body}
<script>
(() => {{
  // Reveal on scroll
  const els = Array.from(document.querySelectorAll('[data-reveal]'));
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (els.length) {{
    if (reduce || !('IntersectionObserver' in window)) {{ els.forEach(el => el.classList.add('in')); }}
    else {{
      const io = new IntersectionObserver((entries) => {{
        for (const e of entries) {{
          if (e.isIntersecting) {{ e.target.classList.add('in'); io.unobserve(e.target); }}
        }}
      }}, {{ threshold: 0.12 }});
      els.forEach(el => {{ el.classList.add('reveal'); io.observe(el); }});
    }}
  }}

  // Copy helpers
  document.addEventListener('click', async (ev) => {{
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    const btn = t.closest('[data-copy],[data-copy-text]');
    if (!btn) return;
    const sel = btn.getAttribute('data-copy');
    const el = sel ? document.querySelector(sel) : null;
    const txt = el ? (el.textContent || '') : (btn.getAttribute('data-copy-text') || '');
    try {{
      await navigator.clipboard.writeText(txt.trim());
      const old = btn.textContent || '';
      btn.textContent = 'Skopiowano';
      setTimeout(() => btn.textContent = old || 'Kopiuj', 1200);
    }} catch(e) {{
      alert('Nie udało się skopiować. Zaznacz tekst i skopiuj ręcznie.');
    }}
  }});

  // Home: splash once per session
  const splash = document.getElementById('splash');
  if (splash) {{
    const key = 'ab_splash_once_v1';
    const already = sessionStorage.getItem(key);
    const hide = () => {{
      if (splash.classList.contains('hide')) return;
      document.body.classList.remove('no-scroll');
      splash.classList.add('hide');
      setTimeout(() => splash.remove(), 920);
    }};
    if (already) {{
      splash.remove();
    }} else {{
      document.body.classList.add('no-scroll');
      sessionStorage.setItem(key, '1');
      setTimeout(hide, 2200);
      window.addEventListener('wheel', hide, {{ passive:true, once:true }});
      window.addEventListener('touchstart', hide, {{ passive:true, once:true }});
      splash.addEventListener('click', hide, {{ once:true }});
    }}
  }}

  // Home: scroll slides
  const shell = document.getElementById('howShell');
  if (shell) {{
    const frames = Array.from(document.querySelectorAll('.scene .frame'));
    const items = Array.from(document.querySelectorAll('.stepList .item'));
    const n = Math.max(frames.length, items.length);
    function setActive(i) {{
      frames.forEach((f, idx) => f.classList.toggle('show', idx===i));
      items.forEach((it, idx) => it.classList.toggle('active', idx===i));
    }}
    function clamp(v, a, b) {{ return Math.max(a, Math.min(b, v)); }}
    function onScroll() {{
      const rect = shell.getBoundingClientRect();
      const vh = window.innerHeight || 800;
      const total = rect.height - vh;
      const y = clamp(-rect.top, 0, total);
      const p = total > 0 ? y / total : 0;
      const idx = clamp(Math.floor(p * n), 0, n-1);
      setActive(idx);
    }}
    items.forEach((it, idx) => {{
      it.addEventListener('click', () => {{
        const rect = shell.getBoundingClientRect();
        const top = window.scrollY + rect.top;
        const target = top + (idx / n) * (rect.height - (window.innerHeight||800));
        window.scrollTo({{ top: target, behavior: 'smooth' }});
      }});
    }});
    window.addEventListener('scroll', onScroll, {{ passive:true }});
    window.addEventListener('resize', onScroll);
    setTimeout(onScroll, 0);
  }}
}})();
</script>
</body>
</html>
"""

def nav_links() -> str:
    return """
      <a href="/#jak">Jak działa</a>
      <a href="/#funkcje">Funkcje</a>
      <a href="/#raport">Raport</a>
      <a href="/#plany">Plany</a>
      <a href="/#faq">FAQ</a>
    """



# =========================
# 5) Render formularza
# =========================

def render_form(action_url: str, *, title: str, subtitle: str, submit_token: Optional[str] = None) -> str:
    blocks = []
    for sec_title, fields in FORM_SCHEMA:
        inner = []
        for f in fields:
            ftype = f["type"]
            name = f["name"]
            label = f["label"]
            ph = f.get("ph", "")
            multiple = bool(f.get("multiple", False))
            opts = f.get("options", [])
            minv = f.get("min")
            maxv = f.get("max")

            if ftype == "checkbox":
                inner.append(f"""
                <div class="field full">
                  <div class="checkrow">
                    <input type="checkbox" name="{esc(name)}" value="1"/>
                    <div>
                      <div style="font-weight:800">{esc(label)}</div>
                      <div style="color:rgba(238,242,255,0.60);font-weight:650;font-size:13px">Zaznacz, jeśli dotyczy lub dokument jest dostępny.</div>
                    </div>
                  </div>
                </div>
                """)
            elif ftype == "select":
                options_html = ['<option value="">— (puste) —</option>']
                for o in opts:
                    options_html.append(f'<option value="{esc(o)}">{esc(o)}</option>')
                inner.append(f"""
                <div class="field">
                  <label>{esc(label)}</label>
                  <select name="{esc(name)}">
                    {''.join(options_html)}
                  </select>
                </div>
                """)
            elif ftype == "textarea":
                inner.append(f"""
                <div class="field full">
                  <label>{esc(label)}</label>
                  <textarea name="{esc(name)}" placeholder="{esc(ph)}"></textarea>
                </div>
                """)
            elif ftype == "file":
                inner.append(f"""
                <div class="field full">
                  <label>{esc(label)}</label>
                  <input type="file" name="{esc(name)}" {'multiple' if multiple else ''}/>
                  <div class="muted">Załączniki są opcjonalne.</div>
                </div>
                """)
            else:
                extra = ""
                if minv is not None:
                    extra += f' min="{minv}"'
                if maxv is not None:
                    extra += f' max="{maxv}"'
                inner.append(f"""
                <div class="field">
                  <label>{esc(label)}</label>
                  <input type="{esc(ftype)}" name="{esc(name)}" placeholder="{esc(ph)}"{extra}/>
                </div>
                """)

        blocks.append(f"""
        <details open>
          <summary>{esc(sec_title)}</summary>
          <div class="fields">
            {''.join(inner)}
          </div>
        </details>
        """)

    return layout(
        title,
        body=f"""
        <div class="wrap formwrap">
          <h1 style="margin:0 0 12px">{esc(title)}</h1>
          <p class="lead" style="max-width:none">{esc(subtitle)}</p>
          <div style="height:14px"></div>
          <div class="notice">
            <b>Informacja:</b> pola mogą pozostać puste. Raport ma wskazać braki i ryzyka oraz przygotować listę pytań uzupełniających.
          </div>

          <form method="post" action="{esc(action_url)}" enctype="multipart/form-data" style="margin-top:16px">
            {f'<input type="hidden" name="_submit_token" value="{esc(submit_token)}"/>' if submit_token else ""}
            {''.join(blocks)}
            <div class="actions">
              <button class="btn gold" type="submit">Zatwierdź brief</button>
              <a class="btn" href="/">Powrót</a>
              <span class="muted">Zatwierdzenie briefu uruchamia analizę i przygotowanie raportu dla architekta.</span>
            </div>
            <script>
              (function(){{
                var f = document.currentScript && document.currentScript.parentElement && document.currentScript.parentElement.closest("form");
                if(!f){{ f = document.querySelector("form"); }}
                if(!f) return;
                f.addEventListener("submit", function(){{
                  var btn = f.querySelector("button[type=submit]");
                  if(btn){{
                    btn.disabled = true;
                    btn.textContent = "Przetwarzanie...";
                  }}
                }}, {{ once: true }});
              }})();
            </script>
          </form>
        </div>
        """,
        nav=nav_links()
    )


# =========================
# 6) AI / fallback report (pozostawiam – możesz rozbudować prompt pod przemysł)
# =========================

# -------------------------
# AI helpers: porządek danych + deterministyczny render raportu
# -------------------------

def _form_to_rows(form: Dict[str, Any]) -> List[Dict[str, str]]:
    """Zamienia form dict -> lista wierszy z sekcją i etykietą (żeby AI nie gubiło pól i nie mieszało danych)."""
    rows: List[Dict[str, str]] = []
    known = set()

    for sec_title, fields in FORM_SCHEMA:
        for f in fields:
            name = f.get("name")
            label = f.get("label", name)
            if not name:
                continue
            if name in form:
                known.add(name)
                rows.append({
                    "section": sec_title,
                    "field": str(name),
                    "label": str(label),
                    "value": str(form.get(name)),
                })

    # Dorzuć ewentualne nieznane klucze (żeby nic nie zginęło)
    for k, v in form.items():
        if k not in known:
            rows.append({
                "section": "Inne (poza schematem)",
                "field": str(k),
                "label": str(k),
                "value": str(v),
            })

    return rows

def _pln(x: float) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except Exception:
        return str(x)

def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()

def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for r in rows:
        out.append("| " + " | ".join(_md_escape(c) for c in r) + " |")
    return "\n".join(out)

def render_architect_report(data: Dict[str, Any], company: Dict[str, Any], architect: Dict[str, Any]) -> str:
    meta = data.get("meta") or {}
    facts = data.get("facts") or []
    fee = data.get("fee_estimate") or {}
    bc = data.get("build_cost_estimate") or {}
    questions = data.get("questions") or {}

    fact_rows: List[List[str]] = []
    for f in facts:
        fact_rows.append([
            str(f.get("section", "")),
            str(f.get("label", "")),
            str(f.get("value", "")),
            str(f.get("source", "")),
            str(round(float(f.get("confidence", 0) or 0), 2)),
        ])

    fee_rows: List[List[str]] = []
    for r in (fee.get("calc_table") or []):
        fee_rows.append([
            str(r.get("item", "")),
            str(r.get("basis", "")),
            str(r.get("qty", "")),
            str(r.get("unit", "")),
            _pln(r.get("unit_price_pln", 0) or 0),
            _pln(r.get("amount_pln", 0) or 0),
            str(r.get("source", "")),
            str(r.get("justification", "")),
        ])

    build_rows = [[
        str(bc.get("standard", "")),
        str(bc.get("region", "")),
        _pln(bc.get("unit_cost_low_pln_m2", 0) or 0),
        _pln(bc.get("unit_cost_mid_pln_m2", 0) or 0),
        _pln(bc.get("unit_cost_high_pln_m2", 0) or 0),
        _pln(bc.get("total_low_pln", 0) or 0),
        _pln(bc.get("total_mid_pln", 0) or 0),
        _pln(bc.get("total_high_pln", 0) or 0),
    ]]

    risk_rows: List[List[str]] = []
    for r in (data.get("risks") or []):
        risk_rows.append([
            str(r.get("area", "")),
            str(r.get("priority", "")),
            str(r.get("risk", "")),
            str(r.get("impact", "")),
            str(r.get("mitigation", "")),
        ])

    client_email = data.get("client_email") or {"subject": "", "body_text": ""}

    report = f"""# RAPORT DLA ARCHITEKTA (przemysł) – {company.get("name","")}

**Projekt:** {meta.get("project_name","")}
**Klient:** {meta.get("client_company","")}
**Lokalizacja:** {meta.get("site_location","")}
**Architekt:** {architect.get("name","")} <{architect.get("email","")}>

---

## 1) Streszczenie
- Raport przygotowany **na podstawie formularza klienta**. Każdy wpis ma źródło: `client_form` lub `assumption`.
- Obiekt: przemysł/logistyka – priorytety: PPOŻ, BHP, technologia, logistyka, media.

---

## 2) Dane wejściowe z formularza (tabela)
{_md_table(["Sekcja", "Parametr", "Wartość", "Źródło", "Pewność"], fact_rows)}

---

## 3) Pytania / RFI
**Blockery (bez tego nie domykamy wyceny / zakresu):**
{chr(10).join([f"- {q}" for q in (questions.get("blockers") or [])])}

**Ważne (wpływ na budżet / terminy / ryzyka):**
{chr(10).join([f"- {q}" for q in (questions.get("important") or [])])}

**Opcjonalne:**
{chr(10).join([f"- {q}" for q in (questions.get("optional") or [])])}

---

## 4) Braki dokumentów / formalności
{chr(10).join([f"- {x}" for x in (data.get("missing_docs") or [])])}

---

## 5) Wycena projektu (kalkulacja + uzasadnienie)
**Podstawa interpretacji cennika:** {fee.get("pricing_basis","")}

{_md_table(["Pozycja", "Baza", "Ilość", "Jedn.", "Stawka [PLN]", "Kwota [PLN]", "Źródło", "Uzasadnienie"], fee_rows)}

**Suma (widełki):** {_pln(fee.get("total_low_pln", 0) or 0)} – {_pln(fee.get("total_high_pln", 0) or 0)} PLN

**W zakresie:**
{chr(10).join([f"- {x}" for x in (fee.get("included_scope") or [])])}

**Poza zakresem:**
{chr(10).join([f"- {x}" for x in (fee.get("excluded_scope") or [])])}

---

## 6) Średni koszt budowy (widełki + czynniki)
{_md_table(["Standard", "Region", "PLN/m² low", "PLN/m² mid", "PLN/m² high", "Total low", "Total mid", "Total high"], build_rows)}

**Czynniki kosztotwórcze:**
{chr(10).join([f"- {x}" for x in (bc.get("drivers") or [])])}

---

## 7) Ryzyka / uwagi architekta (tabela)
{_md_table(["Obszar", "Priorytet", "Ryzyko", "Skutek", "Mitigacja / co sprawdzić"], risk_rows)}

---

## 8) Założenia (jawne)
{chr(10).join([f"- {x}" for x in (data.get("assumptions") or [])])}

---

## 9) Następne kroki
{chr(10).join([f"- {x}" for x in (data.get("next_steps") or [])])}

---

## 10) Wiadomość do klienta (copy/paste)
**Temat:** {client_email.get("subject","")}

```text
{client_email.get("body_text","")}
```
"""
    return report


def fallback_report(form: Dict[str, Any], pricing_text: str) -> str:
    area = float(form.get("usable_area_m2", 0) or 0)
    standard = form.get("cost_standard") or "Standard"
    region = form.get("region_type") or "Mniejsze miasto / okolice"

    base = BUILD_COST_M2_PLN.get(standard, 6000)
    mult = REGION_MULTIPLIER.get(region, 1.0)
    build_low = int(area * base * mult * 0.9) if area else 0
    build_high = int(area * base * mult * 1.15) if area else 0

    missing = []
    for key in ("mpzp_wz_extract", "map_for_design", "geotech_opinion", "power_conditions", "water_conditions", "sewage_conditions"):
        if not form.get(key):
            missing.append(key)

    missing_human = {
        "mpzp_wz_extract": "Wypis i wyrys MPZP / decyzja WZ",
        "map_for_design": "Mapa do celów projektowych (geodeta)",
        "geotech_opinion": "Opinia geotechniczna",
        "power_conditions": "Warunki przyłączenia energii elektrycznej",
        "water_conditions": "Warunki przyłączenia wody",
        "sewage_conditions": "Warunki przyłączenia kanalizacji sanitarnej",
    }

    missing_list = "\n".join([f"- {missing_human.get(m, m)}" for m in missing]) or "- (brak krytycznych braków wykrytych)"

    pricing_note = "Cennik firmy jest pusty lub niepodany – nie wyliczono wynagrodzenia projektowego." if not pricing_text.strip() else "Cennik firmy został dołączony do analizy."

    return f"""RAPORT (tryb bez AI)

1) Podsumowanie briefu
- Typ obiektu: {form.get("object_type","")}
- Lokalizacja: {form.get("plot_address","")}
- Program (opis): {form.get("program_overview","")}

2) Braki / dokumenty do pozyskania
{missing_list}

3) Wstępny koszt budowy (szacunek orientacyjny – V1)
- Założenia: standard={standard}, region={region}
- Koszt m²: ok. {int(base*mult)} PLN/m²
- Estymacja: {build_low:,} – {build_high:,} PLN (orientacyjnie)

4) Cennik / koszt projektu
- {pricing_note}

Uwaga: raport ma charakter informacyjny (MVP). Tryb AI generuje analizę ryzyk, checklisty formalne i listę pytań uzupełniających.
""".replace(",", " ")

def ai_report(form: Dict[str, Any], pricing_text: str, company: Dict[str, Any], architect: Dict[str, Any]) -> str:
    if not OPENAI_API_KEY or OpenAI is None:
        return fallback_report(form, pricing_text)

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Baseline (pomocnicze) – liczby liczymy deterministycznie, AI ma je opisać/uzasadnić i ewentualnie skorygować jako jawne założenia
    area = float(form.get("usable_area_m2", 0) or 0)
    standard = form.get("cost_standard") or "Standard"
    region = form.get("region_type") or "Mniejsze miasto / okolice"
    base = float(BUILD_COST_M2_PLN.get(standard, BUILD_COST_M2_PLN.get("Standard", 6000)))
    mult = float(REGION_MULTIPLIER.get(region, 1.0))
    unit_mid = base * mult
    unit_low = unit_mid * 0.90
    unit_high = unit_mid * 1.15
    total_low = area * unit_low if area else 0.0
    total_mid = area * unit_mid if area else 0.0
    total_high = area * unit_high if area else 0.0

    system = (
        "Jesteś doświadczonym architektem-prowadzącym i koordynatorem projektów przemysłowych w Polsce.\n"
        "Tworzysz: (1) raport wewnętrzny dla architekta oraz (2) gotową wiadomość do klienta do skopiowania.\n\n"
        "KRYTYCZNE ZASADY (bez wyjątków):\n"
        "- Raport jest NA PODSTAWIE FORMULARZA klienta. Nie mieszaj danych klienta z domysłami.\n"
        "- Każdy fakt w polu 'facts' musi mieć source: client_form (z briefu) albo assumption (twoje założenie).\n"
        "- Jeśli brakuje danych do wyceny: podaj widełki i dopisz brak jako questions.blockers (nie zgaduj w ciszy).\n"
        "- Obiekt jest PRZEMYSŁOWY/LOGISTYCZNY: priorytet PPOŻ/BHP/technologia/logistyka/media.\n"
        "- Musisz wyliczyć: (a) koszt projektu na podstawie pricing_text, (b) szacunkowy koszt budowy (widełki) oraz wszystko uzasadnić w tabelach.\n"
        "- Pisz po polsku, rzeczowo.\n"
    )

    brief_rows = _form_to_rows(form)

    user_payload = {
        "purpose": "architect_internal_report_and_client_email",
        "company": {"name": company.get("name", ""), "email": company.get("email", "")},
        "architect": {"name": architect.get("name", ""), "email": architect.get("email", "")},
        "pricing_text": pricing_text,
        "brief_rows": brief_rows,
        "brief_raw": form,
        "build_cost_inputs": {
            "usable_area_m2": area,
            "standard": standard,
            "region": region,
            "base_cost_m2_pln": base,
            "region_multiplier": mult,
            "baseline_unit_low_pln_m2": unit_low,
            "baseline_unit_mid_pln_m2": unit_mid,
            "baseline_unit_high_pln_m2": unit_high,
            "baseline_total_low_pln": total_low,
            "baseline_total_mid_pln": total_mid,
            "baseline_total_high_pln": total_high,
            "table_BUILD_COST_M2_PLN": BUILD_COST_M2_PLN,
            "table_REGION_MULTIPLIER": REGION_MULTIPLIER,
        },
        "rules": {
            "do_not_mix_data": True,
            "assumptions_must_be_explicit": True,
            "tables_required": True,
        },
        "notes": "Jeśli standard/region nie występują w briefie, potraktuj je jako assumption i jasno wpisz w assumptions."
    }

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_schema", "json_schema": REPORT_SCHEMA},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            
        )

        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content) if content else None
        if not isinstance(data, dict):
            return fallback_report(form, pricing_text) + "\n\n[AI ERROR: invalid JSON]"

        return render_architect_report(data, company, architect)

    except Exception as e:
        return fallback_report(form, pricing_text) + f"\n\n[AI ERROR: {type(e).__name__}: {e}]"

def _safe_err(e: BaseException) -> str:
    parts = [f"{type(e).__name__}: {e}"]
    if isinstance(e, OSError) and getattr(e, "errno", None) is not None:
        parts.append(f"errno={e.errno}")
    return " | ".join(parts)

def send_email_via_resend(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    if not (RESEND_API_KEY and RESEND_FROM):
        return False, "RESEND not configured (missing RESEND_API_KEY or RESEND_FROM)"
    try:
        import urllib.request
        payload = json.dumps({
            "from": RESEND_FROM,
            "to": [to_email],
            "subject": subject,
            "text": body,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = int(getattr(resp, "status", 200))
            text = resp.read().decode("utf-8", errors="replace")
            if 200 <= code < 300:
                return True, f"RESEND OK status={code} resp={text[:300]}"
            return False, f"RESEND FAIL status={code} resp={text[:800]}"
    except Exception as e:
        return False, f"RESEND exception: {_safe_err(e)}"

def send_email_via_smtp(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    if not BOT_EMAIL or not BOT_EMAIL_PASSWORD:
        return False, "SMTP not configured (missing BOT_EMAIL or BOT_EMAIL_PASSWORD)"
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = BOT_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        print(f"[EMAIL] SMTP connect {SMTP_HOST}:{SMTP_PORT} as {BOT_EMAIL}")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(BOT_EMAIL, BOT_EMAIL_PASSWORD)
            s.send_message(msg)

        return True, "SMTP OK"
    except (socket.gaierror, TimeoutError, OSError) as e:
        return False, f"SMTP network error: {_safe_err(e)}"
    except Exception as e:
        return False, f"SMTP error: {_safe_err(e)}"

def send_email(to_email: str, subject: str, body: str, *, delivery_id: str) -> bool:
    to_email = (to_email or "").strip()
    if not to_email:
        print(f"[EMAIL] FAIL delivery_id={delivery_id} reason=missing recipient")
        return False

    ok, reason = send_email_via_resend(to_email, subject, body)
    if ok:
        print(f"[EMAIL] OK delivery_id={delivery_id} via=RESEND to={to_email} detail={reason}")
        return True
    print(f"[EMAIL] RESEND not sent delivery_id={delivery_id} to={to_email} detail={reason}")

    ok2, reason2 = send_email_via_smtp(to_email, subject, body)
    if ok2:
        print(f"[EMAIL] OK delivery_id={delivery_id} via=SMTP to={to_email} detail={reason2}")
        return True

    print(f"[EMAIL] FAIL delivery_id={delivery_id} to={to_email} detail={reason2}")
    return False


# =========================
# 8) Stripe – bez zmian
# =========================

def stripe_ready() -> bool:
    return bool(stripe is not None and STRIPE_SECRET_KEY and (STRIPE_PRICE_ID_MONTHLY or STRIPE_PRICE_ID_YEARLY) and STRIPE_WEBHOOK_SECRET)

def subscription_active(company: Dict[str, Any]) -> bool:
    """Czy firma ma dostęp do formularzy/analiz (płatny lub free/beta)."""
    if DEV_BYPASS_SUBSCRIPTION:
        return True
    st = (company.get("stripe") or {}).get("status") or ""
    if st in ("active", "trialing"):
        return True
    plan = _company_plan(company)
    return bool(ENABLE_FREE_PLAN and plan == "free")

def stripe_init() -> None:
    if stripe_ready():
        stripe.api_key = STRIPE_SECRET_KEY  # type: ignore


# =========================
# 9) App + auth – bez zmian
# =========================

app = FastAPI()

_session_key = SESSION_SECRET or "dev-insecure-session-secret"
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    same_site="lax",
    https_only=BASE_URL.startswith("https://"),
)

@app.get("/logo_arch.png")
def logo_arch_png():
    p = os.path.join(os.path.dirname(__file__), "logo_arch.png")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/png")
    return PlainTextResponse("logo_arch.png not found", status_code=404)


@app.get("/favicon.ico")
def favicon_ico():
    return RedirectResponse(url="/logo_arch.png")


def get_company(request: Request) -> Optional[Dict[str, Any]]:
    cid = request.session.get("company_id")
    if not cid:
        return None
    db = _load_db()
    return db["companies"].get(cid)

def require_company(request: Request) -> Optional[RedirectResponse]:
    if not get_company(request):
        return RedirectResponse(url="/login", status_code=302)
    return None

def flash_html(msg: str) -> str:
    return f"""
    <div class="notice" style="border-color:rgba(255,255,255,0.12); background: rgba(255,255,255,0.06)">
      {esc(msg)}
    </div>
    """


# =========================
# 10) Landing page – minimalnie poprawione copy
# =========================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    free_card = ""
    if ENABLE_FREE_PLAN:
        free_card = f'''
        <div class="panel card" data-reveal style="border-color: rgba(214,179,106,0.35); background: rgba(214,179,106,0.07);">
          <div class="k">BETA</div>
          <div class="n" style="font-size:28px;margin-top:8px">0 zł</div>
          <div class="t">Na start / testy. Limit: {FREE_FORMS_PER_MONTH_LIMIT} formularze / mies.</div>
          <div class="divider"></div>
          <div class="muted">• Panel firmy + architekci</div>
          <div class="muted">• Brief + raport</div>
          <div class="muted">• Cennik firmy do wycen (opcjonalnie)</div>
          <div style="height:14px"></div>
          <div class="actions">
            <a class="btn gold" href="/register">Załóż konto</a>
            <a class="btn ghost" href="/login">Logowanie</a>
          </div>
        </div>
        '''

    body = f'''
    <div id="splash" class="splash">
      <div class="box">
        <svg viewBox="0 0 1200 240" role="img" aria-label="ArchiBot">
          <defs>
            <linearGradient id="g" x1="0" x2="1">
              <stop offset="0" stop-color="#D6B36A"/>
              <stop offset="1" stop-color="#EEF2FF"/>
            </linearGradient>
          </defs>
          <text x="50%" y="62%" text-anchor="middle"
                font-family="Syne, system-ui, sans-serif"
                font-size="160"
                font-weight="800"
                fill="transparent"
                stroke="url(#g)"
                stroke-width="4"
                stroke-linejoin="round"
                style="letter-spacing:-0.02em; paint-order: stroke;">
            ArchiBot
          </text>
          <style>
            text {{
              stroke-dasharray: 2200;
              stroke-dashoffset: 2200;
              animation: draw 2.05s ease forwards;
            }}
            @keyframes draw {{
              to {{ stroke-dashoffset: 0; }}
            }}
          </style>
        </svg>
        <div class="hint">SCROLL / CLICK</div>
      </div>
    </div>

    <div class="wrap">
      <section class="heroHome" id="top">
        <div class="heroGrid">
          <div>
            <div class="tag" data-reveal><span class="dot"></span> Brief → Raport → Wycena (przemysł)</div>
            <div style="height:12px"></div>
            <h1 class="titleBig" data-reveal>
              Architekt nie traci czasu na <span class="gold">chaos</span>.
            </h1>
            <p class="lead" data-reveal style="max-width:70ch">
              ArchiBot zamienia nieuporządkowane informacje od inwestora w <b>raport do domknięcia wyceny</b>:
              braki, ryzyka, pytania blokujące, lista dokumentów i gotowa wiadomość do klienta.
            </p>
            <div style="height:16px"></div>
            <div class="actions" data-reveal>
              <a class="btn gold" href="/register">Załóż konto</a>
              <a class="btn" href="/login">Zaloguj</a>
              <a class="btn ghost" href="#jak">Zobacz jak działa</a>
              <a class="btn ghost" href="/demo">Podgląd briefu</a>
              <a class="btn ghost" href="/report-demo">Podgląd raportu</a>
            </div>
            <div style="height:16px"></div>
            <div class="muted" data-reveal>Wersja BETA: włączasz, dodajesz architekta, wysyłasz link do inwestora – reszta dzieje się automatycznie.</div>
          </div>

          <div class="heroCard panel" data-reveal>
            <div class="k">DLACZEGO TO SPRZEDAJE</div>
            <div class="divider"></div>
            <div class="muted">• Ucinamy niedopowiedzenia na starcie.</div>
            <div class="muted">• Zwiększasz skuteczność wyceny (mniej „wróćmy za tydzień”).</div>
            <div class="muted">• Masz gotową listę pytań P0/P1/P2 do klienta.</div>
            <div class="divider"></div>
            <div class="actions">
              <a class="btn gold" href="#plany">Zobacz plany</a>
              <a class="btn" href="/register">Start</a>
            </div>
          </div>
        </div>
      </section>

      <section class="howShow" id="jak">
        <div style="height:8px"></div>
        <div class="k" data-reveal>POKAZ KROK PO KROKU</div>
        <div style="height:10px"></div>
        <h2 class="h1" data-reveal style="margin:0">Przewijasz — a system pokazuje, co się dzieje</h2>
        <p class="lead" data-reveal style="max-width:75ch">To ma być proste dla kogoś, kto wchodzi pierwszy raz. Zero ściany tekstu.</p>
        <div style="height:14px"></div>

        <div id="howShell" class="howShell">
          <div class="howSticky">
            <div class="stepList">
              <div class="item active" data-reveal>
                <div class="num">KROK 01</div>
                <div class="ttl">Ustawiasz pracownię</div>
                <div class="txt">Dodajesz architekta i (opcjonalnie) cennik — raport podciągnie to do wyceny.</div>
              </div>
              <div class="item" data-reveal>
                <div class="num">KROK 02</div>
                <div class="ttl">Inwestor wypełnia brief</div>
                <div class="txt">Jedno miejsce na formalności, media, logistykę, PPOŻ/BHP, technologię.</div>
              </div>
              <div class="item" data-reveal>
                <div class="num">KROK 03</div>
                <div class="ttl">AI składa raport</div>
                <div class="txt">Braki, ryzyka, pytania blokujące, lista dokumentów, następne kroki.</div>
              </div>
              <div class="item" data-reveal>
                <div class="num">KROK 04</div>
                <div class="ttl">Domykasz wycenę</div>
                <div class="txt">Masz gotową wiadomość do klienta i listę tematów do spotkania.</div>
              </div>
            </div>

            <div class="scene" data-reveal>
              <div class="frame show">
                <div class="hdr">
                  <div class="pill"><span class="dot g"></span> Panel • Ustawienia</div>
                  <div class="dots"><span class="dot g"></span><span class="dot"></span><span class="dot"></span></div>
                </div>
                <div class="mini">
                  <b>Dodaj architekta</b> → system generuje link do briefu.<br/>
                  <b>Wklej cennik</b> → raport liczy widełki i uzasadnia.
                </div>
                <div style="height:12px"></div>
                <div class="mini">✅ Checklist: architekt dodany, cennik wklejony, plan aktywny.</div>
              </div>

              <div class="frame">
                <div class="hdr">
                  <div class="pill"><span class="dot g"></span> Brief • Formularz</div>
                  <div class="dots"><span class="dot"></span><span class="dot g"></span><span class="dot"></span></div>
                </div>
                <div class="mini">
                  Inwestor uzupełnia: działka, media, proces, logistyka, BHP/PPOŻ.<br/>
                  Puste pola są OK — raport je wyłapie jako braki.
                </div>
                <div style="height:12px"></div>
                <div class="mini">📎 Załączniki: MPZP/WZ, geotechnika, warunki przyłączy (opcjonalnie).</div>
              </div>

              <div class="frame">
                <div class="hdr">
                  <div class="pill"><span class="dot g"></span> Raport • P0/P1/P2</div>
                  <div class="dots"><span class="dot"></span><span class="dot"></span><span class="dot g"></span></div>
                </div>
                <div class="mini">
                  <b>P0 (blokery):</b> sprinkler? suwnica? wysokość podhacznikowa? OOŚ?<br/>
                  <b>P1:</b> etapowanie, FM 24/7, standard biur.<br/>
                  <b>P2:</b> BIM, ESG, opcje.
                </div>
                <div style="height:12px"></div>
                <div class="mini">📩 Raport idzie na mail architekta + zapis do historii.</div>
              </div>

              <div class="frame">
                <div class="hdr">
                  <div class="pill"><span class="dot g"></span> Wycena • Domknięcie</div>
                  <div class="dots"><span class="dot g"></span><span class="dot"></span><span class="dot g"></span></div>
                </div>
                <div class="mini">
                  Gotowa wiadomość do klienta: „prosimy o uzupełnienia” + lista pytań.<br/>
                  Szybciej domykasz zakres i minimalizujesz ryzyko niedoszacowania.
                </div>
                <div style="height:12px"></div>
                <div class="actions">
                  <a class="btn gold" href="/register">Uruchom u siebie</a>
                  <a class="btn" href="/dashboard">Wejdź do panelu</a>
                </div>
              </div>
            </div>

          </div>
        </div>
      </section>

      <section id="funkcje" style="padding: 24px 0 0">
        <div class="k" data-reveal>FUNKCJE</div>
        <div style="height:10px"></div>
        <h2 class="h1" data-reveal style="margin:0">Wersja „dla przemysłu” — nie ogólnik</h2>
        <div style="height:14px"></div>
        <div class="grid3">
          <div class="panel card" data-reveal>
            <div class="k">BRIEF</div>
            <div class="n">Komplet pytań</div>
            <div class="t">Formalne, media, grunt, technologia, logistyka, PPOŻ/BHP, parametry obiektu.</div>
          </div>
          <div class="panel card" data-reveal>
            <div class="k">RAPORT</div>
            <div class="n">Ryzyka i braki</div>
            <div class="t">Priorytety P0/P1/P2, brakujące dokumenty, niejasności do doprecyzowania.</div>
          </div>
          <div class="panel card" data-reveal>
            <div class="k">SPRZEDAŻ</div>
            <div class="n">Email do klienta</div>
            <div class="t">Copy/paste: prośba o uzupełnienia + lista pytań krytycznych.</div>
          </div>
        </div>
      </section>

      <section id="raport" style="padding: 26px 0 0">
        <div class="panel card" data-reveal>
          <div class="k">RAPORT</div>
          <div class="n" style="font-size:26px;margin-top:10px">Zobacz fragment raportu — bez konta</div>
          <div class="t">Dokładnie to dostajesz na mail (i w historii w panelu).</div>
          <div style="height:14px"></div>
          <div class="actions">
            <a class="btn gold" href="/report-demo">Podgląd raportu</a>
            <a class="btn" href="/demo">Podgląd briefu</a>
            <a class="btn ghost" href="/register">Start</a>
          </div>
        </div>
      </section>

      <section id="plany" style="padding: 28px 0 0">
        <div class="k" data-reveal>PLANY</div>
        <div style="height:10px"></div>
        <h2 class="h1" data-reveal style="margin:0">Proste. Bez „ukrytych” rzeczy.</h2>
        <p class="lead" data-reveal style="max-width:75ch">Subskrypcję anulujesz jednym kliknięciem w portalu Stripe (link w panelu).</p>
        <div style="height:14px"></div>

        <div class="grid3" style="align-items: stretch;">
          {free_card}
          <div class="panel card" data-reveal>
            <div class="k">MIESIĘCZNIE</div>
            <div class="n" style="font-size:28px;margin-top:8px">249 zł</div>
            <div class="t">Limit: {FORMS_PER_MONTH_LIMIT} formularzy / mies.</div>
            <div class="divider"></div>
            <div class="muted">• Panel + architekci</div>
            <div class="muted">• Historia raportów</div>
            <div class="muted">• Cennik firmy (opcjonalnie)</div>
            <div style="height:14px"></div>
            <div class="actions">
              <a class="btn gold" href="/register">Załóż konto</a>
              <a class="btn ghost" href="/login">Logowanie</a>
            </div>
          </div>

          <div class="panel card" data-reveal>
            <div class="k">ROCZNIE</div>
            <div class="n" style="font-size:28px;margin-top:8px">2 690 zł</div>
            <div class="t">Limit: {FORMS_PER_MONTH_LIMIT} formularzy / mies.</div>
            <div class="divider"></div>
            <div class="muted">• To samo co miesięcznie</div>
            <div class="muted">• Stabilne rozliczenie</div>
            <div class="muted">• Wsparcie wdrożeniowe</div>
            <div style="height:14px"></div>
            <div class="actions">
              <a class="btn gold" href="/register">Załóż konto</a>
              <a class="btn ghost" href="/login">Logowanie</a>
            </div>
          </div>
        </div>
      </section>

      <section id="faq" style="padding: 28px 0 0">
        <div class="k" data-reveal>FAQ</div>
        <div style="height:10px"></div>
        <div class="grid2">
          <div class="panel card" data-reveal>
            <div class="n" style="font-size:18px">Czy inwestor widzi raport?</div>
            <div class="t">Nie. Raport jest dla architekta / zespołu projektowego.</div>
          </div>
          <div class="panel card" data-reveal>
            <div class="n" style="font-size:18px">Czy wszystkie pola muszą być wypełnione?</div>
            <div class="t">Nie. Raport pokaże braki i pytania uzupełniające.</div>
          </div>
          <div class="panel card" data-reveal>
            <div class="n" style="font-size:18px">Jak inwestor dostaje formularz?</div>
            <div class="t">W panelu generujesz link przy architekcie i wysyłasz do inwestora.</div>
          </div>
          <div class="panel card" data-reveal>
            <div class="n" style="font-size:18px">Jak anulować subskrypcję?</div>
            <div class="t">W panelu jest przycisk „Zarządzaj subskrypcją” (portal Stripe) — tam anulujesz w 1 klik.</div>
          </div>
        </div>

        <div class="foot">
          <div class="wrap" style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;">
            <div>© {esc(APP_NAME)} • {esc(datetime.datetime.utcnow().year)}</div>
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              <a href="/terms">Regulamin</a>
              <a href="/privacy">Polityka prywatności</a>
              <a href="/security">Bezpieczeństwo</a>
            </div>
          </div>
        </div>
      </section>
    </div>
    '''

    return HTMLResponse(layout("Start", body=body, nav=nav_links(), request=request, page="home"))

# =========================
# 11) Auth: rejestracja / logowanie – bez zmian
# =========================

@app.get("/register", response_class=HTMLResponse)
def register_page():
    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Załóż konto firmy</h1>
      <p class="lead">Konto umożliwia zarządzanie cennikiem oraz listą architektów (linki do formularzy).</p>
      <div style="height:18px"></div>
      <div class="panel card">
        <form method="post" action="/register">
          <div class="fields">
            <div class="field"><label>Nazwa firmy</label><input name="name" placeholder="np. Pracownia XYZ"/></div>
            <div class="field"><label>Email (login)</label><input type="email" name="email" placeholder="biuro@..."/></div>
            <div class="field full"><label>Hasło</label><input type="password" name="password" placeholder="min. 8 znaków"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Utwórz konto</button>
            <a class="btn" href="/login">Mam konto → logowanie</a>
          </div>
        </form>
      </div>
    </div>
    """
    return HTMLResponse(layout("Rejestracja", body=body, nav=nav_links()))

@app.post("/register")
async def register(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = (form.get("password") or "").strip()

    if not name or not email or not password or len(password) < 8:
        return HTMLResponse(layout("Rejestracja", body=flash_html("Uzupełnij nazwę, email i hasło (min. 8 znaków).") + '<div class="wrap formwrap"><a class="btn" href="/register">Wróć</a></div>', nav=nav_links()))

    db = _load_db()
    for c in db["companies"].values():
        if c.get("email") == email:
            return HTMLResponse(layout("Rejestracja", body=flash_html("Ten email jest już użyty.") + '<div class="wrap formwrap"><a class="btn" href="/register">Wróć</a></div>', nav=nav_links()))

    cid = _new_id("cmp")
    db["companies"][cid] = {
        "id": cid,
        "name": name,
        "email": email,
        "password_hash": _hash_password(password),
        "created_at": _now_ts(),
        "pricing_text": "",
        "billing": {"company_name": "", "nip": "", "address": "", "invoice_email": ""},
        "architects": [],
        "reports": [],
        "usage": {"period": _period_key(), "forms_sent": 0},
        "stripe": {"status": "inactive", "customer_id": "", "subscription_id": ""},
        "plan": ("free" if ENABLE_FREE_PLAN else "none"),
    }
    _save_db(db)

    request.session["company_id"] = cid
    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page():
    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Zaloguj się</h1>
      <p class="lead">Panel firmy: cennik, architekci i subskrypcja.</p>
      <div style="height:18px"></div>
      <div class="panel card">
        <form method="post" action="/login">
          <div class="fields">
            <div class="field"><label>Email</label><input type="email" name="email" placeholder="biuro@..."/></div>
            <div class="field"><label>Hasło</label><input type="password" name="password"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Zaloguj</button>
            <a class="btn" href="/register">Załóż konto</a>
          </div>
        </form>
      </div>
    </div>
    """
    return HTMLResponse(layout("Logowanie", body=body, nav=nav_links()))

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = (form.get("password") or "").strip()

    db = _load_db()
    for cid, c in db["companies"].items():
        if c.get("email") == email and _verify_password(password, c.get("password_hash", "")):
            request.session["company_id"] = cid
            return RedirectResponse(url="/dashboard", status_code=302)

    return HTMLResponse(layout("Logowanie", body=flash_html("Błędny email lub hasło.") + '<div class="wrap formwrap"><a class="btn" href="/login">Wróć</a></div>', nav=nav_links()))

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


# =========================
# 12) Dashboard firmy – bez zmian merytorycznych
# =========================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, tab: str = "overview"):
    gate = require_company(request)
    if gate:
        return gate

    company = get_company(request)
    assert company is not None

    # Refresh usage window
    _ensure_usage_period(company)
    sent = int((company.get("usage") or {}).get("forms_sent") or 0)
    remaining = _forms_remaining(company)
    plan = _company_plan(company)
    access_ok = subscription_active(company)

    _ensure_reports(company)
    architects = list(company.get("architects") or [])
    reports = list(company.get("reports") or [])
    reports.sort(key=lambda r: int(r.get("created_at") or 0), reverse=True)

    allowed_tabs = {
        "overview": "Start",
        "reports": "Raporty",
        "architects": "Architekci",
        "pricing": "Cennik",
        "billing": "Faktury",
        "plan": "Plan / Subskrypcja",
    }
    tab = (tab or "overview").strip().lower()
    if tab not in allowed_tabs:
        tab = "overview"

    def nav_item(key: str, label: str, badge: str = "") -> str:
        cls = "navitem active" if key == tab else "navitem"
        b = f'<span class="badge">{esc(badge)}</span>' if badge else ''
        return f'<a class="{cls}" href="/dashboard?tab={esc(key)}"><span>{esc(label)}</span>{b}</a>'

    # Sidebar
    sidebar = f'''
    <div class="panel side">
      <div class="title">PANEL</div>
      {nav_item("overview", "Start")}
      {nav_item("reports", "Raporty", str(len(reports)) if reports else "")}
      {nav_item("architects", "Architekci", str(len(architects)) if architects else "")}
      {nav_item("pricing", "Cennik")}
      {nav_item("billing", "Faktury")}
      {nav_item("plan", "Plan / Subskrypcja")}
      <div class="divider"></div>
      <a class="navitem" href="/demo" target="_blank">Podgląd briefu</a>
      <a class="navitem" href="/logout">Wyloguj</a>
    </div>
    '''

    # Tab content
    content = ""

    if tab == "overview":
        has_arch = len(architects) > 0
        has_price = bool((company.get("pricing_text") or "").strip())
        steps = [
            ("Dodaj architekta", has_arch, "W panelu → Architekci"),
            ("Wklej cennik (opcjonalnie)", has_price, "W panelu → Cennik"),
            ("Aktywny plan", access_ok, "W panelu → Plan / Subskrypcja"),
            ("Wyślij link do inwestora", has_arch and access_ok, "Skopiuj link z listy architektów"),
        ]
        done = sum(1 for _, ok, _ in steps if ok)
        pct = int(round((done / len(steps)) * 100))

        first_link = ""
        if architects:
            a0 = architects[0]
            link = f"{BASE_URL}/f/{a0.get('token','')}"
            first_link = f'''
              <div class="panel card">
                <div class="k">SZYBKA AKCJA</div>
                <div class="n" style="font-size:18px;margin-top:8px">Link do briefu (1 klik do skopiowania)</div>
                <div style="height:10px"></div>
                <div class="notice mono" id="quickLink">{esc(link)}</div>
                <div style="height:10px"></div>
                <div class="actions">
                  <button class="btn" data-copy="#quickLink">Kopiuj</button>
                  <a class="btn gold" href="/dashboard?tab=architects">Zarządzaj architektami</a>
                </div>
              </div>
            '''

        status_badge = '<span class="badge ok">aktywny</span>' if access_ok else '<span class="badge bad">brak dostępu</span>'

        content = f'''
        <div class="headrow">
          <div>
            <h1 class="h1">Start</h1>
            <p class="lead sub">Wszystko w zakładkach. Zero chaosu. Plan: <b>{esc(PLAN_LABELS.get(plan, plan))}</b> {status_badge}</p>
          </div>
          <div class="actions">
            <a class="btn gold" href="/dashboard?tab=architects">Dodaj architekta</a>
            <a class="btn" href="/dashboard?tab=plan">Zarządzaj planem</a>
          </div>
        </div>

        <div class="grid3">
          <div class="stat">
            <div class="k">FORMULARZE</div>
            <div class="n">{esc(str(sent))} / {esc(str(_forms_limit(company)))}</div>
            <div class="t">Wysłane w tym miesiącu (UTC).</div>
          </div>
          <div class="stat">
            <div class="k">POZOSTAŁO</div>
            <div class="n">{esc(str(remaining))}</div>
            <div class="t">Tyle briefów możesz jeszcze przyjąć.</div>
          </div>
          <div class="stat">
            <div class="k">RAPORTY</div>
            <div class="n">{esc(str(len(reports)))}</div>
            <div class="t">Historia raportów w panelu.</div>
          </div>
        </div>

        <div style="height:14px"></div>

        <div class="panel card">
          <div class="k">ONBOARDING</div>
          <div class="n" style="font-size:18px;margin-top:8px">Postęp konfiguracji: {pct}%</div>
          <div class="divider"></div>
          <div class="grid">
            {''.join([f'<div class="notice" style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start"><div><b>{esc(title)}</b><div class="muted" style="margin-top:4px">{esc(hint)}</div></div><div>{("✅" if ok else "—")}</div></div>' for title, ok, hint in steps])}
          </div>
        </div>

        <div style="height:14px"></div>
        {first_link}
        '''

    elif tab == "architects":
        # list architects
        rows = []
        for a in architects:
            aid = str(a.get("id") or "")
            name = str(a.get("name") or "")
            email = str(a.get("email") or "")
            token = str(a.get("token") or "")
            link = f"{BASE_URL}/f/{token}"
            rows.append(f'''
              <tr>
                <td><b>{esc(name)}</b><div class="muted">{esc(email)}</div></td>
                <td>
                  <div class="notice mono" id="l_{esc(aid)}">{esc(link)}</div>
                  <div style="height:8px"></div>
                  <div class="actions">
                    <button class="btn" data-copy="#l_{esc(aid)}">Kopiuj</button>
                    <a class="btn ghost" href="{esc(link)}" target="_blank">Otwórz</a>
                    <a class="btn" href="/dashboard/architect/delete?id={esc(aid)}" onclick="return confirm('Usunąć architekta?')">Usuń</a>
                  </div>
                </td>
              </tr>
            ''')
        if not rows:
            rows_html = '<div class="notice">Brak architektów. Dodaj pierwszego — wtedy pojawi się link do briefu.</div>'
        else:
            rows_html = f'''
              <table class="table">
                <thead><tr><th>Architekt</th><th>Link do briefu</th></tr></thead>
                <tbody>{''.join(rows)}</tbody>
              </table>
            '''

        content = f'''
        <div class="headrow">
          <div>
            <h1 class="h1">Architekci</h1>
            <p class="lead sub">Każdy architekt dostaje własny link do formularza. Wysyłasz inwestorowi i czekasz na raport.</p>
          </div>
        </div>

        <div class="panel card">
          <div class="k">DODAJ ARCHITEKTA</div>
          <div style="height:10px"></div>
          <form method="post" action="/dashboard/architect/add">
            <div class="fields">
              <div class="field"><label>Imię i nazwisko</label><input name="name" placeholder="np. Jan Kowalski"/></div>
              <div class="field"><label>Email (na ten adres idzie raport)</label><input type="email" name="email" placeholder="jan@..."/></div>
            </div>
            <div style="height:12px"></div>
            <div class="actions">
              <button class="btn gold" type="submit">Dodaj</button>
              <a class="btn ghost" href="/demo" target="_blank">Zobacz brief</a>
            </div>
          </form>
        </div>

        <div style="height:14px"></div>
        {rows_html}
        '''

    elif tab == "pricing":
        pt = (company.get("pricing_text") or "").strip()
        content = f'''
        <div class="headrow">
          <div>
            <h1 class="h1">Cennik</h1>
            <p class="lead sub">Opcjonalnie. Jeśli wkleisz cennik, raport spróbuje podać widełki i logikę wyceny.</p>
          </div>
        </div>

        <div class="panel card">
          <form method="post" action="/dashboard/pricing">
            <div class="field full">
              <label>Tekst cennika</label>
              <textarea name="pricing_text" placeholder="Wklej zasady wyceny (np. stawki / zakres / założenia)">{esc(pt)}</textarea>
              <div class="muted" style="margin-top:8px">Tip: wrzuć format „pakiety + dopłaty + wyłączenia”.</div>
            </div>
            <div style="height:12px"></div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz</button>
            </div>
          </form>
        </div>
        '''

    elif tab == "billing":
        b = company.get("billing") or {}
        content = f'''
        <div class="headrow">
          <div>
            <h1 class="h1">Faktury</h1>
            <p class="lead sub">Dane do faktury / rozliczeń. (Stripe może też pobierać te dane w swoim portalu.)</p>
          </div>
        </div>

        <div class="panel card">
          <form method="post" action="/dashboard/billing">
            <div class="fields">
              <div class="field"><label>Nazwa firmy</label><input name="company_name" value="{esc(str(b.get("company_name") or ""))}" placeholder="np. Pracownia XYZ Sp. z o.o."/></div>
              <div class="field"><label>NIP</label><input name="nip" value="{esc(str(b.get("nip") or ""))}" placeholder="np. 1234567890"/></div>
              <div class="field full"><label>Adres</label><input name="address" value="{esc(str(b.get("address") or ""))}" placeholder="ul. ..., miasto"/></div>
              <div class="field full"><label>Email do faktur</label><input type="email" name="invoice_email" value="{esc(str(b.get("invoice_email") or ""))}" placeholder="np. faktury@..."/></div>
            </div>
            <div style="height:12px"></div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz</button>
            </div>
          </form>
        </div>
        '''

    elif tab == "reports":
        def fmt(ts: int) -> str:
            try:
                return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                return "-"

        if reports:
            rows = []
            for r in reports:
                rid = str(r.get("id") or "")
                title = str(r.get("title") or "Raport")[:90]
                arch = str(r.get("architect_name") or "")
                ts = fmt(int(r.get("created_at") or 0))
                sent_flag = bool(r.get("email_sent"))
                pill = '<span class="pill ok">wysłany</span>' if sent_flag else '<span class="pill bad">niewysłany</span>'
                rows.append(f'''
                  <tr>
                    <td><b>{esc(title)}</b><div class="muted">{esc(ts)}</div></td>
                    <td><div class="muted">{esc(arch)}</div><div style="margin-top:6px">{pill}</div></td>
                    <td>
                      <div class="actions">
                        <a class="btn" href="/dashboard/report/view?id={esc(rid)}">Podgląd</a>
                        <a class="btn ghost" href="/dashboard/report/download?id={esc(rid)}">Pobierz .txt</a>
                      </div>
                    </td>
                  </tr>
                ''')
            content = f'''
            <div class="headrow">
              <div>
                <h1 class="h1">Raporty</h1>
                <p class="lead sub">Historia ostatnich raportów (limit: {MAX_REPORTS_PER_COMPANY}).</p>
              </div>
              <div class="actions">
                <a class="btn ghost" href="/report-demo" target="_blank">Zobacz demo</a>
              </div>
            </div>

            <table class="table">
              <thead><tr><th>Raport</th><th>Architekt</th><th>Akcje</th></tr></thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
            '''
        else:
            content = f'''
            <div class="headrow">
              <div>
                <h1 class="h1">Raporty</h1>
                <p class="lead sub">Tu będzie historia raportów po pierwszych wypełnieniach briefu.</p>
              </div>
            </div>
            <div class="notice">Brak raportów. Dodaj architekta → wyślij link do inwestora → raport pojawi się tutaj.</div>
            '''

    elif tab == "plan":
        stripe_status = str((company.get("stripe") or {}).get("status") or "inactive")
        stripe_ready_flag = stripe_ready()
        badge = '<span class="badge ok">Stripe OK</span>' if stripe_ready_flag else '<span class="badge bad">Stripe OFF</span>'
        status_badge = '<span class="badge ok">dostęp aktywny</span>' if access_ok else '<span class="badge bad">brak dostępu</span>'

        pay_actions = ""
        if stripe_ready_flag:
            pay_actions = f'''
              <div class="actions">
                <a class="btn gold" href="/stripe/checkout/monthly">Kup miesięczny</a>
                <a class="btn" href="/stripe/checkout/yearly">Kup roczny</a>
                <a class="btn ghost" href="/billing/portal">Zarządzaj subskrypcją</a>
                <a class="btn ghost" href="/billing/portal">Anuluj subskrypcję</a>
              </div>
              <div class="muted" style="margin-top:10px">Uwaga: anulowanie/zmiana planu odbywa się w portalu Stripe.</div>
            '''
        else:
            pay_actions = '<div class="notice">Stripe nie jest skonfigurowany na serwerze (brak kluczy ENV). Skontaktuj się z adminem wdrożenia.</div>'

        content = f'''
        <div class="headrow">
          <div>
            <h1 class="h1">Plan / Subskrypcja</h1>
            <p class="lead sub">Plan: <b>{esc(PLAN_LABELS.get(plan, plan))}</b> {status_badge} • Stripe status: <b>{esc(stripe_status)}</b> {badge}</p>
          </div>
        </div>

        <div class="panel card">
          <div class="k">CO JEST W PLANIE</div>
          <div class="divider"></div>
          <div class="grid2">
            <div class="notice">
              <b>Limit miesięczny:</b> {esc(str(_forms_limit(company)))}<br/>
              <span class="muted">Aktualnie wysłane: {esc(str(sent))} • Pozostało: {esc(str(remaining))}</span>
            </div>
            <div class="notice">
              <b>Anulowanie subskrypcji:</b> w portalu Stripe.<br/>
              <span class="muted">Przycisk jest poniżej (lub w menu po lewej).</span>
            </div>
          </div>
          <div style="height:12px"></div>
          {pay_actions}
        </div>
        '''

    body = f'''
    <div class="wrap">
      <div class="dash">
        {sidebar}
        <div class="panel main card">
          {content}
        </div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Panel firmy", body=body, nav="", request=request, page="dash"))
@app.get("/dashboard/plan/free")
def dashboard_set_free_plan(request: Request):
    gate = require_company(request)
    if gate:
        return gate
    if not ENABLE_FREE_PLAN:
        return RedirectResponse(url="/dashboard", status_code=302)

    company = get_company(request)
    assert company is not None

    # Nie nadpisuj planu platnego
    st = (company.get("stripe") or {}).get("status") or ""
    if st in ("active", "trialing"):
        return RedirectResponse(url="/dashboard", status_code=302)

    db = _load_db()
    cid = company["id"]
    if cid in db.get("companies", {}):
        db["companies"][cid]["plan"] = "free"
        _save_db(db)
    return RedirectResponse(url="/dashboard", status_code=302)
@app.post("/dashboard/pricing")
async def save_pricing(request: Request):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    form = await request.form()
    pricing_text = (form.get("pricing_text") or "").strip()

    db = _load_db()
    cid = company["id"]
    db["companies"][cid]["pricing_text"] = pricing_text
    _save_db(db)
    return RedirectResponse(url="/dashboard?tab=pricing", status_code=302)

@app.post("/dashboard/billing")
async def save_billing(request: Request):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    form = await request.form()
    billing = {
        "company_name": (form.get("company_name") or "").strip(),
        "nip": (form.get("nip") or "").strip(),
        "address": (form.get("address") or "").strip(),
        "invoice_email": (form.get("invoice_email") or "").strip(),
    }

    db = _load_db()
    cid = company["id"]
    db["companies"][cid]["billing"] = billing
    _save_db(db)
    return RedirectResponse(url="/dashboard?tab=billing", status_code=302)

@app.post("/dashboard/architect/add")
async def add_architect(request: Request):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    form = await request.form()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()

    if not name or not email:
        return RedirectResponse(url="/dashboard?tab=architects", status_code=302)

    db = _load_db()
    cid = company["id"]
    a = {
        "id": _new_id("arch"),
        "name": name,
        "email": email,
        "token": secrets.token_urlsafe(16),
    }
    db["companies"][cid]["architects"].append(a)
    _save_db(db)
    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/dashboard/architect/delete")
def delete_architect(request: Request, id: str = ""):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    db = _load_db()
    cid = company["id"]
    db["companies"][cid]["architects"] = [a for a in db["companies"][cid].get("architects", []) if a.get("id") != id]
    _save_db(db)
    return RedirectResponse(url="/dashboard?tab=architects", status_code=302)


@app.get("/dashboard/report/view", response_class=HTMLResponse)
def dashboard_report_view(request: Request, id: str = ""):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    _ensure_reports(company)
    rep = None
    for r in company.get("reports") or []:
        if str(r.get("id") or "") == str(id or ""):
            rep = r
            break

    if not rep:
        body = flash_html("Nie znaleziono raportu.") + '<div class="wrap formwrap"><a class="btn" href="/dashboard?tab=reports">Wróć</a></div>'
        return HTMLResponse(layout("Raport", body=body, request=request, page="dash"))

    title = str(rep.get("title") or "Raport")
    text = str(rep.get("report") or "")
    meta_arch = str(rep.get("architect_name") or "")

    body = f'''
    <div class="wrap formwrap">
      <div class="headrow">
        <div>
          <h1 class="h1" style="margin:0">{esc(title)}</h1>
          <p class="lead sub">Architekt: <b>{esc(meta_arch)}</b> • Raport zapisany w historii.</p>
        </div>
        <div class="actions">
          <a class="btn" href="/dashboard?tab=reports">Wróć</a>
          <a class="btn ghost" href="/dashboard/report/download?id={esc(str(id))}">Pobierz .txt</a>
          <button class="btn" data-copy="#repText">Kopiuj</button>
        </div>
      </div>
      <div class="panel card">
        <div class="codebox" id="repText">{esc(text)}</div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Raport", body=body, request=request, page="dash"))

@app.get("/dashboard/report/download")
def dashboard_report_download(request: Request, id: str = ""):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    _ensure_reports(company)
    rep = None
    for r in company.get("reports") or []:
        if str(r.get("id") or "") == str(id or ""):
            rep = r
            break
    if not rep:
        return PlainTextResponse("Not found", status_code=404)

    title = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(rep.get("title") or "report"))[:50].strip("_") or "report"
    fname = f"archibot_{title}_{id}.txt"
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return PlainTextResponse(str(rep.get("report") or ""), headers=headers)

@app.get("/billing/portal")
def billing_portal(request: Request):
    gate = require_company(request)
    if gate:
        return gate

    if not stripe_ready():
        return RedirectResponse(url="/dashboard?tab=plan", status_code=302)

    stripe_init()

    db = _load_db()
    company = get_company(request)
    assert company is not None
    cid = company["id"]

    c = db["companies"].get(cid) or company
    stripe_meta = c.get("stripe") or {}
    customer_id = str(stripe_meta.get("customer_id") or "").strip()
    subscription_id = str(stripe_meta.get("subscription_id") or "").strip()

    # Spróbuj odtworzyć customer_id jeśli brakuje
    if (not customer_id) and subscription_id:
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
            customer_id = str(sub.get("customer") or "").strip()
        except Exception:
            customer_id = ""

    if (not customer_id) and c.get("email"):
        try:
            res = stripe.Customer.list(email=str(c.get("email") or "").strip(), limit=1)
            if getattr(res, "data", None):
                customer_id = str(res.data[0].id)
        except Exception:
            customer_id = ""

    if not customer_id:
        # Bez customer_id nie utworzymy sesji portalu — wróć z komunikatem
        return RedirectResponse(url="/dashboard?tab=plan", status_code=302)

    # Zapisz customer_id w bazie dla przyszłych wejść
    db["companies"][cid].setdefault("stripe", {})
    db["companies"][cid]["stripe"]["customer_id"] = customer_id
    _save_db(db)

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{BASE_URL}/dashboard?tab=plan",
        )
        return RedirectResponse(url=portal.url, status_code=303)
    except Exception as e:
        print(f"[STRIPE] billing_portal failed customer_id={customer_id} err={type(e).__name__}: {e}")
        return RedirectResponse(url="/dashboard?tab=plan", status_code=302)



# =========================
# 13) Demo formularza (publiczne)
# =========================

@app.get("/demo", response_class=HTMLResponse)
def demo():
    return HTMLResponse(render_form(
        action_url="/demo/submit",
        title="Brief przemysłowy (podgląd)",
        subtitle="Podgląd formularza. W wersji produkcyjnej raport trafia do architekta."
    ))

@app.post("/demo/submit", response_class=HTMLResponse)
async def demo_submit(request: Request):
    formdata = await request.form()
    form_dict: Dict[str, Any] = {}
    for k in formdata.keys():
        v = formdata.get(k)
        if k == "attachments":
            continue
        if v == "1":
            form_dict[k] = True
        else:
            form_dict[k] = v
    form_clean = _clean_form_dict(form_dict)

    report = """
# RAPORT DLA ARCHITEKTA (przemysł) – ARCHITEKTONICZNE STUDIO HUBERT STENZEL

**Projekt:** Hala Produkcyjno-Magazynowa JK1
**Klient:** Janusz Kowalski Development Sp. z o.o.
**Lokalizacja:** Park Przemysłowy Nowa Strefa, Gmina X
**Architekt:** Franek <franekstenzel@gmail.com>

---

## 1) Streszczenie
- Raport przygotowany **na podstawie formularza klienta**. Każdy wpis ma źródło: `client_form` lub `assumption`.
- Obiekt: przemysł/logistyka – priorytety: PPOŻ, BHP, technologia, logistyka, media.

---

## 2) Dane wejściowe z formularza (tabela)
| Sekcja | Parametr | Wartość | Źródło | Pewność |
| --- | --- | --- | --- | --- |
| A. Inwestor | Nazwa inwestora / spółki | Janusz Kowalski Development Sp. z o.o. | client_form | 0.99 |
| A. Inwestor | Osoba kontaktowa | Janusz Kowalski, Prezes Zarządu / Inwestor | client_form | 0.99 |
| A. Inwestor | Kto podejmuje decyzje projektowe? | Zarząd (Prezes) – decyzje strategiczne; operacyjne delegowane do PM po stronie inwestora; kluczowe etapy wymagają akceptacji Zarządu. | client_form | 0.9 |
| A. Inwestor | Proces akceptacji | Cotygodniowe spotkania online, akceptacje do 72h, odbiory etapowe. | client_form | 0.9 |
| A. Inwestor | Interesariusze po stronie inwestora | BHP – zewnętrzny doradca; PPOŻ – rzeczoznawca; Technologia – kierownik produkcji; IT – zewnętrzny dostawca; FM – przyszły facility manager; Audyt – audytor korporacyjny. | client_form | 0.9 |
| B. Inwestycja | Charakter inwestycji | Nowy obiekt | client_form | 0.99 |
| B. Inwestycja | Typ obiektu | Hala produkcyjna (produkcyjno-magazynowa) | client_form | 0.99 |
| B. Inwestycja | Cel inwestycji / KPI | Nowy zakład produkcyjny komponentów metalowych; praca 2-zmianowa; skalowalność +30% w 5 lat. | client_form | 0.9 |
| B. Inwestycja | Horyzont użytkowania | 25 lat | client_form | 0.9 |
| B. Inwestycja | Rozbudowa/etapowanie | Tak – rozbudowa w przyszłości, wysoka elastyczność | client_form | 0.9 |
| B. Inwestycja | Porażka inwestycji – definicja | Brak możliwości rozbudowy, ograniczenia energetyczne, niedostosowanie do przyszłych linii. | client_form | 0.9 |
| C. Działka | Lokalizacja | Park Przemysłowy Nowa Strefa, Gmina X; działki 123/4, 123/5; pow. 28 000 m² | client_form | 0.99 |
| C. Działka | Status własności | Własność inwestora | client_form | 0.99 |
| C. Działka | Ograniczenia / ryzyka środowiskowe | Brak linii WN, brak stref zalewowych; teren płaski 1–2% spadku; brak drzew kolidujących. | client_form | 0.9 |
| D. Geotechnika | Opinia geotechniczna | Posiadana; grunt: glina; wody gruntowe >5 m p.p.t.; nośność dobra. | client_form | 0.95 |
| E. Formalności | Podstawa planistyczna | MPZP; wypis i wyrys posiadane. | client_form | 0.95 |
| E. Formalności | Decyzja środowiskowa | Status: nie wiem (do potwierdzenia). | client_form | 0.7 |
| F. Media | Warunki przyłączenia mediów | EE, woda, kanalizacja sanitarna, gaz, MEC – warunki posiadane. | client_form | 0.9 |
| F. Media | Zasilanie / moc | Własna stacja trafo; moc teraz 500 kW, rezerwa 800 kW. | client_form | 0.95 |
| F. Media | Woda/ścieki/opadowe | Woda – studnia; ścieki – zbiornik bezodpływowy; deszczówka – zbiornik retencyjny. | client_form | 0.9 |
| G. Program | Powierzchnie funkcjonalne | PU 8 500 m²: produkcja 4 500; magazyn 2 500; wysyłka 800; biura 500; socjal 200. | client_form | 0.99 |
| H. Technologia | Proces produkcyjny – opis | Dostawa → magazyn → obróbka → montaż → pakowanie → wysyłka. | client_form | 0.9 |
| H. Technologia | Warunki procesu / zagrożenia | Hałas 70–80 dB(A); pylenie wysokie (>5 mg/m³); zagrożenia: chemikalia; wymagania temperaturowe: mroźnia (do potwierdzenia). | client_form | 0.75 |
| I. Parametry | Kondygnacje / dach | Hala 1 kond.; biura 2 kond.; dach dwuspadowy. | client_form | 0.95 |
| I. Parametry | Suwnica | Tak – suwnica przewidziana (parametry do ustalenia). | client_form | 0.85 |
| J. Posadzka | Obciążenia posadzki | 50 kN/m²; standardowa posadzka przemysłowa. | client_form | 0.9 |
| K. Logistyka | Strefa załadunku | Rampa + 6–10 doków; dostawy 24/7; wózki LPG; regały wysokiego składowania. | client_form | 0.9 |
| M. Instalacje | Ogrzewanie / wentylacja | Ogrzewanie: sieć ciepłownicza; wentylacja: mechaniczna. | client_form | 0.9 |
| N. PPOŻ | PPOŻ – dane | Sprinkler: nie wiem; obciążenie ogniowe Q ≤ 500 MJ/m². | client_form | 0.8 |
| P. Organizacja | Tryb pracy | Ruch ciągły 24/7; poziom bezpieczeństwa wysoki (strefy krytyczne). | client_form | 0.9 |
| R. Standardy | Wymagania korporacyjne | ISO 9001; BIM – opcjonalnie; NDA wymagane. | client_form | 0.9 |

---

## 3) Pytania / RFI
**Blockery (bez tego nie domykamy wyceny / zakresu):**
- Potwierdzenie, czy faktycznie wymagana jest mroźnia w procesie produkcji komponentów metalowych (to istotnie zmienia instalacje, przegrody i koszty).
- Parametry suwnicy: udźwig [t], rozpiętość, ilość torów, strefy pracy, wymagana wysokość podhacznikowa.
- Wysokość hali w świetle oraz siatka słupów (wymagana vs. dopuszczalna) – wpływ na regały i logistykę.
- Czy wymagana jest instalacja tryskaczowa (FM/VS) – jeżeli tak, jaka klasa ryzyka i źródło wody pożarowej?
- Decyzja środowiskowa: czy wymagane jest postępowanie OOŚ (screening) – prosimy o stanowisko organu/eksperta.
- Dane do składowania/obsługi chemikaliów: rodzaje, ilości, ADR, magazynowanie (regały, kuwetowanie), wentylacja i retencja rozlewów.
- Warunki przyłączenia – prosimy o skany: EE, gaz, woda, kanalizacja, MEC; w szczególności dostępność mocy 800 kW w horyzoncie rozbudowy.
- Rozwiązanie dla Internetu/światłowodu – dostępność operatora, wymagania IT/OT.
- Zatwierdzenie źródeł wody (studnia) i ścieków (zbiornik bezodpływowy) – konieczne pozwolenia wodnoprawne/zgłoszenia?
- Liczba i parametry doków (6, 8 czy 10?) oraz typy ramp/bram; układ dróg pożarowych i TIR.
- Standard wykończenia biur i socjalnych (materiały, HVAC, fit-out).

**Ważne (wpływ na budżet / terminy / ryzyka):**
- Preferowany model realizacji: D&B czy tradycyjny (projekt + przetarg + budowa)?
- Plan rezerw pod rozbudowę: kierunek i minimalny bufor na działce (m², układ dróg/mediów pod etapowanie).
- Wymogi FM/serwisu dla 24/7 (strefowanie, dostęp serwisowy, redundancje).
- Czy wymagane są audyty/dokumentacja pod ISO 9001 na etapie projektu i uruchomienia?
- Poziom automatyzacji magazynu (WMS, pętla indukcyjna, VNA) i wymagania pod posadzkę/znaczniki.
- Docelowa temperatura/warunki w strefach (produkcja, magazyn, wysyłka, biura).
- Wymagania BHP dla pyłów: system odpylania, filtry, ATEX – potwierdzenie braku ATEX.

**Opcjonalne:**
- Czy przewidziane jest BIM (LOD 300–400) – jeśli tak, +20% do ceny projektu.
- Czy oczekiwany jest Inwestor Zastępczy (2,5–4% kosztów)?
- Zakres nadzoru autorskiego: ryczałt wizyt vs. % od inwestycji.
- Wymagania ESG (np. PV na dachu, BREEAM/LEED) – mogą wpływać na projekt i koszty.

---

## 4) Braki dokumentów / formalności
- Wypis i wyrys MPZP – kopia do teczki projektowej.
- Opinia geotechniczna – pełny dokument (PDF) z wierceniami i wnioskami.
- Warunki przyłączenia: EE, woda, kanalizacja, gaz, MEC – kopie.
- Mapa do celów projektowych 1:500 oraz mapa zasadnicza.
- Badania hydro – jeśli planowana studnia/zbiornik retencyjny – decyzje/pozwolenia wodnoprawne (jeśli już są).
- Inwentaryzacja zieleni (jeśli wymagana do zgłoszeń).
- Wstępny layout technologiczny (URS) z danymi o maszynach, emisjach, mediami procesowymi.
- Założenia dla suwnicy (karta techniczna/wytyczne).
- Wytyczne FM/IT (sieć strukturalna, CCTV, kontrola dostępu).
- Polityka bezpieczeństwa/ochrona – strefowanie, ogrodzenie, kontrola dostępu.

---

## 5) Wycena projektu (kalkulacja + uzasadnienie)
**Podstawa interpretacji cennika:** Ceny netto wg cennika (bez VAT 23%). Wariant: projekt wielobranżowy (komplet PB+PT+PW) + prace przedprojektowe i operat ppoż. Widełki wynikają z stawek jednostkowych i pozycji 'od ... PLN'. Pozycje OOŚ, nadzór autorski, projekt technologii – poza sumą (TBD).

| Pozycja | Baza | Ilość | Jedn. | Stawka [PLN] | Kwota [PLN] | Źródło | Uzasadnienie |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Analiza chłonności terenu – LOW | Ryczałt od 3 500 PLN | 1 | ryczałt | 3 500 | 3 500 | pricing_text | Minimalna stawka katalogowa. |
| Analiza chłonności terenu – HIGH | Zakres rozszerzony (spotkania/warianty) | 1 | ryczałt | 6 000 | 6 000 | assumption | Możliwy większy nakład prac przy etapowaniu/rozbudowie. |
| Koncepcja architektoniczna – LOW | 10 PLN/m² | 8500 | m² | 10 | 85 000 | pricing_text | Wizualizacje, rzuty, bilans terenu – wariant podstawowy. |
| Koncepcja architektoniczna – HIGH | 20 PLN/m² | 8500 | m² | 20 | 170 000 | pricing_text | Więcej wariantów, koordynacje międzybranżowe. |
| Audyt techniczny działki (Due Diligence) – LOW | od 4 000 PLN | 1 | ryczałt | 4 000 | 4 000 | pricing_text | Przegląd formalny, uzbrojenie, ograniczenia. |
| Audyt techniczny działki (Due Diligence) – HIGH | rozszerzony zakres | 1 | ryczałt | 8 000 | 8 000 | assumption | Dodatkowe wizje lokalne/uzgodnienia. |
| Projekt wielobranżowy (komplet PB+PT+PW) – LOW | 90 PLN/m² | 8500 | m² | 90 | 765 000 | pricing_text | Architektura, konstrukcja, instalacje wew./zew. – zakres podstawowy. |
| Projekt wielobranżowy (komplet PB+PT+PW) – HIGH | 150 PLN/m² | 8500 | m² | 150 | 1 275 000 | pricing_text | Złożoność: suwnica, wys. obciążenia, możliwa mroźnia/chemikalia. |
| Operat przeciwpożarowy – LOW | od 5 000 PLN | 1 | ryczałt | 5 000 | 5 000 | pricing_text | Operat + uzgodnienia z rzeczoznawcą ppoż. |
| Operat przeciwpożarowy – HIGH | rozszerzony zakres | 1 | ryczałt | 10 000 | 10 000 | assumption | Większa liczba stref pożarowych/uzgodnień. |
| Analiza oddziaływania na środowisko (OŚ) – OPCJA | od 8 000 PLN | 1 | ryczałt | 8 000 | 8 000 | pricing_text | Tylko jeśli organ nakaże OOŚ – poza sumą (TBD). |
| Nadzór autorski – OPCJA | 500 PLN/wizyta lub 1–2% CAPEX | 12 | wizyta | 500 | 6 000 | pricing_text | Model rozliczenia do uzgodnienia – poza sumą (TBD). |

**Suma (widełki):** 862 500 – 1 469 000 PLN

**W zakresie:**
- Analiza chłonności terenu.
- Koncepcja architektoniczna (wstępna).
- Projekt wielobranżowy komplet: PB+PT+PW (architektura, konstrukcja, instalacje wewnętrzne i zewnętrzne do granicy działki).
- Operat przeciwpożarowy i uzgodnienia ppoż/BHP/sanepid.

**Poza zakresem:**
- Wniosek o WZ (nie dotyczy – MPZP).
- Projekt technologii przemysłowej/linii – wycena indywidualna (po wytycznych technologa).
- Decyzja środowiskowa i raport OOŚ – jeśli wymagane przez organ (TBD).
- Nadzór autorski ryczałt/procent – do uzgodnienia (TBD).
- Inwestor Zastępczy (2,5–4% kosztów) – usługa opcjonalna.
- Mapa do celów projektowych i badania geotechniczne – zlecane odrębnie (poza cennikiem).

---

## 6) Średni koszt budowy (widełki + czynniki)
| Standard | Region | PLN/m² low | PLN/m² mid | PLN/m² high | Total low | Total mid | Total high |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Standard | Mniejsze miasto / okolice | 5 400 | 6 000 | 6 900 | 45 900 000 | 51 000 000 | 58 650 000 |

**Czynniki kosztotwórcze:**
- Suwnica – wzrost tonażu konstrukcji, wzmocnienia podtorzy.
- Wysokie obciążenia posadzki (50 kN/m²) – zbrojenie/technologia posadzki, dylatacje.
- Regały wysokiego składowania – wpływ na wysokość hali, instalacje tryskaczowe (jeśli będą).
- Możliwe wymagania mroźni – izolacje termiczne, chłodnictwo, szczelność przegród (do potwierdzenia).
- Chemikalia i wysokie pylenie – instalacje wentylacji/odpylania, separacja stref, retencja rozlewów.
- Zasilanie własną stacją trafo – CAPEX przyłącza/GPZ, rezerwy mocy.
- Ścieki do zbiornika bezodpływowego i retencja deszczówki – dodatkowa infrastruktura zewnętrzna.
- Tryb 24/7 i wysoki poziom bezpieczeństwa – systemy SSWiN, CCTV, KD, redundancje HVAC/EE.
- 6–10 doków i rampa – place manewrowe, nawierzchnie o podwyższonej nośności.

---

## 7) Ryzyka / uwagi architekta (tabela)
| Obszar | Priorytet | Ryzyko | Skutek | Mitigacja / co sprawdzić |
| --- | --- | --- | --- | --- |
| PPOŻ | P0 | Brak decyzji nt. tryskaczy i klasy odporności pożarowej; chemikalia w procesie. | Możliwe przeprojektowania, wzrost kosztów instalacji/gromadzenia wody ppoż. | Wczesne uzgodnienia z rzeczoznawcą ppoż.; analiza Q, scenariusze pożarowe; decyzja o sprinkler/FM. |
| BHP | P1 | Wysokie pylenie i hałas 70–80 dB(A). | Ryzyko niezgodności z NDS/PN, konieczność kosztownych systemów odpylania/wyciszeń. | Projekt systemów odpylania, separacja źródeł hałasu, strefy ruchu pieszych, audyt BHP. |
| Technologia | P0 | Brak szczegółowego layoutu linii i wymagań mediów procesowych. | Ryzyko kolizji międzybranżowych i zmiany konstrukcji/instalacji na późnym etapie. | Warsztaty z kier. produkcji; zamrożenie URS/URS-M na etapie koncepcji; rezerwy w posadzce/kanale mediów. |
| Logistyka | P1 | Niedookreślona liczba doków i parametry ruchu TIR/OSD. | Niewystarczająca przepustowość, zatory, konieczność rozbudowy placów. | Analizy przepustowości, symulacje ruchu, docelowy masterplan z etapowaniem. |
| Media | P0 | Niepewność co do dostępności 800 kW w horyzoncie rozbudowy. | Ograniczenie mocy – ryzyko braku skalowalności. | Wnioski do OSD o rezerwę mocy; projekt stacji trafo pod 800 kW; miejsce na drugie trafo. |
| Konstrukcja | P1 | Suwnica + 50 kN/m² – zwiększone obciążenia na fundamenty i słupy. | Wyższy CAPEX, możliwe zmiany siatki słupów/wysokości. | Wczesne obliczenia statyczne, definicja parametrów suwnicy, próby podłoża pod torowiska. |
| Formalne | P1 | Możliwa konieczność OOŚ/pozwoleń wodnoprawnych (studnia, retencja, zbiornik). | Wydłużenie procedur, warunki środowiskowe dla eksploatacji. | Screening środowiskowy; konsultacja z RDOŚ/Wodami Polskimi; harmonogram decyzji. |
| Środowisko | P2 | Zbiornik bezodpływowy – ryzyko pojemności/wywozu; retencja deszczówki – wymiarowanie. | Koszty operacyjne i inwestycyjne, ewentualne rozbudowy zbiorników. | Bilans ścieków/deszczówki, analiza opadów, przewymiarowanie na etapowanie. |

---

## 8) Założenia (jawne)
- Szacunek kosztów budowy oparty o standard wykonania „Standard” i region „Mniejsze miasto/okolice” – brak wskazania w briefie.
- Jednostkowe widełki dla pozycji z oznaczeniem „od … PLN” (analiza chłonności, due diligence, operat ppoż.) przyjęto orientacyjnie do górnego zakresu kosztów (nie stanowi oferty).
- Wycena projektówa przyjęta jako „Projekt wielobranżowy – komplet (PB+PT+PW)”, aby uniknąć dublowania pozycji PB i PW.
- Mapa do celów projektówych i badania geotechniczne poza zakresem cennika – koszty po stronie zewnętrznych dostawców.
- BIM traktowany jako opcja (+20% do projektu) – w bazowej kalkulacji nie ujęto.
- Nadzór autorski i Inwestor Zastępczy – pozycje opcjonalne, poza bazową sumą.

---

## 9) Następne kroki
- Podpisanie NDA i przekazanie dokumentów: MPZP (wypis/wyrys), geotechnika, warunki przyłączeniowe, mapa do celów projektówych.
- Warsztaty funkcjonalno-technologiczne (2–3 h) z kierownikiem produkcji i BHP/PPOŻ – doprecyzowanie layoutu, suwnicy i stref poż.
- Decyzja dot. mroźni: czy występuje i w jakim zakresie – jeśli tak, przygotujemy wariant instalacyjny.
- Potwierdzenie liczby doków i parametrów placów manewrowych; wstępny masterplan z rezerwą pod rozbudowę.
- Screening środowiskowy (czy wymagane OOŚ) + wstępne uzgodnienia wodnoprawne (studnia, retencja, zbiornik).
- Aktualizacja koncepcji i kosztorysu inwestorskiego (CAPEX) po doprecyzowaniu kluczowych założeń.
- Uzgodnienie trybu współpracy (D&B vs. tradycyjny), kalendarz spotkań i kamieni milowych.

---

## 10) Wiadomość do klienta (copy/paste)
**Temat:** Hala Produkcyjno-Magazynowa JK1 – podsumowanie briefu, widełki kosztów i pytania kluczowe

```text
Szanowny Panie Prezesie,

Dziękujemy za wypełnienie briefu dla projektu „Hala Produkcyjno-Magazynowa JK1”. Poniżej przesyłamy podsumowanie oraz proponowane kolejne kroki.

1) Zakres i wstępne widełki kosztów projektu (netto):
- Analiza chłonności terenu: 3 500 – 6 000 PLN (ryczałt).
- Koncepcja architektoniczna: 10–20 PLN/m² → 85 000 – 170 000 PLN.
- Projekt wielobranżowy (komplet PB+PT+PW): 90–150 PLN/m² → 765 000 – 1 275 000 PLN.
- Operat ppoż.: 5 000 – 10 000 PLN.
- Audyt techniczny działki (Due Diligence): 4 000 – 8 000 PLN.
Suma orientacyjna (bazowy zakres, bez VAT): ok. 862 500 – 1 469 000 PLN.
Pozycje opcjonalne (poza sumą): Analiza oddziaływania na środowisko (od 8 000 PLN – jeśli organ tego wymaga), Nadzór Autorski (500 PLN/wizyta lub 1–2% wartości inwestycji), projekt technologii linii (wycena indywidualna).

2) Szacunek kosztów realizacji (CAPEX, standard „Standard”, lokalizacja: mniejsze miasto – założenia robocze):
- 5 400 – 6 900 PLN/m²; przy 8 500 m² daje to ok. 45,9 – 58,65 mln PLN netto.
Kluczowe czynniki kosztowe: suwnica i obciążenia 50 kN/m², potencjalna mroźnia, chemikalia i odpylanie, liczba doków 6–10, własna stacja trafo, retencja i zbiornik bezodpływowy, tryb 24/7.

3) Pytania blokujące (prosimy o odpowiedź/załączniki):
- Czy faktycznie wymagana jest mroźnia w procesie (dla komponentów metalowych)? Jeśli tak – jakie parametry?
- Parametry suwnicy: udźwig, rozpiętość, ilość torów, wysokość podhacznikowa.
- Wysokość hali w świetle oraz oczekiwana siatka słupów.
- Czy wymagany będzie sprinkler (i jaka klasa ryzyka)?
- Decyzja środowiskowa/OOŚ – czy była wstępna konsultacja z organem?
- Skany warunków przyłączenia (EE, gaz, woda, kanalizacja, MEC) + informacja o rezerwie 800 kW.
- Dane o chemikaliach (rodzaje, ilości, sposób magazynowania, ADR) i wymogi BHP dla pyłów.
- Liczba doków docelowo (6/8/10) i założenia dla placów manewrowych.
- Standard wykończenia biur i socjalnych.

4) Proponowane kolejne kroki:
- Podpisanie NDA i przekazanie dokumentów (MPZP, geotechnika, warunki przyłączenia, mapa do celów projektówych).
- Krótki warsztat funkcjonalno-technologiczny (online) – doprecyzowanie layoutu i kluczowych parametrów.
- Aktualizacja koncepcji i kosztorysu inwestorskiego po uzgodnieniach.

Jesteśmy gotowi rozpocząć od Analizy Chłonności i Koncepcji. Proszę o informację dot. dostępnych terminów na warsztat oraz o dosłanie ww. dokumentów.

Z wyrazami szacunku,
Franek
ARCHITEKTONICZNE STUDIO HUBERT STENZEL
franekstenzel@gmail.com
"""


    body = f"""
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Raport (podgląd)</h1>
      <p class="muted">Wersja demonstracyjna – raport wyświetlany na ekranie.</p>
      <div class="panel card" style="white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;">
{esc(report)}
      </div>
      <div class="actions">
        <a class="btn gold" href="/demo">Wróć</a>
        <a class="btn" href="/">Strona główna</a>
      </div>
    </div>
    """
    return HTMLResponse(layout("Raport demo", body=body, nav=nav_links()))


# =========================
# 13B) Podgląd raportu + strony prawne
# =========================

@app.get("/report-demo", response_class=HTMLResponse)
def report_demo(request: Request):
    sample = """PODSUMOWANIE (fragment)

Cel: przygotować halę magazynowo-produkcyjną pod logistykę (24/7).
Status danych: część informacji brakująca — poniżej lista pytań i dokumentów.

P0 — BLOKERY (bez tego nie wycenisz rzetelnie)
1) Wymogi PPOŻ: tryskacze / hydranty / klasy odporności / scenariusz pożarowy?
2) Parametry procesu: obciążenia posadzki, wysokość składowania, suwnice / wózki, strefy EX?
3) Media: moce przyłączeniowe, woda ppoż., ścieki technologiczne, gaz, sprężone powietrze?
4) Formalno-prawne: MPZP/WZ, decyzja środowiskowa (OOŚ) — czy wymagana?

P1 — DO DOPRECYZOWANIA (wpływa na zakres)
• Etapowanie inwestycji, harmonogram, okna przestojów.
• Standard biur / socjal / BMS / monitoring / kontrola dostępu.
• Warunki dostaw: doki, rampy, place manewrowe, promienie skrętu.

DOKUMENTY (prośba do inwestora)
• MPZP/WZ, mapa do celów projektowych, warunki przyłączy (energia / woda / kanalizacja).
• Badania geotechniczne / nośność gruntu.
• Opis procesu/technologii + wymagania BHP/PPOŻ.

GOTOWA WIADOMOŚĆ DO KLIENTA (copy/paste)
Dzień dobry, aby przygotować rzetelną wycenę projektu prosimy o uzupełnienie: (1) ... (2) ...
W załączeniu lista pytań P0/P1 oraz dokumentów. Po otrzymaniu danych wracamy z wyceną w terminie ..."""

    body = f'''
    <div class="wrap formwrap">
      <div class="headrow">
        <div>
          <h1 class="h1" style="margin:0">Raport demo</h1>
          <p class="lead sub">To jest przykładowy fragment. W produkcji: raport idzie na mail architekta i zapisuje się w historii w panelu.</p>
        </div>
        <div class="actions">
          <a class="btn gold" href="/register">Załóż konto</a>
          <a class="btn" href="/demo">Podgląd briefu</a>
          <a class="btn ghost" href="/">Strona główna</a>
        </div>
      </div>

      <div class="panel card">
        <div class="actions" style="justify-content:flex-end">
          <button class="btn" data-copy="#demoReport">Kopiuj</button>
        </div>
        <div class="divider"></div>
        <div class="codebox" id="demoReport">{esc(sample)}</div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Raport demo", body=body, nav=nav_links(), request=request))

@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    body = '''
    <div class="wrap formwrap">
      <h1 class="h1" style="margin:0 0 10px">Regulamin</h1>
      <div class="panel card">
        <div class="muted">Minimalna wersja (do uzupełnienia):</div>
        <div style="height:10px"></div>
        <div class="muted">1) Usługa: generowanie raportów na podstawie briefu inwestora.</div>
        <div class="muted">2) Odpowiedzialność: raport to wsparcie, nie porada prawna/projektowa.</div>
        <div class="muted">3) Subskrypcje: płatności i anulowanie przez portal Stripe.</div>
        <div class="muted">4) Kontakt: e-mail z panelu / dane firmy.</div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Regulamin", body=body, nav=nav_links(), request=request))

@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    body = '''
    <div class="wrap formwrap">
      <h1 class="h1" style="margin:0 0 10px">Polityka prywatności</h1>
      <div class="panel card">
        <div class="muted">Minimalna wersja (do uzupełnienia):</div>
        <div style="height:10px"></div>
        <div class="muted">• Przechowujemy dane konta firmy oraz treść raportów w bazie JSON (serwer).</div>
        <div class="muted">• Dane z briefu są używane do wygenerowania raportu i wysyłki na e-mail architekta.</div>
        <div class="muted">• Płatności obsługuje Stripe (portal subskrypcji).</div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Prywatność", body=body, nav=nav_links(), request=request))

@app.get("/security", response_class=HTMLResponse)
def security(request: Request):
    body = '''
    <div class="wrap formwrap">
      <h1 class="h1" style="margin:0 0 10px">Bezpieczeństwo</h1>
      <div class="panel card">
        <div class="muted">• Hasła są haszowane (PBKDF2).</div>
        <div class="muted">• Sesje po HTTPS (jeśli BASE_URL ma https).</div>
        <div class="muted">• Link do briefu jest unikalny dla architekta.</div>
      </div>
    </div>
    '''
    return HTMLResponse(layout("Bezpieczeństwo", body=body, nav=nav_links(), request=request))


# =========================
# 14) Formularz firmowy /f/{token}
# =========================

def find_by_token(token: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    db = _load_db()
    for c in db["companies"].values():
        for a in c.get("architects", []):
            if a.get("token") == token:
                return c, a
    return None, None

@app.get("/f/{token}", response_class=HTMLResponse)
def form_for_client(token: str, request: Request):
    company, architect = find_by_token(token)
    if not company or not architect:
        return HTMLResponse(layout("Błąd", body='<div class="wrap formwrap"><h1>Nieprawidłowy link</h1><a class="btn" href="/">Strona główna</a></div>', nav=nav_links()), status_code=404)

    if not subscription_active(company):
        msg = "Dostęp jest czasowo zablokowany." if not ENABLE_FREE_PLAN else "Dostęp wymaga aktywnego planu."
        return HTMLResponse(layout("Dostęp", body=f'<div class="wrap formwrap"><h1>Formularz niedostępny</h1><p class="muted">{msg}</p><a class="btn" href="/">Strona główna</a></div>', nav=nav_links()), status_code=403)

    submit_token = _new_submit_token()
    return HTMLResponse(render_form(
        action_url=f"/f/{token}",
        title=f"Brief inwestorski – {company.get('name','')} / {architect.get('name','')}",
        subtitle="Prosimy o możliwie pełne wypełnienie. Puste pola są dopuszczalne – raport wskaże braki i pytania krytyczne.",
        submit_token=submit_token
    ))

@app.post("/f/{token}", response_class=HTMLResponse)
async def submit_form(token: str, request: Request):
    company, architect = find_by_token(token)
    if not company or not architect:
        return HTMLResponse("Nieprawidłowy link", status_code=404)

    if not subscription_active(company):
        return HTMLResponse("Formularz niedostępny", status_code=403)

    db = _load_db()
    company_id = company.get("id")
    if not company_id or company_id not in db.get("companies", {}):
        return HTMLResponse("Błąd danych firmy", status_code=500)

    c = db["companies"][company_id]
    _ensure_usage_period(c)
    if _forms_remaining(c) <= 0:
        body = f"""
        <div class="wrap formwrap">
          <h1 style="margin:0 0 10px">Limit formularzy wyczerpany</h1>
          <p class="lead">Limit miesięczny został wykorzystany dla tego planu.</p>
          <div class="actions"><a class="btn" href="/">Strona główna</a></div>
        </div>
        """
        return HTMLResponse(layout("Limit", body=body, nav=nav_links()), status_code=429)

    formdata = await request.form()

    submit_token = str(formdata.get("_submit_token") or "")
    if submit_token:
        if _mark_submit_token_used(db, submit_token):
            body = """
            <div class="wrap formwrap">
              <h1 style="margin:0 0 10px">Zgłoszenie zarejestrowane</h1>
              <p class="lead">Brief został już przekazany do analizy.</p>
              <div class="actions"><a class="btn" href="/">Strona główna</a></div>
            </div>
            """
            return HTMLResponse(layout("Status", body=body, nav=nav_links()))
        _save_db(db)

    _increment_forms_sent(db, company_id)
    _save_db(db)

    form_dict: Dict[str, Any] = {}
    for k in formdata.keys():
        if k == "attachments":
            continue
        v = formdata.get(k)
        if v == "1":
            form_dict[k] = True
        else:
            form_dict[k] = v

    form_clean = _clean_form_dict(form_dict)
    pricing_text = company.get("pricing_text", "") or ""

    delivery_id = f"del_{secrets.token_urlsafe(8)}"
    print(f"[FORM] received token={token} company_id={company_id} arch_email={architect.get('email')} delivery_id={delivery_id}")

    report = ai_report(form_clean, pricing_text=pricing_text, company=company, architect=architect)

    sent = False
    if architect.get("email"):
        sent = send_email(
            architect["email"],
            subject=f"[{APP_NAME}] Nowy brief – {company.get('name','')} / {architect.get('name','')}",
            body=report,
            delivery_id=delivery_id,
        )
    else:
        print(f"[EMAIL] FAIL delivery_id={delivery_id} reason=architect has no email in DB")

    # Zapis raportu do historii (panel firmy)
    try:
        _store_report(
            db,
            company_id,
            report_text=report,
            form_clean=form_clean,
            architect=architect,
            delivery_id=delivery_id,
            email_sent=sent,
        )
        _save_db(db)
    except Exception as e:
        print(f"[REPORT] store failed company_id={company_id} err={type(e).__name__}: {e}")

    # Komunikat dla inwestora – profesjonalny, neutralny, bez odsyłania do logów
    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Dziękujemy.</h1>
      <p class="lead">Brief został przekazany do opracowania. Zespół projektówy skontaktuje się w razie potrzeby uzupełnień.</p>
      <div class="actions">
        <a class="btn" href="/">Strona główna</a>
      </div>
    </div>
    """
    return HTMLResponse(layout("Zgłoszenie przyjęte", body=body, nav=nav_links()))


# =========================
# 15) Stripe Checkout + Webhook – bez zmian
# =========================

@app.get("/billing/checkout")
def billing_checkout(request: Request, plan: str = "monthly"):
    gate = require_company(request)
    if gate:
        return gate
    company = get_company(request)
    assert company is not None

    if not stripe_ready():
        return RedirectResponse(url="/dashboard", status_code=302)

    stripe_init()

    price_id = STRIPE_PRICE_ID_MONTHLY if plan == "monthly" else STRIPE_PRICE_ID_YEARLY
    if not price_id:
        return RedirectResponse(url="/dashboard", status_code=302)

    try:
        session = stripe.checkout.Session.create(  # type: ignore
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{BASE_URL}/dashboard",
            cancel_url=f"{BASE_URL}/dashboard",
            customer_email=company.get("email"),
            metadata={"company_id": company.get("id"), "plan": plan},
            subscription_data={"metadata": {"company_id": company.get("id"), "plan": plan}},
        )
        return RedirectResponse(url=session.url, status_code=303)  # type: ignore
    except Exception as e:
        print(f"[STRIPE] checkout error: {type(e).__name__}: {e}")
        return RedirectResponse(url="/dashboard", status_code=302)

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe_ready():
        return PlainTextResponse("stripe disabled", status_code=200)

    stripe_init()

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)  # type: ignore
    except Exception as e:
        print(f"[STRIPE] webhook bad signature: {type(e).__name__}: {e}")
        return PlainTextResponse("bad signature", status_code=400)

    etype = event.get("type")
    data = event.get("data", {}).get("object", {})

    company_id = (data.get("metadata") or {}).get("company_id") or ""
    if not company_id:
        return PlainTextResponse("ok", status_code=200)

    db = _load_db()
    if company_id not in db["companies"]:
        return PlainTextResponse("ok", status_code=200)

    if etype in ("checkout.session.completed",):
        meta = (data.get("metadata") or {})
        chosen_plan = (meta.get("plan") or "monthly").strip().lower()
        if chosen_plan not in ("monthly", "yearly"):
            chosen_plan = "monthly"
        db["companies"][company_id]["stripe"]["status"] = "active"
        db["companies"][company_id]["stripe"]["customer_id"] = data.get("customer", "") or ""
        db["companies"][company_id]["stripe"]["subscription_id"] = data.get("subscription", "") or ""
        db["companies"][company_id]["plan"] = chosen_plan
        _save_db(db)
        print(f"[STRIPE] company_id={company_id} status=active plan={chosen_plan} via checkout.session.completed")

    if etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        status = data.get("status", "") or ""
        db["companies"][company_id]["stripe"]["status"] = status

        # Jesli subskrypcja aktywna, zachowaj/ustaw plan z metadata subskrypcji
        sub_meta = (data.get("metadata") or {})
        sub_plan = (sub_meta.get("plan") or "").strip().lower()
        if status in ("active", "trialing") and sub_plan in ("monthly", "yearly"):
            db["companies"][company_id]["plan"] = sub_plan
        elif status not in ("active", "trialing"):
            db["companies"][company_id]["plan"] = ("free" if ENABLE_FREE_PLAN else "none")

        _save_db(db)
        print(f"[STRIPE] company_id={company_id} status={status} via {etype}")

    return PlainTextResponse("ok", status_code=200)


# =========================
# 16) Health – bez zmian
# =========================

@app.get("/health")
def health():
    return {
        "ok": True,
        "base_url": BASE_URL,
        "stripe_ready": stripe_ready(),
        "openai_ready": bool(OPENAI_API_KEY and OpenAI is not None),
        "email_ready": bool((RESEND_API_KEY and RESEND_FROM) or (BOT_EMAIL and BOT_EMAIL_PASSWORD)),
        "email_mode": "resend" if (RESEND_API_KEY and RESEND_FROM) else ("smtp" if (BOT_EMAIL and BOT_EMAIL_PASSWORD) else "none"),
    }


# =========================
# Run local
# =========================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
