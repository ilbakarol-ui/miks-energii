"""Mapowanie nazw typów produkcji (Energy-Charts / ENTSO-E) na kategorie trackera.

Kategorie szczegółowe są sumowane do 8 grup używanych na wykresach. Kolejność
GROUPS to kolejność warstw na wykresach skumulowanych (od dołu) i zarazem
kolejność kolorów zweryfikowana pod kątem rozróżnialności (także dla daltonistów).
"""
from __future__ import annotations

import re

# kategoria szczegółowa -> (etykieta PL, grupa, czy OZE)
DETAIL = {
    "lignite":         ("Węgiel brunatny", "coal", False),
    "hard_coal":       ("Węgiel kamienny", "coal", False),
    "peat_shale":      ("Torf i łupki bitumiczne", "coal", False),
    "gas":             ("Gaz ziemny", "gas", False),
    "oil":             ("Ropa i produkty naftowe", "other", False),
    "nuclear":         ("Jądrowa", "nuclear", False),
    "wind_onshore":    ("Wiatr na lądzie", "wind", True),
    "wind_offshore":   ("Wiatr na morzu", "wind", True),
    "solar":           ("Słońce", "solar", True),
    "hydro_ror":       ("Woda przepływowa", "hydro", True),
    "hydro_reservoir": ("Woda zbiornikowa", "hydro", True),
    "hydro_other":     ("Woda (inne)", "hydro", True),
    "hydro_pumped":    ("Elektrownie szczytowo-pompowe", "hydro", False),
    "biomass":         ("Biomasa i biogaz", "bio", True),
    "geothermal":      ("Geotermia", "bio", True),
    "other_renewable": ("Inne OZE", "bio", True),
    "waste":           ("Odpady", "other", False),
    "battery":         ("Magazyny bateryjne (oddawanie)", "other", False),
    "misc":            ("Inne", "other", False),
}
DETAIL_KEYS = list(DETAIL)

# grupa -> (etykieta PL, kolor jasny, kolor ciemny)
GROUPS = {
    "gas":     ("Gaz", "#eb6834", "#d95926"),
    "nuclear": ("Jądrowa", "#6250d6", "#9085e9"),
    "coal":    ("Węgiel", "#e34948", "#e66767"),
    "bio":     ("Biomasa i inne OZE", "#008300", "#008300"),
    "other":   ("Ropa, odpady, inne", "#e87ba4", "#d55181"),
    "hydro":   ("Woda", "#2a78d6", "#3987e5"),
    "wind":    ("Wiatr", "#1baf7a", "#199e70"),
    "solar":   ("Słońce", "#eda100", "#c98500"),
}
GROUP_KEYS = list(GROUPS)

RENEWABLE = [k for k, v in DETAIL.items() if v[2]]
FOSSIL = ["lignite", "hard_coal", "peat_shale", "gas", "oil"]

# kolumny pomocnicze w danych dziennych (nie są produkcją)
AUX = ["load", "storage_charge", "xborder"]

# kolumny cenowe w danych dziennych kraju:
# price_avg - średnia czasowa ceny day-ahead (EUR/MWh), price_min/max, price_neg_h - godziny z ceną < 0,
# pw_* - suma cena x energia (tys. EUR), pq_* - energia z dostępną ceną (MWh); cena ważona = pw / pq
PRICE_COLS = ["price_avg", "price_min", "price_max", "price_neg_h",
              "pw_solar", "pq_solar", "pw_wind", "pq_wind", "pw_gen", "pq_gen", "pw_load", "pq_load"]

_IGNORE = re.compile(r"share|residual|price|forecast|day.ahead|installed|emission|co2|frequency", re.I)


def classify(name: str) -> str | None:
    """Zwraca klucz kategorii, 'load', 'storage_charge', 'xborder' albo None (pomiń).

    Nieznane nazwy trafiają do 'misc' - lista nazw jest raportowana w kontroli danych.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    if _IGNORE.search(n):
        return None
    if re.search(r"cross.?border|import|export|exchange|trading", n):
        return "xborder"
    if re.fullmatch(r"(total )?load|load \(.*\)|actual (total )?load|consumption \(load\)", n):
        return "load"
    if "load" in n:
        return None
    if re.search(r"consumption|charg(e|ing)(?!.*dis)|pumping|\bstorage in\b", n) and "discharg" not in n:
        return "storage_charge"
    if "pump" in n:
        return "hydro_pumped"
    if re.search(r"run.?of.?river", n):
        return "hydro_ror"
    if "reservoir" in n or "dam" in n.split():
        return "hydro_reservoir"
    if "hydro" in n or "water" in n:
        return "hydro_other"
    if "lignite" in n or "brown coal" in n:
        return "lignite"
    if "peat" in n or "shale" in n:
        return "peat_shale"
    if "coal" in n:
        return "hard_coal"
    if re.search(r"\bbio|biomass|biogas|biofuel", n):
        return "biomass"
    if "gas" in n:
        return "gas"
    if "oil" in n or "diesel" in n:
        return "oil"
    if "nuclear" in n:
        return "nuclear"
    if "offshore" in n:
        return "wind_offshore"
    if "wind" in n:
        return "wind_onshore"
    if "solar" in n or "photovolt" in n or n == "pv":
        return "solar"
    if "geotherm" in n:
        return "geothermal"
    if "waste" in n:
        return "waste"
    if "battery" in n or "storage" in n:
        return "battery"
    if re.search(r"marine|tidal|wave|renewable", n):
        return "other_renewable"
    return "misc"


def is_known(name: str) -> bool:
    """Czy nazwa została rozpoznana konkretną regułą (a nie wpadła do 'misc' z braku dopasowania)."""
    c = classify(name)
    if c != "misc":
        return True
    return bool(re.search(r"\bother", (name or "").lower()))
