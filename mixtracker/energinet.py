"""Dania z Energinet (Energi Data Service) – uzupełnianie dób, których brakuje w Energy-Charts i ENTSO-E (U-20261114).

Po co: od 4.11.2026 Energinet nie publikuje na ENTSO-E produkcji wg typu (A75) dla DK1 i DK2, więc Energy-Charts
(które bierze dane z ENTSO-E) nie ma dób Danii, a zużycie z ENTSO-E (A65) ma luki ok. 4 h na dobę (pokrycie
0,75–0,83 przy progu 0,8). Ten moduł pobiera dane wprost od operatora: zbiór GenerationProdTypeExchange
(https://api.energidataservice.dk, godzinowo, DK1 + DK2, bez klucza API).

Zasada: TYLKO UZUPEŁNIANIE. Doba z Energy-Charts / ENTSO-E o pokryciu >= min_day_coverage zostaje bez zmian,
więc gdy publikacja na ENTSO-E wróci, dane wrócą do dotychczasowego źródła same.
- produkcja (cov, kolumny produkcji, saldo wymiany) – gdy doba nie ma produkcji albo cov < min_day_coverage,
- zużycie (GrossCon) – gdy doba nie ma zużycia; godziny bez danych o wymianie są pomijane (GrossCon = produkcja +
  wymiana, a 13.11.2026 wymiana była pusta przez całą dobę, więc GrossCon był o ok. 50 GWh za wysoki), a doba
  odbiegająca od mediany z 28 dób o więcej niż `load_tolerance` jest odrzucana (dane SCADA „Initial” bywają błędne),
- ceny dzienne (price_avg/min/max/neg_h) – gdy puste: średnia stref DK1 i DK2 z data/prices (min/max i godziny
  ujemne – przybliżenie średnią stref); ceny ważone produkcją (pw_/pq_) zostają puste.

Wersje danych Energinet: Initial (SCADA, zaraz po godzinie) -> Preliminary (rozliczenia, po 8–10 dniach) -> Final
(czerwiec następnego roku). API zastępuje starszą wersję nowszą; tu dla każdej godziny bierzemy najnowszą wersję.
Uwaga na zgodność: Energinet podaje węgiel kamienny (ok. 5 GWh/d jesienią 2026), którego w danych ENTSO-E /
Energy-Charts dla Danii nie ma, a wersja Preliminary ma wyższy wiatr morski niż SCADA – doby z Energinet mają
więc nieco wyższą produkcję i emisyjność niż doby z Energy-Charts.

Surowe doby: data/energinet/<KOD>.csv (kolumna ver: 0 Initial, 1 Preliminary, 2 Final).
Dane: Energinet, Energi Data Service (licencja: swobodne użycie z podaniem źródła).
"""
from __future__ import annotations

import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import store
from .categories import DETAIL_KEYS
from .energycharts import to_daily

BASE = "https://api.energidataservice.dk/dataset/"
DATASET = "GenerationProdTypeExchange"
FOLDER = store.DATA / "energinet"
CHUNK_DAYS = 31
VERSION_RANK = {"Initial": 0, "Preliminary": 1, "Final": 2}
# kolumny Energinet (MWh/h = średnia moc w MW) -> kolumny trackera
GEN_MAP = {
    "OffshoreWindPower": "wind_offshore", "OnshoreWindPower": "wind_onshore", "SolarPower": "solar",
    "HydroPower": "hydro_ror", "Biomass": "biomass", "Biogas": "biomass", "Waste": "waste",
    "FossilGas": "gas", "FossilOil": "oil", "FossilHardCoal": "hard_coal",
}
GEN_COLS = ["cov"] + DETAIL_KEYS + ["storage_charge", "xborder"]
PRICE_FILL = ["price_avg", "price_min", "price_max", "price_neg_h"]


class EnerginetError(RuntimeError):
    pass


class Client:
    def __init__(self, pause: float = 1.0, retries: int = 4, timeout: int = 120):
        self.pause, self.retries, self.timeout = pause, retries, timeout
        self.requests_made = 0

    def records(self, start_utc: pd.Timestamp, end_utc: pd.Timestamp, areas: list[str]) -> list[dict]:
        params = {"start": start_utc.strftime("%Y-%m-%dT%H:%M"), "end": end_utc.strftime("%Y-%m-%dT%H:%M"),
                  "timezone": "UTC", "filter": json.dumps({"PriceArea": areas}), "limit": 0}
        last = None
        for attempt in range(self.retries):
            try:
                r = requests.get(BASE + DATASET, params=params, timeout=self.timeout,
                                 headers={"User-Agent": "energy-mix-tracker (GitHub Actions)"})
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(10 * (attempt + 1))
                continue
            self.requests_made += 1
            time.sleep(self.pause)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise EnerginetError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json().get("records") or []
        raise EnerginetError(last or "błąd Energi Data Service")


def to_mw(records: list[dict], areas: list[str]) -> pd.DataFrame:
    """Godzinowe MW w kolumnach trackera (+ load, xborder, ver) – tylko godziny z kompletem stref."""
    if not records:
        return pd.DataFrame()
    d = pd.DataFrame(records)
    d["t"] = pd.to_datetime(d["TimeUTC"]).dt.tz_localize("UTC")
    d["ver"] = d.get("Version", pd.Series("Initial", index=d.index)).map(VERSION_RANK).fillna(0)
    d = d.sort_values("ver").drop_duplicates(["t", "PriceArea"], keep="last")   # najnowsza wersja godziny
    d = d[d["PriceArea"].isin(areas)]
    exch = [c for c in d.columns if c.startswith("Exchange")]
    num = [c for c in list(GEN_MAP) + ["GrossCon"] + exch if c in d.columns]
    d[num] = d[num].apply(pd.to_numeric, errors="coerce")
    # GrossCon = produkcja + wymiana; bez danych o wymianie (np. 13.11.2026 cała doba) GrossCon to sama produkcja
    if exch and "GrossCon" in d:
        d.loc[d[exch].isna().all(axis=1), "GrossCon"] = np.nan
    g = d.groupby("t")
    n_area = g["PriceArea"].nunique()
    s = g[num].sum(min_count=1)
    s = s[n_area == len(areas)]
    mw = pd.DataFrame(index=s.index)
    for src, dst in GEN_MAP.items():
        if src in s:
            mw[dst] = mw[dst].add(s[src], fill_value=0) if dst in mw else s[src]
    for k in DETAIL_KEYS:
        if k not in mw:
            mw[k] = 0.0
    mw["load"] = s.get("GrossCon")
    # saldo wymiany: import +, eksport −; Wielki Bełt (DK1–DK2) znosi się w sumie stref
    mw["xborder"] = s[exch].sum(axis=1, min_count=1) if exch else np.nan
    mw["ver"] = g["ver"].min().reindex(mw.index)
    return mw.sort_index()


def fetch_days(client: Client, areas: list[str], tz: str, a: dt.date, b: dt.date, min_cov: float) -> pd.DataFrame:
    """Doby lokalne [a, b]: cov, kolumny produkcji, load, load_cov, xborder, ver (najstarsza wersja w dobie)."""
    s = pd.Timestamp(a).tz_localize(tz).tz_convert("UTC")
    e = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(tz).tz_convert("UTC")
    mw = to_mw(client.records(s, e, areas), areas)
    out = pd.DataFrame(index=pd.DatetimeIndex(pd.date_range(a, b, freq="D"), name="date"))
    if mw.empty:
        return out
    mw = mw[(mw.index >= s) & (mw.index < e)]
    ver = mw.pop("ver")
    gen = mw.drop(columns=["load"])
    d = to_daily(gen, tz, min_cov)
    for c in GEN_COLS:
        out[c] = d[c].reindex(out.index) if c in d else np.nan
    dl = to_daily(mw[["load"]].assign(misc=mw["load"]), tz, min_cov)   # pokrycie samego zużycia
    out["load"] = dl["load"].reindex(out.index)
    out["load_cov"] = dl["cov"].reindex(out.index)
    vday = pd.Series(ver.to_numpy(), index=pd.Index(mw.index.tz_convert(ZoneInfo(tz)).date)).groupby(level=0).min()
    out["ver"] = pd.Series(vday.to_numpy(), index=pd.to_datetime(vday.index)).reindex(out.index)
    return out


def read_raw(code: str) -> pd.DataFrame:
    p = FOLDER / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))
    return pd.read_csv(p, parse_dates=["date"], index_col="date").astype(float).sort_index()


def write_raw(code: str, df: pd.DataFrame):
    FOLDER.mkdir(parents=True, exist_ok=True)
    o = df.copy()
    o.index = o.index.strftime("%Y-%m-%d")
    o.index.name = "date"
    o.round(3).to_csv(FOLDER / f"{code}.csv", float_format="%.10g")


def merge_raw(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Nowe doby zastępują stare, chyba że mają wyraźnie gorsze pokrycie produkcji (ucięty brzeg okna)."""
    new = new.dropna(how="all")
    if old.empty:
        return new.sort_index()
    common = new.index.intersection(old.index)
    worse = [d for d in common if (new.at[d, "cov"] if pd.notna(new.at[d, "cov"]) else 0) + 0.01
             < (old.at[d, "cov"] if "cov" in old and pd.notna(old.at[d, "cov"]) else 0)]
    new = new.drop(index=worse)
    out = pd.concat([old.drop(index=new.index.intersection(old.index)), new])
    return out.sort_index()


def _zone_prices(zones: list[str]) -> pd.DataFrame:
    frames = [store.read_zone(z)[PRICE_FILL] for z in zones]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=PRICE_FILL)
    return pd.concat(frames).groupby(level=0).mean()


def _fp(row: pd.Series) -> float:
    """Odcisk doby (suma produkcji) – czy wiersz w data/daily jest nadal tym, który wpisał ten moduł."""
    return round(float(row.reindex(DETAIL_KEYS).fillna(0).sum()), 0)


def apply(df: pd.DataFrame, raw: pd.DataFrame, cfg: dict, min_cov: float, load_tolerance: float = 0.25,
          zone_prices: pd.DataFrame | None = None, owned: dict | None = None) -> tuple[pd.DataFrame, list, list, dict]:
    """Uzupełnia doby od cfg["from"]. Zwraca (ramka, doby produkcji, doby zużycia, owned).

    owned = {"RRRR-MM-DD": {"gen": odcisk, "load": wartość}} – doby wpisane wcześniej przez ten moduł; można je
    nadpisać nowszą wersją Energinet (Initial -> Preliminary), dopóki nikt inny (Energy-Charts, ENTSO-E) ich nie zmienił.
    """
    owned = dict(owned or {})
    if raw.empty:
        return df, [], [], owned
    raw = raw[raw.index >= pd.Timestamp(cfg["from"])]
    df = df.copy()
    gen_days, load_days, new_owned = [], [], {}

    def mine(d, kind):
        o = owned.get(d.strftime("%Y-%m-%d"), {}).get(kind)
        if o is None or d not in df.index:
            return False
        if kind == "gen":
            return _fp(df.loc[d]) == o
        return pd.notna(df.at[d, "load"]) and round(float(df.at[d, "load"]), 0) == o

    if cfg.get("generation", True) and "cov" in raw:
        cols = [c for c in GEN_COLS if c in raw]
        for d in raw.index[raw["cov"].fillna(0) >= min_cov]:
            cur = df.at[d, "cov"] if d in df.index else np.nan
            if pd.notna(cur) and cur >= min_cov and not mine(d, "gen"):
                continue                                    # dobra doba z Energy-Charts / ENTSO-E
            if d not in df.index:
                df = df.reindex(df.index.union([d]))
            df.loc[d, cols] = raw.loc[d, cols].to_numpy()
            gen_days.append(d)
            new_owned.setdefault(d.strftime("%Y-%m-%d"), {})["gen"] = _fp(df.loc[d])
    if cfg.get("load", True) and "load" in raw:
        df = df.sort_index()
        ok = raw["load"].notna() & (raw.get("load_cov", pd.Series(1.0, index=raw.index)).fillna(0) >= min_cov)
        for d in raw.index[ok]:
            if d in df.index and pd.notna(df.at[d, "load"]) and not mine(d, "load"):
                continue
            hist = df["load"].loc[d - pd.Timedelta(days=28):d - pd.Timedelta(days=1)].dropna()
            if len(hist) >= 7 and abs(raw.at[d, "load"] / hist.median() - 1) > load_tolerance:
                if mine(d, "load"):
                    df.at[d, "load"] = np.nan               # wcześniej wpisana wartość okazała się podejrzana
                continue                                    # podejrzana doba SCADA – lepiej luka niż błąd
            if d not in df.index:
                df = df.reindex(df.index.union([d])).sort_index()
            df.at[d, "load"] = raw.at[d, "load"]
            load_days.append(d)
            new_owned.setdefault(d.strftime("%Y-%m-%d"), {})["load"] = round(float(raw.at[d, "load"]), 0)
    if zone_prices is not None and not zone_prices.empty:
        for d in sorted(set(gen_days) | set(load_days)):
            if d in zone_prices.index:
                for c in PRICE_FILL:
                    if pd.isna(df.at[d, c]):
                        df.at[d, c] = zone_prices.at[d, c]
    df.index.name = "date"
    return df.sort_index(), gen_days, load_days, new_owned


def update(settings: dict, log: list, client: Client | None = None) -> dict:
    """Pobiera ostatnie doby i historię od cfg["from"]; uzupełnia data/daily. Zwraca {kod: (doby produkcji, zużycia)}."""
    conf = settings.get("energinet") or {}
    if not conf:
        return {}
    client = client or Client()
    min_cov = settings["min_day_coverage"]
    tz_of = {c["code"]: c["tz"] for c in store.load_countries()}
    state = store.read_state()
    res = {}
    for code, cfg in conf.items():
        tz = tz_of.get(code, "Europe/Copenhagen")
        areas = cfg.get("areas", ["DK1", "DK2"])
        st = state.setdefault(code, {}).setdefault("energinet", {})
        raw = read_raw(code)
        yesterday = dt.datetime.now(ZoneInfo(tz)).date() - dt.timedelta(days=1)
        frm = dt.date.fromisoformat(cfg["from"])
        weekly = dt.date.today().weekday() == settings["weekly_refetch_weekday"]
        back = settings["refetch_days_weekly"] if weekly else settings["refetch_days"]
        ranges = [(max(frm, yesterday - dt.timedelta(days=back)), yesterday)]
        have = raw.index.min().date() if not raw.empty else ranges[0][0]
        if have > frm:
            ranges.append((frm, have - dt.timedelta(days=1)))
        for a0, b0 in ranges:
            cur_b = b0
            while cur_b >= a0:
                a = max(a0, cur_b - dt.timedelta(days=CHUNK_DAYS - 1))
                try:
                    new = fetch_days(client, areas, tz, a, cur_b, min_cov)
                except Exception as e:
                    log.append(f"{code}: błąd Energinet {a}..{cur_b}: {type(e).__name__}: {e}"[:300])
                    st["last_error"] = log[-1]
                    break
                raw = merge_raw(raw, new)
                write_raw(code, raw)
                st.pop("last_error", None)
                cur_b = a - dt.timedelta(days=1)
        df = store.read_daily(code)
        df2, gen_days, load_days, owned = apply(df, raw, cfg, min_cov, cfg.get("load_tolerance", 0.25),
                                                _zone_prices(cfg.get("price_zones", areas)), st.get("owned"))
        store.write_daily(code, df2)
        rejected = [d for d in raw.index[raw.index >= pd.Timestamp(cfg["from"])]
                    if pd.notna(raw.at[d, "load"]) and (raw.at[d, "load_cov"] if "load_cov" in raw else 1) >= min_cov
                    and d not in load_days and pd.isna(df2.at[d, "load"] if d in df2.index else np.nan)]
        st.update({"owned": owned, "from": cfg["from"], "gen_days": len(gen_days), "load_days": len(load_days),
                   "last": raw.index.max().strftime("%Y-%m-%d") if not raw.empty else None,
                   "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")})
        fmt = lambda ds: ", ".join(d.strftime("%d.%m") for d in ds[-8:])   # noqa: E731
        log.append(f"{code}: Energinet (uzupełnienie od {cfg['from']}) – produkcja {len(gen_days)} dób"
                   + (f" ({fmt(gen_days)})" if gen_days else "")
                   + f", zużycie {len(load_days)} dób" + (f" ({fmt(load_days)})" if load_days else "")
                   + (f"; zużycie odrzucone jako podejrzane: {fmt(rejected)}" if rejected else ""))
        res[code] = (len(gen_days), len(load_days))
        store.write_state(state)
    return res
