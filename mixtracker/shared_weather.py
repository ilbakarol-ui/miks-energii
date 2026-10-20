"""Wspólny magazyn danych pogodowych dla wszystkich dashboardów (Miks energii, Gaz UE, LNG).

Pobierane tutaj, w GitHub Actions, bo zadania w chmurze Claude mają ograniczony dostęp do sieci
(serwer dawnych prognoz Open-Meteo jest tam zablokowany) i wspólny, szybko wyczerpywany dzienny limit Open-Meteo.

Punkty: config/shared_weather_points.csv (pt, group, cc, lat, lon, tz). Pliki w data/shared_weather/:
- points.csv      – kopia listy punktów (dla odbiorców),
- temp_hist.csv   – pt,date,temp: średnia dobowa temperatura z archiwum ERA5 (od 2019 r., dociągane przyrostowo),
- temp_recent.csv – pt,date,temp,fc: ostatnie 14 dni i 16 dni prognozy (fc=1) z bieżącego przebiegu modeli,
- temp_prev.csv   – pt,date,l0..l7: średnia dobowa temperatura z prognoz wydanych 0..7 dni wcześniej
                    (Open-Meteo previous-runs; l1 = prognoza na jutro wydana dzień wcześniej itd.),
- meta.json       – czas aktualizacji, data prognozy, zakres danych, zużycie limitu, błędy.

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
HIST_START = "2019-01-01"
PREV_START = "2024-01-01"
LEADS = 7
ERA5_LAG = 6            # archiwum ERA5 ma ok. 5 dni opóźnienia
PREV_CHUNK_DAYS = 120
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


def http_get(url: str, params: dict, tries: int = 3) -> list[dict]:
    last = None
    for k in range(tries):
        if time.time() > DEADLINE[0]:
            raise Deadline("limit czasu przebiegu")
        t0 = time.time()
        STATS["requests"] += 1
        try:
            r = requests.get(url, params=params, timeout=90,
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
        js = r.json()
        return js if isinstance(js, list) else [js]
    raise RuntimeError(last or "błąd Open-Meteo")


def load_points(path: Path | None = None) -> pd.DataFrame:
    return pd.read_csv(path or POINTS, dtype={"pt": str, "group": str, "cc": str, "tz": str})


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


def _prev_loop(jobs, q, get, end, hourly, new):
    left = 0
    _PREV_LEFT[0] = len(jobs)
    for (start, tz), g in jobs.groupby(["start", "tz"], sort=True):
        for i in range(0, len(g), BATCH):
            gg = g.iloc[i:i + BATCH]
            a = start
            while a <= end:
                b = min(end, a + dt.timedelta(days=PREV_CHUNK_DAYS - 1))
                w = q.weight(len(gg), (b - a).days + 1, len(hourly))
                if not q.can(w):
                    left += len(gg)
                    break
                q.take(w)
                for pt, js in zip(gg.pt, get(PREV_URL, {**_coords(gg), "hourly": ",".join(hourly), "timezone": tz,
                                                         "start_date": a.isoformat(), "end_date": b.isoformat()})):
                    h = js.get("hourly") or {}
                    if not h.get("time"):
                        continue
                    d = daily_from_hourly(h["time"], {f"l{k}": h.get(v, [None] * len(h["time"])) for k, v in enumerate(hourly)})
                    d = d.dropna(how="all").reset_index()
                    d.insert(0, "pt", pt)
                    new.append(d)
                a = b + dt.timedelta(days=1)
    _PREV_LEFT[0] = left


def _save_meta(meta: dict, q: Quota):
    cov = {}
    for f, key in (("temp_hist.csv", "hist"), ("temp_prev.csv", "prev"), ("temp_recent.csv", "recent")):
        df = _read(f, ["pt", "date"])
        if len(df):
            cov[key] = {"points": int(df.pt.nunique()), "from": str(df.date.min()), "to": str(df.date.max()), "rows": int(len(df))}
    meta.update(updated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), coverage=cov,
                weighted_calls=round(q.used, 1))
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))


def run(budget: float = 8000, get=http_get, today: dt.date | None = None, minutes: float = 35) -> dict:
    """Kroki: prognoza (najważniejsza), historia ERA5, dawne prognozy. meta.json zapisywane po każdym kroku,
    dane – także przy błędzie albo po upływie `minutes` minut (przebieg nie zostanie ucięty bez zapisu)."""
    today = today or dt.date.today()
    DEADLINE[0] = time.time() + minutes * 60
    STATS.update(requests=0, failed=0, http429=0, seconds=0.0, last_error=None)
    pts = load_points()
    OUT.mkdir(parents=True, exist_ok=True)
    pts.to_csv(OUT / "points.csv", index=False)
    q = Quota(budget)
    meta_p = OUT / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    meta.update(source="Open-Meteo (CC BY 4.0): ERA5, prognozy bieżące i dawne", errors=[], pending={}, steps={})
    for name, fn in (("recent", step_recent), ("hist", step_hist), ("prev", step_prev)):
        t0, r0, w0 = time.time(), STATS["requests"], q.used
        status = "OK"
        try:
            res = fn(pts, q, get=get, today=today)
            if name == "recent":
                meta["forecast_issued"] = today.isoformat()
            elif res:
                meta["pending"][name] = int(res)
        except Deadline:
            status = "limit czasu"
            meta["errors"].append(f"{name}: limit czasu przebiegu ({minutes:.0f} min) – zebrane dane zapisane, reszta jutro")
        except DailyLimit as e:
            status = "dzienny limit"
            meta["errors"].append(f"{name}: dzienny limit Open-Meteo ({e})")
        except Exception as e:
            status = f"błąd {type(e).__name__}"
            meta["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
        meta["steps"][name] = dict(status=status, seconds=round(time.time() - t0), requests=STATS["requests"] - r0,
                                   weighted=round(q.used - w0, 1), http429=STATS["http429"], last_error=STATS["last_error"])
        print(f"  {name}: {status} ({time.time() - t0:.0f} s, zapytań {STATS['requests'] - r0}, 429: {STATS['http429']})"
              + (f"; ostatni błąd: {STATS['last_error']}" if STATS["last_error"] else ""), file=sys.stderr if status != "OK" else sys.stdout)
        _save_meta(meta, q)
        if status in ("limit czasu", "dzienny limit"):
            break
    _save_meta(meta, q)
    print(f"Wspólna pogoda: zapytania ważone ≈ {q.used:.0f}; błędy: {len(meta['errors'])}")
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--budget", type=float, default=8000, help="limit zapytań ważonych Open-Meteo w tym przebiegu")
    ap.add_argument("--minutes", type=float, default=35, help="po tylu minutach nie zaczyna nowych zapytań i zapisuje wynik")
    a = ap.parse_args()
    meta = run(a.budget, minutes=a.minutes)
    # błąd prognozy = błąd kroku (pozostałe kroki mogą poczekać)
    sys.exit(1 if any(e.startswith("recent") for e in meta["errors"]) else 0)


if __name__ == "__main__":
    main()
