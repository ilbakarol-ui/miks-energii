"""Składa dane do dashboardu (JSON) i renderuje statyczną stronę site/index.html."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import forecast, model, prices, storage_cap, store, trends, verify, weather, weather_tab
from .categories import DETAIL, DETAIL_KEYS, GROUPS, GROUP_KEYS

TEMPLATE = Path(__file__).with_name("template.html")
SITE = store.ROOT / "site"


def _flag_partial_last(df: pd.DataFrame, min_cov: float, ratio: float = 0.6, days: int = 10):
    """Doby z okna dosyłania danych (ostatnie `days` dni) z pełnym pokryciem, ale zużyciem poniżej 60%
    mediany z tych samych dni tygodnia z 4 poprzednich tygodni -> traktowane jako niepełne (źródło
    dosyła resztę później albo wcale). Sprawdzana jest każda doba z okna, nie tylko ostatnia, bo
    niepełna doba nie przestaje być niepełna, gdy przyjdzie kolejna.
    Zwraca (ramka, informacja o oznaczeniu albo None)."""
    if df is None or df.empty or "load" not in df:
        return df, None
    last = df.index.max()
    found = []
    for day in df.index[df.index > last - pd.Timedelta(days=days)]:
        if df.at[day, "cov"] < min_cov:
            continue
        prev = [day - pd.Timedelta(weeks=k) for k in range(1, 5)]
        prev = [d for d in prev if d in df.index and df.at[d, "cov"] >= min_cov]
        if len(prev) < 2:
            continue
        ref, val = df.loc[prev, "load"].median(), df.at[day, "load"]
        if pd.notna(ref) and ref > 0 and pd.notna(val) and val < ratio * ref:
            found.append((day, round(float(val / ref), 2)))
    if not found:
        return df, None
    df = df.copy()
    for day, _ in found:
        df.at[day, "cov"] = min(df.at[day, "cov"], min_cov / 2)
    d, r = found[-1]
    return df, {"date": d.strftime("%Y-%m-%d"), "ratio": r, "days": [x.strftime("%Y-%m-%d") for x, _ in found]}


def _ref_date(frames: dict, min_cov: float) -> dt.date | None:
    lasts = {}
    for code, df in frames.items():
        good = df.index[df["cov"] >= min_cov] if not df.empty else []
        if len(good):
            lasts[code] = good.max().date()
    if not lasts:
        return None
    if "EU" in lasts:
        return lasts["EU"]
    return sorted(lasts.values())[len(lasts) // 2]


def eurostat_block(df: pd.DataFrame, countries: list[dict], n_years: int) -> dict:
    if df is None or df.empty:
        return {}
    out = {}
    by_geo = {c["eurostat"]: c["code"] for c in countries if c["eurostat"]}
    keys = GROUP_KEYS + ["total", "renewable", "consumption", "imports", "exports"]
    for geo, g in df.groupby("geo"):
        code = by_geo.get(geo)
        if not code:
            continue
        g = g.set_index("month").sort_index()
        g = g[g["total"].notna()]
        if g.empty:
            continue
        last = g.index.max()
        ly_ = int(last[:4])
        if ly_ < dt.date.today().year - 2:   # np. Wielka Brytania (dane do 2020)
            out[code] = {"stale": last}
            continue
        recent = g.iloc[-25:]
        y, m = int(last[:4]), int(last[5:7])
        hist = [f"{y - k}-{m:02d}" for k in range(1, n_years + 1)]

        def val(month, k):
            if month not in g.index or pd.isna(g.at[month, k]):
                return None
            return float(g.at[month, k])

        def share(month, k):
            t = val(month, "total"); v = val(month, k)
            return 100 * v / t if t and v is not None else None

        latest = {}
        for k in keys:
            hv = [val(h, k) for h in hist]
            hv = [v for v in hv if v is not None]
            latest[k] = [_r(val(last, k)), _r(val(hist[0], k)), _r(np.mean(hv)) if len(hv) >= 3 else None]
        for k in GROUP_KEYS + ["renewable"]:
            hv = [share(h, k) for h in hist]
            hv = [v for v in hv if v is not None]
            latest["share_" + k] = [_r(share(last, k)), _r(share(hist[0], k)), _r(np.mean(hv)) if len(hv) >= 3 else None]
        out[code] = {
            "last": last,
            "months": list(recent.index),
            **{k: [_r(v, 1) for v in recent[k].tolist()] for k in GROUP_KEYS + ["total", "consumption"]},
            "latest": latest,
        }
    return out


def _r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return round(f, nd)


def forecast_block(fc: dict, e: pd.DataFrame, ref: dt.date, n_years: int, min_years: int) -> dict:
    """Prognoza na kolejne dni + ostatnie 14 dni rzeczywistych + pasmo z 5 lat dla tych samych dni."""
    days = fc["days"]
    recent_idx = pd.date_range(pd.Timestamp(ref) - pd.Timedelta(days=13), pd.Timestamp(ref), freq="D")
    rb = e.reindex(recent_idx) if not e.empty else pd.DataFrame(index=recent_idx)
    recent_dates = [d.strftime("%Y-%m-%d") for d in recent_idx]
    out = {"issued": fc["issued"], "days": days, "methods": fc.get("methods", {}), "errors": fc.get("errors", {}), "notes": fc.get("notes", {}),
           "recent": {"dates": recent_dates}, "band": {}}
    for key in ["solar", "wind", "load"]:
        if fc.get(key) is None:
            continue
        out[key] = fc[key]
        out["recent"][key] = [_r(v) for v in (rb[key].tolist() if key in rb else [None] * len(recent_idx))]
        out["band"][key] = trends.calendar_band(e, recent_dates + days, key, n_years, min_years)
    return out


def apply_known_breaks(frames: dict, known: list[dict], window: int = 28, replaced: set | None = None) -> dict:
    """B-20261027-2: znane skoki raportowania (config/known_breaks.csv). Poziom serii przed datą skoku jest przesuwany
    o skok oszacowany różnicą różnic (średnia `window` dni po minus przed, minus to samo rok wcześniej), także w
    agregatach (np. EU) – inaczej UE-27 r/r i średnia 5 lat są zaniżone (NL 2026: ok. −650 GWh/tydz.).
    Zmienia tylko ramki w pamięci (pliki data/daily bez zmian). Zwraca {kod: opis skoku} dla kontroli danych.

    `replaced` – kraje, których seria pochodzi już z ENTSO-E (U-20261027): ich własnej serii nie korygujemy (inne źródło,
    skok Energy-Charts jej nie dotyczy), a agregaty (EU z Energy-Charts) – tylko stałym skokiem z kolumny shift_per_day."""
    out = {}
    for kb in known:
        code, col = kb["code"], kb.get("series") or "load"
        if replaced and code in replaced:
            try:
                fixed = float(kb.get("shift_per_day") or "nan")
            except ValueError:
                fixed = float("nan")
            if pd.isna(fixed):
                continue
            b = pd.Timestamp(kb["date"])
            for k in [a for a in (kb.get("aggs") or "").split("|") if a]:
                if k in frames and col in frames[k]:
                    g = frames[k].copy()
                    g.loc[g.index < b, col] = g.loc[g.index < b, col] + fixed
                    frames[k] = g
            out[code] = {"date": kb["date"], "series": col, "known": True, "shift_per_day": round(fixed, 1),
                         "entsoe": True, "note": kb.get("note", "")}
            continue
        f = frames.get(code)
        if f is None or f.empty or col not in f:
            continue
        b = pd.Timestamp(kb["date"])
        s = f[col]
        def jump(bb):
            pre = s.loc[bb - pd.Timedelta(days=window):bb - pd.Timedelta(days=1)].mean()
            post = s.loc[bb:bb + pd.Timedelta(days=window - 1)].mean()
            return post - pre
        if s.loc[b:].dropna().shape[0] < window:
            continue
        base = jump(b - pd.Timedelta(days=364))
        shift = jump(b) - (base if pd.notna(base) else 0.0)
        if pd.isna(shift):
            continue
        for k in [code] + [a for a in (kb.get("aggs") or "").split("|") if a]:
            if k in frames and col in frames[k]:
                g = frames[k].copy()
                m = g.index < b
                g.loc[m, col] = g.loc[m, col] + shift
                frames[k] = g
        out[code] = {"date": kb["date"], "series": col, "known": True, "shift_per_day": round(float(shift), 1),
                     "change": round(float(100 * shift / s.loc[b - pd.Timedelta(days=window):b - pd.Timedelta(days=1)].mean()), 1),
                     "note": kb.get("note", "")}
    return out


def build(settings: dict, eurostat_df: pd.DataFrame | None = None, run_info: dict | None = None,
          folder: Path | None = None, today: dt.date | None = None, forecasts: dict | None = None) -> dict:
    countries = store.load_countries()
    min_cov = settings["min_day_coverage"]
    n_years, min_years = settings["history_years"], settings["five_year_min_years"]
    frames = {c["code"]: store.read_daily(c["code"], folder) for c in countries if store.has_daily(c)}
    partial = {}
    for k in list(frames):
        frames[k], p = _flag_partial_last(frames[k], min_cov, days=settings.get("refetch_days", 10))
        if p:
            partial[k] = p
    ref = _ref_date(frames, min_cov) or (today or dt.date.today()) - dt.timedelta(days=1)
    state = store.read_state()
    # U-20261027: kraje z serią z ENTSO-E – skok Energy-Charts ich nie dotyczy (korekta tylko agregatu UE)
    from_entsoe = {k for k, v in state.items() if isinstance(v, dict) and (v.get("entsoe") or {}).get("load_days")}
    known_breaks = apply_known_breaks(frames, store.read_known_breaks(), replaced=from_entsoe)
    periods = trends.reference_periods(ref)

    summary, weekly, daily, monthly, control = {}, {}, {}, {}, {}
    est_load, fcast, price_info, breaks = {}, {}, {}, {}
    winds, fc_raw, derived = {}, {}, {}
    zones_ok = prices.load_zones(settings.get("price_licenses", ["cc_by"]))
    zones_all = prices.all_zones()
    for c in countries:
        code = c["code"]
        df = frames.get(code)
        st = state.get(code, {})
        if df is None or df.empty:
            control[code] = {"source": store.SOURCE_LABEL.get(c.get("source", ""), c.get("source", "")),
                             "status": "brak danych dziennych" if not store.has_daily(c) else "brak danych",
                             "error": st.get("last_error")}
            continue
        e, est = trends.derive(df, min_cov)
        est_load[code] = est
        summary[code] = {k: trends.compare(e, k, p, n_years, min_years) for k, p in periods.items()}
        weekly[code] = trends.weekly_profile(e, ref.year, n_years, min_years,
                                             periods["week"]["iso"][1] if periods["week"]["iso"][0] == ref.year else 53)
        fc0 = (forecasts or {}).get(code) if forecasts is not None else forecast.read_latest(code)
        wfc = trends.weekly_forecast(e, fc0, ref, ref.year,
                                     periods["week"]["iso"][1] if periods["week"]["iso"][0] == ref.year else 0)
        for k, v in wfc.items():   # prognoza tygodniowa (wiatr, słońce, zużycie) w serii trendu
            if k in weekly[code]:
                weekly[code][k].update(v)
        daily[code] = trends.daily_recent(e, ref, settings["daily_chart_days"])
        monthly[code] = trends.monthly_recent(e, periods["month"]["end"], settings["monthly_chart_months"])
        winds[code] = e["wind"] if "wind" in e else pd.Series(dtype=float)
        derived[code] = e[[k for k in ("wind", "solar", "load") if k in e]]
        fc = (forecasts or {}).get(code) if forecasts is not None else forecast.read_latest(code)
        if fc:
            fc_raw[code] = fc
        if fc:
            fcast[code] = forecast_block(fc, e, ref, n_years, min_years)
        used = [z["zone"] for z in zones_ok.get(code, [])]
        allz = [z["zone"] for z in zones_all.get(code, [])]
        if allz:
            price_info[code] = {"zones": used, "all": allz, "partial": bool(used) and len(used) < len(allz),
                                "zone_daily": _zone_recent(used, ref, settings["daily_chart_days"]) if len(used) > 1 else {}}
        brk = known_breaks.get(code) or trends.detect_break(e, ref)
        if brk:
            breaks[code] = brk
        fst = state.get("forecast", {}).get(code, {})
        good = df.index[df["cov"] >= min_cov]
        last30 = df.loc[pd.Timestamp(ref) - pd.Timedelta(days=29):pd.Timestamp(ref), "cov"]
        control[code] = {
            "source": store.SOURCE_LABEL.get(c.get("source", ""), c.get("source", ""))
            + (f" + ENTSO-E od {st['entsoe']['from']}" if (st.get("entsoe") or {}).get("load_days")
               or (st.get("entsoe") or {}).get("gen_days") else ""),
            "last": good.max().strftime("%Y-%m-%d") if len(good) else None,
            "first": good.min().strftime("%Y-%m-%d") if len(good) else None,
            "days30": int((last30 >= min_cov).sum()),
            "load": _load_status(df, est, ref),
            "unknown": st.get("unknown", []),
            "error": st.get("last_error"),
            "history_complete": st.get("history_complete"),
            "years5": summary[code]["month"]["_years5"],
            "prices": ", ".join(used) if used else ("brak (strefy tylko do użytku prywatnego)" if allz else "brak strefy"),
            "forecast": _fc_status(fc),
            "break": brk,
            "gaps": _gap_info(e, code, ref),
            "partial_last": partial.get(code),
        }

    names = {c["code"]: c["name"] for c in countries}
    payload = {
        "meta": {
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "ref": ref.isoformat(),
            "year": ref.year,
            "periods": {k: {"label": v["label"], "short": v["short"], "start": v["start"].isoformat(),
                            "end": v["end"].isoformat()} for k, v in periods.items()},
            "years5": f"{ref.year - n_years}–{ref.year - 1}",
            "run": run_info or {},
        },
        "countries": [{"code": c["code"], "name": c["name"], "group": c["group"],
                       "daily": c["code"] in summary, "source": store.has_daily(c), "est_load": est_load.get(c["code"], False)}
                      for c in countries],
        "groups": [{"key": k, "label": GROUPS[k][0], "light": GROUPS[k][1], "dark": GROUPS[k][2]} for k in GROUP_KEYS],
        "details": [{"key": k, "label": DETAIL[k][0], "group": DETAIL[k][1], "ren": DETAIL[k][2]} for k in DETAIL_KEYS],
        "summary": summary,
        "weekly": weekly,
        "daily": daily,
        "monthly": monthly,
        "movers": trends.movers({k: v for k, v in summary.items() if k not in breaks}, names, GROUPS,
                                settings["movers_count"]),
        "breaks": breaks,
        "eurostat": eurostat_block(eurostat_df, countries, n_years),
        "storage_cap": storage_cap.block(names),
        "forecast": fcast,
        "prices": price_info,
        "price_map": price_map(zones_ok, zones_all, countries),
        "wind_tab": wind_tab(fc_raw, winds, n_years, min_years),
        "weather_tab": _weather_tab(derived, fc_raw, [c["code"] for c in countries], ref, n_years, min_years, settings),
        "verify": _verify(derived, ref, settings, n_years, min_years,
                          {c["code"] for c in countries if c.get("group") == "UE"}),
        "factors": {k: v for k, v in trends.emission_factors().items() if v > 0},
        "control": control,
    }
    return payload


MAP_FILE = Path(__file__).with_name("europe_map.json")


def price_map(zones_ok: dict, zones_all: dict, countries: list[dict], days: int = 30) -> dict:
    """Ceny day-ahead do mapy: ostatnie `days` dni dostawy (także dziś/jutro, jeśli opublikowane).

    Kraj z kilkoma strefami: średnia prosta stref z danymi. Zwraca też wartości stref (tooltip)."""
    zone_frames = {}
    for code, zs in zones_ok.items():
        for z in zs:
            if z["zone"] not in zone_frames:
                zd = store.read_zone(z["zone"])
                if not zd.empty:
                    zone_frames[z["zone"]] = zd
    if not zone_frames:
        return {}
    last = max(df["price_avg"].dropna().index.max() for df in zone_frames.values())
    idx = pd.date_range(last - pd.Timedelta(days=days - 1), last, freq="D")
    zones = {}
    for z, df in zone_frames.items():
        d = df.reindex(idx)
        zones[z] = {k: [_r(v, 1) for v in d[k].tolist()] for k in ("price_avg", "price_min", "price_max", "price_neg_h")}
    out_c = {}
    for c in countries:
        code = c["code"]
        used = [z["zone"] for z in zones_ok.get(code, []) if z["zone"] in zones]
        allz = [z["zone"] for z in zones_all.get(code, [])]
        if not used:
            out_c[code] = {"zones": [], "all": allz, "avg": None}
            continue
        arr = np.array([[np.nan if v is None else v for v in zones[z]["price_avg"]] for z in used], dtype=float)
        with np.errstate(all="ignore"):
            avg = np.nanmean(arr, axis=0)
        out_c[code] = {"zones": used, "all": allz, "avg": [_r(v, 1) for v in avg.tolist()]}
    geo = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
    return {"dates": [d.strftime("%Y-%m-%d") for d in idx], "zones": zones, "countries": out_c, "geo": geo}


def wind_accuracy(actual: dict[str, pd.Series], days_back: int = 60) -> dict:
    """Trafność archiwalnych prognoz wiatru: błąd (suma |błędów| / suma produkcji, %) wg wyprzedzenia
    (0 = prognoza na ten sam dzień, wydana rano) dla prognoz z ostatnich `days_back` dni."""
    from .forecast import ARCHIVE
    if not ARCHIVE.exists():
        return {}
    a = pd.read_csv(ARCHIVE, dtype={"issued": str, "date": str})
    a = a[a["type"] == "wind"]
    if a.empty:
        return {}
    a["lead"] = (pd.to_datetime(a["date"]) - pd.to_datetime(a["issued"])).dt.days
    a = a[(a["lead"] >= 0) & (a["lead"] <= 6)]
    cutoff = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    a = a[a["issued"] >= cutoff]
    out = {}
    for code, g in a.groupby("code"):
        y = actual.get(code)
        if y is None or y.empty:
            continue
        g = g.assign(actual=pd.to_datetime(g["date"]).map(y)).dropna(subset=["actual"])
        res = {}
        for lead, gl in g.groupby("lead"):
            if len(gl) >= 3 and gl["actual"].sum() > 0:
                res[str(int(lead))] = {"n": int(len(gl)),
                                       "err": _r(100 * (gl["gwh"] - gl["actual"]).abs().sum() / gl["actual"].sum(), 1),
                                       "bias": _r(100 * (gl["gwh"].sum() / gl["actual"].sum() - 1), 1)}
        if res:
            # ostatnie prognozy z wyprzedzeniem 1 dnia vs rzeczywistość (wykres)
            g1 = g[g["lead"] == 1].sort_values("date").tail(30)
            out[code] = {"leads": res, "recent": {"dates": g1["date"].tolist(),
                                                  "fc": [_r(v) for v in g1["gwh"]], "act": [_r(v) for v in g1["actual"]]}}
    return out


def wind_tab(fcs: dict, winds: dict[str, pd.Series], n_years: int, min_years: int) -> dict:
    """Dane zakładki „Prognozy wiatru”: prognoza dzienna, norma z 5 lat, godzinowo, trafność."""
    if not fcs:
        return {}
    ref_code = "EU" if "EU" in fcs else next(iter(fcs))
    days = fcs[ref_code]["days"]
    out = {"days": days, "issued": fcs[ref_code]["issued"], "countries": {}}
    for code, fc in fcs.items():
        if not fc.get("wind"):
            continue
        y = winds.get(code)
        e = pd.DataFrame({"wind": y}) if y is not None else pd.DataFrame()
        band = trends.calendar_band(e, fc["days"], "wind", n_years, min_years) if not e.empty else {}
        vals = fc["wind"]
        # przy normie < 1 GWh/d (mała energetyka wiatrowa, np. SI, CH) odchylenie procentowe to szum
        anom = [(_r(100 * (v / a - 1), 1) if (v is not None and a is not None and a >= 1.0) else None)
                for v, a in zip(vals, band.get("avg", [None] * len(vals)))]
        rec = {"days": fc["days"], "wind": vals, "avg": band.get("avg"), "min": band.get("min"), "max": band.get("max"),
               "anom": anom, "ws": fc.get("wind_ws"), "err": (fc.get("errors") or {}).get("wind")}
        if y is not None and not y.dropna().empty:
            last365 = y.dropna().tail(365)
            rec["max365"] = _r(last365.max())
            rec["recent"] = {"dates": [d.strftime("%Y-%m-%d") for d in y.dropna().index[-14:]],
                             "wind": [_r(v) for v in y.dropna().tail(14)]}
        if fc.get("wind_hourly"):
            rec["hourly"] = fc["wind_hourly"]
        out["countries"][code] = rec
    out["accuracy"] = wind_accuracy(winds)
    return out


def _weather_tab(derived, fcs, codes, ref, n_years, min_years, settings) -> dict:
    """Zakładka „Pogoda” (weather_tab.py); jej błąd nie może zatrzymać budowy strony."""
    try:
        return weather_tab.build(derived, fcs, codes, ref, n_years, min_years, settings)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _load_status(df: pd.DataFrame, est, ref: dt.date, max_age: int = 7) -> str:
    """Skąd zużycie; gdy seria ENTSO-E urwała się ponad max_age dni temu - 'BRAK od …' (trafia do kontroli danych)."""
    if est:
        return "szacowane z bilansu"
    s = df["load"].dropna() if "load" in df else pd.Series(dtype=float)
    if s.empty:
        return "brak"
    last = s.index.max()
    age = (pd.Timestamp(ref) - last).days
    if age <= max_age:
        return "ENTSO-E (obciążenie)"
    return f"BRAK od {(last + pd.Timedelta(days=1)):%Y-%m-%d} ({age} dni) – zużycie i udziały w zużyciu pominięte"


GAP_LABEL = {"wind": "wiatr", "solar": "słońce", "load": "zużycie"}


def _gap_info(e: pd.DataFrame, code: str, ref: dt.date, days: int = 90) -> dict:
    """Dni z ewidentną luką w danych źródła w ostatnich `days` dniach (pomijane w modelu i weryfikacji)."""
    try:
        feat = weather.read_cache(code)
    except Exception:
        feat = None
    out = {}
    lo = pd.Timestamp(ref) - pd.Timedelta(days=days - 1)
    for kind in ("wind", "solar", "load"):
        if kind not in e or e[kind].dropna().empty:
            continue
        bad = model.bad_days(e[kind], feat, kind, code)
        bad = bad[(bad >= lo) & (bad <= pd.Timestamp(ref))]
        if not len(bad):
            continue
        ranges, start, prev = [], bad[0], bad[0]
        for d in bad[1:]:
            if (d - prev).days > 1:
                ranges.append([start.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")])
                start = d
            prev = d
        ranges.append([start.strftime("%Y-%m-%d"), prev.strftime("%Y-%m-%d")])
        out[kind] = {"n": int(len(bad)), "ranges": ranges[-3:]}
    return out


def _verify(derived: dict, ref: dt.date, settings: dict, n_years: int, min_years: int,
            members: set | None = None) -> dict:
    if not settings.get("verify", True):
        return {}
    feats = {code: weather.read_cache(code) for code in derived}
    arch = None
    if forecast.ARCHIVE.exists():
        arch = pd.read_csv(forecast.ARCHIVE, dtype={"issued": str, "date": str})
        # tylko prognozy modelu pogodowego (pierwsze wpisy z 16.10.2026 to prognozy Energy-Charts)
        arch = arch[arch["issued"] >= settings.get("verify_archive_from", "2026-10-17")]
    out = verify.verify_all(feats, derived, arch, ref, settings.get("verify_days", 90), n_years, min_years,
                            members=members)
    out["archive_from"] = settings.get("verify_archive_from", "2026-10-17")
    return out


FC_LABEL = {"wind": "wiatr", "solar": "słońce", "load": "zużycie"}


def _fc_status(fc: dict | None) -> str:
    if not fc:
        return "brak"
    errs = [f"{FC_LABEL[k]} {v:.0f}%" for k, v in (fc.get("errors") or {}).items() if v is not None and k in FC_LABEL]
    return "model pogodowy" + (" (błąd: " + ", ".join(errs) + ")" if errs else "")


def _zone_recent(zones: list[str], ref: dt.date, days: int) -> dict:
    idx = pd.date_range(pd.Timestamp(ref) - pd.Timedelta(days=days - 1), pd.Timestamp(ref), freq="D")
    out = {}
    for z in zones:
        zd = store.read_zone(z)
        if zd.empty:
            continue
        out[z] = [_r(v) for v in zd["price_avg"].reindex(idx).tolist()]
    return out


SKELETON_HEAD = (
    '<!doctype html>\n<html lang="pl">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
    'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n'
)


def render(payload: dict, out_dir: Path | None = None, fragment: bool = False) -> Path:
    """Zapisuje site/index.html (pełny dokument) i site/data.json.
    fragment=True - bez szkieletu <html> (do podglądu jako artefakt)."""
    out_dir = out_dir or SITE
    out_dir.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    data = data.replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", data)
    if not fragment:
        html = SKELETON_HEAD + html + "\n</body>\n</html>\n"
    out = out_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    (out_dir / "data.json").write_text(data, encoding="utf-8")
    return out
