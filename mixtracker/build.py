"""Składa dane do dashboardu (JSON) i renderuje statyczną stronę site/index.html."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import forecast, prices, store, trends
from .categories import DETAIL, DETAIL_KEYS, GROUPS, GROUP_KEYS

TEMPLATE = Path(__file__).with_name("template.html")
SITE = store.ROOT / "site"


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


def build(settings: dict, eurostat_df: pd.DataFrame | None = None, run_info: dict | None = None,
          folder: Path | None = None, today: dt.date | None = None, forecasts: dict | None = None) -> dict:
    countries = store.load_countries()
    min_cov = settings["min_day_coverage"]
    n_years, min_years = settings["history_years"], settings["five_year_min_years"]
    frames = {c["code"]: store.read_daily(c["code"], folder) for c in countries if store.has_daily(c)}
    ref = _ref_date(frames, min_cov) or (today or dt.date.today()) - dt.timedelta(days=1)
    periods = trends.reference_periods(ref)
    state = store.read_state()

    summary, weekly, daily, monthly, control = {}, {}, {}, {}, {}
    est_load, fcast, price_info, breaks = {}, {}, {}, {}
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
        daily[code] = trends.daily_recent(e, ref, settings["daily_chart_days"])
        monthly[code] = trends.monthly_recent(e, periods["month"]["end"], settings["monthly_chart_months"])
        fc = (forecasts or {}).get(code) if forecasts is not None else forecast.read_latest(code)
        if fc:
            fcast[code] = forecast_block(fc, e, ref, n_years, min_years)
        used = [z["zone"] for z in zones_ok.get(code, [])]
        allz = [z["zone"] for z in zones_all.get(code, [])]
        if allz:
            price_info[code] = {"zones": used, "all": allz, "partial": bool(used) and len(used) < len(allz),
                                "zone_daily": _zone_recent(used, ref, settings["daily_chart_days"]) if len(used) > 1 else {}}
        brk = trends.detect_break(e, ref)
        if brk:
            breaks[code] = brk
        fst = state.get("forecast", {}).get(code, {})
        good = df.index[df["cov"] >= min_cov]
        last30 = df.loc[pd.Timestamp(ref) - pd.Timedelta(days=29):pd.Timestamp(ref), "cov"]
        control[code] = {
            "source": store.SOURCE_LABEL.get(c.get("source", ""), c.get("source", "")),
            "last": good.max().strftime("%Y-%m-%d") if len(good) else None,
            "first": good.min().strftime("%Y-%m-%d") if len(good) else None,
            "days30": int((last30 >= min_cov).sum()),
            "load": "szacowane z bilansu" if est else ("ENTSO-E (obciążenie)" if df["load"].notna().any() else "brak"),
            "unknown": st.get("unknown", []),
            "error": st.get("last_error"),
            "history_complete": st.get("history_complete"),
            "years5": summary[code]["month"]["_years5"],
            "prices": ", ".join(used) if used else ("brak (strefy tylko do użytku prywatnego)" if allz else "brak strefy"),
            "forecast": _fc_status(fc),
            "break": brk,
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
        "forecast": fcast,
        "prices": price_info,
        "price_map": price_map(zones_ok, zones_all, countries),
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
