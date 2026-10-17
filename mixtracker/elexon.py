"""Wielka Brytania (GB) z Elexon BMRS (Insights Solution API, bez klucza).

- FUELHH: produkcja wg paliwa co 30 min z pomiarów rozliczeniowych (elektrownie
  przyłączone do sieci przesyłowej) oraz przepływy na interkonektorach (INT*,
  import +, eksport -). Podstawowe źródło - kompletne i spójne od 2021 r.
- AGPT (B1620): użyty tylko do słońca (szacunek NESO dla instalacji rozproszonych,
  których FUELHH nie obejmuje) i do podziału wiatru na ląd i morze.

Zużycie = produkcja + saldo interkonektorów (szacunek z bilansu; pompowanie
w elektrowniach szczytowo-pompowych i rozproszona energetyka wiatrowa nie są ujęte).
AGPT bywa niekompletny (całe typy znikają na tygodnie), a ATL (B0610) zmienił
definicję w 2026 r., dlatego nie są używane jako podstawa.

Dane bywają korygowane, więc dla każdego okresu i typu zostaje najpóźniejsza publikacja.
Contains BMRS data © Elexon Limited copyright and database right.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import store
from .categories import DETAIL_KEYS
from .energycharts import to_daily

BASE = "https://data.elexon.co.uk/bmrs/api/v1/datasets"
CODE, TZ = "GB", "Europe/London"
WINDOW_DAYS = 7
FUEL = {"CCGT": "gas", "OCGT": "gas", "NUCLEAR": "nuclear", "COAL": "hard_coal", "OIL": "oil",
        "BIOMASS": "biomass", "NPSHYD": "hydro_ror", "PS": "hydro_pumped", "OTHER": "misc", "WIND": "wind"}
OFFSHORE_SHARE_DEFAULT = 0.6   # udział morskich farm w wietrze przesyłowym, gdy AGPT nie pozwala go ustalić


class ElexonError(RuntimeError):
    pass


def _get(dataset: str, params: dict, retries: int = 4) -> list[dict]:
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


def fetch_fuelhh(a: dt.date, b: dt.date, pause: float = 0.3) -> list[dict]:
    """FUELHH po dniach rozliczeniowych [a, b], okna 7-dniowe."""
    out, cur = [], a
    while cur <= b:
        nxt = min(b, cur + dt.timedelta(days=WINDOW_DAYS - 1))
        out += _get("FUELHH", {"settlementDateFrom": cur.isoformat(), "settlementDateTo": nxt.isoformat()})
        cur = nxt + dt.timedelta(days=1)
        time.sleep(pause)
    return out


def fetch_agpt(start: dt.datetime, end: dt.datetime, pause: float = 0.3) -> list[dict]:
    """AGPT po czasie publikacji, okna 7-dniowe."""
    out, cur = [], start
    while cur < end:
        nxt = min(end, cur + dt.timedelta(days=WINDOW_DAYS))
        out += _get("AGPT", {"publishDateTimeFrom": cur.strftime("%Y-%m-%dT%H:%MZ"),
                             "publishDateTimeTo": nxt.strftime("%Y-%m-%dT%H:%MZ")})
        cur = nxt
        time.sleep(pause)
    return out


def _latest(recs: list[dict], key: tuple, value: str) -> pd.DataFrame:
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    if "startTime" not in df or value not in df:
        return pd.DataFrame()
    df["startTime"] = pd.to_datetime(df["startTime"], utc=True)
    df["publishTime"] = pd.to_datetime(df["publishTime"], utc=True, errors="coerce") if "publishTime" in df else pd.NaT
    df["rev"] = pd.to_numeric(df["documentRevisionNumber"], errors="coerce").fillna(0) if "documentRevisionNumber" in df else 0
    df = df.sort_values(["publishTime", "rev"]).drop_duplicates(list(key), keep="last")
    df[value] = pd.to_numeric(df[value], errors="coerce")
    return df


def to_mw(fuelhh: list[dict], agpt: list[dict]) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """(MW co 30 min w kategoriach trackera + 'xborder', udział morskiej części wiatru w AGPT, nazwy typów)."""
    f = _latest(fuelhh, ("startTime", "fuelType"), "generation")
    names: list[str] = []
    if f.empty:
        return pd.DataFrame(), pd.Series(dtype=float), names
    wide = f.pivot_table(index="startTime", columns="fuelType", values="generation", aggfunc="last").sort_index()
    cols: dict[str, pd.Series] = {}
    inter = [c for c in wide.columns if str(c).upper().startswith("INT")]
    for name in wide.columns:
        names.append(str(name))
        cat = FUEL.get(str(name).upper())
        if cat is None:
            if name not in inter:
                cat = "misc"
            else:
                continue
        cols[cat] = cols[cat].add(wide[name], fill_value=0) if cat in cols else wide[name]
    mw = pd.DataFrame(cols)
    if inter:
        mw["xborder"] = wide[inter].sum(axis=1, min_count=1)
    # słońce i podział wiatru z AGPT
    g = _latest(agpt, ("startTime", "psrType"), "quantity")
    share = pd.Series(dtype=float)
    if not g.empty:
        if "businessType" in g:
            g = g[g["businessType"].fillna("Production").str.lower() != "consumption"]
        ag = g.pivot_table(index="startTime", columns="psrType", values="quantity", aggfunc="last")
        if "Solar" in ag:
            mw = mw.join(ag["Solar"].rename("solar"), how="left")
        if {"Wind Offshore", "Wind Onshore"} <= set(ag.columns):
            share = (ag["Wind Offshore"] / (ag["Wind Offshore"] + ag["Wind Onshore"])).where(
                (ag["Wind Offshore"] + ag["Wind Onshore"]) > 0)
    mw.index.name = None
    return mw, share, names


def split_wind(daily: pd.DataFrame, wind_daily: pd.Series, agpt_wind_daily: pd.Series,
               offshore_share: pd.Series) -> pd.DataFrame:
    """Rozdziela wiatr z FUELHH na ląd i morze wg udziału z AGPT (gdy AGPT pokrywa >= 50%
    wiatru FUELHH), inaczej wg mediany z wiarygodnych dni albo wartości domyślnej."""
    ok = (agpt_wind_daily >= 0.5 * wind_daily) & (agpt_wind_daily <= 1.5 * wind_daily) & offshore_share.notna()
    sh = offshore_share.where(ok)
    fill = float(sh.median()) if sh.notna().any() else OFFSHORE_SHARE_DEFAULT
    sh = sh.fillna(fill).clip(0, 1)
    daily["wind_offshore"] = wind_daily * sh
    daily["wind_onshore"] = wind_daily * (1 - sh)
    return daily


def fetch_days(a: dt.date, b: dt.date) -> tuple[pd.DataFrame, list[str]]:
    """Dzienne dane GB (MWh) dla dni lokalnych [a, b]."""
    tz = ZoneInfo(TZ)
    fuel = fetch_fuelhh(a, b)
    start = dt.datetime.combine(a, dt.time(0), tz).astimezone(dt.timezone.utc) - dt.timedelta(hours=2)
    end = dt.datetime.combine(b + dt.timedelta(days=1), dt.time(0), tz).astimezone(dt.timezone.utc) + dt.timedelta(days=2)
    end = min(end, dt.datetime.now(dt.timezone.utc))
    try:
        agpt = fetch_agpt(start, end)
    except ElexonError:
        agpt = []
    mw, share, names = to_mw(fuel, agpt)
    if mw.empty:
        return pd.DataFrame(), names
    lo = pd.Timestamp(a).tz_localize(TZ).tz_convert("UTC")
    hi = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(TZ).tz_convert("UTC")
    mw = mw[(mw.index >= lo) & (mw.index < hi)]
    if mw.empty:
        return pd.DataFrame(), names
    base = mw.drop(columns=[c for c in ("solar", "wind") if c in mw])
    base["wind_onshore"] = mw["wind"] if "wind" in mw else np.nan    # tymczasowo cały wiatr
    daily = to_daily(base, TZ)
    # słońce: osobne pokrycie; dzień bez danych o słońcu nie jest kompletny
    if "solar" in mw:
        sd = to_daily(mw[["solar"]], TZ)
        daily["solar"] = sd["solar"].reindex(daily.index)
        daily["cov"] = np.minimum(daily["cov"], sd["cov"].reindex(daily.index).fillna(0))
    else:
        daily["solar"] = np.nan
        daily["cov"] = 0.0
    # podział wiatru
    wind_daily = daily["wind_onshore"].copy()
    if not share.empty:
        local = share.index.tz_convert(tz).date
        day_share = share.groupby(local).mean()
        day_share.index = pd.to_datetime(day_share.index)
        # wiatr wg AGPT (do oceny jego kompletności)
        g = _latest(agpt, ("startTime", "psrType"), "quantity")
        g = g[g["psrType"].isin(["Wind Offshore", "Wind Onshore"])]
        aw = g.groupby(pd.Index(g["startTime"].dt.tz_convert(tz).dt.date, name="d"))["quantity"].sum() * 0.5
        aw.index = pd.to_datetime(aw.index)
        daily = split_wind(daily, wind_daily, aw.reindex(daily.index).fillna(0), day_share.reindex(daily.index))
    else:
        daily = split_wind(daily, wind_daily, pd.Series(0.0, index=daily.index), pd.Series(np.nan, index=daily.index))
    daily["load"] = np.nan          # zużycie liczone z bilansu w trends.derive (produkcja + saldo)
    daily["storage_charge"] = np.nan
    daily.loc[daily["cov"] == 0, DETAIL_KEYS] = np.nan
    return daily[store.COLUMNS], names


def update(settings: dict, log: list, budget_minutes: float = 40) -> dict:
    """Ostatnie dni, uzupełnianie historii do 5 lat i naprawa luk - jak dla Energy-Charts."""
    from .update import bad_ranges, chunks
    t_end = time.monotonic() + budget_minutes * 60
    state = store.read_state()
    st = state.setdefault(CODE, {})
    df = store.read_daily(CODE)
    if st.get("method") != "fuelhh":
        # dane z wcześniejszej wersji (AGPT/ATL) - pobranie całej historii od nowa
        df = df.iloc[0:0]
        store.write_daily(CODE, df)
        for k in ("cursor", "floor", "repair", "history_complete", "names", "unknown"):
            st.pop(k, None)
        st["method"] = "fuelhh"
        log.append("GB: przejście na FUELHH - historia pobierana od nowa")
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
            st["unknown"] = sorted({n for n in st["names"] if n.upper() not in FUEL and not n.upper().startswith("INT")})
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
