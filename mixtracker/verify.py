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

from . import model, trends

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
             refit: int = 7, n_years: int = 5, min_years: int = 3, code: str | None = None,
             leads: int = 7, opts: dict | None = None) -> dict | None:
    """Test wsteczny modelu na `days` dniach kończących się w `end` (włącznie).

    Dla dnia t i wyprzedzenia k (0 = prognoza na dzień wydania) prognoza jest wydawana w dniu
    t − k: znane są dane do dnia t − k − 1, model jest dopasowany najpóźniej w dniu wydania
    (ponowne dopasowanie co `refit` dni), a pogoda jest rzeczywista. Wynik główny: k = 0.
    """
    opts = opts or {}
    if feat.empty or e.empty or kind not in e:
        return None
    y = e[kind].dropna()
    if y.empty or y.tail(60).abs().sum() == 0:
        return None
    end = pd.Timestamp(end)
    bad = model.bad_days(y, feat, kind, code) if opts.get("gaps", True) else pd.DatetimeIndex([])
    yc = y.drop(bad, errors="ignore")
    test_days = pd.date_range(end - pd.Timedelta(days=days - 1), end, freq="D")
    first_issue = test_days[0] - pd.Timedelta(days=leads - 1)
    fit_dates = list(pd.date_range(first_issue, end, freq=f"{refit}D"))
    fits = {}
    for fd in fit_dates:
        fits[fd] = model.fit(feat, y, kind, fd, code, bad=bad, **opts)
    # szybka ścieżka (to samo co model.forecast): część pogodowa i błędy liczone raz dla każdego dopasowania
    rng = pd.date_range(first_issue - pd.Timedelta(days=model.MAX_LAG + 7), end, freq="D")
    fr = feat.reindex(rng)
    okf = fr[model.NEED[kind]].notna().all(axis=1).to_numpy()
    # zużycie: model na dobę 24-godzinną, wynik przeliczany na faktyczną długość doby (P-20261026-B2)
    hf = pd.Series(model.hours_factor(rng, code) if kind == "load" else np.ones(len(rng)), index=rng)
    pre = {}
    for fd, m in fits.items():
        if m is None:
            continue
        b = model.base(m, fr.fillna(0)).to_numpy()
        b = np.where(okf, b, np.nan)
        r = pd.Series(yc.reindex(rng).to_numpy() / hf.to_numpy() - b, index=rng)
        pre[fd] = (m, pd.Series(b, index=rng), r)
    known = yc.index.sort_values()
    preds: dict[int, dict] = {k: {} for k in range(leads)}
    for t in test_days:
        if t not in yc.index:
            continue
        for k in range(leads):
            issue = t - pd.Timedelta(days=k)
            fd = max((d for d in fit_dates if d <= issue), default=None)
            if fd not in pre:
                continue
            m, b, r = pre[fd]
            bt = b.get(t)
            if bt is None or np.isnan(bt):
                continue
            p = bt
            if m.rho.any():
                i = known.searchsorted(issue) - 1
                if i >= 0:
                    last = known[i]
                    z = r.loc[last - pd.Timedelta(days=m.ar_days - 1):last].dropna()
                    h = (t - last).days
                    if len(z) and 1 <= h <= model.MAX_LAG:
                        p = bt + m.rho[h - 1] * float(z.mean())
            preds[k][t] = max(0.0, float(p) * float(hf.get(t, 1.0)))
    if len(preds[0]) < 10:
        return None
    f = pd.Series(preds[0])
    act = yc.reindex(f.index)
    pers = yc.shift(1, freq="D").reindex(f.index)            # wartość z dnia przed wydaniem
    clim = climatology(e[[kind]], list(f.index), kind, n_years, min_years)
    out = summarize(f, act, pers, clim)
    if out is None:
        return None
    by_lead = {}
    for k in range(leads):
        fk = pd.Series(preds[k], dtype=float)
        if len(fk) < 10:
            continue
        pk = yc.shift(k + 1, freq="D").reindex(fk.index)       # persystencja: ostatni znany dzień
        sc = scores(fk, yc.reindex(fk.index), {"pers": pk, "clim": clim.reindex(fk.index)})
        if sc:
            by_lead[str(k)] = sc
    out["leads"] = by_lead
    out["gaps"] = int(((bad >= test_days[0]) & (bad <= end)).sum())
    raw = pd.DataFrame({"y": yc.reindex(test_days), "clim": clim.reindex(test_days)}, index=test_days)
    for k in range(leads):
        raw[f"f{k}"] = pd.Series(preds[k], dtype=float).reindex(test_days)
        raw[f"p{k}"] = yc.shift(k + 1, freq="D").reindex(test_days)
    out["_raw"] = raw
    return out


def aggregate(parts: list[pd.DataFrame], leads: int = 7) -> dict | None:
    """Wynik dla sumy krajów (UE-27). Dzień liczy się, gdy wartość jest dla wszystkich krajów."""
    if len(parts) < 2:
        return None
    # kraj bez klimatologii (krótka historia, np. CY) - w sumie jego udział zastępuje persystencja
    parts = [p.assign(clim=p["clim"].fillna(p["p0"])) for p in parts]
    tot = pd.concat(parts).groupby(level=0).sum(min_count=len(parts))
    if tot[["f0", "y"]].dropna().shape[0] < 10:
        return None
    r = summarize(tot["f0"].dropna(), tot["y"], tot["p0"], tot["clim"])
    if not r:
        return None
    r["leads"] = {}
    for k in range(leads):
        if f"f{k}" not in tot:
            continue
        sc = scores(tot[f"f{k}"], tot["y"], {"pers": tot[f"p{k}"], "clim": tot["clim"]})
        if sc:
            r["leads"][str(k)] = sc
    r["countries"] = len(parts)
    return r


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


def operational(archive: pd.DataFrame, actual: dict[str, pd.DataFrame], days_back: int = 90,
                members: set[str] | None = None, feats: dict | None = None) -> dict:
    """Weryfikacja zapisanych prognoz wg wyprzedzenia. actual[code] - ramka z kolumnami wind/solar/load (GWh).

    UE łącznie liczona jest z sumy krajów (prognoz i wartości rzeczywistych), dla par
    (dzień wydania, dzień prognozy), w których brakuje wartości rzeczywistej najwyżej 2 krajów;
    sumy tylko z krajów rozliczonych.
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
        y = y.drop(model.bad_days(y, (feats or {}).get(code), kind, code), errors="ignore")   # luki w danych źródła
        g = g.assign(d=pd.to_datetime(g["date"]), i=pd.to_datetime(g["issued"]))
        g["y"] = g["d"].map(y)
        # persystencja: ostatni znany dzień w chwili wydania (dzień przed wydaniem)
        g["pers"] = (g["i"] - pd.Timedelta(days=1)).map(y)
        if members is None or code in members:
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
        # P-20261024-B2: dzień UE liczy się, gdy brakuje wartości rzeczywistej najwyżej 2 krajów; prognoza
        # i rzeczywistość sumowane tylko z krajów, które mają wartość rzeczywistą (sumy porównywalne)
        df = df.dropna(subset=["y"])
        if df.empty:
            continue
        grp = df.groupby(["issued", "date", "lead"])
        n_ok = grp["y"].count()
        tot = grp[["gwh", "y", "pers"]].sum(min_count=1)
        tot.loc[grp["pers"].count() < n_ok, "pers"] = np.nan
        need = len(parts) - 2 if len(parts) > 5 else len(parts)   # przy kilku krajach - komplet
        tot = tot[n_ok >= need].reset_index()
        if tot.empty:
            continue
        leads = _lead_scores(tot)
        if leads:
            out.setdefault("EU", {})[kind] = {"leads": leads, "first": tot["issued"].min(), "last": tot["issued"].max(),
                                              "issues": int(tot["issued"].nunique()), "countries": len(parts)}
    return out


def verify_all(feats: dict[str, pd.DataFrame], derived: dict[str, pd.DataFrame], archive: pd.DataFrame | None,
               ref: dt.date, days: int = 90, n_years: int = 5, min_years: int = 3,
               members: set[str] | None = None) -> dict:
    end = pd.Timestamp(ref)
    bt: dict = {}
    raw: dict = {}
    for code, e in derived.items():
        feat = feats.get(code)
        if feat is None or feat.empty:
            continue
        for kind in KINDS:
            try:
                r = backtest(feat, e, kind, end, days, n_years=n_years, min_years=min_years, code=code)
            except Exception:   # weryfikacja jest dodatkiem - błąd jednego kraju nie zatrzymuje raportu
                r = None
            if r:
                raw[(code, kind)] = r.pop("_raw")
                bt.setdefault(code, {})[kind] = r
    # UE-27 łącznie: suma krajów członkowskich
    for kind in KINDS:
        parts = [v for (c, k), v in raw.items() if k == kind and (members is None or c in members)]
        r = aggregate(parts)
        if r:
            bt.setdefault("EU", {})[kind] = r
    op = operational(archive, derived, members=members, feats=feats) if archive is not None else {}
    return {"backtest": bt, "operational": op, "days": days, "ref": ref.isoformat()}
