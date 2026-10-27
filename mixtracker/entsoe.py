"""ENTSO-E Transparency Platform – zastępowanie wybranych serii Energy-Charts (U-20261027).

Po co: Energy-Charts przestało podawać zużycie Irlandii Północnej (kod UK, od 24.10.2025),
a dla Holandii od jesieni 2026 r. podaje zaniżone zużycie, słońce i „inne” (README, Ograniczenia).
Ten moduł pobiera dla krajów z `settings["entsoe"]` dane wprost z ENTSO-E i nakłada je na
data/daily/<KOD>.csv od podanej daty:

- "load": Actual Total Load (dokument A65, proces A16) -> kolumna load,
- "generation": Actual Generation per Production Type (A75, A16) -> kolumny produkcji, cov
  i storage_charge (pompowanie / ładowanie magazynów); ceny i saldo wymiany zostają z Energy-Charts.

Surowe doby z ENTSO-E są zapisywane osobno w data/entsoe/<KOD>.csv, a nakładanie odbywa się
po każdym pobraniu z Energy-Charts (które co noc nadpisuje ostatnie 10/45 dni). Doba ENTSO-E
z pokryciem < min_day_coverage nie zastępuje danych Energy-Charts.

Wymaga darmowego tokenu ENTSO-E w zmiennej środowiskowej ENTSOE_TOKEN (sekret repozytorium).
Bez tokenu moduł nic nie robi (jedna linia w logu), więc łatka jest bezpieczna przed jego dodaniem.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import store
from .categories import DETAIL_KEYS, classify
from .energycharts import to_daily

BASE = "https://web-api.tp.entsoe.eu/api"
FOLDER = store.DATA / "entsoe"
CHUNK_DAYS = 30           # A75 dla jednego kraju: ok. 30 dni na zapytanie to bezpieczny rozmiar odpowiedzi

# kody typów produkcji ENTSO-E -> nazwy jak w Energy-Charts (klasyfikuje je categories.classify)
PSR = {
    "B01": "Biomass", "B02": "Fossil Brown coal/Lignite", "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas", "B05": "Fossil Hard coal", "B06": "Fossil Oil", "B07": "Fossil Oil shale",
    "B08": "Fossil Peat", "B09": "Geothermal", "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and poundage", "B12": "Hydro Water Reservoir", "B13": "Marine",
    "B14": "Nuclear", "B15": "Other renewable", "B16": "Solar", "B17": "Waste",
    "B18": "Wind Offshore", "B19": "Wind Onshore", "B20": "Other", "B25": "Energy storage",
}
RES = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}


class EntsoeError(RuntimeError):
    pass


def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for ch in el:
        if _strip(ch.tag) == name:
            return ch
    return None


def _text(el, path: str):
    cur = el
    for p in path.split("/"):
        cur = _find(cur, p) if cur is not None else None
    return cur.text.strip() if cur is not None and cur.text else None


def parse_xml(text: str) -> list[dict]:
    """Szeregi czasowe z dokumentu ENTSO-E: [{psr, direction, series(MW, indeks UTC)}].

    direction: "in" (produkcja / obciążenie w strefie) albo "out" (zużycie: pompowanie, ładowanie).
    Krzywa A03: brakujące pozycje oznaczają tę samą wartość co poprzednia - uzupełniane w przód.
    Dokument "Acknowledgement" (np. kod 999 – brak danych) -> pusta lista.
    """
    root = ET.fromstring(text)
    if "Acknowledgement" in _strip(root.tag):
        reason = _text(root, "Reason/text") or ""
        if "No matching data" in reason or _text(root, "Reason/code") == "999":
            return []
        raise EntsoeError(f"ENTSO-E: {reason[:200]}")
    out = []
    for ts in root:
        if _strip(ts.tag) != "TimeSeries":
            continue
        psr = _text(ts, "MktPSRType/psrType")
        direction = "out" if _find(ts, "outBiddingZone_Domain.mRID") is not None and \
            _find(ts, "inBiddingZone_Domain.mRID") is None else "in"
        curve = _text(ts, "curveType") or "A01"
        parts = []
        for per in ts:
            if _strip(per.tag) != "Period":
                continue
            start = pd.Timestamp(_text(per, "timeInterval/start"))
            end = pd.Timestamp(_text(per, "timeInterval/end"))
            step = RES.get(_text(per, "resolution") or "", None)
            if step is None:
                continue
            n = int((end - start) / pd.Timedelta(minutes=step))
            vals = np.full(n, np.nan)
            for pt in per:
                if _strip(pt.tag) != "Point":
                    continue
                pos, q = _text(pt, "position"), _text(pt, "quantity")
                if pos and q is not None and 1 <= int(pos) <= n:
                    vals[int(pos) - 1] = float(q)
            if curve == "A03":
                vals = pd.Series(vals).ffill().to_numpy()
            idx = pd.date_range(start, periods=n, freq=f"{step}min")
            parts.append(pd.Series(vals, index=idx))
        if parts:
            s = pd.concat(parts)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            out.append({"psr": psr, "direction": direction, "series": s})
    return out


def to_mw(series: list[dict], kind: str) -> pd.DataFrame:
    """MW w kolumnach trackera. kind="load" -> kolumna load; "generation" -> kategorie + storage_charge."""
    cols: dict[str, pd.Series] = {}
    for s in series:
        if kind == "load":
            cat = "load"
        elif s["direction"] == "out":
            cat = "storage_charge"
        else:
            cat = classify(PSR.get(s["psr"] or "", "Other")) or "misc"
        ser = s["series"]
        cols[cat] = cols[cat].add(ser, fill_value=0) if cat in cols else ser
    if not cols:
        return pd.DataFrame()
    mw = pd.DataFrame(cols).sort_index()
    mw.index = mw.index.tz_convert("UTC") if mw.index.tz is not None else mw.index.tz_localize("UTC")
    return mw


class Client:
    def __init__(self, token: str, pause: float = 0.5, retries: int = 4, timeout: int = 120):
        self.token, self.pause, self.retries, self.timeout = token, pause, retries, timeout
        self.requests_made = 0

    def get(self, params: dict) -> str:
        last = None
        for attempt in range(self.retries):
            try:
                r = requests.get(BASE, params={"securityToken": self.token, **params}, timeout=self.timeout,
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
            if r.status_code == 401:
                raise EntsoeError("HTTP 401 – nieprawidłowy token ENTSOE_TOKEN")
            if r.status_code == 400 and "No matching data" in r.text:
                return r.text          # Acknowledgement z kodem 999 bywa zwracany jako 400
            if r.status_code != 200:
                raise EntsoeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.text
        raise EntsoeError(last or "błąd ENTSO-E")


def _period(a: dt.date, b: dt.date, tz: str) -> tuple[str, str]:
    """Doby lokalne [a, b] jako okres UTC dla API (z 1 h zapasu po obu stronach)."""
    s = pd.Timestamp(a).tz_localize(tz).tz_convert("UTC") - pd.Timedelta(hours=1)
    e = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(tz).tz_convert("UTC") + pd.Timedelta(hours=1)
    return s.strftime("%Y%m%d%H%M"), e.strftime("%Y%m%d%H%M")


def fetch_days(client: Client, domain: str, tz: str, a: dt.date, b: dt.date, kinds: list[str],
               min_cov: float = 0.8) -> pd.DataFrame:
    """Doby [a, b]: kolumny load_e (MWh, pokrycie load_cov) i/lub produkcja (cov + DETAIL_KEYS + storage_charge)."""
    ps, pe = _period(a, b, tz)
    out = pd.DataFrame(index=pd.DatetimeIndex(pd.date_range(a, b, freq="D"), name="date"))
    if "load" in kinds:
        txt = client.get({"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": domain,
                          "periodStart": ps, "periodEnd": pe})
        mw = to_mw(parse_xml(txt), "load")
        if not mw.empty:
            # to_daily liczy pokrycie z kolumn produkcji – dla samego zużycia kolumna-cień „misc”
            d = to_daily(mw.assign(misc=mw["load"]), tz, min_cov)
            out["load"] = d["load"].reindex(out.index)
            out["load_cov"] = d["cov"].reindex(out.index)
    if "generation" in kinds:
        txt = client.get({"documentType": "A75", "processType": "A16", "in_Domain": domain,
                          "periodStart": ps, "periodEnd": pe})
        mw = to_mw(parse_xml(txt), "generation")
        if not mw.empty:
            d = to_daily(mw, tz, min_cov)
            for c in ["cov"] + DETAIL_KEYS + ["storage_charge"]:
                out[c] = d[c].reindex(out.index) if c in d else np.nan
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
    """Nowe doby zastępują stare kolumna po kolumnie (puste wartości nowych nie kasują starych)."""
    if old.empty:
        return new.sort_index()
    out = old.reindex(old.index.union(new.index))
    for c in new.columns:
        if c not in out:
            out[c] = np.nan
        v = new[c].dropna()
        out.loc[v.index, c] = v
    return out.sort_index()


def apply(df: pd.DataFrame, raw: pd.DataFrame, cfg: dict, min_cov: float) -> tuple[pd.DataFrame, int, int]:
    """Nakłada doby ENTSO-E na dane Energy-Charts od cfg["from"]. Zwraca (ramka, doby zużycia, doby produkcji)."""
    if raw.empty:
        return df, 0, 0
    frm = pd.Timestamp(cfg["from"])
    raw = raw[raw.index >= frm]
    df = df.copy()
    n_load = n_gen = 0
    if cfg.get("load") and "load" in raw:
        ok = raw["load"].notna() & (raw.get("load_cov", pd.Series(1.0, index=raw.index)).fillna(0) >= min_cov)
        days = raw.index[ok]
        if len(days):
            df = df.reindex(df.index.union(days))
            df.loc[days, "load"] = raw.loc[days, "load"]
            n_load = len(days)
    if cfg.get("generation") and "cov" in raw:
        days = raw.index[raw["cov"].fillna(0) >= min_cov]
        if len(days):
            df = df.reindex(df.index.union(days))
            cols = ["cov"] + DETAIL_KEYS + ["storage_charge"]
            df.loc[days, cols] = raw.loc[days, cols].to_numpy()
            n_gen = len(days)
    df.index.name = "date"
    return df.sort_index(), n_load, n_gen


def compare(ec: pd.DataFrame, raw: pd.DataFrame, cols=("load", "solar", "misc"), days: int = 14) -> str:
    """Krótkie porównanie ENTSO-E z Energy-Charts z ostatnich `days` dób wspólnych (do logu)."""
    parts = []
    for c in cols:
        if c not in raw or c not in ec:
            continue
        both = pd.concat([ec[c], raw[c]], axis=1, keys=["ec", "en"]).dropna().tail(days)
        if len(both) and both["ec"].sum() > 0:
            parts.append(f"{c} {100 * (both['en'].sum() / both['ec'].sum() - 1):+.0f}%")
    return ", ".join(parts)


def update(settings: dict, log: list, budget_minutes: float = 15, client: Client | None = None) -> dict:
    """Pobiera brakujące i ostatnie doby z ENTSO-E i nakłada je na data/daily. Zwraca {kod: (doby zużycia, produkcji)}."""
    conf = settings.get("entsoe") or {}
    if not conf:
        return {}
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if client is None and not token:
        log.append("ENTSO-E: brak sekretu ENTSOE_TOKEN – zużycie UK i dane NL zostają z Energy-Charts")
        return {}
    client = client or Client(token)
    t_end = time.monotonic() + budget_minutes * 60
    min_cov = settings["min_day_coverage"]
    tz_of = {c["code"]: c["tz"] for c in store.load_countries()}
    state = store.read_state()
    res = {}
    for code, cfg in conf.items():
        if not cfg.get("load") and not cfg.get("generation"):
            continue
        tz = tz_of.get(code, "Europe/Brussels")
        st = state.setdefault(code, {}).setdefault("entsoe", {})
        kinds = [k for k in ("load", "generation") if cfg.get(k)]
        raw = read_raw(code)
        yesterday = dt.datetime.now(ZoneInfo(tz)).date() - dt.timedelta(days=1)
        frm = dt.date.fromisoformat(cfg["from"])
        weekly = dt.date.today().weekday() == settings["weekly_refetch_weekday"]
        back = settings["refetch_days_weekly"] if weekly else settings["refetch_days"]
        # 1) ostatnie doby (korekty źródła), 2) historia od cfg["from"] do pierwszej pobranej doby
        ranges = [(max(frm, yesterday - dt.timedelta(days=back)), yesterday)]
        have = raw.index.min().date() if not raw.empty else yesterday - dt.timedelta(days=back)
        if have > frm:
            ranges.append((frm, have - dt.timedelta(days=1)))
        for a0, b0 in ranges:
            cur_b = b0
            while cur_b >= a0 and time.monotonic() < t_end:   # od najnowszych wstecz – przerwany backfill wraca jutro
                a = max(a0, cur_b - dt.timedelta(days=CHUNK_DAYS - 1))
                try:
                    new = fetch_days(client, cfg["domain"], tz, a, cur_b, kinds, min_cov)
                except Exception as e:
                    log.append(f"{code}: błąd ENTSO-E {a}..{cur_b}: {type(e).__name__}: {e}")
                    st["last_error"] = log[-1]
                    break
                raw = merge_raw(raw, new.dropna(how="all"))
                write_raw(code, raw)
                st.pop("last_error", None)
                cur_b = a - dt.timedelta(days=1)
        df = store.read_daily(code)
        df2, n_load, n_gen = apply(df, raw, cfg, min_cov)
        store.write_daily(code, df2)
        st.update({"from": cfg["from"], "load_days": n_load, "gen_days": n_gen,
                   "last": raw.index.max().strftime("%Y-%m-%d") if not raw.empty else None,
                   "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")})
        cmp = compare(df[df.index >= pd.Timestamp(cfg["from"])], raw)
        log.append(f"{code}: ENTSO-E od {cfg['from']} – zużycie {n_load} dób, produkcja {n_gen} dób"
                   + (f"; wobec Energy-Charts (14 dób): {cmp}" if cmp else ""))
        res[code] = (n_load, n_gen)
        store.write_state(state)
    return res
