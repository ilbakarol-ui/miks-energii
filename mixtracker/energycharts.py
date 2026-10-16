"""Klient Energy-Charts API (Fraunhofer ISE) i agregacja mocy (MW) do energii dziennej (MWh).

Dane: publiczna produkcja netto energii elektrycznej wg typu, obciążenie (zużycie)
i saldo wymiany handlowej. Licencja CC BY 4.0, źródło: Energy-Charts.info.
Limit API: ok. 2 zapytania/min na endpoint - klient czeka między zapytaniami
i respektuje nagłówek Retry-After.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from .categories import AUX, DETAIL_KEYS, PRICE_COLS, classify, is_known


class FetchError(RuntimeError):
    pass


@dataclass
class Client:
    """Klient z osobnym odstępem czasowym dla każdego endpointu (limit API jest per endpoint),
    dzięki czemu zapytania o produkcję, ceny i prognozy mogą się przeplatać."""
    base_url: str = "https://api.energy-charts.info"
    interval: float = 31.0
    timeout: float = 180.0
    max_retries: int = 4
    session: requests.Session = field(default_factory=requests.Session)
    _last: dict = field(default_factory=dict)
    requests_made: int = 0
    by_endpoint: dict = field(default_factory=dict)

    def _wait(self, endpoint: str):
        last = self._last.get(endpoint)
        if last is not None:
            delta = time.monotonic() - last
            if delta < self.interval:
                time.sleep(self.interval - delta)

    def get(self, endpoint: str, params: dict) -> dict:
        """GET z ponawianiem. 404 -> {} (brak danych dla zakresu)."""
        last_err = None
        for attempt in range(self.max_retries):
            self._wait(endpoint)
            self._last[endpoint] = time.monotonic()
            self.requests_made += 1
            self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + 1
            try:
                r = self.session.get(f"{self.base_url}/{endpoint}", params=params,
                                     timeout=self.timeout,
                                     headers={"User-Agent": "energy-mix-tracker (GitHub Actions)"})
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 60))
                last_err = f"HTTP 429 (czekam {wait:.0f}s)"
                time.sleep(min(wait, 600) + 1)
                continue
            if r.status_code == 404:
                return {}
            if r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError as e:
                last_err = f"niepoprawny JSON: {e}"
                continue
        raise FetchError(last_err or "nieznany błąd")

    def public_power(self, country: str, start: dt.date, end: dt.date) -> dict:
        return self.get("public_power", {"country": country, "start": start.isoformat(), "end": end.isoformat()})

    def price(self, zone: str, start: dt.date, end: dt.date) -> dict:
        return self.get("price", {"bzn": zone, "start": start.isoformat(), "end": end.isoformat()})

    def forecast(self, country: str, production_type: str, forecast_type: str,
                 start: dt.date | None = None, end: dt.date | None = None, v2: bool = False) -> dict:
        params = {"country": country, "production_type": production_type, "forecast_type": forecast_type}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()
        return self.get("v2/public_power_forecast" if v2 else "public_power_forecast", params)


def parse_public_power(payload: dict) -> tuple[pd.DataFrame, list[str]]:
    """Zwraca (ramka MW indeksowana czasem UTC, kolumny = kategorie, lista nazw źródłowych).

    Obsługuje format v1 (unix_seconds + production_types) i v2 (series + data).
    Kilka nazw źródłowych mapowanych na tę samą kategorię jest sumowanych.
    """
    if not payload:
        return pd.DataFrame(), []
    names: list[str] = []
    cols: dict[str, np.ndarray] = {}
    if "unix_seconds" in payload:
        ts = payload.get("unix_seconds") or []
        if not ts:
            return pd.DataFrame(), []
        idx = pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True)
        for pt in payload.get("production_types") or []:
            name = pt.get("name", "")
            data = pt.get("data")
            if data is None:
                continue
            names.append(name)
            cat = classify(name)
            if cat is None:
                continue
            arr = np.array([np.nan if v is None else v for v in data], dtype=float)
            if len(arr) != len(idx):
                continue
            _add(cols, cat, arr)
    elif "data" in payload and "series" in payload:
        recs = payload.get("data") or []
        if not recs:
            return pd.DataFrame(), []
        idx = pd.to_datetime([r["timestamp"] for r in recs], utc=True)
        for i, s in enumerate(payload["series"]):
            name = s.get("name") or s.get("id", "")
            if s.get("unit") and s["unit"] not in ("MW", "GW"):
                continue
            names.append(name)
            cat = classify(name)
            if cat is None:
                continue
            key = s.get("id", i)
            vals = []
            for r in recs:
                v = r["values"]
                v = v.get(key) if isinstance(v, dict) else (v[i] if i < len(v) else None)
                vals.append(np.nan if v is None else v)
            arr = np.array(vals, dtype=float)
            if s.get("unit") == "GW":
                arr = arr * 1000
            _add(cols, cat, arr)
    else:
        return pd.DataFrame(), []
    df = pd.DataFrame(cols, index=idx).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df, names


def _add(cols, cat, arr):
    if cat == "storage_charge":
        arr = np.abs(arr)
    if cat in cols:
        a, b = cols[cat], arr
        both_nan = np.isnan(a) & np.isnan(b)
        s = np.nan_to_num(a) + np.nan_to_num(b)
        s[both_nan] = np.nan
        cols[cat] = s
    else:
        cols[cat] = arr


def to_daily(mw: pd.DataFrame, tz: str, min_cov: float = 0.8) -> pd.DataFrame:
    """Całkuje moc (MW) do energii (MWh) w dniach czasu lokalnego kraju.

    Kolumna 'cov' = udział godzin doby z danymi produkcji (0-1). Niewielkie luki
    w pojedynczej serii są uzupełniane proporcjonalnie (energia / pokrycie serii),
    jeśli seria ma >= min_cov danych w danej dobie; przy mniejszym pokryciu - suma
    tego, co jest.
    """
    if mw.empty:
        return pd.DataFrame()
    t = mw.index
    secs = t.as_unit("s").asi8
    step = np.diff(secs)
    med = float(np.median(step)) if len(step) else 3600.0
    dur = np.append(step, med).astype(float)
    dur = np.where((dur <= 0) | (dur > 3 * med), med, dur) / 3600.0  # godziny
    local = t.tz_convert(ZoneInfo(tz))
    day = pd.Index(local.date, name="date")

    # oczekiwana liczba godzin w dobie (23/24/25 przy zmianie czasu)
    days = pd.Index(sorted(set(day)))
    exp_h = {}
    for d in days:
        a = pd.Timestamp(d).tz_localize(tz)
        b = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(tz)
        exp_h[d] = (b - a).total_seconds() / 3600.0
    exp = pd.Series(exp_h)

    gen_cols = [c for c in mw.columns if c in DETAIL_KEYS]
    out = {}
    hours = pd.Series(dur, index=day)
    for c in mw.columns:
        v = mw[c].to_numpy()
        have = ~np.isnan(v)
        e = pd.Series(np.where(have, v, 0.0) * dur, index=day).groupby(level=0).sum()
        h = pd.Series(np.where(have, dur, 0.0), index=day).groupby(level=0).sum()
        cov = (h / exp.reindex(h.index)).clip(upper=1.0)
        scaled = np.where(cov >= min_cov, e / cov.replace(0, np.nan), e)
        res = pd.Series(scaled, index=e.index)
        res[h == 0] = np.nan
        out[c] = res
    daily = pd.DataFrame(out)
    if gen_cols:
        any_gen = mw[gen_cols].notna().any(axis=1).to_numpy()
        h = pd.Series(np.where(any_gen, dur, 0.0), index=day).groupby(level=0).sum()
        daily["cov"] = (h / exp.reindex(h.index)).clip(upper=1.0).round(3)
    else:
        daily["cov"] = 0.0
    # pierwsza i ostatnia doba mogą być ucięte zakresem zapytania - zostają z niskim cov
    daily.index = pd.to_datetime(daily.index)
    daily.index.name = "date"
    for c in DETAIL_KEYS + AUX + PRICE_COLS:
        if c not in daily:
            daily[c] = np.nan
    # brak serii w całej dobie => 0 dla produkcji (źródło nie istnieje w kraju)
    filled = daily[DETAIL_KEYS].fillna(0.0)
    filled.loc[daily["cov"] == 0, :] = np.nan
    daily[DETAIL_KEYS] = filled
    return daily[["cov"] + DETAIL_KEYS + AUX + PRICE_COLS]


def unknown_names(names: list[str]) -> list[str]:
    return sorted({n for n in names if classify(n) == "misc" and not is_known(n)})
