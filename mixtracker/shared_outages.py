"""Niedostępność elektrowni z ENTSO-E (UMM, dokument A80) – dla dashboardu „Gaz UE” (wspólne dane jak pogoda).

Po co: gdy we Francji (i Belgii, Hiszpanii) stoją bloki jądrowe, Europa spala więcej gazu w elektrowniach.
Planowe wyłączenia są znane z wyprzedzeniem, więc to zapowiedź popytu na gaz na kolejne tygodnie.
Energy-Charts tych danych nie podaje.

Co robi (przebieg nocny, z sekretem ENTSOE_TOKEN; bez niego – jedna linia w logu):
1. pobiera komunikaty o niedostępności jednostek wytwórczych (A80) dla stref z settings["outages"]["zones"]
   w oknie [dziś − history_days, dziś + horizon_days]; po pierwszym przebiegu tylko komunikaty zmienione
   od ostatniego udanego przebiegu (periodStartUpdate/periodEndUpdate),
2. zostawia ostatnią rewizję każdego komunikatu (mRID); anulowane/wycofane (docStatus A09/A13) usuwa,
3. liczy dla każdej doby lokalnej średnią niedostępną moc (MW) wg grup paliw, osobno planowe (A53)
   i awaryjne (A54); nakładające się komunikaty tego samego bloku liczone raz (maksimum, najwyżej moc nominalna).

Pliki (data/shared_outages/):
- events.csv – segmenty komunikatów: mrid, revision, zone, business_type, unit_eic, unit_name, psr,
  nominal_mw, start, end (UTC), available_mw, published,
- daily.csv – zone, date, group, planned_mw, forced_mw, units_out (bloki ≥ 50% niedostępne średnio w dobie),
- units.json – bloki jądrowe i gazowe ≥ min_unit_mw niedostępne teraz albo w ciągu 30 dni (do listy na dashboardzie),
- meta.json – czas aktualizacji, stan stref, błędy.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from . import store
from .entsoe import BASE, EntsoeError, _find, _strip, _text

FOLDER = store.DATA / "shared_outages"
EVENT_COLS = ["mrid", "revision", "zone", "business_type", "unit_eic", "unit_name", "psr", "nominal_mw",
              "start", "end", "available_mw", "published"]
PAGE = 200                 # ENTSO-E: najwyżej 200 dokumentów w odpowiedzi; dalej parametr offset
MAX_OFFSET = 4800
CHUNK_DAYS = 365           # okres zapytania najwyżej rok
CANCELLED = {"A09", "A13"}
GROUPS = {"B14": "nuclear", "B04": "gas", "B02": "coal", "B05": "coal", "B03": "coal", "B07": "coal", "B08": "coal",
          "B06": "oil", "B10": "hydro", "B11": "hydro", "B12": "hydro"}
RES_MIN = {"PT1M": 1, "PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}
DEFAULTS = {"history_days": 400, "horizon_days": 180, "budget_minutes": 20, "min_unit_mw": 400,
            "backfill_budget_minutes": 60,
            "zones": {"FR": "10YFR-RTE------C", "BE": "10YBE----------2", "ES": "10YES-REE------0",
                      "NL": "10YNL----------L"}}
TZ = {"FR": "Europe/Paris", "BE": "Europe/Brussels", "ES": "Europe/Madrid", "NL": "Europe/Amsterdam",
      "DE": "Europe/Berlin", "CH": "Europe/Zurich", "SE": "Europe/Stockholm", "FI": "Europe/Helsinki",
      "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "HU": "Europe/Budapest", "IT": "Europe/Rome"}


def group_of(psr: str | None) -> str:
    return GROUPS.get(psr or "", "other")


def _fmt(t: pd.Timestamp) -> str:
    return t.tz_convert("UTC").strftime("%Y%m%d%H%M")


# ---------- parsowanie ----------

def _dt(date_s, time_s):
    if not date_s:
        return None
    return pd.Timestamp(f"{date_s}T{(time_s or '00:00:00Z').rstrip('Z')}", tz="UTC")


def parse_doc(text: str | bytes, zone: str) -> list[dict]:
    """Jeden dokument Unavailability_MarketDocument -> segmenty (słowniki z EVENT_COLS + doc_status)."""
    root = ET.fromstring(text)
    if _strip(root.tag).startswith("Acknowledgement"):
        return []
    mrid = _text(root, "mRID")
    rev = int(_text(root, "revisionNumber") or 1)
    status = _text(root, "docStatus/value")
    published = _text(root, "createdDateTime")
    out = []
    for ts in root:
        if _strip(ts.tag) != "TimeSeries":
            continue
        g = lambda name: _text(ts, name)
        nominal = g("production_RegisteredResource.pSRType.powerSystemResources.nominalP")
        base = dict(mrid=mrid, revision=rev, zone=zone, business_type=g("businessType"),
                    unit_eic=g("production_RegisteredResource.pSRType.powerSystemResources.mRID")
                    or g("production_RegisteredResource.mRID"),
                    unit_name=g("production_RegisteredResource.pSRType.powerSystemResources.name")
                    or g("production_RegisteredResource.name"),
                    psr=g("production_RegisteredResource.pSRType.psrType"),
                    nominal_mw=float(nominal) if nominal else np.nan, published=published, doc_status=status)
        curve = g("curveType") or "A03"
        ev_start = _dt(g("start_DateAndOrTime.date"), g("start_DateAndOrTime.time"))
        ev_end = _dt(g("end_DateAndOrTime.date"), g("end_DateAndOrTime.time"))
        n_per = 0
        for per in ts:
            if _strip(per.tag) != "Available_Period":
                continue
            n_per += 1
            a = pd.Timestamp(_text(per, "timeInterval/start"))
            b = pd.Timestamp(_text(per, "timeInterval/end"))
            a = a.tz_localize("UTC") if a.tzinfo is None else a.tz_convert("UTC")
            b = b.tz_localize("UTC") if b.tzinfo is None else b.tz_convert("UTC")
            step = pd.Timedelta(minutes=RES_MIN.get(_text(per, "resolution") or "", 60))
            pts = sorted((int(_text(p, "position")), float(_text(p, "quantity")))
                         for p in per if _strip(p.tag) == "Point" and _text(p, "position") and _text(p, "quantity"))
            for i, (pos, q) in enumerate(pts):
                s = a + (pos - 1) * step
                if curve == "A03":     # wartość obowiązuje do następnego punktu (albo końca okresu)
                    e = a + (pts[i + 1][0] - 1) * step if i + 1 < len(pts) else b
                else:
                    e = min(s + step, b)
                if e > s:
                    out.append({**base, "start": s, "end": e, "available_mw": q})
        if n_per == 0 and ev_start is not None and ev_end is not None:
            out.append({**base, "start": ev_start, "end": ev_end, "available_mw": 0.0})
    if not out:   # dokument bez okresów (np. anulowanie) – znacznik, żeby usunąć wcześniejsze rewizje
        out.append(dict(mrid=mrid, revision=rev, zone=zone, business_type=None, unit_eic=None, unit_name=None, psr=None,
                        nominal_mw=np.nan, published=published, doc_status=status, start=pd.NaT, end=pd.NaT,
                        available_mw=np.nan))
    return out


def parse_response(content: bytes, zone: str) -> tuple[list[dict], int]:
    """Odpowiedź API (ZIP z plikami XML albo pojedynczy XML) -> (segmenty, liczba dokumentów)."""
    if content[:2] == b"PK":
        rows, n = [], 0
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    n += 1
                    rows += parse_doc(z.read(name), zone)
        return rows, n
    root = ET.fromstring(content)
    tag = _strip(root.tag)
    if tag.startswith("Acknowledgement"):
        txt = " ".join(filter(None, [_text(root, "Reason/code"), _text(root, "Reason/text")]))
        if "No matching data" in txt or "999" in txt.split(" ")[:1]:
            return [], 0
        raise EntsoeError(f"ENTSO-E: {txt[:200]}")
    return parse_doc(content, zone), 1


# ---------- pobieranie ----------

class Fetcher:
    def __init__(self, token: str, pause: float = 0.5, retries: int = 4, timeout: int = 180):
        self.token, self.pause, self.retries, self.timeout = token, pause, retries, timeout
        self.requests_made = 0

    def get(self, params: dict) -> bytes:
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
            if r.status_code == 400 and b"No matching data" in r.content:
                return b'<Acknowledgement_MarketDocument><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'
            if r.status_code != 200:
                raise EntsoeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.content
        raise EntsoeError(last or "błąd ENTSO-E")


def run_queue(fx, domain: str, zone: str, queue: list, since: pd.Timestamp | None, now: pd.Timestamp,
              t_end: float) -> tuple[list[dict], list[str]]:
    """Przetwarza kolejkę okresów [[start_iso, end_iso, offset], …] (zmienia ją w miejscu – po przerwaniu
    przez limit czasu następny przebieg zaczyna od zapisanej strony). Okres z ponad MAX_OFFSET + PAGE dokumentami
    dzielony na pół (najkrócej 14 dni). Zwraca (segmenty, uwagi)."""
    rows, notes = [], []
    use_update = since is not None
    while queue:
        c0, c1, offset = pd.Timestamp(queue[0][0]), pd.Timestamp(queue[0][1]), int(queue[0][2])
        if time.monotonic() > t_end:
            notes.append("limit czasu")
            return rows, notes
        p = {"documentType": "A80", "biddingZone_domain": domain, "periodStart": _fmt(c0), "periodEnd": _fmt(c1),
             "offset": offset}
        if use_update:
            p.update(periodStartUpdate=_fmt(since), periodEndUpdate=_fmt(now))
        try:
            got, n = parse_response(fx.get(p), zone)
        except EntsoeError as e:
            if use_update and "HTTP 400" in str(e):   # serwer nie przyjął filtra aktualizacji – pełne okno
                use_update = False
                notes.append("bez filtra aktualizacji (HTTP 400)")
                continue
            raise
        rows += got
        if n < PAGE:
            queue.pop(0)
        elif offset + PAGE > MAX_OFFSET:
            item = queue.pop(0)
            tag = item[3:]                          # znacznik okresu (np. „cur”) przechodzi na połówki
            if c1 - c0 > pd.Timedelta(days=14):     # za dużo dokumentów – dzielimy okres na pół
                mid = (c0 + (c1 - c0) / 2).floor("D")
                queue[:0] = [[c0.isoformat(), mid.isoformat(), 0, *tag], [mid.isoformat(), c1.isoformat(), 0, *tag]]
            else:
                notes.append(f"ponad {MAX_OFFSET + PAGE} dokumentów w {c0.date()}–{c1.date()} – część pominięta")
        else:
            queue[0][2] = offset + PAGE
    return rows, notes


def fetch_zone(fx, domain, zone, a, b, since, now, t_end):
    """Całe okno [a, b) w kawałkach po CHUNK_DAYS (zgodność z testami). Zwraca (segmenty, komplet?, uwaga)."""
    q, cur = [], a
    while cur < b:
        nxt = min(b, cur + pd.Timedelta(days=CHUNK_DAYS))
        q.append([cur.isoformat(), nxt.isoformat(), 0])
        cur = nxt
    rows, notes = run_queue(fx, domain, zone, q, since, now, t_end)
    return rows, not q, "; ".join(notes) or None


def backfill_queue(today: dt.date, history_days: int, ahead_days: int, step_days: int = 60) -> list:
    """Kolejność pierwszego pobierania: najpierw okres bieżący i przyszły, potem historia wstecz kawałkami."""
    t = pd.Timestamp(today).tz_localize("UTC")
    q = [[(t - pd.Timedelta(days=35)).isoformat(), (t + pd.Timedelta(days=ahead_days)).isoformat(), 0, "cur"]]
    end, start = t - pd.Timedelta(days=35), t - pd.Timedelta(days=history_days)
    while end > start:
        s0 = max(start, end - pd.Timedelta(days=step_days))
        q.append([s0.isoformat(), end.isoformat(), 0])
        end = s0
    return q


# ---------- scalanie i agregacja ----------

def merge_events(old: pd.DataFrame, new_rows: list[dict]) -> pd.DataFrame:
    """Ostatnia rewizja każdego mRID; anulowane i wycofane usuwane."""
    new = pd.DataFrame(new_rows)
    if new.empty:
        return old
    for c in ("start", "end"):
        new[c] = pd.to_datetime(new[c], utc=True)
    new["revision"] = new["revision"].astype(int)
    last = new.sort_values("revision", kind="stable").groupby("mrid").tail(1).set_index("mrid")
    dead = set(last.index[last.doc_status.isin(CANCELLED)])
    new = new.drop(columns=["doc_status"])
    allr = pd.concat([old, new], ignore_index=True) if len(old) else new
    allr["revision"] = allr["revision"].astype(int)
    top = allr.groupby("mrid")["revision"].transform("max")
    allr = allr[(allr.revision == top) & ~allr.mrid.isin(dead) & allr.start.notna()]
    return allr.drop_duplicates(["mrid", "revision", "start", "end", "available_mw"]).reset_index(drop=True)[EVENT_COLS]


def daily(events: pd.DataFrame, zone: str, tz: str, a: dt.date, b: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Średnia niedostępna moc w dobach lokalnych [a, b] wg grup; drugi wynik – godzinowa niedostępność bloków."""
    ev = events[events.zone == zone].copy()
    days = pd.date_range(a, b, freq="D")
    empty = pd.DataFrame(columns=["zone", "date", "group", "planned_mw", "forced_mw", "units_out"])
    if ev.empty:
        return empty, pd.DataFrame()
    t0 = pd.Timestamp(a).tz_localize(tz).tz_convert("UTC")
    t1 = (pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(tz).tz_convert("UTC")
    hours = pd.date_range(t0, t1, freq="h", inclusive="left")
    H = len(hours)
    ev = ev[(ev.end > t0) & (ev.start < t1)]
    ev = ev.assign(nominal_mw=ev.nominal_mw.fillna(ev.groupby("unit_eic").nominal_mw.transform("max")))
    per_unit = {}
    for unit, g in ev.groupby("unit_eic"):
        nom = float(g.nominal_mw.max()) if g.nominal_mw.notna().any() else np.nan
        if not np.isfinite(nom) or nom <= 0:
            continue
        un = np.zeros(H)
        kind = np.zeros(H, dtype=np.int8)     # 0 brak, 1 planowe, 2 awaryjne
        for r in g.itertuples():
            i0 = max(0, int(np.floor((r.start - t0) / pd.Timedelta(hours=1))))
            i1 = min(H, int(np.ceil((r.end - t0) / pd.Timedelta(hours=1))))
            if i1 <= i0:
                continue
            v = min(nom, max(0.0, nom - float(r.available_mw)))
            seg = un[i0:i1]
            better = v > seg
            seg[better] = v
            kind[i0:i1][better] = 2 if r.business_type == "A54" else 1
        per_unit[unit] = (group_of(g.psr.iloc[0]), nom, un, kind, g.unit_name.iloc[0])
    local_day = hours.tz_convert(ZoneInfo(tz)).normalize().tz_localize(None)
    rows = []
    for grp in sorted({v[0] for v in per_unit.values()}):
        P, F = np.zeros(H), np.zeros(H)
        units_day = pd.Series(0, index=days)
        for gname, nom, un, kind, _ in per_unit.values():
            if gname != grp:
                continue
            P += np.where(kind == 1, un, 0)
            F += np.where(kind == 2, un, 0)
            share = pd.Series(un / nom, index=local_day).groupby(level=0).mean()
            units_day = units_day.add((share >= 0.5).astype(int).reindex(days, fill_value=0), fill_value=0)
        d = pd.DataFrame({"planned_mw": P, "forced_mw": F}, index=local_day).groupby(level=0).mean().reindex(days)
        for day, x in d.iterrows():
            rows.append(dict(zone=zone, date=day.date().isoformat(), group=grp, planned_mw=round(float(x.planned_mw), 1),
                             forced_mw=round(float(x.forced_mw), 1), units_out=int(units_day.get(day, 0))))
    hourly = pd.DataFrame({u: v[2] for u, v in per_unit.items()}, index=hours)
    return pd.DataFrame(rows), hourly


def unit_list(events: pd.DataFrame, zone: str, now: pd.Timestamp, min_mw: float, ahead_days: int = 30) -> list[dict]:
    """Bloki jądrowe i gazowe ≥ min_mw niedostępne teraz albo w ciągu ahead_days (ciągłe okresy wyłączenia)."""
    ev = events[(events.zone == zone) & events.psr.isin(["B14", "B04"]) & (events.nominal_mw >= min_mw)].copy()
    hor = now + pd.Timedelta(days=ahead_days)
    ev = ev[(ev.end > now) & (ev.start < hor) & (ev.available_mw < ev.nominal_mw * 0.95)]
    out = []
    for unit, g in ev.groupby("unit_eic"):
        g = g.sort_values("start")
        # okres obejmujący „teraz” albo najbliższy: łączymy stykające się segmenty
        spans = []
        for r in g.itertuples():
            if spans and r.start <= spans[-1]["end"] + pd.Timedelta(hours=1):
                sp = spans[-1]
                sp["end"] = max(sp["end"], r.end)
                sp["min_avail"] = min(sp["min_avail"], r.available_mw)
                sp["forced"] = sp["forced"] or r.business_type == "A54"
            else:
                spans.append(dict(start=r.start, end=r.end, min_avail=r.available_mw, forced=r.business_type == "A54"))
        sp = next((s for s in spans if s["start"] <= now < s["end"]), spans[0])
        nom = float(g.nominal_mw.max())
        out.append(dict(zone=zone, unit=str(g.unit_name.iloc[0]), psr=group_of(g.psr.iloc[0]), nominal_mw=round(nom),
                        unavail_mw=round(nom - float(sp["min_avail"])), start=sp["start"].isoformat(),
                        end=sp["end"].isoformat(), now=bool(sp["start"] <= now < sp["end"]),
                        kind="awaryjne" if sp["forced"] else "planowe"))
    return sorted(out, key=lambda x: (not x["now"], -x["unavail_mw"]))


# ---------- przebieg ----------

def _read_events() -> pd.DataFrame:
    p = FOLDER / "events.csv"
    if not p.exists():
        return pd.DataFrame(columns=EVENT_COLS)
    df = pd.read_csv(p, dtype={"mrid": str, "unit_eic": str, "business_type": str, "psr": str})
    for c in ("start", "end"):
        df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601")
    return df


def update(settings: dict, log: list, fetcher=None, now: pd.Timestamp | None = None) -> dict:
    """Pierwsze pobieranie (backfill) jest wznawiane między przebiegami: najpierw okres bieżący i przyszły
    (current_ok), potem historia (history_ok, potrzebna do porównań r/r). Potem tylko komunikaty zmienione
    od ostatniego przebiegu, a raz w tygodniu dalszy horyzont bez filtra (nowe dni na końcu okna)."""
    conf = {**DEFAULTS, **(settings.get("outages") or {})}
    if not conf.get("zones"):
        return {}
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if fetcher is None and not token:
        log.append("Niedostępność elektrowni (ENTSO-E): brak sekretu ENTSOE_TOKEN – pominięte")
        return {}
    fx = fetcher or Fetcher(token)
    now = (now or pd.Timestamp.now(tz="UTC")).floor("min")
    FOLDER.mkdir(parents=True, exist_ok=True)
    meta_p = FOLDER / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    zstate = meta.setdefault("zones", {})
    for z in list(zstate):
        if z not in conf["zones"]:
            zstate.pop(z)
    events = _read_events()
    events = events[events.zone.isin(list(conf["zones"]))] if len(events) else events
    today = now.tz_convert("Europe/Brussels").date()
    a = pd.Timestamp(today - dt.timedelta(days=conf["history_days"])).tz_localize("UTC")
    ahead = min(CHUNK_DAYS - 35, max(conf["horizon_days"] + 1, conf.get("fetch_ahead_days", 330)))
    b = pd.Timestamp(today + dt.timedelta(days=ahead)).tz_localize("UTC")
    backfilling = any(not zstate.get(z, {}).get("history_ok") for z in conf["zones"])
    budget = conf.get("backfill_budget_minutes", 60) if backfilling else conf["budget_minutes"]
    t_end = time.monotonic() + budget * 60
    res = {}
    # kolejność: strefy bez bieżących danych najpierw, potem reszta
    order = sorted(conf["zones"], key=lambda z: (bool(zstate.get(z, {}).get("current_ok")), list(conf["zones"]).index(z)))
    for zone in order:
        domain = conf["zones"][zone]
        st = zstate.setdefault(zone, {})
        try:
            if not st.get("history_ok"):
                rows, notes = [], []
                if st.get("current_ok") and st.get("upd_since"):   # w trakcie pobierania historii – zmiany bieżących komunikatów
                    ru, cu, nu = fetch_zone(fx, domain, zone, a, b, pd.Timestamp(st["upd_since"]) - pd.Timedelta(hours=2), now, t_end)
                    rows += ru
                    if cu:
                        st["upd_since"] = now.isoformat()
                q = st.get("queue")
                if not q:
                    q = backfill_queue(today, conf["history_days"], ahead)
                    st["started"] = now.isoformat()
                r, notes2 = run_queue(fx, domain, zone, q, None, now, t_end)
                rows += r
                notes += notes2
                if not st.get("current_ok") and not any(x[3:] == ["cur"] for x in q):
                    st["current_ok"] = True
                    st["upd_since"] = st.get("started", now.isoformat())
                st["queue"] = q
                if not q:
                    st["history_ok"] = True
                    st["last_ok"] = st.get("upd_since", now.isoformat())
                    for k in ("queue", "started", "upd_since"):
                        st.pop(k, None)
                complete = not q
            else:
                since = pd.Timestamp(st["last_ok"]) - pd.Timedelta(hours=2)
                rows, complete, note = fetch_zone(fx, domain, zone, a, b, since, now, t_end)
                notes = [note] if note else []
                if complete and now.weekday() == 0:   # nowe dni na końcu okna: komunikaty opublikowane dawno
                    r2, c2, n2 = fetch_zone(fx, domain, zone, b - pd.Timedelta(days=40), b, None, now, t_end)
                    rows += r2
                    complete = complete and c2
                if complete:
                    st["last_ok"] = now.isoformat()
            events = merge_events(events, rows)
            st.pop("last_error", None)
            st["note"] = "; ".join(notes) or None
            res[zone] = (len(rows), complete)
        except Exception as e:
            st["last_error"] = f"{now.isoformat()} {type(e).__name__}: {e}"[:300]
            log.append(f"Niedostępność {zone}: błąd ENTSO-E: {type(e).__name__}: {e}"[:300])
            res[zone] = (0, False)
    # porządek: bez segmentów zakończonych przed oknem historii
    if len(events):
        events = events[events.end > a - pd.Timedelta(days=30)]
        events.sort_values(["zone", "unit_eic", "start", "mrid"])[EVENT_COLS].to_csv(
            FOLDER / "events.csv", index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    d_all, units = [], []
    for zone in conf["zones"]:
        tz = TZ.get(zone, "Europe/Brussels")
        d, _ = daily(events, zone, tz, today - dt.timedelta(days=conf["history_days"]),
                     today + dt.timedelta(days=conf["horizon_days"]))
        d_all.append(d)
        units += unit_list(events, zone, now, conf["min_unit_mw"])
    dd = pd.concat([x for x in d_all if len(x)], ignore_index=True) if any(len(x) for x in d_all) else pd.DataFrame()
    if len(dd):
        dd.to_csv(FOLDER / "daily.csv", index=False)
    (FOLDER / "units.json").write_text(json.dumps(units, ensure_ascii=False, indent=0), encoding="utf-8")
    meta.update(updated_at=now.isoformat(), requests=getattr(fx, "requests_made", None),
                window=[a.date().isoformat(), (b - pd.Timedelta(days=1)).date().isoformat()],
                events=int(len(events)), zones_conf=conf["zones"])
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    cur = [z for z in conf["zones"] if zstate.get(z, {}).get("current_ok")]
    hist = [z for z in conf["zones"] if zstate.get(z, {}).get("history_ok")]
    nuc = dd[(dd.group == "nuclear") & (dd.date == today.isoformat()) & dd.zone.isin(cur)] if len(dd) else pd.DataFrame()
    now_txt = ", ".join(f"{r.zone} {((r.planned_mw + r.forced_mw) / 1000):.1f} GW" for r in nuc.itertuples())
    left = {z: len(zstate[z].get("queue") or []) for z in conf["zones"] if not zstate.get(z, {}).get("history_ok")}
    log.append(f"Niedostępność elektrowni (ENTSO-E, dla Gaz UE): bieżące {','.join(cur) or '—'}; historia {','.join(hist) or '—'}"
               + (f" (pobieranie historii trwa, zostało okresów: {left})" if left else "")
               + f"; {getattr(fx, 'requests_made', 0)} zapytań; atom dziś niedostępny: {now_txt or '—'}")
    return res
