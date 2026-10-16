"""Ceny day-ahead (Energy-Charts /price) i ceny ważone produkcją (capture price).

Licencje: część stref jest na CC BY 4.0 (dane SMARD / Bundesnetzagentur), pozostałe
Energy-Charts udostępnia wyłącznie do użytku prywatnego i wewnętrznego. Domyślnie
tracker pobiera tylko strefy CC BY (ustawienie "price_licenses" w settings.json).
"""
from __future__ import annotations

import csv
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import store
from .categories import DETAIL_KEYS, PRICE_COLS
from .energycharts import durations_h

CAPTURE = {"solar": ["solar"], "wind": ["wind_onshore", "wind_offshore"], "gen": None, "load": ["load"]}


def load_zones(licenses: list[str] | None = None) -> dict[str, list[dict]]:
    """{kod kraju: [{zone, license}]} - tylko strefy z dozwoloną licencją."""
    licenses = licenses or ["cc_by"]
    out: dict[str, list[dict]] = {}
    with open(store.ROOT / "config" / "price_zones.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["license"] in licenses:
                out.setdefault(row["country"], []).append({"zone": row["zone"], "license": row["license"]})
    return out


def all_zones() -> dict[str, list[dict]]:
    return load_zones(["cc_by", "private"])


def parse_price(payload: dict) -> pd.Series:
    """Cena w EUR/MWh indeksowana czasem UTC (początek okresu)."""
    if not payload:
        return pd.Series(dtype=float)
    if "unix_seconds" in payload:
        ts = payload.get("unix_seconds") or []
        vals = payload.get("price") or []
        if not ts or len(ts) != len(vals):
            return pd.Series(dtype=float)
        idx = pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True)
        s = pd.Series([np.nan if v is None else v for v in vals], index=idx, dtype=float)
    elif "data" in payload:
        recs = payload.get("data") or []
        if not recs:
            return pd.Series(dtype=float)
        idx = pd.to_datetime([r["timestamp"] for r in recs], utc=True)
        vals = []
        for r in recs:
            v = r.get("values")
            if isinstance(v, dict):
                v = next((x for x in v.values() if isinstance(x, (int, float))), None)
            elif isinstance(v, list):
                v = next((x for x in v if isinstance(x, (int, float))), None)
            vals.append(np.nan if v is None else v)
        s = pd.Series(vals, index=idx, dtype=float)
    else:
        return pd.Series(dtype=float)
    unit = str(payload.get("unit", "")).replace(" ", "").upper()
    if unit.startswith("EUR/KWH"):
        s = s * 1000
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _durations(index: pd.DatetimeIndex) -> np.ndarray:
    """Czas trwania punktów w sekundach (lokalnie, odporne na mieszane rozdzielczości)."""
    return durations_h(index) * 3600.0


def combine_zones(series: list[pd.Series]) -> pd.Series:
    """Średnia prosta stref (dla krajów z kilkoma strefami cenowymi)."""
    series = [s for s in series if not s.empty]
    if not series:
        return pd.Series(dtype=float)
    if len(series) == 1:
        return series[0]
    df = pd.concat(series, axis=1).sort_index().ffill(limit=4)
    return df.mean(axis=1, skipna=True)


def align_to(price: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Średnia cena w okresie każdego punktu produkcji [t, t + czas trwania).

    Cena dotyczy okresu od swojego znacznika czasu do następnego. Obie serie są
    rozkładane na siatkę 15-minutową, więc działa to przy każdym połączeniu
    rozdzielczości (1 h / 15 min, także zmiennym w obrębie zakresu)."""
    if price.empty or len(index) == 0:
        return pd.Series(np.nan, index=index)
    q = pd.Timedelta("15min")
    start = min(price.index[0], index[0]).floor("15min")
    end = max(price.index[-1], index[-1]).ceil("15min") + pd.Timedelta(hours=2)
    grid = pd.date_range(start, end, freq="15min")
    # cena na siatce: każdy punkt ceny obowiązuje przez swój czas trwania
    pdur = np.maximum(1, np.round(durations_h(price.index) * 4)).astype(int)
    vals = np.full(len(grid), np.nan)
    pos = ((price.index - start) // q).to_numpy().astype(int)
    pv = price.to_numpy()
    for k in range(int(pdur.max())):
        m = pdur > k
        idx = pos[m] + k
        ok = idx < len(grid)
        vals[idx[ok]] = pv[m][ok]
    have = ~np.isnan(vals)
    cs = np.concatenate([[0.0], np.cumsum(np.where(have, vals, 0.0))])
    cn = np.concatenate([[0], np.cumsum(have)])
    i0 = ((index - start) // q).to_numpy().astype(int)
    n = np.maximum(1, np.round(durations_h(index) * 4)).astype(int)
    i1 = np.minimum(i0 + n, len(grid))
    cnt = cn[i1] - cn[i0]
    avg = np.where(cnt > 0, (cs[i1] - cs[i0]) / np.maximum(cnt, 1), np.nan)
    return pd.Series(avg, index=index)


def daily_prices(price: pd.Series, mw: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Dzienne wskaźniki cenowe kraju i sumy do cen ważonych produkcją."""
    if price.empty:
        return pd.DataFrame(columns=PRICE_COLS)
    local_day = pd.Index(price.index.tz_convert(ZoneInfo(tz)).date, name="date")
    dur = _durations(price.index) / 3600.0
    have = price.notna().to_numpy()
    v = price.to_numpy()
    g = pd.DataFrame({"pw": np.where(have, v * dur, 0.0), "h": np.where(have, dur, 0.0),
                      "neg": np.where(have & (v < 0), dur, 0.0), "p": v}, index=local_day)
    agg = g.groupby(level=0).agg(pw=("pw", "sum"), h=("h", "sum"), neg=("neg", "sum"),
                                 mn=("p", "min"), mx=("p", "max"))
    out = pd.DataFrame(index=agg.index)
    out["price_avg"] = (agg["pw"] / agg["h"]).where(agg["h"] >= 18)
    out["price_min"] = agg["mn"].where(agg["h"] >= 18)
    out["price_max"] = agg["mx"].where(agg["h"] >= 18)
    out["price_neg_h"] = agg["neg"].where(agg["h"] >= 18)

    if mw is not None and not mw.empty:
        p = align_to(price, mw.index).to_numpy()
        mdur = _durations(mw.index) / 3600.0
        mday = pd.Index(mw.index.tz_convert(ZoneInfo(tz)).date, name="date")
        gen_cols = [c for c in mw.columns if c in DETAIL_KEYS]
        for key, cols in CAPTURE.items():
            cols = gen_cols if cols is None else [c for c in cols if c in mw.columns]
            if not cols:
                continue
            q = mw[cols].sum(axis=1, min_count=1).to_numpy()
            ok = ~np.isnan(q) & ~np.isnan(p)
            e = np.where(ok, q * mdur, 0.0)                     # MWh
            pw = pd.Series(np.where(ok, e * p, 0.0) / 1000.0, index=mday).groupby(level=0).sum()  # tys. EUR
            pq = pd.Series(e, index=mday).groupby(level=0).sum()
            out[f"pw_{key}"] = pw.reindex(out.index)
            out[f"pq_{key}"] = pq.reindex(out.index)
    out.index = pd.to_datetime(out.index)
    out.index.name = "date"
    for c in PRICE_COLS:
        if c not in out:
            out[c] = np.nan
    return out[PRICE_COLS]


def zone_daily(price: pd.Series, tz: str) -> pd.DataFrame:
    d = daily_prices(price, None, tz)
    return d[["price_avg", "price_min", "price_max", "price_neg_h"]]
