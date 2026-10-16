"""Prognoza na 7 dni: wiatr, słońce i zużycie z prognozy pogody (Open-Meteo).

Energy-Charts publikuje prognozy tylko na 1-2 dni, więc tracker liczy własną prognozę:
1. Pogoda dla kilku punktów w każdym kraju (config/weather_points.csv): prędkość wiatru
   na 100 m, promieniowanie słoneczne, temperatura - historia (ERA5) i prognoza na 8 dni.
2. Dla każdego kraju prosty model dopasowany do jego własnej historii produkcji:
   - wiatr: produkcja ~ wskaźnik mocy turbiny z prędkości wiatru (ostatnie 120 dni),
   - słońce: produkcja ~ promieniowanie (ostatnie 60 dni),
   - zużycie: ~ dni grzewcze i chłodnicze + dzień tygodnia (ostatnie 365 dni).
3. Błąd modelu = błąd na ostatnich 28 dniach przy znanej pogodzie (model dopasowany
   bez tych dni). Nie obejmuje błędu samej prognozy pogody.

Dane pogodowe: Open-Meteo (CC BY 4.0), ERA5 (Copernicus) i modele numeryczne.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import store

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "wind_speed_100m,shortwave_radiation,temperature_2m"
WEATHER = store.DATA / "weather"
FEATURES = ["cf", "ws", "ghi", "temp", "hdd", "cdd"]
METHOD = "model pogodowy (Open-Meteo)"


def load_points() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with open(store.ROOT / "config" / "weather_points.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["code"], []).append({"lat": float(r["lat"]), "lon": float(r["lon"]), "kind": r["kind"]})
    return out


def turbine_cf(ws: np.ndarray) -> np.ndarray:
    """Uproszczona krzywa mocy turbiny (100 m): start 3 m/s, moc znamionowa od 12 m/s, wyłączenie > 25 m/s."""
    ws = np.asarray(ws, dtype=float)
    cf = np.clip((ws - 3.0) / 9.0, 0, 1) ** 3
    cf = np.where(ws >= 12.0, 1.0, cf)
    cf = np.where(ws > 25.0, 0.0, cf)
    return np.where(np.isnan(ws), np.nan, cf)


def _get(url: str, params: dict, retries: int = 4) -> list[dict]:
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=120,
                             headers={"User-Agent": "energy-mix-tracker (GitHub Actions)"})
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        js = r.json()
        return js if isinstance(js, list) else [js]
    raise RuntimeError(last or "błąd Open-Meteo")


def fetch_points(points: list[dict], url: str, extra: dict) -> list[pd.DataFrame]:
    """Godzinowa pogoda (UTC) dla listy punktów; zapytania po 40 punktów."""
    out = []
    for i in range(0, len(points), 40):
        chunk = points[i:i + 40]
        params = {"latitude": ",".join(f"{p['lat']:.2f}" for p in chunk),
                  "longitude": ",".join(f"{p['lon']:.2f}" for p in chunk),
                  "hourly": HOURLY, "wind_speed_unit": "ms", "timezone": "GMT", **extra}
        for js in _get(url, params):
            h = js.get("hourly") or {}
            if not h.get("time"):
                out.append(pd.DataFrame())
                continue
            df = pd.DataFrame({k: h.get(k) for k in ["wind_speed_100m", "shortwave_radiation", "temperature_2m"]},
                              index=pd.to_datetime(h["time"], utc=True))
            out.append(df.astype(float))
        time.sleep(1)
    return out


def daily_features(frames: list[pd.DataFrame], points: list[dict], tz: str) -> pd.DataFrame:
    """Cechy dzienne kraju w jego czasie lokalnym."""
    wind, solar, temp = [], [], []
    for p, df in zip(points, frames):
        if df.empty:
            continue
        if p["kind"] in ("all", "wind"):
            wind.append(df["wind_speed_100m"])
        if p["kind"] in ("all", "solar"):
            solar.append(df["shortwave_radiation"])
        if p["kind"] == "all":
            temp.append(df["temperature_2m"])
    if not wind or not temp:
        return pd.DataFrame(columns=FEATURES)
    ws = pd.concat(wind, axis=1)
    h = pd.DataFrame({
        "cf": pd.DataFrame(turbine_cf(ws.to_numpy()), index=ws.index).mean(axis=1),
        "ws": ws.mean(axis=1),
        "ghi": pd.concat(solar, axis=1).mean(axis=1) / 1000.0 if solar else np.nan,   # kWh/m2 na godzinę
        "temp": pd.concat(temp, axis=1).mean(axis=1),
    })
    day = pd.Index(h.index.tz_convert(ZoneInfo(tz)).date, name="date")
    cnt = h["temp"].notna().groupby(day).sum()
    d = pd.DataFrame({"cf": h["cf"].groupby(day).mean(), "ws": h["ws"].groupby(day).mean(),
                      "ghi": h["ghi"].groupby(day).sum(min_count=1), "temp": h["temp"].groupby(day).mean()})
    d = d[cnt >= 20]
    d["hdd"] = (15.5 - d["temp"]).clip(lower=0)
    d["cdd"] = (d["temp"] - 22.0).clip(lower=0)
    d.index = pd.to_datetime(d.index)
    return d[FEATURES]


def read_cache(code: str) -> pd.DataFrame:
    p = WEATHER / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame(columns=FEATURES)
    return pd.read_csv(p, parse_dates=["date"], index_col="date").astype(float).sort_index()


def write_cache(code: str, df: pd.DataFrame):
    WEATHER.mkdir(parents=True, exist_ok=True)
    out = df[FEATURES].sort_index().copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    out.round(4).to_csv(WEATHER / f"{code}.csv")


# ---------- modele ----------

def _design(f: pd.DataFrame, kind: str) -> np.ndarray:
    if kind == "wind":
        return np.column_stack([np.ones(len(f)), f["cf"], f["cf"] ** 2])
    if kind == "solar":
        return np.column_stack([np.ones(len(f)), f["ghi"]])
    dow = f.index.dayofweek
    return np.column_stack([np.ones(len(f)), f["hdd"], f["cdd"], (dow == 5).astype(float), (dow == 6).astype(float)])


WINDOWS = {"wind": 120, "solar": 60, "load": 365}


def fit(feat: pd.DataFrame, y: pd.Series, kind: str, end: pd.Timestamp) -> np.ndarray | None:
    """Regresja liniowa na oknie kończącym się przed `end`."""
    start = end - pd.Timedelta(days=WINDOWS[kind])
    df = feat.join(y.rename("y"), how="inner")
    df = df[(df.index >= start) & (df.index < end)].dropna()
    if len(df) < max(20, WINDOWS[kind] // 4) or df["y"].abs().sum() == 0:
        return None
    X = _design(df, kind)
    coef, *_ = np.linalg.lstsq(X, df["y"].to_numpy(), rcond=None)
    return coef


def predict(coef: np.ndarray, feat: pd.DataFrame, kind: str) -> pd.Series:
    return pd.Series(np.clip(_design(feat, kind) @ coef, 0, None), index=feat.index)


def model_error(feat: pd.DataFrame, y: pd.Series, kind: str, last: pd.Timestamp, days: int = 28) -> float | None:
    """Błąd (suma |błędów| / suma wartości, %) na ostatnich `days` dniach, model bez tych dni."""
    cut = last - pd.Timedelta(days=days - 1)
    coef = fit(feat, y, kind, cut)
    if coef is None:
        return None
    test = feat.join(y.rename("y"), how="inner")
    test = test[(test.index >= cut) & (test.index <= last)].dropna()
    if len(test) < days // 2 or test["y"].sum() <= 0:
        return None
    p = predict(coef, test[FEATURES], kind)
    return round(float(100 * (p - test["y"]).abs().sum() / test["y"].sum()), 1)


TARGETS = {"wind": "wind", "solar": "solar", "load": "load"}


def forecast_country(code: str, hist: pd.DataFrame, fut: pd.DataFrame, e: pd.DataFrame,
                     today: dt.date, days: int = 7) -> dict | None:
    """Prognoza dzienna (GWh) na `days` dni od dziś z modeli dopasowanych do historii kraju."""
    if fut.empty or e.empty:
        return None
    last = e["gen_total"].dropna().index.max()
    if pd.isna(last):
        return None
    sel = [pd.Timestamp(today) + pd.Timedelta(days=i) for i in range(days)]
    fut = fut.reindex(sel)
    out = {"issued": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "days": [d.strftime("%Y-%m-%d") for d in sel], "methods": {}, "errors": {}}
    for kind, col in TARGETS.items():
        y = e[col].dropna() if col in e else pd.Series(dtype=float)
        if y.empty or (y.tail(60).abs().sum() == 0 and kind != "load"):
            if kind in ("wind", "solar") and not y.empty:
                out[kind] = [0.0] * days
                out["methods"][kind] = METHOD
            continue
        coef = fit(hist, y, kind, last + pd.Timedelta(days=1))
        if coef is None or fut[FEATURES].isna().all().all():
            continue
        p = predict(coef, fut[FEATURES].ffill(), kind)
        out[kind] = [None if pd.isna(v) else round(float(v), 2) for v in p.tolist()]
        out["methods"][kind] = METHOD
        out["errors"][kind] = model_error(hist, y, kind, last)
    if not any(k in out for k in TARGETS):
        return None
    return out


# ---------- przebieg ----------

def update(settings: dict, frames: dict[str, pd.DataFrame], derive, countries: list[dict], log: list) -> dict:
    """Aktualizuje cache pogody, liczy prognozy i zapisuje data/forecast/<KOD>.json."""
    from .forecast import _archive   # archiwum prognoz (ocena trafności)

    pts = load_points()
    codes = [c for c in countries if c["code"] in pts and c["code"] in frames]
    flat, owner = [], []
    for c in codes:
        for p in pts[c["code"]]:
            flat.append(p)
            owner.append(c["code"])
    if not flat:
        return {}
    # prognoza + ostatnie dni analizy (uzupełnia cache)
    try:
        fr = fetch_points(flat, FORECAST_URL, {"forecast_days": 8, "past_days": 10})
    except Exception as e:
        log.append(f"Open-Meteo: prognoza niedostępna ({e})")
        return {}
    by_code: dict[str, list[pd.DataFrame]] = {}
    for code, df in zip(owner, fr):
        by_code.setdefault(code, []).append(df)

    hist_days = settings.get("weather_history_days", 420)
    results = {}
    min_cov = settings["min_day_coverage"]
    for c in codes:
        code, tz = c["code"], c["tz"]
        today = dt.datetime.now(ZoneInfo(tz)).date()
        recent = daily_features(by_code.get(code, []), pts[code], tz)
        cache = read_cache(code)
        need_from = pd.Timestamp(today - dt.timedelta(days=hist_days))
        if cache.empty or cache.index.min() > need_from + pd.Timedelta(days=7):
            try:
                arch = fetch_points(pts[code], ARCHIVE_URL, {
                    "start_date": (today - dt.timedelta(days=hist_days)).isoformat(),
                    "end_date": (today - dt.timedelta(days=6)).isoformat()})
                hist_new = daily_features(arch, pts[code], tz)
                cache = pd.concat([hist_new, cache[~cache.index.isin(hist_new.index)]]).sort_index()
            except Exception as e:
                log.append(f"{code}: historia pogody niedostępna ({e})")
        past = recent[recent.index < pd.Timestamp(today)]
        cache = pd.concat([cache[~cache.index.isin(past.index)], past]).sort_index()
        cache = cache[cache.index >= need_from - pd.Timedelta(days=30)]
        write_cache(code, cache)
        fut = recent[recent.index >= pd.Timestamp(today)]
        e, _ = derive(frames[code], min_cov)
        try:
            fc = forecast_country(code, cache, fut, e, today)
        except Exception as ex:
            log.append(f"{code}: błąd modelu prognozy ({type(ex).__name__}: {ex})")
            fc = None
        if fc:
            store.FORECAST.mkdir(parents=True, exist_ok=True)
            (store.FORECAST / f"{code}.json").write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
            _archive(code, fc)
            results[code] = fc
    # UE-27: suma prognoz krajów członkowskich (gdy są wszystkie)
    members = [c["code"] for c in countries if c.get("group") == "UE" and store.has_daily(c)]
    if members and all(m in results for m in members):
        days = results[members[0]]["days"]
        eu = {"issued": results[members[0]]["issued"], "days": days, "methods": {}, "errors": {}}
        for k in TARGETS:
            if all(results[m].get(k) for m in members):
                eu[k] = [round(sum(results[m][k][i] or 0 for m in members), 2) for i in range(len(days))]
                eu["methods"][k] = METHOD + ", suma krajów"
        (store.FORECAST / "EU.json").write_text(json.dumps(eu, ensure_ascii=False), encoding="utf-8")
        _archive("EU", eu)
        results["EU"] = eu
    return results
