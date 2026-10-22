"""Moc zainstalowana w magazynach energii elektrycznej (dane roczne).

Źródła:
- Elektrownie szczytowo-pompowe: Energy-Charts /installed_power (rocznie; dla krajów poza
  Niemcami dane ENTSO-E o mocy zainstalowanej, dla Szwajcarii BFE), seria „Hydro pumped storage”.
- Magazyny bateryjne: Eurostat nrg_inf_bt (moc CAP_BAP w MW, pojemność CAP_BAST w MWh,
  rocznie, publikacja z ok. 10-miesięcznym opóźnieniem). Dla Niemiec Energy-Charts
  („Battery storage (power)” i „(capacity)” z rejestru MaStR, z magazynami domowymi) – Eurostat
  podaje dla Niemiec tylko część magazynów sieciowych (ok. 0,8 GW w 2024 r. wobec 12,7 GW w MaStR).
- UE-27: moc szczytowo-pompowa z Energy-Charts (agregat „eu”), baterie = Eurostat UE-27
  z Niemcami podmienionymi na MaStR.

Dane surowe: data/storage/ec_installed.csv (kod, rok, seria, GW lub GWh) i
data/storage/eurostat_batteries.csv; wynik: data/storage/installed.csv.
Odświeżanie: Eurostat co 7 dni, Energy-Charts co 30 dni, najwyżej `per_run` krajów na przebieg
(limit zapytań Energy-Charts ok. 2 na minutę).
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import requests

from . import store

DIR = store.DATA / "storage"
EC_RAW = DIR / "ec_installed.csv"
ES_RAW = DIR / "eurostat_batteries.csv"
OUT = DIR / "installed.csv"
STATE = DIR / "state.json"
ES_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_inf_bt"
EC_SERIES = {"Hydro pumped storage": "pumped_gw", "Battery storage (power)": "bat_gw",
             "Battery storage (capacity)": "bat_gwh"}
EC_BATTERY = {"DE"}           # kraje, dla których baterie bierzemy z Energy-Charts (MaStR)
FIRST_YEAR = 2019
ES_FROM = 2022              # od tego roku dane Eurostatu o bateriach są kompletne


def _read(p, cols):
    return pd.read_csv(p, dtype={"code": str}) if p.exists() else pd.DataFrame(columns=cols)


def parse_ec(code: str, js: dict, this_year: int) -> pd.DataFrame:
    """Serie magazynów z odpowiedzi /installed_power. Rok bieżący tylko wtedy, gdy wartość nie spada
    poniżej 90% poprzedniego roku (agregaty bywają niepełne w trakcie roku)."""
    years = [str(t) for t in js.get("time") or []]
    rows = []
    for p in js.get("production_types") or []:
        key = EC_SERIES.get(p.get("name"))
        if not key:
            continue
        vals = dict(zip(years, p.get("data") or []))
        for y, v in vals.items():
            if not y.isdigit() or v is None:
                continue
            yi = int(y)
            if yi < FIRST_YEAR or yi > this_year:
                continue
            if yi == this_year:
                prev = vals.get(str(yi - 1))
                if prev is not None and v < 0.9 * prev:
                    continue
            rows.append({"code": code, "year": yi, "series": key, "value": float(v)})
    return pd.DataFrame(rows, columns=["code", "year", "series", "value"])


def fetch_eurostat(geo_to_code: dict[str, str]) -> pd.DataFrame:
    from .eurostat import _flatten
    r = requests.get(ES_URL, params=[("format", "JSON"), ("lang", "en"), ("siec", "BA100"),
                                     ("sinceTimePeriod", str(FIRST_YEAR))], timeout=120)
    r.raise_for_status()
    df = _flatten(r.json())
    df = df[df["plant_tec"].isin(["CAP_BAP", "CAP_BAST"]) & df["unit"].isin(["MW", "MWH"])]
    df = df[df["geo"].isin(geo_to_code)]
    df["series"] = df["plant_tec"].map({"CAP_BAP": "bat_gw", "CAP_BAST": "bat_gwh"})
    df["value"] = df["value"].astype(float) / 1000.0           # MW -> GW, MWh -> GWh
    df["code"] = df["geo"].map(geo_to_code)
    df["year"] = df["time"].astype(int)
    return df[["code", "year", "series", "value"]].sort_values(["code", "series", "year"])


def combine(ec: pd.DataFrame, es: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """Jedna tabela: kod, rok, pumped_gw, bat_gw, bat_gwh, bat_src."""
    def piv(df):
        if df.empty:
            return pd.DataFrame(columns=["pumped_gw", "bat_gw", "bat_gwh"])
        return df.pivot_table(index=["code", "year"], columns="series", values="value", aggfunc="last")
    e, s = piv(ec), piv(es)
    out = []
    years = sorted(set(e.index.get_level_values(1) if len(e) else []) | set(s.index.get_level_values(1) if len(s) else []))
    for code in codes:
        for y in years:
            row = {"code": code, "year": int(y), "pumped_gw": None, "bat_gw": None, "bat_gwh": None, "bat_src": None}
            if (code, y) in e.index and "pumped_gw" in e:
                row["pumped_gw"] = e.at[(code, y), "pumped_gw"]
            src = e if code in EC_BATTERY else s
            if (code, y) in src.index:
                for k in ("bat_gw", "bat_gwh"):
                    if k in src:
                        row[k] = src.at[(code, y), k]
                row["bat_src"] = "MaStR (Energy-Charts)" if code in EC_BATTERY else "Eurostat"
            if code == "EU":
                # UE-27 z Niemcami wg MaStR zamiast niepełnych danych Eurostatu
                def at(df, c, k):
                    return df.at[(c, y), k] if (c, y) in df.index and k in df else float("nan")
                for k in ("bat_gw", "bat_gwh"):
                    base, de_s, de_e = at(s, "EU", k), at(s, "DE", k), at(e, "DE", k)
                    if pd.notna(base) and pd.notna(de_s) and pd.notna(de_e):
                        row[k], row["bat_src"] = base - de_s + de_e, "Eurostat, Niemcy wg MaStR"
            if any(pd.notna(row[k]) for k in ("pumped_gw", "bat_gw", "bat_gwh")):
                out.append(row)
    df = pd.DataFrame(out, columns=["code", "year", "pumped_gw", "bat_gw", "bat_gwh", "bat_src"])
    # Eurostat zbiera dane o bateriach obowiązkowo od roku 2022 - wcześniejsze zera to brak danych, nie brak magazynów
    early0 = (df["year"] < ES_FROM) & (df["bat_src"] == "Eurostat")
    for k in ("bat_gw", "bat_gwh"):
        df.loc[early0 & (pd.to_numeric(df[k], errors="coerce") == 0), k] = None
    for k in ("pumped_gw", "bat_gw", "bat_gwh"):
        df[k] = pd.to_numeric(df[k], errors="coerce").round(3)
    return df


def update(settings: dict, log: list, client=None, per_run: int = 6, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    DIR.mkdir(parents=True, exist_ok=True)
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    countries = store.load_countries()
    ec_codes = {c["code"]: c["ec"] for c in countries if c.get("ec")}
    # Eurostat co 7 dni
    es = _read(ES_RAW, ["code", "year", "series", "value"])
    if (today - dt.date.fromisoformat(st.get("eurostat", "2000-01-01"))).days >= 7:
        try:
            geo = {("EU27_2020" if c["code"] == "EU" else c["eurostat"]): c["code"]
                   for c in countries if c.get("eurostat") or c["code"] == "EU"}
            es = fetch_eurostat(geo)
            es.to_csv(ES_RAW, index=False)
            st["eurostat"] = today.isoformat()
        except Exception as e:
            log.append(f"Magazyny: Eurostat nrg_inf_bt niedostępny ({type(e).__name__})")
    # Energy-Charts co 30 dni, najstarsze najpierw
    ec = _read(EC_RAW, ["code", "year", "series", "value"])
    done = st.setdefault("ec", {})
    due = sorted((c for c in ec_codes if (today - dt.date.fromisoformat(done.get(c, "2000-01-01"))).days >= 30),
                 key=lambda c: done.get(c, ""))[:per_run]
    if due:
        if client is None:
            from .energycharts import Client
            client = Client(base_url=settings["energycharts_base_url"], interval=settings["request_interval_seconds"],
                            timeout=settings["request_timeout_seconds"], max_retries=settings["max_retries"])
        for code in due:
            try:
                js = client.get("installed_power", {"country": ec_codes[code], "time_step": "yearly",
                                                    "installation_decommission": "false"})
            except Exception as e:
                log.append(f"Magazyny: Energy-Charts installed_power {code}: {type(e).__name__}")
                continue
            new = parse_ec(code, js or {}, today.year)
            ec = pd.concat([ec[ec["code"] != code], new], ignore_index=True)
            done[code] = today.isoformat()
        ec.sort_values(["code", "series", "year"]).to_csv(EC_RAW, index=False)
    res = combine(ec, es, [c["code"] for c in countries])
    res.to_csv(OUT, index=False)
    STATE.write_text(json.dumps(st, indent=1))
    return {"fetched": due, "rows": len(res)}


def read() -> pd.DataFrame:
    return _read(OUT, ["code", "year", "pumped_gw", "bat_gw", "bat_gwh", "bat_src"])


def block(names: dict[str, str], last_years: int = 6) -> dict:
    """Dane do dashboardu: ostatnie `last_years` lat."""
    df = read()
    if df.empty:
        return {}
    years = sorted(df["year"].unique())[-last_years:]
    out = {"years": [int(y) for y in years], "countries": {}}
    for code, g in df.groupby("code"):
        g = g.set_index("year").reindex(years)
        def vals(k):
            return [None if pd.isna(v) else round(float(v), 3) for v in g[k]]
        src = [s for s in g["bat_src"].dropna().unique()]
        out["countries"][code] = {"pumped": vals("pumped_gw"), "bat": vals("bat_gw"), "bat_gwh": vals("bat_gwh"),
                                  "bat_src": src[-1] if src else None}
    return out
