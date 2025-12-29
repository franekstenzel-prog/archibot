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

# Optional deps – aplikacja działa również bez konfiguracji
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

# Base URL (Render): np. https://archibot.onrender.com
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Sesje (Render ENV: SESSION_SECRET)
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

# Email
BOT_EMAIL = os.getenv("BOT_EMAIL", "twoj.bot.architektoniczny@gmail.com").strip()
BOT_EMAIL_PASSWORD = (os.getenv("BOT_EMAIL_PASSWORD", "") or "").strip().replace(" ", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Email przez HTTPS API – Resend
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM = os.getenv("RESEND_FROM", "").strip()  # np. "ArchiBot <noreply@twojadomena.pl>"

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY", "").strip()
STRIPE_PRICE_ID_YEARLY = os.getenv("STRIPE_PRICE_ID_YEARLY", "").strip()

# DEV bypass
DEV_BYPASS_SUBSCRIPTION = (
    os.getenv("DEV_BYPASS_SUBSCRIPTION", "false").lower() in ("1", "true", "yes", "y", "on")
)


# =========================
# 1) KOSZT BUDOWY (V1)
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
# 2) FORMULARZ – schema
# =========================

Field = Dict[str, Any]
Section = Tuple[str, List[Field]]

FORM_SCHEMA: List[Section] = [
    ("A. Dane inwestora i kontakt", [
        {"name": "investor_name", "label": "Imię i nazwisko / nazwa inwestora", "type": "text", "ph": "np. Jan Kowalski / Sp. z o.o."},
        {"name": "investor_email", "label": "Adres e-mail do kontaktu", "type": "email", "ph": "np. biuro@..."},
        {"name": "investor_phone", "label": "Telefon", "type": "text", "ph": "np. +48..."},
        {"name": "preferred_contact", "label": "Preferowany kanał kontaktu", "type": "select", "options": ["E-mail", "Telefon", "WhatsApp", "Inne"]},
        {"name": "household_adults", "label": "Liczba dorosłych (jeśli dotyczy domu)", "type": "number", "min": 0},
        {"name": "household_children", "label": "Liczba dzieci (jeśli dotyczy domu)", "type": "number", "min": 0},
        {"name": "special_needs", "label": "Wymagania szczególne (dostępność, senior, niepełnosprawność itp.)", "type": "textarea", "ph": "Opcjonalnie"},
    ]),

    ("B. Działka i lokalizacja", [
        {"name": "plot_address", "label": "Adres / miejscowość", "type": "text"},
        {"name": "plot_ewidencyjny", "label": "Nr działki ewidencyjnej (jeśli znany)", "type": "text"},
        {"name": "plot_pow_m2", "label": "Powierzchnia działki [m²]", "type": "number", "min": 0},
        {"name": "plot_shape", "label": "Kształt działki", "type": "select", "options": ["Prostokątna", "Nieregularna", "Wąska", "Szeroka", "Inna"]},
        {"name": "plot_slope", "label": "Ukształtowanie terenu", "type": "select", "options": ["Płasko", "Lekki spadek", "Duży spadek", "Tarasowanie/skarpy", "Nie wiem"]},
        {"name": "region_type", "label": "Lokalizacja (współczynnik kosztu wykonawstwa)", "type": "select", "options": list(REGION_MULTIPLIER.keys())},
        {"name": "neighbors_notes", "label": "Sąsiedztwo (odległości, zacienienie, uciążliwości)", "type": "textarea"},
        {"name": "world_sides", "label": "Orientacja stron świata (jeśli znana)", "type": "textarea", "ph": "np. wjazd od północy, ogród od południa"},
        {"name": "trees_inventory", "label": "Zieleń/drzewa do zachowania/wycinki", "type": "textarea"},
    ]),

    ("C. Stan prawny i dokumenty (MPZP/WZ itd.)", [
        {"name": "mpzp_or_wz", "label": "Podstawa planistyczna", "type": "select", "options": ["MPZP", "WZ", "Nie wiem", "W trakcie"]},
        {"name": "kw_number", "label": "Numer księgi wieczystej (jeśli jest)", "type": "text"},
        {"name": "land_register_extract", "label": "Wypis z rejestru gruntów – dostępny", "type": "checkbox"},
        {"name": "right_to_dispose", "label": "Oświadczenie o prawie do dysponowania nieruchomością – dostępne", "type": "checkbox"},
        {"name": "mpzp_wz_extract", "label": "Wypis i wyrys MPZP / decyzja WZ – dostępne", "type": "checkbox"},
        {"name": "access_road", "label": "Dostęp do drogi publicznej", "type": "select", "options": ["Bezpośredni", "Służebność", "Droga wewnętrzna", "Nie wiem"]},
        {"name": "driveway_consent", "label": "Warunki/zgoda na zjazd z drogi publicznej – dostępne", "type": "checkbox"},
        {"name": "legal_constraints", "label": "Ograniczenia (służebności, linie energetyczne, konserwator, Natura 2000 itp.)", "type": "textarea"},
    ]),

    ("D. Geodezja i grunt", [
        {"name": "map_for_design", "label": "Mapa do celów projektowych – dostępna", "type": "checkbox"},
        {"name": "geotech_opinion", "label": "Opinia geotechniczna – dostępna", "type": "checkbox"},
        {"name": "soil_type", "label": "Rodzaj gruntu (jeśli znany)", "type": "select", "options": ["Piaski", "Glina", "Iły", "Nasypy", "Mieszany", "Nie wiem"]},
        {"name": "groundwater_level", "label": "Poziom wód gruntowych", "type": "select", "options": ["Nisko", "Średnio", "Wysoko", "Nie wiem"]},
        {"name": "flood_risk", "label": "Ryzyko zalewowe / teren podmokły", "type": "select", "options": ["Tak", "Nie", "Nie wiem"]},
        {"name": "foundation_preference", "label": "Preferencja posadowienia (jeśli jest)", "type": "select", "options": ["Ławy/tradycyjne", "Płyta fundamentowa", "Nie wiem"]},
    ]),

    ("E. Media i warunki przyłączy", [
        {"name": "power_conditions", "label": "Warunki przyłączenia energii elektrycznej – dostępne", "type": "checkbox"},
        {"name": "water_conditions", "label": "Warunki przyłączenia wody – dostępne", "type": "checkbox"},
        {"name": "sewage_conditions", "label": "Warunki kanalizacji sanitarnej – dostępne", "type": "checkbox"},
        {"name": "gas_conditions", "label": "Warunki przyłączenia gazu – dostępne (jeśli dotyczy)", "type": "checkbox"},
        {"name": "internet_fiber", "label": "Światłowód / Internet", "type": "select", "options": ["Jest", "Brak", "Nie wiem"]},
        {"name": "water_solution", "label": "Źródło wody", "type": "select", "options": ["Sieć", "Studnia", "Nie wiem"]},
        {"name": "sewage_solution", "label": "Odprowadzenie ścieków", "type": "select", "options": ["Kanalizacja", "Szambo", "Przydomowa oczyszczalnia", "Nie wiem"]},
        {"name": "rainwater_conditions", "label": "Warunki/uzgodnienia dla wód opadowych – dostępne (jeśli dotyczy)", "type": "checkbox"},
        {"name": "mec_connection", "label": "Przyłącze do sieci ciepłowniczej (MEC) – dostępne (jeśli dotyczy)", "type": "checkbox"},
    ]),

    ("F. Parametry budynku – bryła i metraż (dla zabudowy mieszkaniowej)", [
        {"name": "building_type", "label": "Typ obiektu", "type": "select", "options": ["Dom jednorodzinny", "Bliźniak", "Szeregowiec", "Inne"]},
        {"name": "usable_area_m2", "label": "Docelowa powierzchnia użytkowa [m²]", "type": "number", "min": 0},
        {"name": "garage", "label": "Garaż", "type": "select", "options": ["Brak", "1-stanowiskowy", "2-stanowiskowy", "Wiata", "Wolnostojący"]},
        {"name": "storeys", "label": "Kondygnacje", "type": "select", "options": ["Parterowy", "Parter + poddasze", "Piętrowy", "Z piwnicą", "Inne"]},
        {"name": "roof_type", "label": "Dach", "type": "select", "options": ["Płaski", "Dwuspadowy", "Czterospadowy", "Wielospadowy", "Nie wiem"]},
        {"name": "roof_covering", "label": "Pokrycie dachu (jeśli znane / preferowane)", "type": "select", "options": ["Dachówka ceramiczna", "Dachówka betonowa", "Blacha", "Papa/EPDM", "Gont", "Nie wiem"]},
        {"name": "foundation_type", "label": "Fundament (jeśli znany / preferowany)", "type": "select", "options": ["Ławy tradycyjne", "Płyta fundamentowa", "Piwnica", "Nie wiem"]},
        {"name": "roof_slope_deg", "label": "Nachylenie dachu [°] (jeśli znane)", "type": "number", "min": 0, "max": 60},
        {"name": "roof_area_m2", "label": "Szacowana powierzchnia dachu [m²] (jeśli znana)", "type": "number", "min": 0},
        {"name": "building_height_m", "label": "Wysokość budynku [m] (jeśli wymagana/znana)", "type": "number", "min": 0},
        {"name": "style", "label": "Styl", "type": "select", "options": ["Nowoczesny", "Tradycyjny", "Stodoła", "Dworkowy", "Minimalistyczny", "Inne"]},
    ]),

    ("G. Układ funkcjonalny (dla zabudowy mieszkaniowej)", [
        {"name": "bedrooms", "label": "Liczba sypialni", "type": "number", "min": 0},
        {"name": "bathrooms", "label": "Liczba łazienek", "type": "number", "min": 0},
        {"name": "wc_count", "label": "Liczba osobnych WC", "type": "number", "min": 0},
        {"name": "kitchen_type", "label": "Kuchnia", "type": "select", "options": ["Otwarta", "Zamknięta", "Z wyspą", "Ze spiżarnią", "Nie wiem"]},
        {"name": "home_office", "label": "Gabinet / praca zdalna", "type": "select", "options": ["Tak", "Nie", "Opcjonalnie"]},
        {"name": "utility_rooms", "label": "Pomieszczenia techniczne i gospodarcze (pralnia, kotłownia, garderoby)", "type": "textarea"},
        {"name": "special_rooms", "label": "Funkcje dodatkowe (warsztat, siłownia, kino, sauna itp.)", "type": "textarea"},
    ]),

    ("H. Konstrukcja, elewacje, stolarka (dla zabudowy mieszkaniowej)", [
        {"name": "wall_tech", "label": "Technologia ścian", "type": "select", "options": ["Ceramika", "Beton komórkowy", "Silikat", "Drewno", "Prefabrykat", "Nie wiem"]},
        {"name": "facade_materials", "label": "Materiały elewacyjne", "type": "textarea", "ph": "np. tynk + drewno + spiek"},
        {"name": "windows", "label": "Przeszklenia", "type": "select", "options": ["Standardowe", "Duże okna", "HS przesuwne", "Dużo okien dachowych", "Nie wiem"]},
        {"name": "shading", "label": "Osłony przeciwsłoneczne", "type": "select", "options": ["Rolety", "Żaluzje fasadowe", "Pergole", "Brak", "Nie wiem"]},
        {"name": "terrace", "label": "Tarasy / balkony (opis)", "type": "textarea"},
    ]),

    ("I. Instalacje i standard (dla zabudowy mieszkaniowej)", [
        {"name": "heating", "label": "Źródło ciepła", "type": "select", "options": ["Pompa ciepła", "Gaz", "Pellet", "Elektryczne", "Inne", "Nie wiem"]},
        {"name": "ventilation", "label": "Wentylacja", "type": "select", "options": ["Grawitacyjna", "Mechaniczna z rekuperacją", "Nie wiem"]},
        {"name": "pv", "label": "Fotowoltaika", "type": "select", "options": ["Tak", "Nie", "Rozważane", "Nie wiem"]},
        {"name": "ac", "label": "Klimatyzacja", "type": "select", "options": ["Tak", "Nie", "Rozważane", "Nie wiem"]},
        {"name": "smart_home", "label": "Automatyka budynkowa", "type": "select", "options": ["Tak", "Nie", "Podstawowy", "Nie wiem"]},
        {"name": "finish_standard", "label": "Standard wykończenia", "type": "select", "options": ["Ekonomiczny", "Standard", "Premium"]},
        {"name": "flooring", "label": "Podłogi (opis)", "type": "textarea", "ph": "np. panele, deska, płytki, mikrocement"},
        {"name": "bathroom_level", "label": "Łazienki – standard", "type": "select", "options": ["Podstawowy", "Standard", "Premium", "Nie wiem"]},
        {"name": "kitchen_level", "label": "Kuchnia – standard", "type": "select", "options": ["Podstawowy", "Standard", "Premium", "Nie wiem"]},
        {"name": "stairs", "label": "Schody (jeśli dotyczy)", "type": "select", "options": ["Brak (dom parterowy)", "Żelbet + okładzina", "Drewniane", "Metal/drewno", "Nie wiem"]},
        {"name": "cost_standard", "label": "Standard kosztu budowy (do estymacji)", "type": "select", "options": list(BUILD_COST_M2_PLN.keys())},
    ]),

    ("J. Zagospodarowanie terenu", [
        {"name": "driveway_material", "label": "Podjazd (materiał)", "type": "select", "options": ["Kostka", "Beton", "Żwir", "Asfalt", "Nie wiem"]},
        {"name": "fence", "label": "Ogrodzenie", "type": "select", "options": ["Tak", "Nie", "Rozważane"]},
        {"name": "garden_plan", "label": "Projekt zieleni", "type": "select", "options": ["Tak", "Nie", "Rozważane"]},
        {"name": "additional_objects", "label": "Obiekty dodatkowe (basen, altana, wiata, śmietnik, schowek)", "type": "textarea"},
        {"name": "rainwater", "label": "Gospodarka wodami opadowymi", "type": "select", "options": ["Zbiornik", "Rozsączanie", "Nie wiem", "Nie dotyczy"]},
    ]),

    ("K. Budżet i terminy", [
        {"name": "budget_total", "label": "Budżet całej inwestycji [PLN] (jeśli określony)", "type": "number", "min": 0},
        {"name": "budget_build_only", "label": "Budżet robót budowlanych [PLN] (bez gruntu) (jeśli określony)", "type": "number", "min": 0},
        {"name": "timeline_start", "label": "Planowany termin rozpoczęcia robót", "type": "select", "options": ["0–3 mies.", "3–6 mies.", "6–12 mies.", "12+ mies.", "Nieokreślony"]},
        {"name": "timeline_deadline", "label": "Wymagany termin zakończenia (jeśli jest)", "type": "textarea"},
        {"name": "priority", "label": "Priorytet inwestora", "type": "select", "options": ["Koszt", "Czas", "Jakość", "Energooszczędność", "Estetyka"]},
    ]),

    ("L. Inspiracje i informacje dodatkowe", [
        {"name": "inspirations_links", "label": "Inspiracje (linki, Pinterest, IG, materiały referencyjne)", "type": "textarea"},
        {"name": "must_have", "label": "Wymagania kluczowe (must-have)", "type": "textarea"},
        {"name": "nice_to_have", "label": "Wymagania dodatkowe (nice-to-have)", "type": "textarea"},
        {"name": "dont_want", "label": "Wykluczenia / elementy nieakceptowalne", "type": "textarea"},
        {"name": "unknowns", "label": "Obszary do rekomendacji architekta / wymagające doprecyzowania", "type": "textarea"},
    ]),

    # =========================
    # DODATEK: tabela inwestycyjna (biuro/produkcja/magazyn) – wg PDF
    # =========================
    ("M. Inwestycja biurowo-produkcyjno-magazynowa (jeśli dotyczy)", [
        {"name": "inv_name", "label": "Nazwa inwestycji (budowa/przebudowa/rozbudowa)", "type": "text"},
        {"name": "inv_location", "label": "Lokalizacja inwestycji", "type": "text"},
        {"name": "plot_size_note", "label": "Wielkość działki (opis / potwierdzenie)", "type": "textarea"},
        {"name": "land_class", "label": "Klasa gruntów (jeśli znana)", "type": "text"},
        {"name": "ownership_form", "label": "Forma własności działki", "type": "text"},

        {"name": "function_office", "label": "Funkcja: biurowa", "type": "checkbox"},
        {"name": "function_production", "label": "Funkcja: produkcyjna", "type": "checkbox"},
        {"name": "function_warehouse", "label": "Funkcja: magazynowa", "type": "checkbox"},
        {"name": "function_split", "label": "Podział na części (opis)", "type": "textarea"},

        {"name": "tech_production_type", "label": "Technologia: rodzaj produkcji", "type": "textarea"},
        {"name": "tech_material_type_qty", "label": "Technologia: rodzaj i ilość materiału", "type": "textarea"},
        {"name": "tech_required_temps", "label": "Technologia: wymagane temperatury (proces/komfort)", "type": "textarea"},
        {"name": "tech_heating_method", "label": "Technologia: sposób ogrzewania (założenia / wymagania)", "type": "textarea"},
        {"name": "tech_devices", "label": "Technologia: urządzenia (lista / główne parametry)", "type": "textarea"},

        {"name": "staff_physical_total", "label": "Zatrudnienie: pracownicy fizyczni – liczba łącznie", "type": "number", "min": 0},
        {"name": "staff_physical_women", "label": "Zatrudnienie: pracownicy fizyczni – kobiety", "type": "number", "min": 0},
        {"name": "staff_physical_men", "label": "Zatrudnienie: pracownicy fizyczni – mężczyźni", "type": "number", "min": 0},
        {"name": "staff_office_total", "label": "Zatrudnienie: pracownicy biurowi – liczba łącznie", "type": "number", "min": 0},
        {"name": "staff_office_women", "label": "Zatrudnienie: pracownicy biurowi – kobiety", "type": "number", "min": 0},
        {"name": "staff_office_men", "label": "Zatrudnienie: pracownicy biurowi – mężczyźni", "type": "number", "min": 0},

        {"name": "shifts_count", "label": "System zmianowy: liczba zmian", "type": "number", "min": 0},
        {"name": "people_per_shift", "label": "System zmianowy: liczba osób na jednej zmianie", "type": "number", "min": 0},

        {"name": "building_len_m", "label": "Wymiary budynku: długość [m]", "type": "number", "min": 0},
        {"name": "building_wid_m", "label": "Wymiary budynku: szerokość [m]", "type": "number", "min": 0},
        {"name": "building_h_to_underside_m", "label": "Wysokość do spodu konstrukcji [m]", "type": "number", "min": 0},

        {"name": "crane_required", "label": "Suwnica – przewidziana", "type": "checkbox"},
        {"name": "crane_note", "label": "Suwnica – parametry (udźwig, rozpiętość, tor, wysokość, strefy)", "type": "textarea"},

        {"name": "floor_load_kN_m2", "label": "Obciążenia użytkowe posadzki [kN/m²] (jeśli znane)", "type": "text"},

        {"name": "daylight_windows", "label": "Doświetlenie: okna (opis)", "type": "textarea"},
        {"name": "daylight_skylights", "label": "Doświetlenie: świetliki (opis)", "type": "textarea"},

        {"name": "gates_segmental", "label": "Bramy: segmentowe – wymiary/ilości", "type": "textarea"},
        {"name": "gates_roller", "label": "Bramy: rolowane – wymiary/ilości", "type": "textarea"},
        {"name": "gates_sliding", "label": "Bramy: przesuwne – wymiary/ilości", "type": "textarea"},

        {"name": "internal_plinths", "label": "Cokoły wewnętrzne (opis)", "type": "textarea"},
        {"name": "ramps_docks", "label": "Rampy i doki dla TIR (opis, ilości, wysokości)", "type": "textarea"},

        {"name": "ventilation_type_ind", "label": "Wentylacja (hala):", "type": "select", "options": ["Grawitacyjna", "Mechaniczna", "Mechaniczna z odzyskiem", "Nie określono"]},
        {"name": "heating_source_ind", "label": "Ogrzewanie (hala): źródło ciepła (opis)", "type": "textarea"},
        {"name": "compressor_room", "label": "Sprężarkownia (opis / wymagania)", "type": "textarea"},

        {"name": "forklifts_type", "label": "Wózki widłowe: rodzaj (np. elektryczne/LPG)", "type": "text"},
        {"name": "forklifts_qty", "label": "Wózki widłowe: ilość", "type": "number", "min": 0},

        {"name": "plan_build_conditions", "label": "Warunki zabudowy / ustalenia planu (opis)", "type": "textarea"},
        {"name": "tech_conditions_summary", "label": "Warunki techniczne (przyłącza) – opis/uwagi", "type": "textarea"},

        {"name": "geological_tests", "label": "Badania geologiczne – dostępne / planowane (opis)", "type": "textarea"},
        {"name": "env_decision", "label": "Decyzja środowiskowa – dostępna / wymagana (opis)", "type": "textarea"},
        {"name": "agri_exclusion", "label": "Wyłączenie z produkcji rolnej – dotyczy (opis)", "type": "textarea"},
        {"name": "water_law_permit", "label": "Operat wodnoprawny (odprowadzenie wód opadowych) – status/uwagi", "type": "textarea"},
        {"name": "conservation_protection", "label": "Ochrona konserwatorska – status/uwagi", "type": "textarea"},
        {"name": "driveway_access_detail", "label": "Zjazd z posesji – status/uwagi", "type": "textarea"},

        {"name": "constraints_distances", "label": "Ograniczenia i odległości (las, linie, gazociągi, tereny zalewowe, drogi, istniejące obiekty)", "type": "textarea"},

        {"name": "fire_water_availability", "label": "Dostępność wody do celów ppoż. (opis)", "type": "textarea"},
        {"name": "fire_load", "label": "Obciążenie ogniowe (jeśli znane / do określenia)", "type": "textarea"},
        {"name": "stored_material_type_qty", "label": "Rodzaj magazynowanego materiału oraz ilość", "type": "textarea"},
    ]),

    ("N. Załączniki (opcjonalnie)", [
        {"name": "attachments", "label": "Pliki (MPZP/WZ, mapa, geotechnika, warunki przyłączy, szkice)", "type": "file", "multiple": True},
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
      --stroke: rgba(255,255,255,0.12);
      --text: #EEF2FF;
      --muted: rgba(238,242,255,0.70);
      --gold: #D6B36A;
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
      transition: transform .15s ease, background .15s ease, border-color .15s ease;
    }}
    .btn:hover {{ transform: translateY(-1px); background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.18); }}
    .btn.gold {{
      background: linear-gradient(180deg, rgba(214,179,106,1), rgba(184,148,67,1));
      color: #0b0f1a;
      border-color: rgba(214,179,106,0.85);
      box-shadow: 0 14px 40px rgba(214,179,106,0.18);
    }}
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
      max-width: 62ch;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }}
    .card {{ padding: 20px; }}
    .stats {{ display:grid; gap: 14px; }}
    .stat {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .stat .n {{ font-size: 26px; font-weight: 800; }}
    .stat .t {{ color: var(--muted); font-weight: 700; }}

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
    .tile h3 {{ margin: 2px 0 8px; font-size: 18px; }}
    .tile p {{ margin: 0; color: var(--muted); line-height: 1.55; }}

    .how {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      align-items: start;
    }}
    .step {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .step .k {{ color: var(--gold); font-weight: 800; letter-spacing: .6px; font-size: 12px; }}
    .step h3 {{ margin: 8px 0 8px; }}
    .step p {{ margin: 0; color: var(--muted); line-height: 1.55; }}

    .pricing {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      align-items: stretch;
    }}
    .price {{
      padding: 22px;
      border-radius: 22px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .price h3 {{ margin: 0 0 6px; }}
    .price .big {{ font-size: 40px; font-weight: 900; letter-spacing: -0.5px; margin: 6px 0 8px; }}
    .price ul {{ margin: 12px 0 0; padding-left: 18px; color: var(--muted); line-height: 1.7; }}
    .foot {{
      padding: 26px 0 60px;
      color: rgba(238,242,255,0.55);
      border-top: 1px solid rgba(255,255,255,0.06);
    }}

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
    textarea {{ min-height: 90px; resize: vertical; }}
    select option {{ color: #0b0f1a; background: #ffffff; }}
    .field.full {{ grid-column: 1 / -1; }}
    .checkrow {{
      display:flex; align-items:center; gap:10px;
      padding: 10px 12px;
      border-radius: 14px;
      border:1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.04);
    }}
    .checkrow input[type="checkbox"] {{ width: 18px; height: 18px; }}
    .actions {{ display:flex; gap: 12px; align-items:center; margin-top: 18px; flex-wrap: wrap; }}
    .muted {{ color: var(--muted); font-weight: 650; line-height: 1.6; }}

    @media (max-width: 920px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .grid3 {{ grid-template-columns: 1fr; }}
      .how {{ grid-template-columns: 1fr; }}
      .pricing {{ grid-template-columns: 1fr; }}
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
          <a class="btn" href="/login">Logowanie</a>
          <a class="btn gold" href="/register">Rejestracja</a>
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
      <a href="/#funkcje">Zakres</a>
      <a href="/#jak">Proces</a>
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
                options_html = ['<option value="">—</option>']
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
            <b>Informacja:</b> dopuszczalne są braki danych. Celem briefu jest uporządkowanie informacji i wskazanie braków do uzupełnienia.
          </div>

          <form method="post" action="{esc(action_url)}" enctype="multipart/form-data" style="margin-top:16px">
            {f'<input type="hidden" name="_submit_token" value="{esc(submit_token)}"/>' if submit_token else ""}
            {''.join(blocks)}
            <div class="actions">
              <button class="btn gold" type="submit">Zatwierdź brief</button>
              <a class="btn" href="/">Powrót</a>
              <span class="muted">Zatwierdzenie briefu uruchamia analizę i generuje raport.</span>
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
                    btn.textContent = "Przetwarzanie…";
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
# 6) AI / fallback report
# =========================

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
        "map_for_design": "Mapa do celów projektowych",
        "geotech_opinion": "Opinia geotechniczna",
        "power_conditions": "Warunki przyłączenia energii elektrycznej",
        "water_conditions": "Warunki przyłączenia wody",
        "sewage_conditions": "Warunki kanalizacji sanitarnej",
    }

    missing_list = "\n".join([f"- {missing_human.get(m, m)}" for m in missing]) or "- Brak krytycznych braków wykrytych na podstawie danych wejściowych."

    pricing_note = (
        "Cennik firmy nie został uzupełniony – wycena wynagrodzenia projektowego może wymagać doprecyzowania."
        if not pricing_text.strip()
        else "Cennik firmy został uwzględniony w analizie."
    )

    return f"""RAPORT (tryb podstawowy)

1) Podsumowanie danych
- Typ: {form.get("building_type","")}
- Powierzchnia: {form.get("usable_area_m2","")} m²
- Kondygnacje: {form.get("storeys","")}
- Dach: {form.get("roof_type","")}
- Standard: {form.get("cost_standard","")}

2) Braki / dokumenty do pozyskania
{missing_list}

3) Szacunkowy koszt robót budowlanych (model orientacyjny)
- Założenia: standard={standard}, region={region}
- Koszt jednostkowy: ok. {int(base*mult)} PLN/m²
- Estymacja: {build_low:,} – {build_high:,} PLN (orientacyjnie)

4) Wycena projektowa
- {pricing_note}
""".replace(",", " ")

def ai_report(form: Dict[str, Any], pricing_text: str, company: Dict[str, Any], architect: Dict[str, Any]) -> str:
    if not OPENAI_API_KEY or OpenAI is None:
        return fallback_report(form, pricing_text)

    client = OpenAI(api_key=OPENAI_API_KEY)

    system = (
        "Jesteś architektem-prowadzącym i kosztorysantem w Polsce. "
        "Tworzysz raport roboczy dla architekta na podstawie briefu inwestora. "
        "Format (Markdown):\n"
        "## 1) Streszczenie projektu\n"
        "## 2) Dane wejściowe (tabela)\n"
        "## 3) Założenia do kosztorysu\n"
        "## 4) Kalkulacja kosztów (kategorie + uzasadnienie)\n"
        "## 5) Braki i pytania do uzupełnienia\n"
        "## 6) Checklista dokumentów i formalności\n"
        "## 7) Ryzyka i kolizje (informacyjnie)\n"
        "## 8) Wycena wynagrodzenia projektowego na podstawie cennika firmy\n"
        "## 9) Rekomendowane dalsze kroki\n\n"
        "Pisz po polsku, rzeczowo i profesjonalnie. Nie udzielaj porady prawnej."
    )

    user_payload = {
        "company": {"name": company.get("name"), "email": company.get("email")},
        "architect": architect,
        "pricing_text_from_company": pricing_text,
        "brief": form,
        "build_cost_table_m2": {
            "standard_base_m2_pln": BUILD_COST_M2_PLN,
            "region_multiplier": REGION_MULTIPLIER,
        },
        "note": "Jeżeli brakuje danych, wskaż założenia oraz listę pytań koniecznych do doprecyzowania."
    }

    prompt = json.dumps(user_payload, ensure_ascii=False)

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content or fallback_report(form, pricing_text)
    except Exception as e:
        return fallback_report(form, pricing_text) + f"\n\n[AI ERROR: {type(e).__name__}: {e}]"


# =========================
# 7) Email (Resend HTTPS + SMTP fallback)
# =========================

def _safe_err(e: BaseException) -> str:
    parts = [f"{type(e).__name__}: {e}"]
    if isinstance(e, OSError) and getattr(e, "errno", None) is not None:
        parts.append(f"errno={e.errno}")
    return " | ".join(parts)

def send_email_via_resend(to_email: str, subject: str, body: str) -> Tuple[bool, str]:
    if not (RESEND_API_KEY and RESEND_FROM):
        return False, "RESEND not configured"

    try:
        import urllib.request
        import urllib.error

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

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = int(getattr(resp, "status", 200))
                text = resp.read().decode("utf-8", errors="replace")
                if 200 <= code < 300:
                    return True, f"RESEND OK status={code} resp={text[:200]}"
                return False, f"RESEND FAIL status={code} resp={text[:400]}"
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return False, f"RESEND HTTPError status={e.code} resp={err_body[:400]}"

    except Exception as e:
        return False, f"RESEND exception: {_safe_err(e)}"

def send_email_via_smtp(to_email: str, subject: str, body: str) -> Tuple[bool, str]:
    if not BOT_EMAIL or not BOT_EMAIL_PASSWORD:
        return False, "SMTP not configured"

    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = BOT_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

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
# 8) Stripe
# =========================

def stripe_ready() -> bool:
    return bool(
        stripe is not None
        and STRIPE_SECRET_KEY
        and (STRIPE_PRICE_ID_MONTHLY or STRIPE_PRICE_ID_YEARLY)
        and STRIPE_WEBHOOK_SECRET
    )

def subscription_active(company: Dict[str, Any]) -> bool:
    if DEV_BYPASS_SUBSCRIPTION:
        return True
    st = (company.get("stripe") or {}).get("status") or ""
    return st in ("active", "trialing")

def stripe_init() -> None:
    if stripe_ready():
        stripe.api_key = STRIPE_SECRET_KEY  # type: ignore


# =========================
# 9) App + auth
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
# 10) Landing page
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
              {badge("AI – skonfigurowane" if openai_ok else "AI – nieaktywne", openai_ok)}
              {badge("E-mail – skonfigurowane" if mail_ok else "E-mail – nieaktywne", mail_ok)}
              {badge("Płatności – skonfigurowane" if stripe_ok else "Płatności – opcjonalne", stripe_ok)}
            </div>
            <h1>Brief inwestorski i analiza <span class="gold">dla pracowni architektonicznych</span></h1>
            <p class="lead">
              Inwestor uzupełnia uporządkowany brief. System przygotowuje raport dla architekta:
              braki danych, ryzyka, checklisty formalne oraz wstępną estymację kosztów.
            </p>
            <div style="height:18px"></div>
            <div class="cta" style="justify-content:flex-start">
              <a class="btn gold" href="/register">Utwórz konto</a>
              <a class="btn" href="/demo">Podgląd briefu</a>
            </div>
          </div>
          <div class="panel card">
            <div class="stats">
              <div class="stat">
                <div class="n">Jednolity standard</div>
                <div class="t">brief i dane wejściowe w spójnej strukturze</div>
              </div>
              <div class="stat">
                <div class="n">Raport roboczy</div>
                <div class="t">materiał do rozmowy i doprecyzowania zakresu</div>
              </div>
              <div class="stat">
                <div class="n">Cennik firmy</div>
                <div class="t">możliwość wyceny na podstawie zasad pracowni</div>
              </div>
            </div>
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
          <h1 style="margin:0 0 14px">FAQ</h1>
          <div class="panel card">
            <p class="muted"><b>Czy można pozostawić pola puste?</b><br/>Tak. Raport wskaże braki i pytania do uzupełnienia.</p>
            <p class="muted"><b>Do kogo trafia raport?</b><br/>Do wskazanego architekta (odbiorcy) zdefiniowanego w panelu firmy.</p>
            <p class="muted"><b>Czy koszt budowy jest wyceną wiążącą?</b><br/>Nie. To estymacja robocza na potrzeby wstępnego etapu.</p>
          </div>
        </div>
      </section>

      <div class="foot">
        <div class="wrap">
          © {esc(APP_NAME)} • {badge("Tryb deweloperski", DEV_BYPASS_SUBSCRIPTION)}
        </div>
      </div>
    </div>
    """

    return HTMLResponse(layout("Start", body=body, nav=nav_links()))


# =========================
# 11) Auth: rejestracja / logowanie
# =========================

@app.get("/register", response_class=HTMLResponse)
def register_page():
    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Rejestracja konta firmy</h1>
      <p class="lead">Konto firmy zarządza cennikiem oraz listą odbiorców raportów.</p>
      <div style="height:18px"></div>
      <div class="panel card">
        <form method="post" action="/register">
          <div class="fields">
            <div class="field"><label>Nazwa firmy</label><input name="name" placeholder="np. Pracownia XYZ"/></div>
            <div class="field"><label>E-mail (login)</label><input type="email" name="email" placeholder="biuro@..."/></div>
            <div class="field full"><label>Hasło</label><input type="password" name="password" placeholder="min. 8 znaków"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Utwórz konto</button>
            <a class="btn" href="/login">Przejdź do logowania</a>
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
        return HTMLResponse(layout(
            "Rejestracja",
            body=flash_html("Uzupełnij nazwę firmy, adres e-mail oraz hasło (min. 8 znaków).")
                 + '<div class="wrap formwrap"><a class="btn" href="/register">Powrót</a></div>',
            nav=nav_links()
        ))

    db = _load_db()
    for c in db["companies"].values():
        if c.get("email") == email:
            return HTMLResponse(layout(
                "Rejestracja",
                body=flash_html("Podany adres e-mail jest już zarejestrowany.")
                     + '<div class="wrap formwrap"><a class="btn" href="/register">Powrót</a></div>',
                nav=nav_links()
            ))

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
      <h1 style="margin:0 0 10px">Logowanie</h1>
      <p class="lead">Panel firmy: cennik, odbiorcy raportów oraz subskrypcja.</p>
      <div style="height:18px"></div>
      <div class="panel card">
        <form method="post" action="/login">
          <div class="fields">
            <div class="field"><label>E-mail</label><input type="email" name="email" placeholder="biuro@..."/></div>
            <div class="field"><label>Hasło</label><input type="password" name="password"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Zaloguj</button>
            <a class="btn" href="/register">Rejestracja</a>
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

    return HTMLResponse(layout(
        "Logowanie",
        body=flash_html("Nieprawidłowy e-mail lub hasło.")
             + '<div class="wrap formwrap"><a class="btn" href="/login">Powrót</a></div>',
        nav=nav_links()
    ))

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


# =========================
# 12) Dashboard firmy
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
    stripe_msg = "Płatności nieaktywne" if not stripe_ready() else f"Status subskrypcji: {st}"

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
              <a class="btn" href="{esc(link)}" target="_blank">Otwórz brief</a>
              <a class="btn" href="/dashboard/architect/delete?id={esc(a['id'])}">Usuń</a>
            </div>
          </div>
          <div style="height:8px"></div>
          <div class="muted">Link do briefu dla inwestora:</div>
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
          <div class="muted">{badge("Subskrypcja aktywna" if sub_ok else "Subskrypcja nieaktywna", sub_ok)} • {esc(stripe_msg)}</div>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <a class="btn" href="/demo">Podgląd briefu</a>
          <a class="btn" href="/logout">Wyloguj</a>
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="grid3" style="grid-template-columns: 1fr 1fr; align-items: start;">
        <div class="panel card">
          <h3 style="margin:0 0 10px">Cennik firmy (tekst)</h3>
          <p class="muted" style="margin-top:0">
            Wklej zasady wyceny (stawki, minimalne kwoty, etapy, dodatki). System uwzględni je w raporcie.
          </p>
          <form method="post" action="/dashboard/pricing">
            <div class="field">
              <label>Treść cennika</label>
              <textarea name="pricing_text" placeholder="np. Projekt budowlany: 120 zł/m², min. 2 690 zł...">{esc(company.get("pricing_text",""))}</textarea>
            </div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz</button>
            </div>
          </form>
        </div>

        <div class="panel card">
          <h3 style="margin:0 0 10px">Dane rozliczeniowe (opcjonalnie)</h3>
          <p class="muted" style="margin-top:0">Dane są zapisywane w profilu firmy.</p>
          <form method="post" action="/dashboard/billing">
            <div class="fields">
              <div class="field"><label>Nazwa firmy</label><input name="company_name" value="{esc((company.get("billing") or {}).get("company_name",""))}"/></div>
              <div class="field"><label>NIP</label><input name="nip" value="{esc((company.get("billing") or {}).get("nip",""))}"/></div>
              <div class="field full"><label>Adres</label><input name="address" value="{esc((company.get("billing") or {}).get("address",""))}"/></div>
              <div class="field full"><label>E-mail do faktur</label><input name="invoice_email" value="{esc((company.get("billing") or {}).get("invoice_email",""))}"/></div>
            </div>
            <div class="actions">
              <button class="btn gold" type="submit">Zapisz</button>
            </div>
          </form>
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="panel card">
        <h3 style="margin:0 0 10px">Odbiorcy raportów (architekci)</h3>
        <p class="muted" style="margin-top:0">Dodaj odbiorcę raportów i wygeneruj indywidualny link do briefu.</p>

        <form method="post" action="/dashboard/architect/add">
          <div class="fields">
            <div class="field"><label>Imię / identyfikator</label><input name="name" placeholder="np. Jan Kowalski"/></div>
            <div class="field"><label>E-mail odbiorcy</label><input type="email" name="email" placeholder="jan@pracownia.pl"/></div>
          </div>
          <div class="actions">
            <button class="btn gold" type="submit">Dodaj</button>
          </div>
        </form>

        <div style="height:14px"></div>
        <div class="grid3" style="grid-template-columns: 1fr; gap: 12px;">
          {''.join(arch_rows) if arch_rows else '<div class="muted">Brak zdefiniowanych odbiorców raportów.</div>'}
        </div>
      </div>

      <div style="height:18px"></div>

      <div class="panel card">
        <h3 style="margin:0 0 10px">Subskrypcja</h3>
        <p class="muted" style="margin-top:0">
          Subskrypcja zapewnia dostęp do platformy oraz limit wysyłek briefów.
        </p>
        <div class="actions">
          <a class="btn" href="/billing/checkout?plan=monthly">Subskrypcja miesięczna (249 zł)</a>
          <a class="btn" href="/billing/checkout?plan=yearly">Subskrypcja roczna (2 690 zł)</a>
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
    db["companies"][cid]["architects"] = [
        a for a in db["companies"][cid].get("architects", []) if a.get("id") != id
    ]
    _save_db(db)
    return RedirectResponse(url="/dashboard", status_code=302)


# =========================
# 13) Demo formularza (publiczne)
# =========================

@app.get("/demo", response_class=HTMLResponse)
def demo():
    return HTMLResponse(render_form(
        action_url="/demo/submit",
        title="Brief (podgląd)",
        subtitle="Podgląd formularza. W trybie demonstracyjnym raport jest prezentowany na ekranie."
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
        pricing_text="(DEMO) cennik nie dotyczy",
        company={"name": "DEMO", "email": ""},
        architect={"name": "DEMO", "email": ""},
    )

    body = f"""
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Raport (podgląd)</h1>
      <p class="muted">Raport w trybie demonstracyjnym.</p>
      <div class="panel card" style="white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;">
{esc(report)}
      </div>
      <div class="actions">
        <a class="btn gold" href="/demo">Powrót</a>
        <a class="btn" href="/">Strona główna</a>
      </div>
    </div>
    """
    return HTMLResponse(layout("Raport demo", body=body, nav=nav_links()))


# =========================
# 14) Formularz firmowy /f/{token}
# =========================

def find_by_token(token: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
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
        return HTMLResponse(layout(
            "Błąd",
            body='<div class="wrap formwrap"><h1>Nieprawidłowy link</h1><a class="btn" href="/">Strona główna</a></div>',
            nav=nav_links()
        ), status_code=404)

    if not subscription_active(company):
        return HTMLResponse(layout(
            "Dostęp ograniczony",
            body='<div class="wrap formwrap"><h1>Formularz niedostępny</h1><p class="muted">Dostęp do briefu jest obecnie nieaktywny.</p><a class="btn" href="/">Strona główna</a></div>',
            nav=nav_links()
        ), status_code=403)

    submit_token = _new_submit_token()
    return HTMLResponse(render_form(
        action_url=f"/f/{token}",
        title=f"Brief – {company.get('name','')} / {architect.get('name','')}",
        subtitle="Prosimy o uzupełnienie dostępnych informacji. Braki danych zostaną wskazane w raporcie.",
        submit_token=submit_token
    ))

@app.post("/f/{token}", response_class=HTMLResponse)
async def submit_form(token: str, request: Request):
    company, architect = find_by_token(token)
    if not company or not architect:
        return HTMLResponse("Nieprawidłowy link", status_code=404)

    if not subscription_active(company):
        return HTMLResponse("Dostęp do formularza jest nieaktywny.", status_code=403)

    db = _load_db()
    company_id = company.get("id")
    if not company_id or company_id not in db.get("companies", {}):
        return HTMLResponse("Błąd danych firmy", status_code=500)

    c = db["companies"][company_id]
    _ensure_usage_period(c)
    if _forms_remaining(c) <= 0:
        body = f"""
        <div class="wrap formwrap">
          <h1 style="margin:0 0 10px">Limit formularzy został wykorzystany</h1>
          <p class="lead">Limit miesięczny wynosi {FORMS_PER_MONTH_LIMIT} wysłanych formularzy.</p>
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
              <h1 style="margin:0 0 10px">Brief został przyjęty</h1>
              <p class="lead">Zgłoszenie jest przetwarzane. Dziękujemy.</p>
              <div class="actions"><a class="btn" href="/">Strona główna</a></div>
            </div>
            """
            return HTMLResponse(layout("Potwierdzenie", body=body, nav=nav_links()))
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
            subject=f"[{APP_NAME}] Brief inwestorski – {company.get('name','')} / {architect.get('name','')}",
            body=report,
            delivery_id=delivery_id,
        )

    # UI: profesjonalnie i bez informacji o logach/hostingu
    if sent:
        body = """
        <div class="wrap formwrap">
          <h1 style="margin:0 0 10px">Dziękujemy.</h1>
          <p class="lead">Brief został przyjęty i przekazany do odbiorcy raportu.</p>
          <div class="actions">
            <a class="btn" href="/">Strona główna</a>
          </div>
        </div>
        """
        return HTMLResponse(layout("Potwierdzenie", body=body, nav=nav_links()))

    body = """
    <div class="wrap formwrap">
      <h1 style="margin:0 0 10px">Dziękujemy.</h1>
      <p class="lead">Brief został przyjęty.</p>
      <p class="muted">Jeżeli w ciągu kilku minut nie zostanie potwierdzone przekazanie raportu, prosimy o kontakt z pracownią.</p>
      <div class="actions">
        <a class="btn" href="/">Strona główna</a>
      </div>
    </div>
    """
    return HTMLResponse(layout("Potwierdzenie", body=body, nav=nav_links()))


# =========================
# 15) Stripe Checkout + Webhook
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
# 16) Health
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
