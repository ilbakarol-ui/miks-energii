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
  zużycie gazu), prognoza deterministyczna ECMWF IFS (ok. 14 dni) i zakres wariantów ensemble.
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

from . import store, trends

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
    return {
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

def _pts_for(points: pd.DataFrame, code: str, use: str) -> pd.DataFrame:
    p = points[points["group"] == "eu_gas"]
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
    p = _pts_for(pts, code, "temp")
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


# ---------------- całość ----------------

def build(derived: dict, fcs: dict, codes: list[str], ref: dt.date, n_years: int, min_years: int,
          settings: dict | None = None, folder: Path | None = None) -> dict:
    conf = {**DEFAULTS, **((settings or {}).get("weather_tab") or {})}
    W = load(folder)
    if W["points"] is None:
        return {}
    ens, eh = W["ens"], W["ens_hist"]
    ens_codes = sorted(set(ens["code"])) if ens is not None and not ens.empty else []
    weights = wind_weights(derived, ens_codes)
    out = {}
    for code in codes:
        rec = {}
        try:
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
            "issued": {"forecast": det_issued, "ensemble": ens_issued, "seasonal": seas_fetched,
                       "updated": max([x for x in (m.get("updated"), mx.get("updated"), me.get("updated")) if x] or [None])},
            "wind_weights": {k: _r(v, 3) for k, v in weights.items()}}
