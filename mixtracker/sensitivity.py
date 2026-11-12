"""Wrażliwość zużycia gazu i prądu na temperaturę (zakładka „Pogoda”, od 12.11.2026).

- Gaz: miesięczne zużycie krajowe z Eurostatu (nrg_cb_gasm, IC_CAL_MG – te same dane, do których
  kalibruje się raport Gaz UE), od 2023 r. (po kryzysowym spadku popytu w 2022 r.), regresja
  zużycia dziennego w miesiącu na średnich stopniodniach grzania (HDD, baza 15,5 °C):
  GWh/d = a + b · HDD. b = ile GWh/d gazu więcej na każdy stopień chłodniej (poniżej 15,5 °C).
- Prąd: dzienne zużycie z dashboardu (data/daily) z ostatnich 730 dni, regresja na HDD, CDD (baza 22 °C)
  i dniu tygodnia.
- Temperatura kraju: średnia punktów sieci gazowej z bazy data/shared_weather (ERA5 + ostatnie dni),
  czyli ta sama, z której liczona jest prognoza – współczynniki pasują do prognozy.
Dane Eurostatu: data/sensitivity/gas_monthly.csv (odświeżane co 30 dni w przebiegu nocnym).
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import requests

from . import store

FOLDER = store.DATA / "sensitivity"
GAS = FOLDER / "gas_monthly.csv"
STATE = FOLDER / "state.json"
URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_cb_gasm"
HDD_BASE, CDD_BASE = 15.5, 22.0
GAS_SINCE = "2023-01"


def hdd(t):
    return np.clip(HDD_BASE - np.asarray(t, dtype=float), 0, None)


def cdd(t):
    return np.clip(np.asarray(t, dtype=float) - CDD_BASE, 0, None)


# ---------------- Eurostat ----------------

def fetch_gas(geo_to_code: dict[str, str], since: str = "2019-01", get=requests.get) -> pd.DataFrame:
    from .eurostat import _flatten
    r = get(URL, params=[("format", "JSON"), ("lang", "en"), ("siec", "G3000"), ("nrg_bal", "IC_CAL_MG"),
                         ("unit", "TJ_GCV"), ("sinceTimePeriod", since)] + [("geo", g) for g in geo_to_code],
            timeout=120)
    r.raise_for_status()
    df = _flatten(r.json())
    df = df[df["geo"].isin(geo_to_code)]
    out = pd.DataFrame({"code": df["geo"].map(geo_to_code), "month": df["time"],
                        "gwh": (df["value"].astype(float) / 3.6).round(1)})      # TJ (GCV) -> GWh
    return out.sort_values(["code", "month"]).reset_index(drop=True)


def update(log: list[str], today: dt.date | None = None, every_days: int = 30, get=requests.get) -> bool:
    """Odświeża gas_monthly.csv co `every_days` dni. Błąd nie przerywa przebiegu."""
    today = today or dt.date.today()
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    if GAS.exists() and (today - dt.date.fromisoformat(st.get("gas", "2000-01-01"))).days < every_days:
        return False
    geo = {("EU27_2020" if c["code"] == "EU" else c["eurostat"]): c["code"]
           for c in store.load_countries() if c.get("eurostat") or c["code"] == "EU"}
    try:
        df = fetch_gas(geo, get=get)
    except Exception as e:
        log.append(f"Wrażliwość na temperaturę: Eurostat nrg_cb_gasm niedostępny ({type(e).__name__})")
        return False
    if df.empty:
        log.append("Wrażliwość na temperaturę: Eurostat nrg_cb_gasm bez danych")
        return False
    FOLDER.mkdir(parents=True, exist_ok=True)
    df.to_csv(GAS, index=False)
    st["gas"] = today.isoformat()
    st["gas_last_month"] = str(df["month"].max())
    STATE.write_text(json.dumps(st, indent=1))
    return True


def read_gas() -> pd.DataFrame:
    return pd.read_csv(GAS, dtype={"code": str}) if GAS.exists() else pd.DataFrame(columns=["code", "month", "gwh"])


# ---------------- dopasowanie ----------------

def fit_gas(monthly: pd.Series, temp: pd.Series, since: str = GAS_SINCE, min_months: int = 18,
            min_r2: float = 0.2) -> dict | None:
    """monthly: GWh w miesiącu (indeks 'RRRR-MM'); temp: średnia dobowa (indeks dat). GWh/d = a + b·HDD."""
    if monthly is None or temp is None or temp.empty:
        return None
    t = temp.dropna()
    t.index = pd.to_datetime(t.index)
    h = pd.Series(hdd(t.values), index=t.index)
    g = h.groupby(h.index.strftime("%Y-%m")).agg(["mean", "size"])
    rows = []
    for m, v in monthly.dropna().items():
        if m < since or m not in g.index:
            continue
        days = pd.Period(m).days_in_month
        if g.at[m, "size"] < days - 2:
            continue
        rows.append((m, v / days, g.at[m, "mean"]))
    if len(rows) < min_months:
        return None
    d = pd.DataFrame(rows, columns=["m", "y", "x"])
    b, a = np.polyfit(d["x"], d["y"], 1)
    pred = a + b * d["x"]
    ss = float(((d["y"] - d["y"].mean()) ** 2).sum())
    r2 = 1 - float(((d["y"] - pred) ** 2).sum()) / ss if ss > 0 else 0.0
    if b <= 0 or r2 < min_r2:   # zużycie niezależne od temperatury (np. gaz głównie w elektrowniach)
        return None
    return {"a": round(float(a), 1), "b": round(float(b), 2), "r2": round(r2, 2), "n": len(d),
            "from": d["m"].min(), "to": d["m"].max()}


def fit_power(load: pd.Series, temp: pd.Series, ref: dt.date, days: int = 730, min_days: int = 300,
              min_cdd_days: int = 40) -> dict | None:
    """GWh/d = a + bh·HDD + bc·CDD + dzień tygodnia (ostatnie `days` dni do `ref`)."""
    if load is None or temp is None or load.dropna().empty or temp.empty:
        return None
    y = load.dropna()
    y.index = pd.to_datetime(y.index)
    t = temp.dropna()
    t.index = pd.to_datetime(t.index)
    d = pd.concat([y.rename("y"), t.rename("t")], axis=1, join="inner")
    d = d[(d.index > pd.Timestamp(ref) - pd.Timedelta(days=days)) & (d.index <= pd.Timestamp(ref))]
    if len(d) < min_days:
        return None
    c = cdd(d["t"])
    use_c = int((c > 0).sum()) >= min_cdd_days   # chłodzenie tylko przy dość wielu ciepłych dniach (IE: 7 dni → szum)
    X = np.column_stack([np.ones(len(d)), hdd(d["t"])] + ([c] if use_c else [])
                        + [(d.index.dayofweek == k).astype(float) for k in range(1, 7)])
    coef, *_ = np.linalg.lstsq(X, d["y"].to_numpy(), rcond=None)
    pred = X @ coef
    ss = float(((d["y"] - d["y"].mean()) ** 2).sum())
    r2 = 1 - float(((d["y"] - pred) ** 2).sum()) / ss if ss > 0 else 0.0
    bh = max(float(coef[1]), 0.0)
    bc = max(float(coef[2]), 0.0) if use_c else 0.0
    if bh == 0 and bc == 0:
        return None
    wk = float(np.mean(np.concatenate([[0.0], coef[3 if use_c else 2:]])))   # średni efekt dnia tygodnia
    return {"a": round(float(coef[0]) + wk, 1), "bh": round(bh, 2), "bc": round(bc, 2), "r2": round(r2, 2),
            "n": len(d), "from": d.index.min().strftime("%Y-%m-%d"), "to": d.index.max().strftime("%Y-%m-%d")}
