from __future__ import annotations

import os
import json
import hmac
import time
import html
import base64
import hashlib
import secrets
import datetime
import ssl
import socket
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
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
    return ["< 500 MJ/m²", "500–1000 MJ/m²", "1000–2000 MJ/m²", "> 2000 MJ/m²", "Nie wiem"]

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
        {"name": "fire_load", "label": "Obciążenie ogniowe (szacunek)", "type": "select", "options": _fire_load()},
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
    return max(0, FORMS_PER_MONTH_LIMIT - sent)

def _increment_forms_sent(db: Dict[str, Any], company_id: str) -> None:
    c = db["companies"][company_id]
    _ensure_usage_period(c)
    c["usage"]["forms_sent"] = int(c["usage"].get("forms_sent") or 0) + 1

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

def layout(title: str, body: str, *, nav: str = "") -> str:
    return f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{esc(title)} • {APP_NAME}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #070B16;
      --panel: rgba(255,255,255,0.06);
      --panel2: rgba(255,255,255,0.08);
      --stroke: rgba(255,255,255,0.12);
      --text: #EEF2FF;
      --muted: rgba(238,242,255,0.70);
      --gold: #D6B36A;
      --gold2: #B89443;
      --danger: #ff5b5b;
      --ok: #49d17d;
      --shadow: 0 12px 40px rgba(0,0,0,0.40);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      font-family: "Syne", system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background:
        radial-gradient(900px 400px at 20% 10%, rgba(214,179,106,0.12), transparent 50%),
        radial-gradient(900px 400px at 80% 20%, rgba(255,255,255,0.08), transparent 45%),
        radial-gradient(900px 600px at 50% 90%, rgba(214,179,106,0.08), transparent 55%),
        var(--bg);
      color: var(--text);
      overflow-x: hidden;
    }}
    a {{ color: inherit; text-decoration: none; }}
    .wrap {{ width: min(1120px, calc(100% - 40px)); margin: 0 auto; }}
    .topbar {{
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(10px);
      background: rgba(7,11,22,0.55);
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    .nav {{
      display:flex; align-items:center; justify-content:space-between;
      padding: 14px 0;
    }}
    .brand {{
      display:flex; align-items:center; gap:10px; font-weight:800; letter-spacing: 0.2px;
    }}
    .logo {{
      width:34px; height:34px; border-radius: 12px;
      background: linear-gradient(135deg, rgba(214,179,106,1), rgba(214,179,106,0.35));
      box-shadow: 0 10px 30px rgba(214,179,106,0.20);
    }}
    .menu {{ display:flex; align-items:center; gap:16px; color: var(--muted); font-weight:600; }}
    .menu a {{ padding: 8px 10px; border-radius: 12px; }}
    .menu a:hover {{ background: rgba(255,255,255,0.06); color: var(--text); }}
    .cta {{ display:flex; align-items:center; gap:10px; }}
    .btn {{
      display:inline-flex; align-items:center; justify-content:center;
      gap:10px;
      padding: 11px 14px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
      color: var(--text);
      font-weight: 700;
      box-shadow: none;
      transition: transform .15s ease, background .15s ease, border-color .15s ease;
    }}
    .btn:hover {{ transform: translateY(-1px); background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.18); }}
    .btn.gold {{
      background: linear-gradient(180deg, rgba(214,179,106,1), rgba(184,148,67,1));
      color: #0b0f1a;
      border-color: rgba(214,179,106,0.85);
      box-shadow: 0 14px 40px rgba(214,179,106,0.18);
    }}
    .btn.gold:hover {{ transform: translateY(-1px); }}
    .btn.ghost {{ background: transparent; }}
    .badge {{ padding: 6px 10px; border-radius: 999px; font-weight: 700; font-size: 12px; border:1px solid rgba(255,255,255,0.12); }}
    .badge.ok {{ color: var(--ok); border-color: rgba(73,209,125,0.35); background: rgba(73,209,125,0.08); }}
    .badge.bad {{ color: var(--danger); border-color: rgba(255,91,91,0.35); background: rgba(255,91,91,0.08); }}

    .deck {{ scroll-snap-type: y mandatory; }}
    section.slide {{
      scroll-snap-align: start;
      min-height: calc(100vh - 64px);
      padding: 56px 0;
      display:flex; align-items:center;
    }}
    .hero {{
      display:grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 34px;
      align-items: center;
    }}
    .kicker {{
      display:inline-flex; align-items:center; gap:10px;
      padding: 8px 12px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
      color: var(--muted);
      font-weight:700;
      width: fit-content;
    }}
    h1 {{
      margin: 14px 0 10px;
      font-size: clamp(40px, 4.2vw, 64px);
      line-height: 1.03;
      letter-spacing: -0.8px;
    }}
    .gold {{ color: var(--gold); }}
    .lead {{
      margin: 0;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.6;
      max-width: 68ch;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }}
    .card {{ padding: 20px; }}

    .grid3 {{
      display:grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
    }}
    .tile {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
      transition: transform .15s ease, background .15s ease;
    }}
    .tile:hover {{ transform: translateY(-2px); background: rgba(255,255,255,0.07); }}

    .formwrap {{ padding: 32px 0 60px; }}
    .notice {{
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(214,179,106,0.10);
      border: 1px solid rgba(214,179,106,0.25);
      color: rgba(238,242,255,0.85);
      line-height: 1.5;
    }}
    details {{
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 18px;
      padding: 14px 14px;
      margin: 12px 0;
    }}
    summary {{
      cursor: pointer;
      font-weight: 800;
      color: var(--text);
      outline: none;
      list-style: none;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    .fields {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 14px;
    }}
    .field {{ display:flex; flex-direction: column; gap: 7px; }}
    .field label {{ color: rgba(238,242,255,0.80); font-weight: 700; font-size: 13px; }}
    input, select, textarea {{
      width: 100%;
      padding: 12px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(7,11,22,0.55);
      color: var(--text);
      font-weight: 650;
      outline: none;
    }}
    input::placeholder, textarea::placeholder {{ color: rgba(238,242,255,0.40); }}
    textarea {{ min-height: 100px; resize: vertical; }}
    select option {{ color: #0b0f1a; background: #ffffff; }}
    .field.full {{ grid-column: 1 / -1; }}
    .checkrow {{ display:flex; align-items:center; gap:10px; padding: 10px 12px; border-radius: 14px; border:1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.04); }}
    .checkrow input[type="checkbox"] {{ width: 18px; height: 18px; }}
    .actions {{ display:flex; gap: 12px; align-items:center; margin-top: 18px; flex-wrap: wrap; }}
    .muted {{ color: var(--muted); font-weight: 650; line-height: 1.6; }}

    

    .big {{ font-size: 40px; font-weight: 900; letter-spacing: -0.5px; margin: 6px 0 8px; }}

    .foot {{
          padding: 26px 0 60px;
          color: rgba(238,242,255,0.55);
          border-top: 1px solid rgba(255,255,255,0.06);
        }}

    .how {{
          display:grid;
          grid-template-columns: 1fr 1fr;
          gap: 18px;
          align-items: start;
        }}

    .k {{ color: var(--gold); font-weight: 800; letter-spacing: .6px; font-size: 12px; }}

    .n {{ font-size: 26px; font-weight: 800; }}

    .price {{
          padding: 22px;
          border-radius: 22px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
        }}

    .pricing {{
          display:grid;
          grid-template-columns: 1fr 1fr;
          gap: 16px;
          align-items: stretch;
        }}

    .stat {{
          padding: 16px;
          border-radius: 18px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
        }}

    .stats {{ display:grid; gap: 14px; }}

    .step {{
          padding: 18px;
          border-radius: 20px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
        }}

    .t {{ color: var(--muted); font-weight: 700; }}

@media (max-width: 920px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .grid3 {{ grid-template-columns: 1fr; }}
      .fields {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="wrap">
      <div class="nav">
        <div class="brand">
          <div class="logo"></div>
          <div>{esc(APP_NAME)}</div>
        </div>
        <div class="menu">
          {nav}
        </div>
        <div class="cta">
          <a class="btn ghost" href="/demo">Podgląd briefu</a>
          <a class="btn" href="/login">Zaloguj</a>
          <a class="btn gold" href="/register">Załóż konto</a>
        </div>
      </div>
    </div>
  </div>
  {body}
</body>
</html>
"""

def nav_links() -> str:
    return """
      <a href="/#funkcje">Funkcje</a>
      <a href="/#jak">Jak działa</a>
      <a href="/#cennik">Cennik</a>
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
    if DEV_BYPASS_SUBSCRIPTION:
        return True
    st = (company.get("stripe") or {}).get("status") or ""
    return st in ("active", "trialing")

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
def home():
    openai_ok = bool(OPENAI_API_KEY and OpenAI is not None)
    resend_ok = bool(RESEND_API_KEY and RESEND_FROM)
    smtp_ok = bool(BOT_EMAIL and BOT_EMAIL_PASSWORD)
    mail_ok = resend_ok or smtp_ok
    stripe_ok = stripe_ready()

    body = f"""
    <div class="deck">
      <section class="slide">
        <div class="wrap hero">
          <div>
            <div class="kicker">
              {badge("AI: aktywne" if openai_ok else "AI: nieaktywne", openai_ok)}
              {badge("Email: skonfigurowany" if mail_ok else "Email: brak konfiguracji", mail_ok)}
              {badge("Stripe: skonfigurowany" if stripe_ok else "Stripe: opcjonalnie", stripe_ok)}
            </div>
            <h1>Brief inwestorski + analiza <span class="gold">dla projektów przemysłowych</span></h1>
            <p class="lead">
              Inwestor wypełnia szczegółowy brief. System generuje raport dla architekta: braki, ryzyka,
              checklisty formalne oraz pytania krytyczne do doprecyzowania.
            </p>
            <div style="height:18px"></div>
            <div class="cta" style="justify-content:flex-start">
              <a class="btn gold" href="/register">Rozpocznij</a>
              <a class="btn" href="/demo">Zobacz brief (demo)</a>
            </div>
          </div>
          <div class="panel card">
            <div class="muted" style="font-weight:800">Korzyści</div>
            <div style="height:10px"></div>
            <div class="muted">• Standaryzacja danych wejściowych</div>
            <div class="muted">• Redukcja ryzyk i niejednoznaczności</div>
            <div class="muted">• Raport dla architekta po zatwierdzeniu briefu</div>
          </div>
        </div>
      </section>

      <section class="slide" id="funkcje">
        <div class="wrap">
          <h1 style="margin:0 0 14px">Zakres</h1>
          <p class="lead" style="max-width:70ch">Brief obejmuje zabudowę mieszkaniową oraz inwestycje biurowo-produkcyjno-magazynowe.</p>
          <div style="height:18px"></div>
          <div class="grid3">
            <div class="tile">
              <h3>Komplet pytań</h3>
              <p>Stan prawny, media, grunt, funkcja, technologia i parametry obiektu – w jednej strukturze.</p>
            </div>
            <div class="tile">
              <h3>Lista braków</h3>
              <p>Raport wskazuje braki, ryzyka i pytania do doprecyzowania na etapie ofertowania.</p>
            </div>
            <div class="tile">
              <h3>Wycena prac projektowych</h3>
              <p>Możliwość oparcia wyceny o zasady zdefiniowane przez firmę (cennik tekstowy).</p>
            </div>
          </div>
        </div>
      </section>

<section class="slide" id="jak">
        <div class="wrap">
          <h1 style="margin:0 0 14px">Proces</h1>
          <div class="how">
            <div class="step">
              <div class="k">ETAP 01</div>
              <h3>Ustawienia firmy</h3>
              <p>Firma uzupełnia cennik i tworzy listę architektów (odbiorców raportów).</p>
            </div>
            <div class="step">
              <div class="k">ETAP 02</div>
              <h3>Brief inwestorski</h3>
              <p>Inwestor wypełnia formularz; pola opcjonalne mogą pozostać puste.</p>
            </div>
            <div class="step">
              <div class="k">ETAP 03</div>
              <h3>Raport dla architekta</h3>
              <p>System przygotowuje raport roboczy i przekazuje go do wskazanego odbiorcy.</p>
            </div>
            <div class="step">
              <div class="k">ETAP 04</div>
              <h3>Dalsze działania</h3>
              <p>Raport stanowi podstawę do doprecyzowania zakresu oraz ustalenia kolejnych kroków projektowych.</p>
            </div>
          </div>
          <div style="height:18px"></div>
          <div class="cta" style="justify-content:flex-start">
            <a class="btn gold" href="/demo">Podgląd briefu</a>
            <a class="btn" href="/register">Rejestracja</a>
          </div>
        </div>
      </section>

<section class="slide" id="cennik">
        <div class="wrap">
          <h1 style="margin:0 0 14px">Cennik</h1>
          <p class="lead" style="max-width:70ch">Dostęp do platformy w rozliczeniu miesięcznym lub rocznym. Płatności realizowane są przez Stripe.</p>
          <div style="height:18px"></div>
          <div class="pricing">
            <div class="price">
              <h3>Miesięcznie</h3>
              <div class="big">249 zł</div>
              <div class="muted">Dla pracowni, które preferują rozliczenie miesięczne.</div>
              <ul>
                <li>Panel firmy + architekci</li>
                <li>Brief + raport</li>
                <li>Maks. {FORMS_PER_MONTH_LIMIT} formularzy / miesiąc</li>
                <li>Cennik firmy do wycen</li>
              </ul>
            </div>
            <div class="price" style="border-color: rgba(214,179,106,0.35); background: rgba(214,179,106,0.07)">
              <h3>Rocznie</h3>
              <div class="big">2 690 zł</div>
              <div class="muted">Dla pracowni realizujących inwestycje w trybie ciągłym.</div>
              <ul>
                <li>To samo co miesięcznie</li>
                <li>Maks. {FORMS_PER_MONTH_LIMIT} formularzy / miesiąc</li>
                <li>Wsparcie wdrożeniowe</li>
                <li>Odnowienia cykliczne</li>
              </ul>
            </div>
          </div>
        </div>
      </section>

<section class="slide" id="faq">
        <div class="wrap">
          <h1 style="margin:0 0 14px">Informacje</h1>
          <div class="panel card">
            <p class="muted"><b>Czy wszystkie pola muszą być wypełnione?</b><br/>Nie. Raport ma wskazać braki oraz pytania uzupełniające.</p>
            <p class="muted"><b>Czy inwestor widzi raport?</b><br/>Nie. Raport jest przeznaczony dla architekta.</p>
          </div>
        </div>
      </section>

      <div class="foot">
        <div class="wrap">
          © {esc(APP_NAME)} • {badge("DEV_BYPASS_SUBSCRIPTION=ON", DEV_BYPASS_SUBSCRIPTION)}
        </div>
      </div>
    </div>
    """
    return HTMLResponse(layout("Start", body=body, nav=nav_links()))


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
        "usage": {"period": _period_key(), "forms_sent": 0},
        "stripe": {"status": "inactive", "customer_id": "", "subscription_id": ""},
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
def dashboard(request: Request):
    gate = require_company(request)
    if gate:
        return gate

    company = get_company(request)
    assert company is not None

    st = (company.get("stripe") or {}).get("status") or "inactive"
    sub_ok = subscription_active(company)
    stripe_msg = "Stripe niepodłączony" if not stripe_ready() else f"Stripe: {st}"

    architects = company.get("architects", [])
    arch_rows = []
    for a in architects:
        link = f"{BASE_URL}/f/{a['token']}"
        arch_rows.append(f"""
        <div class="tile">
          <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">
            <div>
              <div style="font-weight:900">{esc(a.get('name',''))}</div>
              <div class="muted">{esc(a.get('email',''))}</div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">
              <a class="btn" href="{esc(link)}" target="_blank">Otwórz formularz</a>
              <a class="btn" href="/dashboard/architect/delete?id={esc(a['id'])}">Usuń</a>
            </div>
          </div>
          <div style="height:8px"></div>
          <div class="muted">Link do briefu:</div>
          <div style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: rgba(238,242,255,0.85); word-break: break-all;">
            {esc(link)}
          </div>
        </div>
        """)

    body = f"""
    <div class="wrap formwrap">
      <div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap">
        <div>
          <h1 style="margin:0 0 8px">{esc(company.get("name"))}</h1>
          <div class="muted">Panel firmy • {badge("Subskrypcja aktywna" if sub_ok else "Subskrypcja nieaktywna", sub_ok)} • {esc(stripe_msg)}</div>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <a class="btn" href="/demo">Podgląd briefu</a>
          <a class="btn" href="/logout">Wyloguj</a>
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="grid3" style="grid-template-columns: 1fr 1fr; align-items: start;">
        <div class="panel card">
          <h3 style="margin:0 0 10px">Cennik firmy (dowolny tekst)</h3>
          <p class="muted" style="margin-top:0">
            Wklej zasady wyceny (np. stawki za m², pakiety, dodatki, minimalna kwota, etapy).
          </p>
          <form method="post" action="/dashboard/pricing">
            <div class="field">
              <label>Treść cennika</label>
              <textarea name="pricing_text" placeholder="np. Koncepcja: ..., PB: ..., PW: ...">{esc(company.get("pricing_text",""))}</textarea>
            </div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz cennik</button>
            </div>
          </form>
        </div>

        <div class="panel card">
          <h3 style="margin:0 0 10px">Dane do faktury (opcjonalnie)</h3>
          <form method="post" action="/dashboard/billing">
            <div class="fields">
              <div class="field"><label>Nazwa firmy</label><input name="company_name" value="{esc((company.get("billing") or {}).get("company_name",""))}"/></div>
              <div class="field"><label>NIP</label><input name="nip" value="{esc((company.get("billing") or {}).get("nip",""))}"/></div>
              <div class="field full"><label>Adres</label><input name="address" value="{esc((company.get("billing") or {}).get("address",""))}"/></div>
              <div class="field full"><label>Email do faktur</label><input name="invoice_email" value="{esc((company.get("billing") or {}).get("invoice_email",""))}"/></div>
            </div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz dane</button>
            </div>
          </form>
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="panel card">
        <h3 style="margin:0 0 10px">Architekci i linki do formularzy</h3>
        <form method="post" action="/dashboard/architect/add">
          <div class="fields">
            <div class="field"><label>Imię / identyfikator</label><input name="name" placeholder="np. Jan Kowalski"/></div>
            <div class="field"><label>Email architekta (na raport)</label><input type="email" name="email" placeholder="jan@pracownia.pl"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Dodaj architekta</button>
          </div>
        </form>

        <div style="height:14px"></div>
        <div class="grid3" style="grid-template-columns: 1fr; gap: 12px;">
          {''.join(arch_rows) if arch_rows else '<div class="muted">Brak architektów. Dodaj pierwszego powyżej.</div>'}
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="panel card">
        <h3 style="margin:0 0 10px">Subskrypcja</h3>
        <div class="actions">
          <a class="btn" href="/billing/checkout?plan=monthly">Kup miesięczną (249 zł)</a>
          <a class="btn" href="/billing/checkout?plan=yearly">Kup roczną (2 690 zł)</a>
          <span class="muted">Limit: {FORMS_PER_MONTH_LIMIT} formularzy / miesiąc.</span>
        </div>
      </div>
    </div>
    """
    return HTMLResponse(layout("Panel firmy", body=body, nav=nav_links()))

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
    return RedirectResponse(url="/dashboard", status_code=302)

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
    return RedirectResponse(url="/dashboard", status_code=302)

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
        return RedirectResponse(url="/dashboard", status_code=302)

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
    return RedirectResponse(url="/dashboard", status_code=302)


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

    report = ai_report(
        form_clean,
        pricing_text="(DEMO) brak cennika firmy",
        company={"name": "DEMO", "email": ""},
        architect={"name": "DEMO", "email": ""},
    )

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
        return HTMLResponse(layout("Subskrypcja", body=f'<div class="wrap formwrap"><h1>Formularz niedostępny</h1><p class="muted">Dostęp jest czasowo zablokowany.</p><a class="btn" href="/">Strona główna</a></div>', nav=nav_links()), status_code=403)

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
          <p class="lead">Maksymalnie {FORMS_PER_MONTH_LIMIT} wysłanych formularzy / miesiąc.</p>
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

    # Komunikat dla inwestora – profesjonalny, neutralny, bez odsyłania do logów
    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Dziękujemy.</h1>
      <p class="lead">Brief został przekazany do opracowania. Zespół projektowy skontaktuje się w razie potrzeby uzupełnień.</p>
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
            metadata={"company_id": company.get("id")},
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
        db["companies"][company_id]["stripe"]["status"] = "active"
        db["companies"][company_id]["stripe"]["customer_id"] = data.get("customer", "") or ""
        db["companies"][company_id]["stripe"]["subscription_id"] = data.get("subscription", "") or ""
        _save_db(db)
        print(f"[STRIPE] company_id={company_id} status=active via checkout.session.completed")

    if etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        status = data.get("status", "") or ""
        db["companies"][company_id]["stripe"]["status"] = status
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
