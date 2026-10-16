"""Wskaźniki i trendy: tydzień do tygodnia, miesiąc do miesiąca, rok do roku,
na tle średniej i zakresu (min-maks) z poprzednich 5 lat.

Wszystkie wartości energii w GWh, udziały w % produkcji krajowej.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .categories import DETAIL, DETAIL_KEYS, FOSSIL, GROUP_KEYS, RENEWABLE

ENERGY_SERIES = list(dict.fromkeys(GROUP_KEYS + DETAIL_KEYS +
                                   ["gen_total", "renewable", "fossil", "low_carbon", "load", "net_import"]))
# sumowalne składniki wskaźników cenowych i emisyjnych
AUX_SUMS = ["co2", "price_sum", "price_n", "price_neg_h",
            "pw_solar", "pq_solar", "pw_wind", "pq_wind", "pw_gen", "pq_gen", "pw_load", "pq_load"]
ADDITIVE = ENERGY_SERIES + AUX_SUMS
SHARE_SERIES = ["share_" + g for g in GROUP_KEYS] + ["share_renewable", "share_fossil", "share_low_carbon"]
# wskaźniki nieaddytywne liczone z sum okresu
RATIO_SERIES = ["intensity", "price_avg", "capture_solar", "capture_wind", "capture_load", "capture_gen",
                "capture_rate_solar", "capture_rate_wind"]
EXTRA_SERIES = ["co2", "price_neg_h"]
CHART_WEEKLY = (["load", "gen_total", "net_import", "renewable", "fossil"] + GROUP_KEYS +
                ["lignite", "hard_coal", "wind_onshore", "wind_offshore", "share_renewable", "share_fossil",
                 "intensity", "co2", "price_avg", "capture_solar", "capture_wind", "price_neg_h"])
CHART_DAILY = GROUP_KEYS + ["load", "net_import", "intensity", "price_avg", "price_min", "price_max",
                            "price_neg_h", "wind", "solar"]
MIN_PRICE_FRAC = 0.8

_FACTORS_PATH = Path(__file__).resolve().parent.parent / "config" / "emission_factors.json"


def emission_factors() -> dict:
    try:
        raw = json.loads(_FACTORS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    return {k: float(raw.get(k, 0.0)) for k in DETAIL_KEYS}


def derive(daily: pd.DataFrame, min_cov: float) -> tuple[pd.DataFrame, bool]:
    """Dzienne serie pochodne: energia w GWh, emisje w kt CO2, składniki cen.
    Dni z niskim pokryciem -> NaN. Zwraca (ramka, czy zużycie jest szacowane z bilansu)."""
    if daily.empty:
        return pd.DataFrame(columns=ADDITIVE), False
    d = daily[daily["cov"] >= min_cov].copy()
    full_idx = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    d = d.reindex(full_idx)
    e = pd.DataFrame(index=full_idx)
    for k in DETAIL_KEYS:
        e[k] = d[k] / 1000.0
    for g in GROUP_KEYS:
        e[g] = e[[k for k in DETAIL_KEYS if DETAIL[k][1] == g]].sum(axis=1, min_count=1)
    e["gen_total"] = e[DETAIL_KEYS].sum(axis=1, min_count=1)
    e["renewable"] = e[RENEWABLE].sum(axis=1, min_count=1)
    e["fossil"] = e[FOSSIL].sum(axis=1, min_count=1)
    e["low_carbon"] = e["renewable"] + e["nuclear"]
    charge = d["storage_charge"].fillna(0) / 1000.0
    load = d["load"] / 1000.0
    estimated = False
    have_load = load.notna().sum()
    if have_load < 0.5 * e["gen_total"].notna().sum():
        # brak danych o obciążeniu - zużycie = produkcja - pompowanie + saldo wymiany
        xb = d["xborder"] / 1000.0
        if xb.notna().sum() > 0:
            load = e["gen_total"] - charge + xb
            estimated = True
    e["load"] = load
    e["net_import"] = e["load"] - (e["gen_total"] - charge)
    e.loc[e["gen_total"].isna(), ["load", "net_import"]] = np.nan
    # emisje: GWh x t/MWh = kt CO2
    f = emission_factors()
    e["co2"] = sum(e[k].fillna(0) * f[k] for k in DETAIL_KEYS)
    e.loc[e["gen_total"].isna(), "co2"] = np.nan
    # ceny
    pa = d["price_avg"] if "price_avg" in d else pd.Series(np.nan, index=full_idx)
    e["price_sum"] = pa
    e["price_n"] = pa.notna().astype(float).where(e["gen_total"].notna())
    e["price_min"] = d["price_min"] if "price_min" in d else np.nan
    e["price_max"] = d["price_max"] if "price_max" in d else np.nan
    e["price_neg_h"] = d["price_neg_h"] if "price_neg_h" in d else np.nan
    for k in ["solar", "wind", "gen", "load"]:
        for pre in ("pw_", "pq_"):
            col = pre + k
            e[col] = d[col] if col in d else np.nan
    return e, estimated


def ratios(x) -> dict:
    """Wskaźniki nieaddytywne z sum (działa dla Series sum okresu i DataFrame sum tygodniowych)."""
    def div(a, b, mult=1.0):
        a, b = x[a], x[b]
        if isinstance(a, pd.Series):
            return (a * mult / b.where(b > 0))
        return (a * mult / b) if (a is not None and b and not pd.isna(a) and not pd.isna(b) and b > 0) else None
    out = {
        "intensity": div("co2", "gen_total", 1000.0),           # g/kWh
        "price_avg": div("price_sum", "price_n"),               # EUR/MWh
    }
    for k in ["solar", "wind", "load", "gen"]:
        out["capture_" + k] = div("pw_" + k, "pq_" + k, 1000.0)  # EUR/MWh
    for k in ["solar", "wind"]:
        c, p = out["capture_" + k], out["price_avg"]
        if isinstance(c, pd.Series):
            out["capture_rate_" + k] = 100 * c / p.where(p.abs() > 1)
        else:
            out["capture_rate_" + k] = (100 * c / p) if (c is not None and p is not None and abs(p) > 1) else None
    return out


def _scaled_sum(block: pd.DataFrame, n_days: int, min_frac: float = 0.85) -> pd.Series | None:
    valid = block["gen_total"].notna().sum()
    if n_days == 0 or valid < math.ceil(min_frac * n_days):
        return None
    return block.sum(min_count=1) * (n_days / valid)


def period_values(e: pd.DataFrame, start: dt.date, end: dt.date, strict: bool = False) -> dict | None:
    """Sumy energii, udziały i wskaźniki dla okresu [start, end]. strict=True wymaga kompletu dni."""
    n = (end - start).days + 1
    cols = [c for c in ADDITIVE if c in e.columns]
    block = e.loc[pd.Timestamp(start):pd.Timestamp(end), cols] if not e.empty else e
    if block.empty:
        return None
    s = _scaled_sum(block, n, 1.0 if strict else 0.85)
    if s is None:
        return None
    out = {k: (None if k not in s or pd.isna(s.get(k)) else float(s[k])) for k in ADDITIVE}
    gen = out["gen_total"] or 0
    for k in GROUP_KEYS + ["renewable", "fossil", "low_carbon"]:
        out["share_" + k] = (100.0 * out[k] / gen) if gen > 0 and out[k] is not None else None
    valid = block["gen_total"].notna().sum()
    priced = block["price_n"].sum() if "price_n" in block else 0
    if valid == 0 or priced < MIN_PRICE_FRAC * valid:
        for k in ["price_sum", "price_n", "price_neg_h"] + [c for c in AUX_SUMS if c.startswith(("pw_", "pq_"))]:
            out[k] = None
    out.update({k: (None if v is None else float(v)) for k, v in ratios(out).items()})
    return out


def _shift_years(d: dt.date, years: int) -> dt.date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 lutego
        return d.replace(year=d.year + years, day=28)


def iso_week_range(year: int, week: int) -> tuple[dt.date, dt.date] | None:
    try:
        a = dt.date.fromisocalendar(year, week, 1)
    except ValueError:
        return None
    return a, a + dt.timedelta(days=6)


def month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    a = dt.date(year, month, 1)
    b = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
    return a, b


def reference_periods(ref: dt.date) -> dict:
    """Ostatni pełny tydzień (pon.-niedz.), ostatni pełny miesiąc i rok do daty."""
    week_end = ref - dt.timedelta(days=(ref.weekday() + 1) % 7)
    week_start = week_end - dt.timedelta(days=6)
    iy, iw, _ = week_start.isocalendar()
    m_end = ref if (ref + dt.timedelta(days=1)).day == 1 else ref.replace(day=1) - dt.timedelta(days=1)
    m_start = m_end.replace(day=1)
    months_pl = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień",
                 "wrzesień", "październik", "listopad", "grudzień"]
    return {
        "week": {"start": week_start, "end": week_end, "iso": (iy, iw),
                 "label": f"tydz. {iw}/{iy} ({week_start:%d.%m}–{week_end:%d.%m})",
                 "short": f"tydz. {iw}"},
        "month": {"start": m_start, "end": m_end,
                  "label": f"{months_pl[m_start.month - 1]} {m_start.year}",
                  "short": f"{months_pl[m_start.month - 1][:3]} {m_start.year}"},
        "ytd": {"start": dt.date(ref.year, 1, 1), "end": ref,
                "label": f"od 1.01 do {ref:%d.%m.%Y}", "short": f"{ref.year} do daty"},
    }


def compare(e: pd.DataFrame, kind: str, p: dict, n_years: int, min_years: int) -> dict:
    """Wartości bieżące i porównania dla wszystkich serii.

    Zwraca {seria: [bieżąca, poprzedni okres, rok wcześniej, śr. 5 lat, min 5 lat, maks 5 lat]}.
    """
    s, t = p["start"], p["end"]
    cur = period_values(e, s, t, strict=(kind != "ytd"))
    if kind == "week":
        y, w = p["iso"]
        prev = period_values(e, s - dt.timedelta(days=7), t - dt.timedelta(days=7))
        hist = []
        for k in range(1, n_years + 1):
            rng = iso_week_range(y - k, w) or iso_week_range(y - k, 52)
            hist.append(period_values(e, *rng) if rng else None)
    elif kind == "month":
        pm_end = s - dt.timedelta(days=1)
        prev = period_values(e, pm_end.replace(day=1), pm_end)
        hist = [period_values(e, *month_range(s.year - k, s.month)) for k in range(1, n_years + 1)]
    else:
        prev = None
        hist = [period_values(e, _shift_years(s, -k), _shift_years(t, -k)) for k in range(1, n_years + 1)]
    ly = hist[0]
    out = {}
    for key in ENERGY_SERIES + SHARE_SERIES + RATIO_SERIES + EXTRA_SERIES:
        vals = [h[key] for h in hist if h and h.get(key) is not None]
        stats = ([float(np.mean(vals)), float(np.min(vals)), float(np.max(vals))]
                 if len(vals) >= min_years else [None, None, None])
        out[key] = [_r(cur and cur.get(key)), _r(prev and prev.get(key)), _r(ly and ly.get(key))] + [_r(v) for v in stats]
    out["_years5"] = len([h for h in hist if h])
    out["_complete"] = cur is not None
    return out


def _r(v, nd=2):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), nd)


def weekly_profile(e: pd.DataFrame, year: int, n_years: int, min_years: int, last_week: int) -> dict:
    """Serie tygodniowe (tyg. ISO 1-53): bieżący rok, poprzedni rok, średnia/min/maks 5 lat."""
    if e.empty:
        return {}
    iso = e.index.isocalendar()
    g = e[[c for c in ADDITIVE if c in e.columns]].copy()
    g["_y"], g["_w"] = iso["year"].to_numpy(), iso["week"].to_numpy()
    cnt = g.groupby(["_y", "_w"])["gen_total"].count()
    sums = g.groupby(["_y", "_w"]).sum(min_count=1)
    ok = cnt >= 6
    sums = sums[ok].mul(7 / cnt[ok], axis=0)
    for k in ["renewable", "fossil"]:
        sums["share_" + k] = 100 * sums[k] / sums["gen_total"]
    if "price_n" in sums:
        low = sums["price_n"] < MIN_PRICE_FRAC * 7
        for c in [c for c in AUX_SUMS if c != "co2" and c in sums]:
            sums.loc[low, c] = np.nan
    for k, v in ratios(sums).items():
        sums[k] = v
    res = {}
    weeks = range(1, 54)
    for key in CHART_WEEKLY:
        if key not in sums:
            continue
        col = sums[key]

        def arr(y):
            return [_r(col.get((y, w)), 1) if (y, w) in col.index else None for w in weeks]
        cur = arr(year)
        cur = [v if w <= last_week else None for w, v in zip(weeks, cur)]
        hist = [arr(year - k) for k in range(1, n_years + 1)]
        avg, mn, mx = [], [], []
        for i in range(53):
            vals = [h[i] for h in hist if h[i] is not None]
            if len(vals) >= min_years:
                avg.append(_r(np.mean(vals), 1)); mn.append(_r(min(vals), 1)); mx.append(_r(max(vals), 1))
            else:
                avg.append(None); mn.append(None); mx.append(None)
        res[key] = {"cur": cur, "ly": hist[0], "avg": avg, "min": mn, "max": mx}
    return res


def daily_recent(e: pd.DataFrame, ref: dt.date, days: int) -> dict:
    idx = pd.date_range(pd.Timestamp(ref) - pd.Timedelta(days=days - 1), pd.Timestamp(ref), freq="D")
    b = e.reindex(idx).copy()
    b["intensity"] = 1000 * b["co2"] / b["gen_total"].where(b["gen_total"] > 0)
    b["price_avg"] = b["price_sum"]
    return {"dates": [d.strftime("%Y-%m-%d") for d in idx],
            **{k: [_r(v) for v in b[k].tolist()] for k in CHART_DAILY if k in b}}


def monthly_recent(e: pd.DataFrame, ref_month_end: dt.date, months: int) -> dict:
    out = {"months": []}
    keys = GROUP_KEYS + ["gen_total", "load", "net_import", "price_avg", "intensity", "co2",
                         "capture_solar", "capture_wind", "price_neg_h"]
    for k in keys:
        out[k] = []
    y, m = ref_month_end.year, ref_month_end.month
    seq = []
    for _ in range(months):
        seq.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    for y, m in reversed(seq):
        a, b = month_range(y, m)
        v = period_values(e, a, b)
        out["months"].append(f"{y}-{m:02d}")
        for k in keys:
            out[k].append(_r(v[k], 1) if v and v.get(k) is not None else None)
    return out


def movers(summary: dict, names: dict, groups_meta: dict, top: int) -> list[dict]:
    """Największe zmiany udziałów (r/r, p.p.) i wartości poza zakresem 5 lat."""
    items = []
    for code, per in summary.items():
        for kind in ("month", "week"):
            s = per.get(kind)
            if not s or not s.get("_complete"):
                continue
            for g in GROUP_KEYS + ["renewable"]:
                v = s.get("share_" + g)
                if not v or v[0] is None:
                    continue
                cur, ly, mn, mx = v[0], v[2], v[4], v[5]
                label = groups_meta.get(g, ("OZE łącznie",))[0] if g in groups_meta else "OZE łącznie"
                if ly is not None and abs(cur - ly) >= 3:
                    items.append({"code": code, "name": names[code], "period": kind, "series": g,
                                  "label": label, "type": "yoy", "cur": cur, "ref": ly,
                                  "delta": round(cur - ly, 1), "score": abs(cur - ly)})
                if mx is not None and cur > mx + 0.5:
                    items.append({"code": code, "name": names[code], "period": kind, "series": g,
                                  "label": label, "type": "max5", "cur": cur, "ref": mx,
                                  "delta": round(cur - mx, 1), "score": abs(cur - mx) + 2})
                elif mn is not None and cur < mn - 0.5:
                    items.append({"code": code, "name": names[code], "period": kind, "series": g,
                                  "label": label, "type": "min5", "cur": cur, "ref": mn,
                                  "delta": round(cur - mn, 1), "score": abs(cur - mn) + 2})
            it = s.get("intensity")
            if it and it[0] is not None and it[2]:
                ch = 100 * (it[0] / it[2] - 1)
                if abs(ch) >= 12:
                    items.append({"code": code, "name": names[code], "period": kind, "series": "intensity",
                                  "label": "Emisyjność", "type": "int_yoy", "cur": it[0], "ref": it[2],
                                  "delta": round(ch, 1), "score": abs(ch) * 0.5})
            ld = s.get("load")
            if ld and ld[0] is not None and ld[2]:
                ch = 100 * (ld[0] / ld[2] - 1)
                if abs(ch) >= 5:
                    items.append({"code": code, "name": names[code], "period": kind, "series": "load",
                                  "label": "Zużycie", "type": "load_yoy", "cur": ld[0], "ref": ld[2],
                                  "delta": round(ch, 1), "score": abs(ch) * 0.8})
    items.sort(key=lambda x: -x["score"])
    seen, out = set(), []
    for it in items:
        k = (it["code"], it["series"], it["period"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= top:
            break
    return out


def calendar_band(e: pd.DataFrame, dates: list[str], key: str, n_years: int, min_years: int,
                  window: int = 3) -> dict:
    """Dla każdej daty: średnia, min i maks z tych samych dni kalendarzowych (+/- window)
    w poprzednich n_years latach (dla porównania prognozy z historią)."""
    out = {"avg": [], "min": [], "max": []}
    if e.empty or key not in e:
        return {k: [None] * len(dates) for k in out}
    col = e[key]
    for ds in dates:
        d = dt.date.fromisoformat(ds)
        vals = []
        for k in range(1, n_years + 1):
            c = _shift_years(d, -k)
            blk = col.loc[pd.Timestamp(c - dt.timedelta(days=window)):pd.Timestamp(c + dt.timedelta(days=window))]
            if blk.notna().sum() >= window:
                vals.append(float(blk.mean()))
        if len(vals) >= min_years:
            out["avg"].append(_r(np.mean(vals))); out["min"].append(_r(min(vals))); out["max"].append(_r(max(vals)))
        else:
            out["avg"].append(None); out["min"].append(None); out["max"].append(None)
    return out
