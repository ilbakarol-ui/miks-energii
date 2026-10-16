"""Wielka Brytania (GB) z Elexon BMRS (Insights Solution API, bez klucza).

- AGPT (B1620): produkcja wg typu, co 30 min, MW: biomasa, gaz, węgiel, ropa, jądrowa,
  woda przepływowa, szczytowo-pompowe, wiatr na lądzie i na morzu, słońce, inne.
- ATL (B0610): obciążenie (zużycie) całkowite, co 30 min, MW.

Zapytania po oknach czasu publikacji (7 dni). Dane bywają korygowane, więc dla każdego
okresu i typu zostaje rekord z najpóźniejszą publikacją.
Dane: Elexon BMRS, licencja BMRS (wymagane oznaczenie źródła).
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import store
from .categories import classify, is_known
from .energycharts import to_daily

BASE = "https://data.elexon.co.uk/bmrs/api/v1/datasets"
CODE, TZ = "GB", "Europe/London"
WINDOW_DAYS = 7


class ElexonError(RuntimeError):
    pass


def _get(dataset: str, start: dt.datetime, end: dt.datetime, retries: int = 4) -> list[dict]:
    params = {"publishDateTimeFrom": start.strftime("%Y-%m-%dT%H:%MZ"),
              "publishDateTimeTo": end.strftime("%Y-%m-%dT%H:%MZ")}
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}/{dataset}/stream", params=params, timeout=120,
                             headers={"User-Agent": "energy-mix-tracker (GitHub Actions)"})
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise ElexonError(f"HTTP {r.status_code}: {r.text[:200]}")
        js = r.json()
        return js.get("data", []) if isinstance(js, dict) else js
    raise ElexonError(last or "błąd Elexon")


def fetch(dataset: str, start: dt.datetime, end: dt.datetime, pause: float = 0.3) -> list[dict]:
    out = []
    cur = start
    while cur < end:
        nxt = min(end, cur + dt.timedelta(days=WINDOW_DAYS))
        out += _get(dataset, cur, nxt)
        cur = nxt
        time.sleep(pause)
    return out


def _latest(recs: list[dict], key: tuple) -> pd.DataFrame:
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    if "startTime" not in df or "quantity" not in df:
        return pd.DataFrame()
    df["startTime"] = pd.to_datetime(df["startTime"], utc=True)
    df["publishTime"] = pd.to_datetime(df.get("publishTime"), utc=True, errors="coerce")
    df["rev"] = pd.to_numeric(df.get("documentRevisionNumber", 0), errors="coerce").fillna(0)
    df = df.sort_values(["publishTime", "rev"]).drop_duplicates(list(key), keep="last")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    return df


def to_mw(agpt: list[dict], atl: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    """Ramka MW (co 30 min, UTC) w kategoriach trackera + 'load'."""
    g = _latest(agpt, ("startTime", "psrType"))
    names: list[str] = []
    cols = {}
    if not g.empty:
        if "businessType" in g:
            g = g[g["businessType"].fillna("Production").str.lower() != "consumption"]
        wide = g.pivot_table(index="startTime", columns="psrType", values="quantity", aggfunc="last")
        for name in wide.columns:
            names.append(name)
            cat = classify(name)
            if cat is None:
                continue
            v = wide[name].abs() if cat == "storage_charge" else wide[name]
            cols[cat] = cols[cat].add(v, fill_value=0) if cat in cols else v
    mw = pd.DataFrame(cols)
    a = _latest(atl, ("startTime",))
    if not a.empty:
        load = a.set_index("startTime")["quantity"].sort_index()
        mw = mw.join(load.rename("load"), how="outer") if not mw.empty else load.rename("load").to_frame()
    if mw.empty:
        return mw, names
    mw = mw.sort_index()
    mw.index.name = None
    return mw, names


def fetch_days(a: dt.date, b: dt.date) -> tuple[pd.DataFrame, list[str]]:
    """Dzienne dane GB (MWh) dla dni lokalnych [a, b]."""
    tz = ZoneInfo(TZ)
    start = dt.datetime.combine(a, dt.time(0), tz).astimezone(dt.timezone.utc) - dt.timedelta(hours=2)
    end = dt.datetime.combine(b + dt.timedelta(days=1), dt.time(0), tz).astimezone(dt.timezone.utc) + dt.timedelta(days=2)
    end = min(end, dt.datetime.now(dt.timezone.utc))
    agpt = fetch("AGPT", start, end)
    atl = fetch("ATL", start, end)
    mw, names = to_mw(agpt, atl)
    if mw.empty:
        return pd.DataFrame(), names
    # tylko okresy z dni [a, b] (publikacje sięgają dalej)
    lo = pd.Timestamp(a).tz_localize(TZ).tz_convert("UTC")
    hi = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(TZ).tz_convert("UTC")
    mw = mw[(mw.index >= lo) & (mw.index < hi)]
    return to_daily(mw, TZ), names


def update(settings: dict, log: list, budget_minutes: float = 40) -> dict:
    """Ostatnie dni, uzupełnianie historii do 5 lat i naprawa luk - jak dla Energy-Charts."""
    from .update import bad_ranges, chunks
    t_end = time.monotonic() + budget_minutes * 60
    state = store.read_state()
    st = state.setdefault(CODE, {})
    df = store.read_daily(CODE)
    yesterday = dt.datetime.now(ZoneInfo(TZ)).date() - dt.timedelta(days=1)
    min_cov = settings["min_day_coverage"]

    def run(a, b):
        nonlocal df
        try:
            new, names = fetch_days(a, b)
        except Exception as e:
            log.append(f"GB: błąd Elexon {a}..{b}: {type(e).__name__}: {e}")
            st["last_error"] = log[-1]
            return None
        if names:
            st["names"] = sorted(set(st.get("names", [])) | set(names))
            st["unknown"] = sorted({n for n in st["names"] if classify(n) == "misc" and not is_known(n)})
        if not new.empty:
            df = store.merge_daily(df, new)
            store.write_daily(CODE, df)
        st.pop("last_error", None)
        st["last_ok"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
        return new

    # 1) ostatnie dni
    good = df.index[df["cov"] >= min_cov] if not df.empty else []
    last = good.max().date() if len(good) else None
    back = settings["refetch_days_weekly"] if dt.date.today().weekday() == settings["weekly_refetch_weekday"] else settings["refetch_days"]
    start = (last - dt.timedelta(days=back)) if last else yesterday - dt.timedelta(days=35)
    for a, b in chunks(start, yesterday, 30):
        run(a, b)
    store.write_state(state)

    # 2) historia wstecz, po 30 dni
    target = dt.date(dt.date.today().year - settings["history_years"], 1, 1)
    have = df.index[df["cov"] > 0] if not df.empty else []
    cursor = dt.date.fromisoformat(st["cursor"]) if st.get("cursor") else (have.min().date() if len(have) else yesterday)
    floor = dt.date.fromisoformat(st["floor"]) if st.get("floor") else None
    empty_in_row = 0
    while cursor > target and not (floor and cursor <= floor) and time.monotonic() < t_end:
        b = cursor - dt.timedelta(days=1)
        a = max(target, b - dt.timedelta(days=29))
        new = run(a, b)
        if new is None:
            break
        if new.empty or new["cov"].max() == 0:
            empty_in_row += 1
            if empty_in_row >= 3:      # 90 dni bez danych - źródło nie ma starszej historii
                st["floor"] = cursor.isoformat()
                log.append(f"GB: brak danych Elexon przed {cursor}")
                break
        else:
            empty_in_row = 0
        cursor = a
        st["cursor"] = cursor.isoformat()
        store.write_state(state)

    # 3) naprawa luk (raz na 7 dni na zakres)
    today = dt.date.today()
    tried = {k: v for k, v in st.get("repair", {}).items() if (today - dt.date.fromisoformat(v)).days < 30}
    until = yesterday - dt.timedelta(days=settings["refetch_days"])
    for a, b in bad_ranges(df, [], st, target, until, settings):
        key = f"{a}_{b}"
        if key in tried and (today - dt.date.fromisoformat(tried[key])).days < 7:
            continue
        if time.monotonic() >= t_end:
            break
        for x, y in chunks(a, b, 30):
            run(x, y)
        tried[key] = today.isoformat()
    st["repair"] = tried
    have = df.index[df["cov"] > 0] if not df.empty else []
    st["history_complete"] = bool(len(have)) and (have.min().date() <= target or bool(floor and have.min().date() <= floor)
                                                  or bool(st.get("floor")))
    store.write_state(state)
    return {"history_complete": st["history_complete"], "last": str(df.index.max().date()) if not df.empty else None}
