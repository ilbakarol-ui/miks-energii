"""Weryfikacja prognoz wiatru, słońca i zużycia.

Dwa poziomy:
1. Test wsteczny modelu (backtest): dla każdego dnia z ostatnich `days` dni model jest
   dopasowany wyłącznie do danych sprzed tego dnia (okno jak w prognozie operacyjnej,
   ponowne dopasowanie co `refit` dni) i dostaje rzeczywistą pogodę z tego dnia (reanaliza
   ERA5 / analiza Open-Meteo). Mierzy błąd samego modelu przeliczającego pogodę na energię.
2. Weryfikacja operacyjna: prognozy zapisane w data/forecast/archive.csv w chwili ich
   wydania, porównane z produkcją/zużyciem, które potem nastąpiły, osobno dla każdego
   wyprzedzenia (0 = prognoza na dzień wydania, 1 = na następny dzień itd.). Obejmuje
   też błąd prognozy pogody.

Miary (y - wartość rzeczywista, f - prognoza, sumy po dniach okresu):
- błąd (WAPE) = Σ|f − y| / Σy · 100%
- odchylenie systematyczne (bias) = (Σf / Σy − 1) · 100%
- umiejętność (skill) względem metody odniesienia R = (1 − Σ|f − y| / Σ|R − y|) · 100%;
  > 0 - model lepszy od odniesienia, 0 - tak samo, < 0 - gorszy.
Metody odniesienia: persystencja (wartość z ostatniego znanego dnia, czyli z dnia przed
wydaniem prognozy) i klimatologia (średnia z tych samych dni kalendarza ±3 dni z 5 lat).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import trends, weather

KINDS = ["wind", "solar", "load"]


def _r(v, nd=1):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), nd)


def scores(f: pd.Series, y: pd.Series, refs: dict[str, pd.Series] | None = None) -> dict | None:
    """WAPE, bias i umiejętność względem metod odniesienia dla wspólnych dni."""
    df = pd.DataFrame({"f": f, "y": y}).dropna()
    if len(df) < 3 or df["y"].sum() <= 0:
        return None
    ae = (df["f"] - df["y"]).abs().sum()
    out = {"n": int(len(df)), "wape": _r(100 * ae / df["y"].sum()),
           "bias": _r(100 * (df["f"].sum() / df["y"].sum() - 1))}
    for name, r in (refs or {}).items():
        d2 = df.join(r.rename("r"), how="inner").dropna()
        if len(d2) >= 3:
            ar = (d2["r"] - d2["y"]).abs().sum()
            am = (d2["f"] - d2["y"]).abs().sum()
            out["skill_" + name] = _r(100 * (1 - am / ar)) if ar > 0 else None
            out["wape_" + name] = _r(100 * ar / d2["y"].sum())
    return out


def climatology(e: pd.DataFrame, dates: list[pd.Timestamp], key: str, n_years: int, min_years: int) -> pd.Series:
    band = trends.calendar_band(e, [d.strftime("%Y-%m-%d") for d in dates], key, n_years, min_years)
    return pd.Series(band["avg"], index=dates, dtype=float)


def backtest(feat: pd.DataFrame, e: pd.DataFrame, kind: str, end: pd.Timestamp, days: int = 90,
             refit: int = 7, n_years: int = 5, min_years: int = 3) -> dict | None:
    """Test wsteczny jednego modelu na `days` dniach kończących się w `end` (włącznie)."""
    if feat.empty or e.empty or kind not in e:
        return None
    y = e[kind].dropna()
    if y.empty or y.tail(60).abs().sum() == 0:
        return None
    test_days = pd.date_range(end - pd.Timedelta(days=days - 1), end, freq="D")
    preds = {}
    coef, fitted_at = None, None
    for t in test_days:
        if t not in feat.index or t not in y.index:
            continue
        if coef is None or (t - fitted_at).days >= refit:
            coef = weather.fit(feat, y, kind, t)
            fitted_at = t
        if coef is None:
            continue
        row = feat.loc[[t], weather.FEATURES].astype(float)
        if row.isna().any(axis=1).iloc[0]:
            continue
        preds[t] = float(weather.predict(coef, row, kind).iloc[0])
    if len(preds) < 10:
        return None
    f = pd.Series(preds)
    act = y.reindex(f.index)
    pers = y.shift(1, freq="D").reindex(f.index)            # wartość z poprzedniego dnia
    clim = climatology(e[[kind]], list(f.index), kind, n_years, min_years)
    out = summarize(f, act, pers, clim)
    if out is not None:
        out["_raw"] = pd.DataFrame({"f": f, "y": act, "pers": pers, "clim": clim})
    return out


def summarize(f: pd.Series, act: pd.Series, pers: pd.Series, clim: pd.Series) -> dict | None:
    sc = scores(f, act, {"pers": pers, "clim": clim})
    if not sc:
        return None
    last = f.index[-60:]
    sc["series"] = {"dates": [d.strftime("%Y-%m-%d") for d in last],
                    "act": [_r(v, 2) for v in act.reindex(last)],
                    "fc": [_r(v, 2) for v in f.reindex(last)],
                    "clim": [_r(v, 2) for v in clim.reindex(last)]}
    # błąd w kolejnych tygodniach (stabilność modelu)
    wk = pd.DataFrame({"ae": (f - act).abs(), "y": act}).dropna().resample("W-SUN").sum()
    sc["weekly"] = {"weeks": [d.strftime("%Y-%m-%d") for d in wk.index],
                    "wape": [_r(100 * a / b) if b > 0 else None for a, b in zip(wk["ae"], wk["y"])]}
    sc["period"] = [f.index[0].strftime("%Y-%m-%d"), f.index[-1].strftime("%Y-%m-%d")]
    return sc


def _lead_scores(g: pd.DataFrame) -> dict:
    leads = {}
    for lead, gl in g.groupby("lead"):
        idx = pd.RangeIndex(len(gl))
        sc = scores(pd.Series(gl["gwh"].to_numpy(), index=idx), pd.Series(gl["y"].to_numpy(), index=idx),
                    {"pers": pd.Series(gl["pers"].to_numpy(), index=idx)})
        if sc:
            leads[str(int(lead))] = sc
    return leads


def operational(archive: pd.DataFrame, actual: dict[str, pd.DataFrame], days_back: int = 90) -> dict:
    """Weryfikacja zapisanych prognoz wg wyprzedzenia. actual[code] - ramka z kolumnami wind/solar/load (GWh).

    UE łącznie liczona jest z sumy krajów (prognoz i wartości rzeczywistych), tylko dla par
    (dzień wydania, dzień prognozy), w których rozliczono wszystkie kraje danej wielkości.
    """
    if archive is None or archive.empty:
        return {}
    a = archive.copy()
    a["lead"] = (pd.to_datetime(a["date"]) - pd.to_datetime(a["issued"])).dt.days
    a = a[(a["lead"] >= 0) & (a["lead"] <= 6) & (a["code"] != "EU")]
    cutoff = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    a = a[a["issued"] >= cutoff]
    out: dict = {}
    eu: dict[str, list] = {}
    for (code, kind), g in a.groupby(["code", "type"]):
        e = actual.get(code)
        if e is None or kind not in e:
            continue
        y = e[kind].dropna()
        g = g.assign(d=pd.to_datetime(g["date"]), i=pd.to_datetime(g["issued"]))
        g["y"] = g["d"].map(y)
        # persystencja: ostatni znany dzień w chwili wydania (dzień przed wydaniem)
        g["pers"] = (g["i"] - pd.Timedelta(days=1)).map(y)
        eu.setdefault(kind, []).append(g[["issued", "date", "lead", "gwh", "y", "pers"]])
        g = g.dropna(subset=["y"])
        if g.empty:
            continue
        leads = _lead_scores(g)
        if leads:
            out.setdefault(code, {})[kind] = {
                "leads": leads, "first": g["issued"].min(), "last": g["issued"].max(), "issues": int(g["issued"].nunique())}
    for kind, parts in eu.items():
        if len(parts) < 2:
            continue
        df = pd.concat(parts)
        grp = df.groupby(["issued", "date", "lead"])
        n_ok = grp["y"].count()
        tot = grp[["gwh", "y", "pers"]].sum(min_count=1)
        tot.loc[grp["pers"].count() < len(parts), "pers"] = np.nan
        tot = tot[n_ok == len(parts)].reset_index()
        if tot.empty:
            continue
        leads = _lead_scores(tot)
        if leads:
            out.setdefault("EU", {})[kind] = {"leads": leads, "first": tot["issued"].min(), "last": tot["issued"].max(),
                                              "issues": int(tot["issued"].nunique()), "countries": len(parts)}
    return out


def verify_all(feats: dict[str, pd.DataFrame], derived: dict[str, pd.DataFrame], archive: pd.DataFrame | None,
               ref: dt.date, days: int = 90, n_years: int = 5, min_years: int = 3) -> dict:
    end = pd.Timestamp(ref)
    bt: dict = {}
    raw: dict = {}
    for code, e in derived.items():
        feat = feats.get(code)
        if feat is None or feat.empty:
            continue
        for kind in KINDS:
            try:
                r = backtest(feat, e, kind, end, days, n_years=n_years, min_years=min_years)
            except Exception:   # weryfikacja jest dodatkiem - błąd jednego kraju nie zatrzymuje raportu
                r = None
            if r:
                raw[(code, kind)] = r.pop("_raw")
                bt.setdefault(code, {})[kind] = r
    # UE łącznie: suma prognoz i wartości krajów, tylko dni, w których są wszystkie kraje danej wielkości
    for kind in KINDS:
        parts = [v for (c, k), v in raw.items() if k == kind]
        if len(parts) < 2:
            continue
        # kraj bez klimatologii (krótka historia, np. CY) - w sumie UE jego udział zastępuje persystencja
        parts = [p.assign(clim=p["clim"].fillna(p["pers"])) for p in parts]
        df = pd.concat(parts, keys=range(len(parts)))
        full = df.dropna(subset=["f", "y"]).groupby(level=1).size() == len(parts)
        tot = df.groupby(level=1).sum(min_count=len(parts))
        tot = tot[full.reindex(tot.index, fill_value=False)]
        if len(tot) >= 10:
            r = summarize(tot["f"], tot["y"], tot["pers"], tot["clim"])
            if r:
                r["countries"] = len(parts)
                bt.setdefault("EU", {})[kind] = r
    op = operational(archive, derived) if archive is not None else {}
    return {"backtest": bt, "operational": op, "days": days, "ref": ref.isoformat()}
