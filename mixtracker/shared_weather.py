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

- power_daily.csv, power_hourly.csv.gz, power_meta.json – pogoda do prognoz wiatru, słońca i zużycia Miksu energii
                    (U-20261114-3): deterministyczna prognoza Open-Meteo dla punktów z config/weather_points.csv
                    (rejony farm wiatrowych i słonecznych, miasta), 10 dni wstecz i 16 dni naprzód, przeliczona na cechy
                    dzienne krajów (code,date,cf,ws,ghi,temp,hdd,cdd – doba w czasie lokalnym kraju) i godzinowy wiatr
                    (code,time UTC,cf,ws; 10 dni). Pobierana w zadaniach "temp" (noc) i "evening". Miks energii czyta
                    te pliki zamiast pytać Open-Meteo sam (mixtracker/weather.py); stan kroku w meta["power"].

Kolumna uses w pliku punktów: temp (temperatura: historia, prognoza, dawne prognozy), ens (ensemble do energetyki),
seas (prognoza sezonowa).
Grupy z TRIAL_GROUPS (punkty próbne, np. eu_gas_x / EX_…): prognoza razem z resztą, historia i dawne prognozy po
punktach podstawowych (limit TRIAL_MAX_MINUTES); błędy, zaległości i zakres danych w meta.json["trial"] (U-20261114).

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
HIST_CHUNK_DAYS = 365   # U-20261114: zaległa historia ERA5 w kawałkach (waga zapytania ≤ ok. 260)
# U-20261114: grupy punktów próbnych – pobierane po punktach podstawowych, z osobnym limitem czasu; ich błędy,
# zaległości i zakres danych są w meta.json pod kluczem "trial", a nie w errors / pending / coverage,
# które czytają raporty (Gaz UE, poranna wiadomość Miksu)
TRIAL_GROUPS = ("eu_gas_x",)
TRIAL_MAX_MINUTES = 10
# U-20261114-3: pogoda do prognoz Miksu energii (krok "power") – te same punkty i cechy co mixtracker/weather.py
POWER_POINTS = store.ROOT / "config" / "weather_points.csv"
POWER_PAST, POWER_DAYS, POWER_BATCH = 10, 16, 25
POWER_HOURLY_DAYS = 9       # godzinowy wiatr: od wczoraj do dziś + 9 dni (prognoza godzinowa obejmuje 7 dni)
POWER_MAX_MINUTES = 5       # krok nie zabiera czasu pozostałym; brakujące kraje Miks energii pobierze sam
POWER_JOBS = ("temp", "evening")
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


# B-20261031-3: w 12 z 12 przebiegów 23–31.10 krok ensemble miał 1 × HTTP 429 (61 s) i 1 × ReadTimeout (90 s)
ENSEMBLE_PAUSE_S = 4.0
LAST_ENS = [0.0]


def _timeout(url: str, params: dict) -> float:
    """B-20261028-3: dawne prognozy w dużych paczkach (do 60 dni) potrzebują 150 s; dzienny przyrost (< 10 dni)
    serwer zwraca w kilka sekund – zawieszone zapytanie lepiej szybko ponowić niż czekać 150 s."""
    if "previous-runs" not in url:
        if "seasonal-api" in url:
            return 180      # B-20261105-3: odpowiedź SEAS5 (4 punkty × 51 wariantów × 183 dni) bywa dłuższa niż 90 s
        # ensemble: w listopadzie 2026 odpowiedzi (8 punktów × 51 wariantów × 17 dni) często trwają 45–90 s – limit 45 s
        # (B-20261031-3) dawał 4–10 ReadTimeout na przebieg i przekroczenie 12 min wieczorem 5–6.11; 120 s jak dla dużych paczek
        return 120 if "ensemble-api" in url else 90
    try:
        days = (dt.date.fromisoformat(params["end_date"]) - dt.date.fromisoformat(params["start_date"])).days + 1
    except (KeyError, TypeError, ValueError):
        return 150
    # B-20261112-3: dzienny przyrost (5 dni × 5 punktów) w 17 nocach z rzędu kończył się ReadTimeout przy 40 s –
    # 3 próby × 40 s to za mało przy wolnym serwerze dawnych prognoz
    return 90 if days < 10 else 150


def http_get(url: str, params: dict, tries: int = 3) -> list[dict]:
    last = None
    for k in range(tries):
        if time.time() > DEADLINE[0]:
            raise Deadline("limit czasu przebiegu")
        if "ensemble-api" in url and LAST_ENS[0]:   # B-20261031-3: odstęp między ciężkimi zapytaniami (limit minutowy)
            time.sleep(max(0.0, ENSEMBLE_PAUSE_S - (time.time() - LAST_ENS[0])))
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
        if "ensemble-api" in url:
            LAST_ENS[0] = time.time()
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

_RECENT_FAILED: list[str] = []


def step_recent(pts, q: Quota, get=http_get, today: dt.date | None = None) -> pd.DataFrame:
    """Prognoza temperatury (14 dni wstecz, 16 naprzód). B-20261113-2: nieudana paczka nie przerywa kroku –
    pozostałe paczki są pytane dalej, nieudane raz jeszcze na końcu, a punkty bez dzisiejszej prognozy
    zostają z poprzedniego pliku (błąd w meta.json). Gdy prognozy nie ma dla połowy punktów – wyjątek jak dotąd."""
    today = today or dt.date.today()
    rows, failed = [], []

    def ask(g, tz):
        res = get(FORECAST_URL, {**_coords(g), "daily": "temperature_2m_mean", "timezone": tz,
                                 "past_days": 14, "forecast_days": 16})
        for pt, js in zip(g.pt, res):
            d = js.get("daily") or {}
            for t, v in zip(d.get("time", []), d.get("temperature_2m_mean", [])):
                if v is not None:
                    rows.append((pt, t, v, int(t >= today.isoformat())))

    for tz, g in _batches(pts):
        q.take(q.weight(len(g), 30))
        try:
            ask(g, tz)
        except (Deadline, DailyLimit):
            raise
        except RuntimeError as e:
            failed.append((g, tz, str(e)))
    again = []
    for g, tz, err in failed:          # serwer bywa chwilowo przeciążony – jedna ponowna próba na końcu
        try:
            ask(g, tz)
        except (Deadline, DailyLimit):
            raise
        except RuntimeError as e:
            again.append((g, tz, str(e)))
    df = pd.DataFrame(rows, columns=["pt", "date", "temp", "fc"])
    if df.empty or df.pt.nunique() < 0.5 * len(pts):
        raise RuntimeError("pusta prognoza" if df.empty else f"prognoza tylko dla {df.pt.nunique()} z {len(pts)} punktów")
    if again:
        lost = [p_ for g, _, _ in again for p_ in g.pt]
        old = _read("temp_recent.csv", ["pt", "date", "temp", "fc"])
        df = pd.concat([df, old[old.pt.isin(lost)]], ignore_index=True)
        _RECENT_FAILED.append(f"{len(lost)} punktów z poprzedniej prognozy, np. {','.join(lost[:5])}: {again[0][2][:60]}")
    _write(df, "temp_recent.csv")
    return df


_HIST_FAILED: list[str] = []


def step_hist(pts, q: Quota, get=http_get, today: dt.date | None = None) -> int:
    """Archiwum ERA5: dla każdego punktu od ostatniej zapisanej daty (albo od HIST_START) do dziś-6.

    U-20261114 (punkty próbne EX_): najpierw dzienny przyrost (punkty z aktualną historią), potem zaległa historia
    w kawałkach po HIST_CHUNK_DAYS dni – zapytanie 10 punktów × 365 dni waży ok. 260 (limit minutowy w kodzie 300,
    Open-Meteo 600), a 10 punktów × 7,9 roku ważyło ok. 2 050. Nieudana paczka nie przerywa kroku (reszta pobierana
    dalej, błąd w _HIST_FAILED -> meta.json errors); przerwaną historię następny przebieg wznawia od ostatniej daty.
    Gdy budżet się kończy, reszta czeka do następnego przebiegu."""
    today = today or dt.date.today()
    end = today - dt.timedelta(days=ERA5_LAG)
    old = _read("temp_hist.csv", ["pt", "date", "temp"])
    last = old.groupby("pt").date.max().to_dict() if len(old) else {}
    jobs = pts.copy()
    jobs["start"] = [(dt.date.fromisoformat(last[p]) + dt.timedelta(days=1)) if p in last else dt.date.fromisoformat(HIST_START)
                     for p in jobs.pt]
    jobs = jobs[jobs.start <= end]
    new, done = [], set()
    try:
        # najpierw przyrost (najpóźniejsza data początkowa), potem zaległa historia
        for (start, tz), g in sorted(jobs.groupby(["start", "tz"]), key=lambda kv: kv[0][0], reverse=True):
            for i in range(0, len(g), BATCH):
                gg = g.iloc[i:i + BATCH]
                a = start
                while a <= end:
                    b = min(end, a + dt.timedelta(days=HIST_CHUNK_DAYS - 1))
                    w = q.weight(len(gg), (b - a).days + 1)
                    if not q.can(w):
                        break
                    q.take(w)
                    try:
                        res = get(ARCHIVE_URL, {**_coords(gg), "daily": "temperature_2m_mean", "timezone": tz,
                                                "start_date": a.isoformat(), "end_date": b.isoformat()})
                    except (Deadline, DailyLimit):
                        raise
                    except Exception as e:      # jedna paczka nie przerywa kroku – reszta punktów dalej
                        _HIST_FAILED.append(f"{','.join(gg.pt)} {a}..{b}: {type(e).__name__}")
                        break
                    for pt, js in zip(gg.pt, res):
                        d = js.get("daily") or {}
                        part = pd.DataFrame({"pt": pt, "date": d.get("time", []),
                                             "temp": d.get("temperature_2m_mean", [])}).dropna()
                        new.append(part)
                        if b == end:
                            done.add(pt)
                    a = b + dt.timedelta(days=1)
    finally:   # to, co zdążyło przyjść, zapisujemy także przy błędzie albo końcu czasu
        if new:
            _write(pd.concat([old] + new, ignore_index=True), "temp_hist.csv")
    return len(jobs) - len(done)


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


def _get_one(get, url, params):
    """Pojedynczy punkt po nieudanej paczce: 2 próby zamiast 3 (http_get), inne funkcje pobierania bez zmian."""
    return get(url, params, tries=2) if get is http_get else get(url, params)


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
                    # B-20261104-1 / B-20261112-3: paczka odrzucona 3 razy – ponów pojedynczymi punktami
                    # (krótsza odpowiedź; jeden wolny punkt nie blokuje pozostałych)
                    res, bad = [], []
                    for k in range(len(gg) if len(gg) > 1 else 0):
                        one = gg.iloc[k:k + 1]
                        q.take(q.weight(1, (b - a).days + 1, len(hourly)))
                        try:
                            res += _get_one(get, PREV_URL, {**_coords(one), "hourly": ",".join(hourly), "timezone": tz,
                                                            "start_date": a.isoformat(), "end_date": b.isoformat()})
                        except (Deadline, DailyLimit):
                            raise
                        except Exception as e1:
                            res.append({})
                            bad.append(f"{one.pt.iloc[0]} {a}..{b}: {type(e1).__name__}")
                    if not any((r or {}).get("hourly") for r in res):
                        _PREV_FAILED.append(f"{','.join(gg.pt)} {a}..{b}: {type(e).__name__}")
                        left += len(gg)
                        break
                    _PREV_FAILED.extend(bad)
                    left += len(bad)
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


def step_power(q: Quota, get=http_get, today: dt.date | None = None) -> dict:
    """Pogoda do prognoz Miksu energii: godzinowy wiatr na 100 m, promieniowanie i temperatura dla punktów
    z config/weather_points.csv, przeliczone tak samo jak w mixtracker/weather.py na cechy dzienne krajów.

    Nieudana paczka punktów jest pytana jeszcze raz na końcu; kraj bez kompletu punktów nie trafia do pliku
    (Miks energii pobierze go sam). Pliki są zapisywane tylko wtedy, gdy jest co najmniej połowa krajów –
    inaczej zostają poprzednie. Zwraca opis kroku (do meta["power"])."""
    from . import weather            # tu, a nie na górze: weather importuje modele (numpy, holidays)
    today = today or dt.date.today()
    wp = weather.load_points(POWER_POINTS)
    tz_of = {c["code"]: c["tz"] for c in store.load_countries()}
    flat = [(code, p) for code in sorted(wp) if code in tz_of for p in wp[code]]
    if not flat:
        raise RuntimeError("brak punktów pogodowych")
    frames: list = [None] * len(flat)

    def ask(idx):
        chunk = [flat[i][1] for i in idx]
        res = get(FORECAST_URL, {"latitude": ",".join(f"{p['lat']:.2f}" for p in chunk),
                                 "longitude": ",".join(f"{p['lon']:.2f}" for p in chunk),
                                 "hourly": weather.HOURLY, "wind_speed_unit": "ms", "timezone": "GMT",
                                 "past_days": POWER_PAST, "forecast_days": POWER_DAYS})
        if len(res) != len(idx):
            raise RuntimeError(f"odpowiedź dla {len(res)} z {len(idx)} punktów")
        for i, js in zip(idx, res):
            frames[i] = weather.hourly_frame(js)

    batches = [list(range(i, min(i + POWER_BATCH, len(flat)))) for i in range(0, len(flat), POWER_BATCH)]
    failed, err, stopped = [], None, None
    for rnd in (batches, failed):                 # druga runda: paczki, które się nie udały
        for b in list(rnd):
            try:
                q.take(q.weight(len(b), POWER_PAST + POWER_DAYS, 3))
                ask(b)
            except (Deadline, DailyLimit) as e:
                stopped = "limit czasu" if isinstance(e, Deadline) else "dzienny limit"
                break
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:100]}"
                if rnd is batches:
                    failed.append(b)
        if stopped:
            break
    daily, hourly, missing = [], [], []
    lo = pd.Timestamp(today - dt.timedelta(days=1), tz="UTC")
    hi = pd.Timestamp(today + dt.timedelta(days=POWER_HOURLY_DAYS), tz="UTC")
    for code in sorted({c for c, _ in flat}):
        idx = [i for i, (c, _) in enumerate(flat) if c == code]
        fr = [frames[i] for i in idx]
        if any(f is None or f.empty for f in fr):
            missing.append(code)
            continue
        pp = [flat[i][1] for i in idx]
        d = weather.daily_features(fr, pp, tz_of[code])
        if d.empty:
            missing.append(code)
            continue
        d = d.reset_index()
        d.columns = ["date"] + list(d.columns[1:])
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        d.insert(0, "code", code)
        daily.append(d)
        h = weather.hourly_wind(fr, pp)
        h = h[(h.index >= lo) & (h.index < hi)].dropna(how="all")
        if len(h):
            hourly.append(pd.DataFrame({"code": code, "time": h.index.strftime("%Y-%m-%dT%H:%MZ"),
                                        "cf": h["cf"].to_numpy(), "ws": h["ws"].to_numpy()}))
    n_all = len(daily) + len(missing)
    out = {"countries": len(daily), "points": len(flat), "missing": missing, "last_error": err}
    if len(daily) < 0.5 * n_all:
        out["status"] = stopped or "błąd"
        out["error"] = f"pogoda tylko dla {len(daily)} z {n_all} krajów" + (f" ({err})" if err else "")
        return out
    OUT.mkdir(parents=True, exist_ok=True)
    pd.concat(daily, ignore_index=True).to_csv(OUT / "power_daily.csv", index=False, float_format="%.4f")
    if hourly:
        pd.concat(hourly, ignore_index=True).to_csv(OUT / "power_hourly.csv.gz", index=False, float_format="%.3f",
                                                     compression={"method": "gzip", "mtime": 0})
    issued = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    out.update(status="OK" if not missing else "częściowo", issued=issued)
    (OUT / "power_meta.json").write_text(json.dumps({
        "issued": issued, "countries": len(daily), "points": len(flat), "missing": missing,
        "past_days": POWER_PAST, "forecast_days": POWER_DAYS, "hourly_days": POWER_HOURLY_DAYS,
        "source": "Open-Meteo (CC BY 4.0), prognoza deterministyczna (best match); punkty: config/weather_points.csv",
        "columns": {"cf": "wskaźnik mocy turbiny (średnia godzin i punktów)", "ws": "wiatr 100 m, m/s",
                    "ghi": "promieniowanie, kWh/m2 na dobę", "temp": "°C", "hdd": "15,5 − temp", "cdd": "temp − 22"}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _run_power(meta: dict, q: Quota, get, today: dt.date, meta_name: str):
    """U-20261114-3: krok "power" przed pozostałymi (krótki, najwyżej POWER_MAX_MINUTES). Stan w meta["power"],
    a nie w errors / steps, które czytają raporty (Gaz UE) – brak tej pogody nie psuje żadnego raportu:
    Miks energii pobiera ją wtedy sam."""
    end_all = DEADLINE[0]
    DEADLINE[0] = min(end_all, time.time() + POWER_MAX_MINUTES * 60)
    t0, r0, w0 = time.time(), STATS["requests"], q.used
    try:
        res = step_power(q, get=get, today=today)
    except Exception as e:
        res = {"status": f"błąd {type(e).__name__}", "error": str(e)[:150]}
    finally:
        DEADLINE[0] = end_all
    res.update(seconds=round(time.time() - t0), requests=STATS["requests"] - r0, weighted=round(q.used - w0, 1))
    meta["power"] = res
    print(f"  power: {res['status']} ({res['seconds']} s, zapytań {res['requests']}, krajów {res.get('countries', 0)}"
          + (f", bez {','.join(res['missing'])}" if res.get("missing") else "") + ")"
          + (f"; {res['error']}" if res.get("error") else ""), file=sys.stderr if res["status"] != "OK" else sys.stdout)
    _save_meta(meta, q, meta_name)


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


TRIAL_PTS: set[str] = set()


def _save_meta(meta: dict, q: Quota, name: str = "meta.json"):
    cov = {}
    # zakres plików temperatury opisuje tylko meta.json (zadanie "extra" ma w checkoucie starsze kopie tych plików)
    files = (("temp_hist.csv", "hist"), ("temp_prev.csv", "prev"), ("temp_recent.csv", "recent")) if name == "meta.json" else ()
    trial_cov = {}
    for f, key in files:
        df = _read(f, ["pt", "date"])
        tr = df[df.pt.isin(TRIAL_PTS)] if len(df) else df
        df = df[~df.pt.isin(TRIAL_PTS)] if len(df) else df    # U-20261114: zakres tylko punktów podstawowych
        if len(df):
            last = df.groupby("pt").date.max()
            cov[key] = {"points": int(df.pt.nunique()), "points_to_end": int((last == last.max()).sum()),
                        "from": str(df.date.min()), "to": str(df.date.max()), "rows": int(len(df))}
            if len(tr):
                lt = tr.groupby("pt").date.max()
                trial_cov[key] = {"points": int(tr.pt.nunique()), "points_to_end": int((lt >= last.max()).sum()),
                                  "from": str(tr.date.min()), "to": str(tr.date.max()), "rows": int(len(tr))}
    if trial_cov and "trial" in meta:
        meta["trial"]["coverage"] = trial_cov
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


def _run_trial(meta: dict, trial: pd.DataFrame, q: Quota, get, today: dt.date, meta_name: str):
    """U-20261114: historia ERA5 i dawne prognozy punktów próbnych – po punktach podstawowych, najwyżej
    TRIAL_MAX_MINUTES minut. Błędy, zaległości i statusy w meta["trial"]; errors / pending / coverage bez zmian."""
    tm = meta["trial"]
    end_all = DEADLINE[0]
    DEADLINE[0] = min(end_all, time.time() + TRIAL_MAX_MINUTES * 60)
    try:
        for name, fn in (("hist", step_hist), ("prev", step_prev)):
            t0, r0, w0 = time.time(), STATS["requests"], q.used
            STATS["last_error"] = None
            status = "OK"
            p = _sel(trial, "temp")
            try:
                res = fn(p, q, get=get, today=today)
                if res:
                    tm["pending"][name] = int(res)
            except Deadline:
                status = "limit czasu"
                tm["pending"][name] = _pending(name, p, today)
            except DailyLimit as e:
                status = "dzienny limit"
                tm["errors"].append(f"{name}: dzienny limit Open-Meteo ({e})")
            except Exception as e:
                status = f"błąd {type(e).__name__}"
                tm["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
                tm["pending"][name] = _pending(name, p, today)
            failed = _HIST_FAILED if name == "hist" else _PREV_FAILED
            if failed:
                tm["errors"].append(f"{name}: {len(failed)} paczek punktów bez danych, np. {failed[0]}")
                failed.clear()
            tm["steps"][name] = dict(status=status, seconds=round(time.time() - t0), requests=STATS["requests"] - r0,
                                     weighted=round(q.used - w0, 1), last_error=STATS["last_error"])
            print(f"  punkty próbne {name}: {status} ({time.time() - t0:.0f} s, zapytań {STATS['requests'] - r0})")
            _save_meta(meta, q, meta_name)
            if status in ("limit czasu", "dzienny limit"):
                break
    finally:
        DEADLINE[0] = end_all


def run(budget: float = 8000, get=http_get, today: dt.date | None = None, minutes: float = 35, job: str = "temp") -> dict:
    """Zadanie "temp": prognoza (najważniejsza), historia ERA5, dawne prognozy. Zadanie "extra": ensemble do energetyki
    i SEAS5. meta zapisywane po każdym kroku, dane – także przy błędzie albo po upływie `minutes` minut."""
    today = today or dt.date.today()
    DEADLINE[0] = time.time() + minutes * 60
    STATS.update(requests=0, failed=0, http429=0, seconds=0.0, last_error=None)
    pts = load_points()
    OUT.mkdir(parents=True, exist_ok=True)
    pts.to_csv(OUT / "points.csv", index=False)
    is_trial = pts["group"].isin(TRIAL_GROUPS)
    core, trial = pts[~is_trial].reset_index(drop=True), pts[is_trial].reset_index(drop=True)
    TRIAL_PTS.clear()
    TRIAL_PTS.update(trial.pt)
    q = Quota(budget)
    meta_name, steps = JOBS[job]
    meta_p = OUT / meta_name
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    meta.update(source="Open-Meteo (CC BY 4.0): ERA5, ECMWF IFS (prognozy bieżące, dawne, ensemble), ECMWF SEAS5",
                errors=[], pending={}, steps={})
    if job == "temp" and len(trial):
        meta["trial"] = {"groups": list(TRIAL_GROUPS), "points": int(len(trial)), "errors": [], "pending": {}, "steps": {}}
    else:
        meta.pop("trial", None)
    meta.pop("power", None)
    if job in POWER_JOBS and POWER_POINTS.exists():
        _run_power(meta, q, get, today, meta_name)
    stopped = False
    for name, fn, use in steps:
        # U-20261114: prognoza (recent) dla wszystkich punktów, historia i dawne prognozy – najpierw podstawowe
        step_pts = pts if name == "recent" else core
        t0, r0, w0, h0 = time.time(), STATS["requests"], q.used, STATS["http429"]
        STATS["last_error"] = None       # błąd i liczniki 429 opisują tylko ten krok
        status = "OK"
        try:
            res = fn(_sel(step_pts, use), q, get=get, today=today)
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
            if name == "hist" and _HIST_FAILED:
                meta["errors"].append(f"hist: {len(_HIST_FAILED)} paczek punktów bez danych, np. {_HIST_FAILED[0]}")
                _HIST_FAILED.clear()
            if name == "recent" and _RECENT_FAILED:
                meta["errors"].append(f"recent (częściowo): {_RECENT_FAILED[0]}")
                _RECENT_FAILED.clear()
        except Deadline:
            status = "limit czasu"
            meta["errors"].append(f"{name}: limit czasu przebiegu ({minutes:.0f} min) – zebrane dane zapisane, reszta jutro")
            meta["pending"][name] = _pending(name, _sel(step_pts, use), today)
        except DailyLimit as e:
            status = "dzienny limit"
            meta["errors"].append(f"{name}: dzienny limit Open-Meteo ({e})")
        except Exception as e:
            status = f"błąd {type(e).__name__}"
            meta["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
            meta["pending"][name] = _pending(name, _sel(step_pts, use), today)
        meta["steps"][name] = dict(status=status, seconds=round(time.time() - t0), requests=STATS["requests"] - r0,
                                   weighted=round(q.used - w0, 1), http429=STATS["http429"] - h0, last_error=STATS["last_error"])
        print(f"  {name}: {status} ({time.time() - t0:.0f} s, zapytań {STATS['requests'] - r0}, 429: {STATS['http429'] - h0})"
              + (f"; ostatni błąd: {STATS['last_error']}" if STATS["last_error"] else ""), file=sys.stderr if status != "OK" else sys.stdout)
        _save_meta(meta, q, meta_name)
        if status in ("limit czasu", "dzienny limit"):
            stopped = True
            break
    if "trial" in meta and not stopped:
        _run_trial(meta, trial, q, get, today, meta_name)
    _HIST_FAILED.clear()
    _PREV_FAILED.clear()
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
    # B-20261113-2: częściowa prognoza (reszta punktów z poprzedniego pliku) nie oznacza przebiegu jako nieudanego
    sys.exit(1 if any(e.startswith(("recent", "ensemble")) and not e.startswith("recent (częściowo)")
                      for e in meta["errors"]) else 0)


if __name__ == "__main__":
    main()
