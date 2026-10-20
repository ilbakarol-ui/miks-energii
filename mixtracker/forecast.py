"""Prognozy produkcji wiatru i słońca oraz zużycia (Energy-Charts /public_power_forecast).

Nie każdy kraj ma prognozy w źródle. Tracker sprawdza dostępność i zapamiętuje ją
w data/state.json; kraj bez prognoz jest sprawdzany ponownie co 7 dni. Dla każdego
typu najpierw próbuje prognozy tygodniowej (v2, week-ahead), potem bieżącej (v1).
Każda pobrana prognoza trafia do archiwum, żeby później można było ocenić jej trafność.
"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import store
from .energycharts import Client, FetchError, durations_h

TYPES = ["solar", "wind_onshore", "wind_offshore", "load"]
# (wersja API, typ prognozy) w kolejności prób
METHODS = {
    "solar": [("v2", "week-ahead"), ("v1", "current"), ("v1", "day-ahead")],
    "wind_onshore": [("v2", "week-ahead"), ("v1", "current"), ("v1", "day-ahead")],
    "wind_offshore": [("v2", "week-ahead"), ("v1", "current"), ("v1", "day-ahead")],
    "load": [("v2", "week-ahead"), ("v1", "day-ahead")],
}
RECHECK_DAYS = 7
ARCHIVE = store.FORECAST / "archive.csv"
ARCHIVE_PM = store.FORECAST / "archive_pm.csv"   # prognozy z przebiegu popołudniowego (świeższa pogoda)


def parse_forecast(payload: dict) -> pd.Series:
    """Moc prognozowana (MW) indeksowana czasem UTC."""
    if not payload:
        return pd.Series(dtype=float)
    if "unix_seconds" in payload:
        ts = payload.get("unix_seconds") or []
        vals = payload.get("forecast_values") or payload.get("values") or []
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
        if str(payload.get("unit", "")).strip().upper() == "GW":
            s = s * 1000
    else:
        return pd.Series(dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index().dropna()


def daily_energy(s: pd.Series, tz: str, min_cov: float = 0.95) -> pd.Series:
    """Energia dzienna (GWh) dla dni z pokryciem >= min_cov."""
    if s.empty:
        return pd.Series(dtype=float)
    dur = durations_h(s.index)
    day = pd.Index(s.index.tz_convert(ZoneInfo(tz)).date)
    e = pd.Series(s.to_numpy() * dur, index=day).groupby(level=0).sum() / 1000.0
    h = pd.Series(dur, index=day).groupby(level=0).sum()
    exp = pd.Series({d: ((pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(tz)
                         - pd.Timestamp(d).tz_localize(tz)).total_seconds() / 3600 for d in h.index})
    cov = h / exp
    e = e[cov >= min_cov]
    e.index = pd.to_datetime(e.index)
    return e


def _fetch_type(client: Client, ec: str, ptype: str, st: dict, today: dt.date) -> tuple[pd.Series, str | None]:
    methods = METHODS[ptype]
    known = st.get("methods", {}).get(ptype)
    if known:
        methods = [tuple(known)] + [m for m in methods if list(m) != list(known)]
    end = today + dt.timedelta(days=8)
    for ver, ftype in methods:
        try:
            payload = client.forecast(ec, ptype, ftype, today, end, v2=(ver == "v2"))
        except FetchError:
            continue
        s = parse_forecast(payload)
        if s.empty:
            continue
        s = s[s.index >= pd.Timestamp(today).tz_localize("UTC") - pd.Timedelta(hours=14)]
        if not s.empty and s.notna().any():
            st.setdefault("methods", {})[ptype] = [ver, ftype]
            return s, f"{ver}:{ftype}"
    return pd.Series(dtype=float), None


def update_country(client: Client, c: dict, state: dict, log: list, can_request) -> dict | None:
    """Pobiera prognozy dla kraju (jeśli dostępne) i zapisuje data/forecast/<KOD>.json."""
    code, tz = c["code"], c["tz"]
    st = state.setdefault("forecast", {}).setdefault(code, {})
    today = dt.datetime.now(ZoneInfo(tz)).date()
    if st.get("available") is False and st.get("checked"):
        if (today - dt.date.fromisoformat(st["checked"])).days < RECHECK_DAYS:
            return None
    result, methods = {}, {}
    for ptype in TYPES:
        if not can_request():
            log.append(f"{code}: prognozy pominięte (limit czasu)")
            return None
        if ptype != "solar" and not result and st.get("available") is not True:
            break   # brak prognozy słońca przy pierwszym sprawdzeniu = kraj bez prognoz
        s, how = _fetch_type(client, c["ec"], ptype, st, today)
        if how:
            result[ptype] = daily_energy(s, tz)
            methods[ptype] = how
    st["checked"] = today.isoformat()
    st["available"] = bool(result)
    if not result:
        return None
    days = sorted({d for s in result.values() for d in s.index if d.date() >= today})[:7]
    out = {"issued": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "days": [d.strftime("%Y-%m-%d") for d in days], "methods": methods}
    for ptype, s in result.items():
        out[ptype] = [None if d not in s.index else round(float(s[d]), 2) for d in days]
    if "wind_onshore" in out or "wind_offshore" in out:
        on = out.get("wind_onshore") or [None] * len(days)
        off = out.get("wind_offshore") or [0.0] * len(days)
        out["wind"] = [None if a is None else round(a + (b or 0), 2) for a, b in zip(on, off)]
    store.FORECAST.mkdir(parents=True, exist_ok=True)
    (store.FORECAST / f"{code}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    _archive(code, out)
    return out


def _archive(code: str, fc: dict, path=None):
    """Zapis prognozy w archiwum w chwili wydania. Rano: archive.csv (prognoza podstawowa,
    weryfikowana); po południu: archive_pm.csv - poranna prognoza nie jest nadpisywana."""
    path = path or ARCHIVE
    rows = []
    for key in ["solar", "wind", "load"]:
        for d, v in zip(fc["days"], fc.get(key) or []):
            if v is not None:
                rows.append({"issued": fc["issued"][:10], "code": code, "date": d, "type": key, "gwh": v})
    if not rows:
        return
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path, dtype={"issued": str, "date": str})
        old = old[~((old["issued"] == new["issued"].iloc[0]) & (old["code"] == code))]
        new = pd.concat([old, new])
    # archiwum z ostatnich 400 dni
    cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    new = new[new["issued"] >= cutoff]
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, index=False)


def read_latest(code: str, max_age_days: int = 2) -> dict | None:
    p = store.FORECAST / f"{code}.json"
    if not p.exists():
        return None
    fc = json.loads(p.read_text(encoding="utf-8"))
    issued = dt.date.fromisoformat(fc["issued"][:10])
    if (dt.date.today() - issued).days > max_age_days:
        return None
    return fc
