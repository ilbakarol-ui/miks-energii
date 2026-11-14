"""Zakładka „Pogoda”: najdłuższe dostępne prognozy wiatru i temperatury ze wspólnej bazy pogodowej.

Źródło: data/shared_weather (workflow „Wspólna pogoda dla dashboardów”, Open-Meteo, CC BY 4.0) – niczego
tu nie pobieramy, tylko czytamy pliki z repozytorium.

- Wiatr (GWh/dobę): produkcja rzeczywista z ostatnich 14 dni, prognoza modelu z zakładki „Prognozy wiatru”
  (7 dni, z przedłużeniem do 14 dni, gdy jest) i dłuższa prognoza z 51 wariantów ECMWF ensemble (ok. 12 dni). Warianty podają prędkość wiatru
  na 100 m; na produkcję przelicza ją zależność liniowa dopasowana do produkcji rzeczywistej z ostatnich
  dni (ensemble_hist.csv – historia rośnie z każdym przebiegiem). Kraj bez wiarygodnego dopasowania
  (za mało dni albo R² < min_r2) nie dostaje pasma. Dla UE prędkość jest średnią krajów ważoną produkcją
  z wiatru w ostatnich 365 dniach. Norma: średnia z tych samych dni kalendarza w poprzednich 5 latach.
- Temperatura (°C): średnia punktów kraju (dla UE – punkty sieci gazowej, gęściej tam, gdzie większe
  zużycie gazu; dla krajów – od U-20261114 także punkty próbne EX_, gdy mają pełną historię ERA5 i prognozę), prognoza deterministyczna ECMWF IFS (ok. 14 dni) i zakres wariantów ensemble.
  Norma dzienna: średnia z ostatnich pełnych lat (ERA5), wygładzona ±15 dni.
- SEAS5 (miesięcznie, do 6 miesięcy): odchylenie średniej miesięcznej każdego z 51 wariantów od normy
  miesięcznej tych samych punktów. Surowy model, bez korekty błędu systematycznego.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import sensitivity, store, trends

FOLDER = store.DATA / "shared_weather"
DEFAULTS = {"calib_days": 120, "min_calib_days": 14, "min_r2": 0.6, "norm_years": 7, "recent_days": 14,
            "seas_months": 6, "seas_min_days": 20}


def _r(v, nd=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


def _read_csv(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_csv(path) if path.exists() else None
    except Exception:
        return None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def load(folder: Path | None = None) -> dict:
    f = folder or FOLDER
    W = {
        "points": _read_csv(f / "points.csv"),
        "recent": _read_csv(f / "temp_recent.csv"),
        "hist": _read_csv(f / "temp_hist.csv"),
        "ens": _read_csv(f / "ensemble_power.csv.gz"),
        "ens_hist": _read_csv(f / "ensemble_hist.csv"),
        "seas": _read_csv(f / "seasonal.csv.gz"),
        "meta": _read_json(f / "meta.json"),
        "meta_extra": _read_json(f / "meta_extra.json"),
        "meta_evening": _read_json(f / "meta_evening.json"),
    }
    W["trial_ready"] = trial_ready(W)
    return W


# ---------------- wiatr ----------------

def wind_weights(derived: dict, codes, days: int = 365) -> pd.Series:
    """Udział krajów w produkcji z wiatru w ostatnich `days` dniach (wagi do średniej prędkości wiatru UE)."""
    w = {}
    for c in codes:
        e = derived.get(c)
        if e is not None and "wind" in e:
            s = e["wind"].dropna().tail(days)
            if len(s) and s.sum() > 0:
                w[c] = float(s.sum())
    w = pd.Series(w, dtype=float)
    return w / w.sum() if len(w) and w.sum() > 0 else w


def wind_capacity(codes, derived: dict, path: Path | None = None, cf_lo: float = 0.12, cf_hi: float = 0.5) -> dict:
    """Moc farm wiatrowych (GW, ląd + morze, ostatni rok z danymi) z Energy-Charts /installed_power
    (data/storage/ec_installed.csv). Gdy współczynnik wykorzystania mocy z produkcji 365 dni wychodzi poza
    [cf_lo, cf_hi] (dane mocy niepełne, np. IE 1,9 GW przy 12 TWh), moc szacowana z produkcji przy medianie
    współczynnika krajów z wiarygodną mocą. Wynik: {kod: {"gw", "year", "src", "cf"}}."""
    path = path or (store.DATA / "storage" / "ec_installed.csv")
    cap = {}
    try:
        ec = pd.read_csv(path) if path.exists() else pd.DataFrame()
    except Exception:
        ec = pd.DataFrame()
    if not ec.empty:
        w = ec[ec["series"].isin(["wind_on_gw", "wind_off_gw"])]
        for code, g in w.groupby("code"):
            y = int(g["year"].max())
            cap[code] = (float(g[g["year"] == y]["value"].sum()), y)
    gen = {}
    for c in codes:
        e = derived.get(c)
        if e is not None and "wind" in e:
            s_ = e["wind"].dropna().tail(365)
            if len(s_) >= 300 and s_.sum() > 0:
                gen[c] = float(s_.mean() * 365)            # GWh/rok
    out, cfs = {}, []
    for c in codes:
        if c in cap and cap[c][0] > 0 and c in gen:
            cf = gen[c] / (cap[c][0] * 8760)
            out[c] = {"gw": round(cap[c][0], 2), "year": cap[c][1], "src": "Energy-Charts", "cf": round(cf, 3)}
            if cf_lo <= cf <= cf_hi:
                cfs.append(cf)
    med = float(np.median(cfs)) if cfs else 0.25
    for c in codes:
        if c in gen and (c not in out or not (cf_lo <= out[c]["cf"] <= cf_hi)):
            out[c] = {"gw": round(gen[c] / (med * 8760), 2), "year": None,
                      "src": "szacunek z produkcji" + (f" (Energy-Charts: {out[c]['gw']} GW)" if c in out else ""),
                      "cf": round(med, 3)}
    return out


def capacity_weights(cap: dict, codes) -> pd.Series:
    w = pd.Series({c: cap[c]["gw"] for c in codes if c in cap and cap[c]["gw"] > 0}, dtype=float)
    return w / w.sum() if len(w) and w.sum() > 0 else w


def _weighted(df: pd.DataFrame, keys: list[str], col: str, weights: pd.Series, min_cover: float = 0.9) -> pd.Series:
    """Średnia ważona `col` po krajach (kolumna code) w grupach `keys`; grupy z pokryciem wag < min_cover pomijane."""
    d = df[df["code"].isin(weights.index)].dropna(subset=[col]).copy()
    if d.empty:
        return pd.Series(dtype=float)
    d["w"] = d["code"].map(weights)
    d["x"] = d[col] * d["w"]
    g = d.groupby(keys).agg(x=("x", "sum"), w=("w", "sum"))
    g = g[g["w"] >= min_cover * weights.sum()]
    return g["x"] / g["w"]


def ens_ws(ens: pd.DataFrame, code: str, weights: pd.Series | None = None) -> pd.DataFrame:
    """Prędkość wiatru wariantów ensemble: indeks = data, kolumny = warianty."""
    if ens is None or ens.empty:
        return pd.DataFrame()
    if code == "EU":
        if weights is None or weights.empty:
            return pd.DataFrame()
        s = _weighted(ens, ["date", "member"], "ws", weights)
        return s.unstack() if len(s) else pd.DataFrame()
    d = ens[ens["code"] == code].dropna(subset=["ws"])
    return d.pivot_table(index="date", columns="member", values="ws") if len(d) else pd.DataFrame()


def hist_ws(eh: pd.DataFrame, code: str, weights: pd.Series | None = None) -> pd.Series:
    if eh is None or eh.empty:
        return pd.Series(dtype=float)
    if code == "EU":
        return _weighted(eh, ["date"], "ws", weights) if weights is not None and len(weights) else pd.Series(dtype=float)
    d = eh[eh["code"] == code].dropna(subset=["ws"])
    return d.set_index("date")["ws"]


def calibrate(ws: pd.Series, actual: pd.Series, days: int, min_n: int, min_r2: float) -> dict:
    """Produkcja ~ a + b·prędkość wiatru (ostatnie `days` dni z obiema wartościami)."""
    if ws is None or ws.empty or actual is None or actual.dropna().empty:
        return {"ok": False, "n": 0, "why": "brak historii"}
    y = actual.dropna().copy()
    y.index = pd.to_datetime(y.index).strftime("%Y-%m-%d")
    m = pd.concat([ws.rename("ws"), y.rename("y")], axis=1, join="inner").dropna().sort_index().tail(days)
    n = len(m)
    if n < min_n:
        return {"ok": False, "n": n, "why": f"za mało dni ({n} z {min_n})"}
    b, a = np.polyfit(m["ws"], m["y"], 1)
    pred = a + b * m["ws"]
    ss = float(((m["y"] - m["y"].mean()) ** 2).sum())
    r2 = 1 - float(((m["y"] - pred) ** 2).sum()) / ss if ss > 0 else 0.0
    mae = float((m["y"] - pred).abs().mean())
    res = {"n": n, "r2": round(r2, 2), "a": float(a), "b": float(b),
           "mae_pct": _r(100 * mae / m["y"].mean(), 0) if m["y"].mean() > 0 else None}
    if b <= 0 or r2 < min_r2:
        return {**res, "ok": False, "why": f"słabe dopasowanie (R² {r2:.2f})"}
    return {**res, "ok": True}


def wind_block(code: str, e: pd.DataFrame | None, fc: dict | None, ws_m: pd.DataFrame, ws_h: pd.Series,
               ref: dt.date, n_years: int, min_years: int, conf: dict) -> dict | None:
    act = e["wind"].dropna() if e is not None and "wind" in e else pd.Series(dtype=float)
    act.index = pd.to_datetime(act.index)
    act = act[act.index <= pd.Timestamp(ref)].tail(conf["recent_days"])
    rdates = [d.strftime("%Y-%m-%d") for d in act.index]
    det = dict(zip(fc["days"], fc["wind"])) if fc and fc.get("wind") else {}
    x = (fc or {}).get("ext") or {}
    if det and x.get("wind"):   # przedłużenie modelu (dni 8–14)
        det.update({d: v for d, v in zip(x["days"], x["wind"]) if v is not None})
    cal = calibrate(ws_h, e["wind"] if e is not None and "wind" in e else None,
                    conf["calib_days"], conf["min_calib_days"], conf["min_r2"])
    q = {}
    if cal["ok"] and not ws_m.empty:
        fut = ws_m[ws_m.index > ref.isoformat()]
        g = (cal["a"] + cal["b"] * fut).clip(lower=0)
        q = {d: (row.quantile(0.1), row.quantile(0.5), row.quantile(0.9)) for d, row in g.iterrows() if row.notna().sum() >= 10}
    fdates = sorted(d for d in set(det) | set(q) if d > ref.isoformat())
    if not fdates and not rdates:
        return None
    dates = rdates + fdates
    band = trends.calendar_band(e, dates, "wind", n_years, min_years) if e is not None and "wind" in e else {"avg": [None] * len(dates)}
    nr = len(rdates)
    return {
        "dates": dates, "split": nr,
        "act": [_r(v) for v in act.values] + [None] * len(fdates),
        "det": [None] * nr + [_r(det.get(d)) for d in fdates],
        "p10": [None] * nr + [_r(q[d][0]) if d in q else None for d in fdates],
        "p50": [None] * nr + [_r(q[d][1]) if d in q else None for d in fdates],
        "p90": [None] * nr + [_r(q[d][2]) if d in q else None for d in fdates],
        "avg": band["avg"],
        "fit": {k: cal.get(k) for k in ("ok", "n", "r2", "mae_pct", "why")},
    }


# ---------------- temperatura ----------------

TRIAL_GROUP = "eu_gas_x"


def _complete_dates(df: pd.DataFrame | None, pts) -> set:
    """Daty, dla których wszystkie punkty `pts` mają wartość temperatury."""
    if df is None or df.empty or not len(pts):
        return set()
    d = df[df["pt"].isin(pts)].dropna(subset=["temp"])
    n = d.groupby("date")["pt"].nunique()
    return set(n[n == len(set(pts))].index)


def trial_ready(W: dict, min_years: int = 5) -> set:
    """U-20261114: punkty próbne (grupa eu_gas_x, pt EX_…) gotowe do średniej temperatury kraju – mają ERA5 dla
    każdego dnia, który mają wszystkie punkty podstawowe kraju (co najmniej `min_years` lat), i bieżące dni oraz
    prognozę dla tych samych dat. Dopóki historia się dociąga, punkt jest pomijany, więc średnia kraju nie zmienia
    się w połowie serii (norma, wrażliwość i prognoza liczone z tego samego zestawu punktów)."""
    pts, hist, rec = W.get("points"), W.get("hist"), W.get("recent")
    if pts is None or hist is None or rec is None or "group" not in pts:
        return set()
    has_temp = pts["uses"].fillna("").str.split(";").apply(lambda u: "temp" in u)
    core, trial = pts[(pts["group"] == "eu_gas") & has_temp], pts[(pts["group"] == TRIAL_GROUP) & has_temp]
    ready = set()
    for cc, g in trial.groupby("cc"):
        base = list(core.loc[core["cc"] == cc, "pt"])
        if not base:
            continue
        need_h = _complete_dates(hist, base)
        if len(need_h) < 360 * min_years:
            continue
        need_r = {fc: _complete_dates(rec[rec["fc"] == fc], base) for fc in (0, 1)}
        for pt in g["pt"]:
            if need_h <= _complete_dates(hist, [pt]) and all(
                    need_r[fc] <= _complete_dates(rec[rec["fc"] == fc], [pt]) for fc in (0, 1)):
                ready.add(pt)
    return ready


def _pts_for(points: pd.DataFrame, code: str, use: str, ready: set | None = None) -> pd.DataFrame:
    """Punkty kraju: sieć gazowa (eu_gas); dla temperatury kraju (nie UE) także gotowe punkty próbne EX_ (U-20261114).
    Średnia UE zostaje przy punktach eu_gas – punkty próbne zagęszczają tylko część krajów i zmieniłyby wagi krajów."""
    g = points["group"]
    m = (g == "eu_gas")
    if ready and use == "temp" and code != "EU":
        m = m | ((g == TRIAL_GROUP) & points["pt"].isin(ready))
    p = points[m]
    p = p[p["uses"].fillna("").str.split(";").apply(lambda u: use in u)]
    return p if code == "EU" else p[p["cc"] == code]


def _mean_pts(df: pd.DataFrame, pts: pd.DataFrame, keys: list[str], col: str, weights: dict | None = None) -> pd.Series:
    """Średnia (ważona) po punktach; tylko grupy z kompletem punktów."""
    d = df[df["pt"].isin(pts["pt"])].dropna(subset=[col]).copy()
    if d.empty:
        return pd.Series(dtype=float)
    d["w"] = d["pt"].map(weights).fillna(0) if weights else 1.0
    d["x"] = d[col] * d["w"]
    g = d.groupby(keys).agg(x=("x", "sum"), w=("w", "sum"), n=("pt", "nunique"))
    g = g[(g["n"] == pts["pt"].nunique()) & (g["w"] > 0)]
    return g["x"] / g["w"]


def _md_index(dates) -> np.ndarray:
    """Dzień roku w roku nieprzestępnym (29 lutego -> 28 lutego), 0..364."""
    d = pd.to_datetime(pd.Series(dates))
    d = d.where(~((d.dt.month == 2) & (d.dt.day == 29)), d - pd.Timedelta(days=1))
    return np.array([dt.date(2001, x.month, x.day).timetuple().tm_yday - 1 for x in d])


def norm_years(hist: pd.DataFrame, pts: pd.DataFrame, ref: dt.date, years: int) -> list[int]:
    if hist is None or hist.empty:
        return []
    d = hist[hist["pt"].isin(pts["pt"])]
    yrs = pd.to_datetime(d["date"]).dt.year
    cnt = d.groupby(yrs)["date"].nunique()
    full = [int(y) for y, n in cnt.items() if n >= 360 and y < ref.year]
    return sorted(full)[-years:]


def daily_norm(hist: pd.DataFrame, pts: pd.DataFrame, yrs: list[int], weights: dict | None = None) -> np.ndarray | None:
    """Norma dzienna (365 wartości), wygładzona średnią kroczącą ±15 dni (cyklicznie)."""
    if not yrs:
        return None
    h = hist[pd.to_datetime(hist["date"]).dt.year.isin(yrs)]
    s = _mean_pts(h, pts, ["date"], "temp", weights)
    if s.empty:
        return None
    clim = pd.Series(s.values).groupby(_md_index(s.index)).mean().reindex(range(365)).interpolate(limit_direction="both")
    ext = pd.concat([clim.iloc[-15:], clim, clim.iloc[:15]], ignore_index=True)
    return ext.rolling(31, center=True).mean().iloc[15:-15].to_numpy()


def temp_block(code: str, W: dict, ref: dt.date, conf: dict) -> dict | None:
    pts, rec = W.get("points"), W.get("recent")
    if pts is None or rec is None:
        return None
    p = _pts_for(pts, code, "temp", W.get("trial_ready"))
    if p.empty:
        return None
    s = rec[rec["pt"].isin(p["pt"])]
    act = _mean_pts(s[s["fc"] == 0], p, ["date"], "temp").sort_index().tail(conf["recent_days"])
    det = _mean_pts(s[s["fc"] == 1], p, ["date"], "temp").sort_index()
    q = {}
    ens = W.get("ens")
    if ens is not None and not ens.empty:
        if code == "EU":
            wc = p[p["uses"].str.contains("ens")]["cc"].value_counts()
            t = _weighted(ens, ["date", "member"], "t", wc / wc.sum())
            t = t.unstack() if len(t) else pd.DataFrame()
        else:
            d = ens[ens["code"] == code].dropna(subset=["t"])
            t = d.pivot_table(index="date", columns="member", values="t") if len(d) else pd.DataFrame()
        start = det.index.min() if len(det) else (ref + dt.timedelta(days=1)).isoformat()
        for d, row in t[t.index >= start].iterrows() if len(t) else []:
            if row.notna().sum() >= 10:
                q[d] = (row.quantile(0.1), row.quantile(0.5), row.quantile(0.9))
    rdates = list(act.index)
    fdates = sorted(d for d in set(det.index) | set(q) if not rdates or d > rdates[-1])
    if not rdates and not fdates:
        return None
    dates = rdates + fdates
    yrs = norm_years(W.get("hist"), p, ref, conf["norm_years"])
    nd = daily_norm(W["hist"], p, yrs) if yrs else None
    nr = len(rdates)
    return {
        "dates": dates, "split": nr,
        "act": [_r(v) for v in act.values] + [None] * len(fdates),
        "det": [None] * nr + [_r(det.get(d)) for d in fdates],
        "p10": [None] * nr + [_r(q[d][0]) if d in q else None for d in fdates],
        "p50": [None] * nr + [_r(q[d][1]) if d in q else None for d in fdates],
        "p90": [None] * nr + [_r(q[d][2]) if d in q else None for d in fdates],
        "norm": [_r(nd[i]) for i in _md_index(dates)] if nd is not None else [None] * len(dates),
        "norm_years": f"{yrs[0]}–{yrs[-1]}" if yrs else None,
        "points": int(p["pt"].nunique()),
        "trial_points": int((p["group"] == TRIAL_GROUP).sum()),
    }


def seas_block(code: str, W: dict, ref: dt.date, conf: dict) -> dict | None:
    pts, seas, hist = W.get("points"), W.get("seas"), W.get("hist")
    if pts is None or seas is None or seas.empty or hist is None:
        return None
    p = _pts_for(pts, code, "seas")
    if p.empty:
        return None
    weights = None
    if code == "EU":   # jeden punkt SEAS5 na kraj, waga = liczba punktów temperatury kraju
        wc = _pts_for(pts, "EU", "temp")["cc"].value_counts()
        weights = {r.pt: float(wc.get(r.cc, 1)) for r in p.itertuples()}
    s = seas[seas["pt"].isin(p["pt"])]
    daily = _mean_pts(s, p, ["member", "date"], "temp", weights)
    if daily.empty:
        return None
    df = daily.rename("t").reset_index()
    df["m"] = df["date"].str[:7]
    first = (ref.replace(day=1) + dt.timedelta(days=32)).replace(day=1).isoformat()[:7]   # od następnego miesiąca
    cnt = df.groupby("m")["date"].nunique()
    months = [m for m in sorted(cnt.index) if m >= first and cnt[m] >= conf["seas_min_days"]][: conf["seas_months"]]
    yrs = norm_years(hist, p, ref, conf["norm_years"])
    if not months or not yrs:
        return None
    h = hist[pd.to_datetime(hist["date"]).dt.year.isin(yrs)]
    hn = _mean_pts(h, p, ["date"], "temp", weights)
    mon_norm = hn.groupby(pd.to_datetime(hn.index).month).mean()
    out = []
    for m in months:
        a = df[df["m"] == m].groupby("member")["t"].mean() - mon_norm.get(int(m[5:]), np.nan)
        a = a.dropna()
        if len(a) < 10:
            continue
        out.append({"m": m, "norm": _r(mon_norm.get(int(m[5:]))), "p10": _r(a.quantile(0.1)), "p50": _r(a.quantile(0.5)),
                    "p90": _r(a.quantile(0.9)), "warm": _r(100 * (a > 0).mean(), 0), "n": int(len(a))})
    return {"months": out, "norm_years": f"{yrs[0]}–{yrs[-1]}"} if out else None


# ---------------- wpływ temperatury na zużycie gazu i prądu ----------------

def country_temp(W: dict, p: pd.DataFrame) -> pd.Series:
    """Średnia dobowa temperatura punktów `p`: ERA5 (temp_hist) uzupełnione o ostatnie dni (temp_recent, fc=0)."""
    parts = []
    if W.get("hist") is not None:
        parts.append(_mean_pts(W["hist"], p, ["date"], "temp"))
    r = W.get("recent")
    if r is not None:
        parts.append(_mean_pts(r[r["fc"] == 0], p, ["date"], "temp"))
    parts = [x for x in parts if len(x)]
    if not parts:
        return pd.Series(dtype=float)
    t = pd.concat(parts)
    t = t[~t.index.duplicated(keep="last")].sort_index()
    t.index = pd.to_datetime(t.index)
    return t


def _norm_degree_days(t: pd.Series, yrs: list[int]) -> tuple[np.ndarray, np.ndarray] | None:
    """Norma dzienna HDD i CDD (365 wartości): średnia stopniodni z tych samych dni w latach `yrs`, wygładzona ±15 dni."""
    h = t[t.index.year.isin(yrs)]
    if h.empty:
        return None
    out = []
    for f in (sensitivity.hdd, sensitivity.cdd):
        v = pd.Series(f(h.values)).groupby(_md_index(h.index)).mean().reindex(range(365)).interpolate(limit_direction="both")
        ext = pd.concat([v.iloc[-15:], v, v.iloc[:15]], ignore_index=True)
        out.append(ext.rolling(31, center=True).mean().iloc[15:-15].to_numpy())
    return out[0], out[1]


def _q(m: np.ndarray):
    return (float(np.quantile(m, 0.1)), float(np.quantile(m, 0.5)), float(np.quantile(m, 0.9)))


def impact_country(code: str, W: dict, coef: dict, ref: dt.date, conf: dict) -> dict | None:
    """Odchylenie zużycia gazu i prądu od normy (GWh/d) wynikające z temperatury: 14 dni wstecz, prognoza ECMWF,
    pasmo 80% wariantów ensemble (przesuniętych o średnią różnicę z prognozą w dniach 1–3)."""
    p = _pts_for(W["points"], code, "temp", W.get("trial_ready"))
    if p.empty or not coef:
        return None
    t_all = country_temp(W, p)
    yrs = norm_years(W.get("hist"), p, ref, conf["norm_years"])
    nd = _norm_degree_days(t_all, yrs) if yrs else None
    tn = daily_norm(W["hist"], p, yrs) if yrs else None
    if nd is None or tn is None:
        return None
    nh, nc = nd
    rec = W["recent"][W["recent"]["pt"].isin(p["pt"])]
    act = _mean_pts(rec[rec["fc"] == 0], p, ["date"], "temp").sort_index().tail(conf["recent_days"])
    det = _mean_pts(rec[rec["fc"] == 1], p, ["date"], "temp").sort_index()
    det = det[det.index > (act.index.max() if len(act) else "")]
    dates = list(act.index) + list(det.index)
    if not dates:
        return None
    T = np.array(list(act.values) + list(det.values), dtype=float)
    idx = _md_index(dates)
    g, pw = coef.get("gas"), coef.get("power")

    def eff(temp, i):   # (gaz, prąd) GWh/d względem normy dla temperatury `temp` w dniu o indeksie normy i
        dh = sensitivity.hdd(temp) - nh[i]
        dc = sensitivity.cdd(temp) - nc[i]
        return (g["b"] * dh if g else None, (pw["bh"] * dh + pw["bc"] * dc) if pw else None)

    gas_v, pow_v = zip(*[eff(T[k], idx[k]) for k in range(len(dates))])
    # ensemble: warianty temperatury kraju, przesunięte do prognozy deterministycznej
    members = {}
    ens = W.get("ens")
    if ens is not None and not ens.empty and len(det):
        d = ens[ens["code"] == code].dropna(subset=["t"])
        m = d.pivot_table(index="date", columns="member", values="t") if len(d) else pd.DataFrame()
        m = m[m.index.isin(det.index)]
        if len(m) >= 3:
            first = [x for x in det.index[:3] if x in m.index]
            shift = float(np.mean([det[x] - m.loc[x].median() for x in first])) if first else 0.0
            pos = {x: k for k, x in enumerate(dates)}
            for x, row in m.iterrows():
                v = row.dropna().to_numpy() + shift
                if len(v) >= 10:
                    k = pos[x]
                    ge, pe = eff(v, idx[k])
                    members[x] = {"gas": ge, "power": pe}
    nr = len(act)

    def series(kind, vals):
        if vals[0] is None:
            return None
        q = {x: _q(members[x][kind]) for x in members if members[x][kind] is not None}
        return {"act": [_r(v, 0) for v in vals[:nr]] + [None] * (len(dates) - nr),
                "det": [None] * nr + [_r(v, 0) for v in vals[nr:]],
                "p10": [_r(q[x][0], 0) if x in q else None for x in dates],
                "p50": [_r(q[x][1], 0) if x in q else None for x in dates],
                "p90": [_r(q[x][2], 0) if x in q else None for x in dates]}
    out = {"dates": dates, "split": nr,
           "tanom": [_r(T[k] - tn[idx[k]]) for k in range(len(dates))],
           "_members": members, "_gas": list(gas_v), "_power": list(pow_v)}
    if g:
        out["gas"] = series("gas", gas_v)
        out["gas"]["norm"] = [_r(g["a"] + g["b"] * nh[i], 0) for i in idx]
    if pw:
        out["power"] = series("power", pow_v)
        out["power"]["norm"] = [_r(pw["a"] + pw["bh"] * nh[i] + pw["bc"] * nc[i], 0) for i in idx]
    return out


def impact_eu(blocks: dict, coefs: dict) -> dict | None:
    """UE = suma krajów (każdy kraj ze swoją wrażliwością). Temperatura UE ważona wrażliwością gazu i prądu."""
    codes = [c for c, b in blocks.items() if b]
    if not codes:
        return None
    common = sorted(set.intersection(*[set(blocks[c]["dates"]) for c in codes]))
    if not common:
        return None
    b0 = blocks[codes[0]]
    last_act = b0["dates"][b0["split"] - 1] if b0["split"] else ""
    split = sum(1 for x in common if x <= last_act)
    out = {"dates": common, "split": split, "countries": len(codes)}
    for kind, key in (("gas", "b"), ("power", "bh")):
        cs = [c for c in codes if kind in blocks[c] and blocks[c][kind]]
        if not cs:
            continue
        pos = {c: {x: k for k, x in enumerate(blocks[c]["dates"])} for c in cs}
        tot = [sum(blocks[c]["_" + kind][pos[c][x]] for c in cs) for x in common]
        norm = [sum(blocks[c][kind]["norm"][pos[c][x]] or 0 for c in cs) for x in common]
        w = {c: coefs[c][kind][key] for c in cs}
        tw = [sum(w[c] * (blocks[c]["tanom"][pos[c][x]] or 0) for c in cs) / sum(w.values()) for x in common]
        q = {}
        for x in common:
            ms = [blocks[c]["_members"].get(x, {}).get(kind) for c in cs]
            if all(m is not None for m in ms) and len({len(m) for m in ms}) == 1:
                q[x] = _q(np.sum(ms, axis=0))
        out[kind] = {"act": [_r(v, 0) for v in tot[:split]] + [None] * (len(common) - split),
                     "det": [None] * split + [_r(v, 0) for v in tot[split:]],
                     "p10": [_r(q[x][0], 0) if x in q else None for x in common],
                     "p50": [_r(q[x][1], 0) if x in q else None for x in common],
                     "p90": [_r(q[x][2], 0) if x in q else None for x in common],
                     "norm": [_r(v, 0) for v in norm], "countries": len(cs)}
        out["tw_" + kind] = [_r(v) for v in tw]
    return out


def impact_seas(code: str, W: dict, coef: dict, ref: dt.date, conf: dict) -> dict | None:
    """SEAS5: odchylenie miesięcznego zużycia gazu i prądu (GWh w miesiącu) od normy – warianty × wrażliwość.
    Zwraca {"months": {m: {"gas": array, "power": array}}} (warianty, do sumy UE)."""
    if not coef or W.get("seas") is None or W["seas"].empty:
        return None
    p = _pts_for(W["points"], code, "seas")
    if p.empty:
        return None
    s = W["seas"][W["seas"]["pt"].isin(p["pt"])]
    daily = _mean_pts(s, p, ["member", "date"], "temp")
    if daily.empty:
        return None
    df = daily.rename("t").reset_index()
    df["m"] = df["date"].str[:7]
    df["hdd"] = sensitivity.hdd(df["t"])
    df["cdd"] = sensitivity.cdd(df["t"])
    first = (ref.replace(day=1) + dt.timedelta(days=32)).replace(day=1).isoformat()[:7]
    cnt = df.groupby("m")["date"].nunique()
    months = [m for m in sorted(cnt.index) if m >= first and cnt[m] >= conf["seas_min_days"]][: conf["seas_months"]]
    t_hist = country_temp(W, p)
    yrs = norm_years(W.get("hist"), p, ref, conf["norm_years"])
    if not months or not yrs or t_hist.empty:
        return None
    h = t_hist[t_hist.index.year.isin(yrs)]
    mh = pd.Series(sensitivity.hdd(h.values), index=h.index).groupby(h.index.month).mean()
    mc = pd.Series(sensitivity.cdd(h.values), index=h.index).groupby(h.index.month).mean()
    g, pw = coef.get("gas"), coef.get("power")
    out = {}
    for m in months:
        mm = df[df["m"] == m].groupby("member")[["hdd", "cdd"]].mean()
        if len(mm) < 10:
            continue
        days = pd.Period(m).days_in_month
        dh = mm["hdd"].to_numpy() - mh.get(int(m[5:]), np.nan)
        dc = mm["cdd"].to_numpy() - mc.get(int(m[5:]), np.nan)
        rec = {"members": mm.index.tolist()}
        if g:
            rec["gas"] = g["b"] * dh * days
            rec["gas_norm"] = (g["a"] + g["b"] * mh.get(int(m[5:]), np.nan)) * days
        if pw:
            rec["power"] = (pw["bh"] * dh + pw["bc"] * dc) * days
            rec["power_norm"] = (pw["a"] + pw["bh"] * mh.get(int(m[5:]), np.nan) + pw["bc"] * mc.get(int(m[5:]), np.nan)) * days
        out[m] = rec
    return out or None


def _seas_out(months: dict) -> list[dict]:
    res = []
    for m in sorted(months):
        r = {"m": m}
        for kind in ("gas", "power"):
            v = months[m].get(kind)
            if v is None or not len(v) or np.isnan(v).all():
                continue
            v = v[~np.isnan(v)] / 1000      # TWh w miesiącu
            nrm = months[m].get(kind + "_norm")
            r[kind] = {"p10": _r(np.quantile(v, 0.1), 2), "p50": _r(np.quantile(v, 0.5), 2), "p90": _r(np.quantile(v, 0.9), 2),
                       "norm": _r(float(nrm) / 1000, 1) if nrm is not None and np.isfinite(nrm) else None}
        if len(r) > 1:
            res.append(r)
    return res


def seas_eu(per: dict) -> list[dict]:
    """Suma krajów per wariant (warianty SEAS5 0–50 są wspólne dla wszystkich punktów)."""
    months = sorted(set.intersection(*[set(v) for v in per.values()])) if per else []
    out = {}
    for m in months:
        rec = {}
        for kind in ("gas", "power"):
            parts = [(per[c][m]["members"], per[c][m][kind], per[c][m].get(kind + "_norm")) for c in per if kind in per[c][m]]
            if not parts:
                continue
            common = sorted(set.intersection(*[set(x[0]) for x in parts]))
            if len(common) < 10:
                continue
            tot = np.zeros(len(common))
            for mem, v, _ in parts:
                pos = {x: k for k, x in enumerate(mem)}
                tot += np.array([v[pos[x]] for x in common])
            rec[kind] = tot
            rec[kind + "_norm"] = float(np.nansum([x[2] for x in parts]))
        if rec:
            out[m] = rec
    return _seas_out(out)


def strip_private(block: dict | None) -> dict | None:
    return {k: v for k, v in block.items() if not k.startswith("_")} if block else None


# ---------------- całość ----------------

def build(derived: dict, fcs: dict, codes: list[str], ref: dt.date, n_years: int, min_years: int,
          settings: dict | None = None, folder: Path | None = None) -> dict:
    conf = {**DEFAULTS, **((settings or {}).get("weather_tab") or {})}
    W = load(folder)
    if W["points"] is None:
        return {}
    ens, eh = W["ens"], W["ens_hist"]
    ens_codes = sorted(set(ens["code"])) if ens is not None and not ens.empty else []
    # B/12.11.2026: prędkość wiatru UE ważona mocą farm wiatrowych (zamiast udziału w produkcji)
    cap = wind_capacity(ens_codes, derived)
    weights = capacity_weights(cap, ens_codes)
    if weights.empty:
        weights = wind_weights(derived, ens_codes)
    # wrażliwość na temperaturę: gaz (Eurostat, miesięcznie) i prąd (zużycie dzienne)
    gas_m = sensitivity.read_gas()
    coefs, imp, seas_imp = {}, {}, {}
    for code in codes:
        if code == "EU":
            continue
        try:
            p = _pts_for(W["points"], code, "temp", W.get("trial_ready"))
            if p.empty:
                continue
            t = country_temp(W, p)
            e = derived.get(code)
            c = {"gas": sensitivity.fit_gas(gas_m[gas_m["code"] == code].set_index("month")["gwh"], t),
                 "power": sensitivity.fit_power(e["load"], t, ref) if e is not None and "load" in e else None}
            c = {k: v for k, v in c.items() if v}
            if not c:
                continue
            coefs[code] = c
            imp[code] = impact_country(code, W, c, ref, conf)
            si = impact_seas(code, W, c, ref, conf)
            if si:
                seas_imp[code] = si
        except Exception as ex:   # jeden kraj nie może zatrzymać zakładki
            coefs.setdefault(code, {})["error"] = f"{type(ex).__name__}: {ex}"
    eu_imp = impact_eu({k: v for k, v in imp.items() if v}, coefs)
    out = {}
    for code in codes:
        rec = {}
        try:
            if code == "EU":
                if eu_imp:
                    rec["impact"] = eu_imp
                if seas_imp:
                    se = seas_eu(seas_imp)
                    if se:
                        rec["impact_seas"] = se
            else:
                if imp.get(code):
                    rec["impact"] = strip_private(imp[code])
                if seas_imp.get(code):
                    rec["impact_seas"] = _seas_out(seas_imp[code])
            ws_m = ens_ws(ens, code, weights) if (code in ens_codes or code == "EU") else pd.DataFrame()
            ws_h = hist_ws(eh, code, weights) if (code in ens_codes or code == "EU") else pd.Series(dtype=float)
            wb = wind_block(code, derived.get(code), (fcs or {}).get(code), ws_m, ws_h, ref, n_years, min_years, conf)
            if wb:
                rec["wind"] = wb
            tb = temp_block(code, W, ref, conf)
            if tb:
                rec["temp"] = tb
            sb = seas_block(code, W, ref, conf)
            if sb:
                rec["seas"] = sb
        except Exception as e:   # zakładka pomocnicza – błąd jednego kraju nie może zatrzymać budowy strony
            rec["error"] = f"{type(e).__name__}: {e}"
        if rec:
            out[code] = rec
    m, mx, me = W["meta"], W["meta_extra"], W["meta_evening"]
    seas_fetched = str(W["seas"]["fetched"].max()) if W["seas"] is not None and "fetched" in W["seas"] else None
    ens_issued = max([x for x in (mx.get("ensemble_issued"), me.get("ensemble_issued")) if x] or [None]) if (mx or me) else None
    det_issued = max([x for x in (m.get("forecast_issued"), me.get("forecast_issued")) if x] or [None]) if (m or me) else None
    return {"countries": out,
            "trial_points": sorted(W.get("trial_ready") or []),
            "issued": {"forecast": det_issued, "ensemble": ens_issued, "seasonal": seas_fetched,
                       "updated": max([x for x in (m.get("updated"), mx.get("updated"), me.get("updated")) if x] or [None])},
            "wind_weights": {k: _r(v, 3) for k, v in weights.items()},
            "wind_capacity": cap,
            "sensitivity": {k: v for k, v in coefs.items() if v},
            "gas_source": {"last_month": str(gas_m["month"].max()) if len(gas_m) else None,
                           "since": sensitivity.GAS_SINCE}}
