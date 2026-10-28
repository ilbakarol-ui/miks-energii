"""Wspólny magazyn danych pogodowych dla wszystkich dashboardów (Miks energii, Gaz UE, LNG).

Pobierane tutaj, w GitHub Actions, bo zadania w chmurze Claude mają ograniczony dostęp do sieci
(serwer dawnych prognoz Open-Meteo jest tam zablokowany) i wspólny, szybko wyczerpywany dzienny limit Open-Meteo.

Punkty: config/shared_weather_points.csv (pt, group, cc, lat, lon, tz). Pliki w data/shared_weather/:
- points.csv      – kopia listy punktów (dla odbiorców),
- temp_hist.csv   – pt,date,temp: średnia dobowa temperatura z archiwum ERA5 (od 2019 r., dociągane przyrostowo),
- temp_recent.csv – pt,date,temp,fc: ostatnie 14 dni i 16 dni prognozy (fc=1) z bieżącego przebiegu modeli,
- temp_prev.csv   – pt,date,l0..l7: średnia dobowa temperatura z prognoz wydanych 0..7 dni wcześniej
                    (Open-Meteo previous-runs; l1 = prognoza na jutro wydana dzień wcześniej itd.),
- meta.json       – czas aktualizacji, data prognozy, zakres danych, zużycie limitu, błędy (kroki temperatury),
- ensemble_power.csv.gz – code,date,member,sr,t,ws: ensemble ECMWF IFS (51 wariantów) – wiatr na 100 m, promieniowanie,
                    temperatura, średnio po punktach kraju (miasta + rejony farm OZE); 5 dni wstecz i 12 dni prognozy,
- ensemble_hist.csv – code,date,ws,sr,t: minione dni z ensemble (średnia wariantów), 400 dni – do kalibracji pogoda → produkcja,
- seasonal.csv.gz – pt,member,date,temp,fetched: prognoza sezonowa ECMWF SEAS5 (51 wariantów, 183 dni), odświeżana co 7 dni,
- meta_extra.json – to samo co meta.json dla ensemble i SEAS5 (osobne zadanie w przebiegu, osobny limit Open-Meteo),
- meta_evening.json – lekki przebieg wieczorny (zadanie "evening", ok. 18 UTC): tylko świeża prognoza temperatury
                    (temp_recent.csv) i ensemble do energetyki (ensemble_power.csv.gz); forecast_issued, ensemble_issued
                    i statusy kroków. ERA5, dawne prognozy i SEAS5 tylko w nocy. Czyta go raport Gaz UE (WEATHER EVENING).

Kolumna uses w pliku punktów: temp (temperatura: historia, prognoza, dawne prognozy), ens (ensemble do energetyki),
seas (prognoza sezonowa).

Doba liczona w strefie czasowej punktu (tz). Dane: Open-Meteo (CC BY 4.0), ERA5 (Copernicus), modele numeryczne.
Każdy krok jest niezależny: błąd jednego nie zatrzymuje pozostałych, a poprzednie pliki zostają.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from . import store

OUT = store.DATA / "shared_weather"
POINTS = store.ROOT / "config" / "shared_weather_points.csv"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREV_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
SEASONAL_URL = "https://seasonal-api.open-meteo.com/v1/seasonal"
ENS_VARS = {"ws": "wind_speed_100m_mean", "sr": "shortwave_radiation_sum", "t": "temperature_2m_mean"}
ENS_PAST, ENS_DAYS, ENS_HIST_DAYS = 5, 12, 400
SEAS_DAYS, SEAS_MAX_AGE = 183, 7
HIST_START = "2019-01-01"
PREV_START = "2024-01-01"
LEADS = 7
ERA5_LAG = 6            # archiwum ERA5 ma ok. 5 dni opóźnienia
PREV_CHUNK_DAYS = 60    # serwer dawnych prognoz odpowiada wolno; duże zapytania kończyły się po 90 s bez odpowiedzi
PREV_BATCH = 5
BATCH = 10              # punktów w jednym zapytaniu
PER_MINUTE = 300        # limit minutowy Open-Meteo to 600 zapytań ważonych (adresy GitHub dzielą limit z innymi)
DEADLINE = [float("inf")]   # po tym czasie (time.time()) nie zaczynamy nowych zapytań
STATS = {"requests": 0, "failed": 0, "http429": 0, "seconds": 0.0, "last_error": None}


class Deadline(RuntimeError):
    """Skończył się czas przebiegu – zebrane dane są zapisywane, reszta czeka do następnego dnia."""


class Quota:
    """Licznik zapytań ważonych Open-Meteo (lokalizacje × max(1, zmienne/10) × max(1, dni/14))."""

    def __init__(self, budget: float):
        self.budget, self.used, self.win = budget, 0.0, []

    @staticmethod
    def weight(n_loc: int, days: int, n_vars: int = 1) -> float:
        return n_loc * max(1.0, n_vars / 10) * max(1.0, days / 14)

    def can(self, w: float) -> bool:
        return self.used + w <= self.budget

    def take(self, w: float):
        now = time.time()
        if now > DEADLINE[0]:
            raise Deadline("limit czasu przebiegu")
        self.win = [(t, x) for t, x in self.win if now - t < 60]
        if self.win and sum(x for _, x in self.win) + w > PER_MINUTE:
            time.sleep(max(1.0, 61 - (now - self.win[0][0])))
            self.win = []
        self.win.append((time.time(), w))
        self.used += w


class DailyLimit(RuntimeError):
    pass


def _timeout(url: str, params: dict) -> float:
    """B-20261028-3: dawne prognozy w dużych paczkach (do 60 dni) potrzebują 150 s; dzienny przyrost (< 10 dni)
    serwer zwraca w kilka sekund – zawieszone zapytanie lepiej szybko ponowić niż czekać 150 s."""
    if "previous-runs" not in url:
        return 90
    try:
        days = (dt.date.fromisoformat(params["end_date"]) - dt.date.fromisoformat(params["start_date"])).days + 1
    except (KeyError, TypeError, ValueError):
        return 150
    return 40 if days < 10 else 150


def http_get(url: str, params: dict, tries: int = 3) -> list[dict]:
    last = None
    for k in range(tries):
        if time.time() > DEADLINE[0]:
            raise Deadline("limit czasu przebiegu")
        t0 = time.time()
        STATS["requests"] += 1
        try:
            r = requests.get(url, params=params, timeout=_timeout(url, params),
                             headers={"User-Agent": "energy-dashboards shared weather (GitHub Actions)"})
        except requests.RequestException as e:
            STATS["seconds"] += time.time() - t0
            last = f"{type(e).__name__}"
            STATS["failed"] += 1; STATS["last_error"] = f"{url.split('/')[2]}: {last}"
            time.sleep(10 * (k + 1))
            continue
        STATS["seconds"] += time.time() - t0
        if r.status_code != 200:
            STATS["failed"] += 1; STATS["last_error"] = f"{url.split('/')[2]}: HTTP {r.status_code} {r.text[:120]}"
        if r.status_code == 429 and "daily" in r.text.lower():
            raise DailyLimit(r.text[:120])
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            STATS["http429"] += r.status_code == 429
            time.sleep(61 if r.status_code == 429 else 15 * (k + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            js = r.json()
        except ValueError as e:          # ucięta/niepełna odpowiedź (np. JSONDecodeError) - ponów jak błąd sieci
            last = f"{type(e).__name__}"
            STATS["failed"] += 1; STATS["last_error"] = f"{url.split('/')[2]}: {last} ({len(r.content)} B)"
            time.sleep(10 * (k + 1))
            continue
        return js if isinstance(js, list) else [js]
    raise RuntimeError(last or "błąd Open-Meteo")


def load_points(path: Path | None = None) -> pd.DataFrame:
    p = pd.read_csv(path or POINTS, dtype={"pt": str, "group": str, "cc": str, "tz": str})
    if "uses" not in p:
        p["uses"] = "temp"
    p["uses"] = p["uses"].fillna("temp")
    return p


def _sel(pts: pd.DataFrame, use: str) -> pd.DataFrame:
    return pts[pts["uses"].str.split(";").apply(lambda u: use in u)].reset_index(drop=True)


def _batches(pts: pd.DataFrame):
    """Paczki punktów o tej samej strefie czasowej."""
    for tz, g in pts.groupby("tz", sort=False):
        for i in range(0, len(g), BATCH):
            yield tz, g.iloc[i:i + BATCH]


def _coords(g: pd.DataFrame) -> dict:
    return {"latitude": ",".join(f"{x:.2f}" for x in g.lat), "longitude": ",".join(f"{x:.2f}" for x in g.lon)}


def _read(name: str, cols: list[str]) -> pd.DataFrame:
    p = OUT / name
    if p.exists():
        return pd.read_csv(p, dtype={"pt": str, "date": str})
    return pd.DataFrame(columns=cols)


def _write(df: pd.DataFrame, name: str, keys=("pt", "date")):
    OUT.mkdir(parents=True, exist_ok=True)
    df = df.drop_duplicates(list(keys), keep="last").sort_values(list(keys))
    df.to_csv(OUT / name, index=False, float_format="%.2f")


def daily_from_hourly(times: list[str], cols: dict[str, list]) -> pd.DataFrame:
    """Średnie dobowe z godzinowych wartości (czas lokalny już w odpowiedzi); doba wymaga >= 20 godzin."""
    h = pd.DataFrame(cols, dtype=float)
    h["date"] = [t[:10] for t in times]
    cnt = h.groupby("date").count()
    m = h.groupby("date").mean()
    return m.where(cnt >= 20)


# ---------- kroki ----------

def step_recent(pts, q: Quota, get=http_get, today: dt.date | None = None) -> pd.DataFrame:
    today = today or dt.date.today()
    rows = []
    for tz, g in _batches(pts):
        w = q.weight(len(g), 30)
        q.take(w)
        for pt, js in zip(g.pt, get(FORECAST_URL, {**_coords(g), "daily": "temperature_2m_mean", "timezone": tz,
                                                    "past_days": 14, "forecast_days": 16})):
            d = js.get("daily") or {}
            for t, v in zip(d.get("time", []), d.get("temperature_2m_mean", [])):
                if v is not None:
                    rows.append((pt, t, v, int(t >= today.isoformat())))
    df = pd.DataFrame(rows, columns=["pt", "date", "temp", "fc"])
    if df.empty:
        raise RuntimeError("pusta prognoza")
    _write(df, "temp_recent.csv")
    return df


def step_hist(pts, q: Quota, get=http_get, today: dt.date | None = None) -> int:
    """Archiwum ERA5: dla każdego punktu od ostatniej zapisanej daty (albo od HIST_START) do dziś-6.
    Punkty z tą samą datą początkową pobierane razem; gdy budżet się kończy, reszta czeka do następnego przebiegu."""
    today = today or dt.date.today()
    end = today - dt.timedelta(days=ERA5_LAG)
    old = _read("temp_hist.csv", ["pt", "date", "temp"])
    last = old.groupby("pt").date.max().to_dict() if len(old) else {}
    jobs = pts.copy()
    jobs["start"] = [(dt.date.fromisoformat(last[p]) + dt.timedelta(days=1)) if p in last else dt.date.fromisoformat(HIST_START)
                     for p in jobs.pt]
    jobs = jobs[jobs.start <= end]
    new, done = [], 0
    try:
        for (start, tz), g in jobs.groupby(["start", "tz"], sort=True):
            for i in range(0, len(g), BATCH):
                gg = g.iloc[i:i + BATCH]
                w = q.weight(len(gg), (end - start).days + 1)
                if not q.can(w):
                    continue
                q.take(w)
                for pt, js in zip(gg.pt, get(ARCHIVE_URL, {**_coords(gg), "daily": "temperature_2m_mean", "timezone": tz,
                                                            "start_date": start.isoformat(), "end_date": end.isoformat()})):
                    d = js.get("daily") or {}
                    part = pd.DataFrame({"pt": pt, "date": d.get("time", []), "temp": d.get("temperature_2m_mean", [])}).dropna()
                    new.append(part)
                    done += 1
    finally:   # to, co zdążyło przyjść, zapisujemy także przy błędzie albo końcu czasu
        if new:
            _write(pd.concat([old] + new, ignore_index=True), "temp_hist.csv")
    return len(jobs) - done


def step_prev(pts, q: Quota, get=http_get, today: dt.date | None = None) -> int:
    """Dawne prognozy (previous runs): l0..l7 dla dni zakończonych (do wczoraj). Przyrostowo od ostatniej daty − 3 dni;
    zaległa historia od PREV_START w kawałkach po PREV_CHUNK_DAYS dni."""
    today = today or dt.date.today()
    end = today - dt.timedelta(days=1)
    old = _read("temp_prev.csv", ["pt", "date"] + [f"l{k}" for k in range(LEADS + 1)])
    last = old.groupby("pt").date.max().to_dict() if len(old) else {}
    hourly = ["temperature_2m"] + [f"temperature_2m_previous_day{k}" for k in range(1, LEADS + 1)]
    jobs = pts.copy()
    jobs["start"] = [max(dt.date.fromisoformat(PREV_START), dt.date.fromisoformat(last[p]) - dt.timedelta(days=3))
                     if p in last else dt.date.fromisoformat(PREV_START) for p in jobs.pt]
    new = []
    try:
        _prev_loop(jobs, q, get, end, hourly, new)
    finally:
        if new:
            _write(pd.concat([old] + new, ignore_index=True), "temp_prev.csv")
    return _PREV_LEFT[0]


_PREV_LEFT = [0]
_PREV_FAILED: list[str] = []


def _prev_loop(jobs, q, get, end, hourly, new):
    left = 0
    _PREV_LEFT[0] = len(jobs)
    # najpierw przyrost (punkty z aktualną historią – wczorajsze l0..l7 dla raportów), potem zaległa historia
    for (start, tz), g in sorted(jobs.groupby(["start", "tz"]), key=lambda kv: kv[0][0], reverse=True):
        for i in range(0, len(g), PREV_BATCH):
            gg = g.iloc[i:i + PREV_BATCH]
            a = start
            while a <= end:
                b = min(end, a + dt.timedelta(days=PREV_CHUNK_DAYS - 1))
                w = q.weight(len(gg), (b - a).days + 1, len(hourly))
                if not q.can(w):
                    left += len(gg)
                    break
                q.take(w)
                try:
                    res = get(PREV_URL, {**_coords(gg), "hourly": ",".join(hourly), "timezone": tz,
                                         "start_date": a.isoformat(), "end_date": b.isoformat()})
                except (Deadline, DailyLimit):
                    raise
                except Exception as e:      # jedna paczka punktów nie przerywa całego kroku
                    _PREV_FAILED.append(f"{','.join(gg.pt)} {a}..{b}: {type(e).__name__}")
                    left += len(gg)
                    break
                for pt, js in zip(gg.pt, res):
                    h = js.get("hourly") or {}
                    if not h.get("time"):
                        continue
                    d = daily_from_hourly(h["time"], {f"l{k}": h.get(v, [None] * len(h["time"])) for k, v in enumerate(hourly)})
                    d = d.dropna(how="all").reset_index()
                    d.insert(0, "pt", pt)
                    new.append(d)
                a = b + dt.timedelta(days=1)
    _PREV_LEFT[0] = left


def _members(daily: dict, key: str):
    """(numer wariantu, wartości) dla zmiennej z odpowiedzi ensemble/sezonowej (kontrolny = 0, *_memberNN = NN)."""
    for col in daily:
        if col == key:
            yield 0, daily[col]
        elif col.startswith(key + "_member"):
            yield int(col.rsplit("member", 1)[1]), daily[col]


def step_ensemble(pts, q: Quota, get=http_get, today: dt.date | None = None) -> str:
    """Ensemble ECMWF IFS do prognozy energetyki: średnia po punktach kraju dla każdego z 51 wariantów.
    Gdy serwer ensemble zawiedzie – deterministyczna prognoza ECMWF (jeden wariant). Zwraca rodzaj prognozy."""
    today = today or dt.date.today()
    daily = ",".join(ENS_VARS.values())

    def fetch(url, extra):
        rows = []
        for tz, g in _batches(pts):
            for i in range(0, len(g), 8):
                gg = g.iloc[i:i + 8]
                q.take(q.weight(len(gg), ENS_PAST + ENS_DAYS, len(ENS_VARS)))
                for cc, js in zip(gg.cc, get(url, {**_coords(gg), "daily": daily, "past_days": ENS_PAST,
                                                   "forecast_days": ENS_DAYS, "timezone": tz, **extra})):
                    d = js.get("daily") or {}
                    for var, key in ENS_VARS.items():
                        for m, vals in _members(d, key):
                            rows += [(cc, t, m, var, float(v)) for t, v in zip(d.get("time", []), vals) if v is not None]
        return rows

    kind = "ensemble"
    try:
        rows = fetch(ENSEMBLE_URL, {"models": "ecmwf_ifs025"})
    except (Deadline, DailyLimit):
        raise
    except Exception as e:
        print(f"  ensemble niedostępny ({type(e).__name__}); prognoza deterministyczna ECMWF", file=sys.stderr)
        kind = "deterministic"
        rows = fetch(FORECAST_URL, {"models": "ecmwf_ifs025"})
    if not rows:
        raise RuntimeError("pusta odpowiedź ensemble")
    df = pd.DataFrame(rows, columns=["code", "date", "member", "var", "value"])
    df = df.groupby(["code", "date", "member", "var"], as_index=False).value.mean()
    w = df.pivot_table(index=["code", "date", "member"], columns="var", values="value").reset_index()
    OUT.mkdir(parents=True, exist_ok=True)
    w.to_csv(OUT / "ensemble_power.csv.gz", index=False, float_format="%.3f", compression="gzip")
    past = w[w.date < today.isoformat()].groupby(["code", "date"], as_index=False)[list(ENS_VARS)].mean().dropna(subset=["ws", "sr"])
    old = _read("ensemble_hist.csv", ["code", "date"] + list(ENS_VARS))
    h = pd.concat([old, past]).drop_duplicates(["code", "date"], keep="last")
    h = h[h.date >= (today - dt.timedelta(days=ENS_HIST_DAYS)).isoformat()].sort_values(["code", "date"])
    h.to_csv(OUT / "ensemble_hist.csv", index=False, float_format="%.3f")
    return kind


def step_seasonal(pts, q: Quota, get=http_get, today: dt.date | None = None) -> int:
    """SEAS5 (51 wariantów, 183 dni) dla punktów z uses=seas; punkt odświeżany, gdy jego kopia ma ≥ 7 dni.
    Zwraca liczbę odświeżonych punktów."""
    today = today or dt.date.today()
    p = OUT / "seasonal.csv.gz"
    old = pd.read_csv(p, dtype={"pt": str, "date": str, "fetched": str}) if p.exists() else \
        pd.DataFrame(columns=["pt", "member", "date", "temp", "fetched"])
    age = old.groupby("pt").fetched.max().to_dict() if len(old) else {}
    due = pts[[(today - dt.date.fromisoformat(age[x])).days >= SEAS_MAX_AGE if x in age else True for x in pts.pt]]
    new = []
    try:
        for tz, g in _batches(due):
            for i in range(0, len(g), 4):
                gg = g.iloc[i:i + 4]
                q.take(q.weight(len(gg), SEAS_DAYS))
                for pt, js in zip(gg.pt, get(SEASONAL_URL, {**_coords(gg), "daily": "temperature_2m_mean",
                                                            "forecast_days": SEAS_DAYS, "timezone": tz})):
                    d = js.get("daily") or {}
                    rows = [(pt, m, t, float(v)) for m, vals in _members(d, "temperature_2m_mean")
                            for t, v in zip(d.get("time", []), vals) if v is not None]
                    if rows:
                        new.append(pd.DataFrame(rows, columns=["pt", "member", "date", "temp"]).assign(fetched=today.isoformat()))
    finally:
        if new:
            done = {x for n in new for x in n.pt.unique()}
            out = pd.concat([old[~old.pt.isin(done)]] + new, ignore_index=True).sort_values(["pt", "member", "date"])
            OUT.mkdir(parents=True, exist_ok=True)
            out.to_csv(p, index=False, float_format="%.2f", compression="gzip")
    return len({x for n in new for x in n.pt.unique()})


def _pending(name: str, pts: pd.DataFrame, today: dt.date) -> int:
    """Ile punktów nie ma jeszcze danych do oczekiwanej daty (po przerwanym kroku)."""
    files = {"hist": ("temp_hist.csv", ERA5_LAG), "prev": ("temp_prev.csv", 1)}
    if name not in files:
        return 0
    f, lag = files[name]
    old = _read(f, ["pt", "date"])
    last = old.groupby("pt").date.max().to_dict() if len(old) else {}
    want = (today - dt.timedelta(days=lag)).isoformat()
    return int(sum(last.get(p, "") < want for p in pts.pt))


def _save_meta(meta: dict, q: Quota, name: str = "meta.json"):
    cov = {}
    # zakres plików temperatury opisuje tylko meta.json (zadanie "extra" ma w checkoucie starsze kopie tych plików)
    files = (("temp_hist.csv", "hist"), ("temp_prev.csv", "prev"), ("temp_recent.csv", "recent")) if name == "meta.json" else ()
    for f, key in files:
        df = _read(f, ["pt", "date"])
        if len(df):
            last = df.groupby("pt").date.max()
            cov[key] = {"points": int(df.pt.nunique()), "points_to_end": int((last == last.max()).sum()),
                        "from": str(df.date.min()), "to": str(df.date.max()), "rows": int(len(df))}
    meta.update(updated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), weighted_calls=round(q.used, 1))
    if cov:
        meta["coverage"] = cov
    else:
        meta.pop("coverage", None)
    (OUT / name).write_text(json.dumps(meta, ensure_ascii=False, indent=1))


JOBS = {
    # zadanie -> (plik meta, kroki: nazwa, funkcja, flaga punktów)
    "temp": ("meta.json", [("recent", step_recent, "temp"), ("hist", step_hist, "temp"), ("prev", step_prev, "temp")]),
    "extra": ("meta_extra.json", [("ensemble", step_ensemble, "ens"), ("seasonal", step_seasonal, "seas")]),
    # lekki przebieg wieczorny: nowsze przebiegi modeli przed wieczornym raportem gazowym (20:41); bez historii i SEAS5
    "evening": ("meta_evening.json", [("recent", step_recent, "temp"), ("ensemble", step_ensemble, "ens")]),
}


def run(budget: float = 8000, get=http_get, today: dt.date | None = None, minutes: float = 35, job: str = "temp") -> dict:
    """Zadanie "temp": prognoza (najważniejsza), historia ERA5, dawne prognozy. Zadanie "extra": ensemble do energetyki
    i SEAS5. meta zapisywane po każdym kroku, dane – także przy błędzie albo po upływie `minutes` minut."""
    today = today or dt.date.today()
    DEADLINE[0] = time.time() + minutes * 60
    STATS.update(requests=0, failed=0, http429=0, seconds=0.0, last_error=None)
    pts = load_points()
    OUT.mkdir(parents=True, exist_ok=True)
    pts.to_csv(OUT / "points.csv", index=False)
    q = Quota(budget)
    meta_name, steps = JOBS[job]
    meta_p = OUT / meta_name
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    meta.update(source="Open-Meteo (CC BY 4.0): ERA5, ECMWF IFS (prognozy bieżące, dawne, ensemble), ECMWF SEAS5",
                errors=[], pending={}, steps={})
    for name, fn, use in steps:
        t0, r0, w0, h0 = time.time(), STATS["requests"], q.used, STATS["http429"]
        STATS["last_error"] = None       # błąd i liczniki 429 opisują tylko ten krok
        status = "OK"
        try:
            res = fn(_sel(pts, use), q, get=get, today=today)
            if name == "recent":
                meta["forecast_issued"] = today.isoformat()
            elif name == "ensemble":
                meta.update(ensemble_issued=today.isoformat(), ensemble_kind=res)
            elif name == "seasonal":
                meta["seasonal_refreshed_points"] = int(res)
            elif res:
                meta["pending"][name] = int(res)
            if name == "prev" and _PREV_FAILED:
                meta["errors"].append(f"prev: {len(_PREV_FAILED)} paczek punktów bez danych po 3 próbach, np. {_PREV_FAILED[0]}")
                _PREV_FAILED.clear()
        except Deadline:
            status = "limit czasu"
            meta["errors"].append(f"{name}: limit czasu przebiegu ({minutes:.0f} min) – zebrane dane zapisane, reszta jutro")
            meta["pending"][name] = _pending(name, _sel(pts, use), today)
        except DailyLimit as e:
            status = "dzienny limit"
            meta["errors"].append(f"{name}: dzienny limit Open-Meteo ({e})")
        except Exception as e:
            status = f"błąd {type(e).__name__}"
            meta["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
            meta["pending"][name] = _pending(name, _sel(pts, use), today)
        meta["steps"][name] = dict(status=status, seconds=round(time.time() - t0), requests=STATS["requests"] - r0,
                                   weighted=round(q.used - w0, 1), http429=STATS["http429"] - h0, last_error=STATS["last_error"])
        print(f"  {name}: {status} ({time.time() - t0:.0f} s, zapytań {STATS['requests'] - r0}, 429: {STATS['http429'] - h0})"
              + (f"; ostatni błąd: {STATS['last_error']}" if STATS["last_error"] else ""), file=sys.stderr if status != "OK" else sys.stdout)
        _save_meta(meta, q, meta_name)
        if status in ("limit czasu", "dzienny limit"):
            break
    _save_meta(meta, q, meta_name)
    print(f"Wspólna pogoda ({job}): zapytania ważone ≈ {q.used:.0f}; błędy: {len(meta['errors'])}")
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--job", choices=list(JOBS), default="temp", help="temp = temperatura; extra = ensemble i SEAS5; evening = wieczorem tylko prognoza temperatury i ensemble")
    ap.add_argument("--budget", type=float, default=8000, help="limit zapytań ważonych Open-Meteo w tym przebiegu")
    ap.add_argument("--minutes", type=float, default=35, help="po tylu minutach nie zaczyna nowych zapytań i zapisuje wynik")
    a = ap.parse_args()
    meta = run(a.budget, minutes=a.minutes, job=a.job)
    # błąd prognozy (temperatury albo ensemble) = błąd zadania; pozostałe kroki mogą poczekać
    sys.exit(1 if any(e.startswith(("recent", "ensemble")) for e in meta["errors"]) else 0)


if __name__ == "__main__":
    main()
