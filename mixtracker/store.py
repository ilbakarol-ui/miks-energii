"""Przechowywanie dziennych danych (MWh) w plikach CSV - jeden plik na kraj."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from .categories import AUX, DETAIL_KEYS, PRICE_COLS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAILY = DATA / "daily"
STATE = DATA / "state.json"
COLUMNS = ["cov"] + DETAIL_KEYS + AUX + PRICE_COLS
PRICES = DATA / "prices"
FORECAST = DATA / "forecast"


def load_countries(path: Path | None = None) -> list[dict]:
    path = path or ROOT / "config" / "countries.csv"
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if not r.get("source"):
            r["source"] = "energycharts" if r.get("ec") else "eurostat"
    return rows


SOURCE_LABEL = {"energycharts": "Energy-Charts", "elexon": "Elexon BMRS", "eurostat": "Eurostat (tylko miesięcznie)"}


def read_known_breaks(path: Path | None = None) -> list[dict]:
    """Znane skoki raportowania (config/known_breaks.csv): code, series, date, aggs (agregaty rozdzielone |), note."""
    p = path or ROOT / "config" / "known_breaks.csv"
    if not p.exists():
        return []
    import csv
    with open(p, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("code") and r.get("date")]


def has_daily(c: dict) -> bool:
    """Czy kraj ma źródło danych dziennych (Energy-Charts albo Elexon)."""
    return bool(c.get("ec")) or c.get("source") == "elexon"


def load_settings() -> dict:
    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def read_daily(code: str, folder: Path | None = None) -> pd.DataFrame:
    p = (folder or DAILY) / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="date"), dtype=float)
    df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    for c in COLUMNS:
        if c not in df:
            df[c] = float("nan")
    return df[COLUMNS].astype(float).sort_index()


def merge_daily(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Nowe dni zastępują stare, chyba że nowa doba ma wyraźnie gorsze pokrycie
    (np. ucięty brzeg zakresu zapytania)."""
    if new is None or new.empty:
        return old
    if old.empty:
        return new.sort_index()
    new = new[COLUMNS]
    common = new.index.intersection(old.index)
    worse = [d for d in common if new.at[d, "cov"] + 0.01 < (old.at[d, "cov"] or 0)]
    new = new.drop(index=worse)
    out = pd.concat([old.drop(index=new.index.intersection(old.index)), new]).sort_index()
    return out[COLUMNS]


def write_daily(code: str, df: pd.DataFrame, folder: Path | None = None):
    folder = folder or DAILY
    folder.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    rounding = {c: 1 for c in DETAIL_KEYS + AUX + PRICE_COLS}
    rounding.update({"price_avg": 2, "price_min": 2, "price_max": 2, "price_neg_h": 2})
    out.round(rounding).to_csv(folder / f"{code}.csv", float_format="%.10g")


def read_zone(zone: str) -> pd.DataFrame:
    p = PRICES / f"{zone}.csv"
    if not p.exists():
        return pd.DataFrame(columns=["price_avg", "price_min", "price_max", "price_neg_h"],
                            index=pd.DatetimeIndex([], name="date"), dtype=float)
    return pd.read_csv(p, parse_dates=["date"], index_col="date").astype(float).sort_index()


def write_zone(zone: str, df: pd.DataFrame):
    PRICES.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    out.round(2).to_csv(PRICES / f"{zone}.csv", float_format="%.10g")


def read_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def write_state(state: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
