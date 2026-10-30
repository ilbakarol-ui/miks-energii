"""Prognoza w trendzie tygodniowym: model na 14 dni (pole "ext") i sumy tygodniowe z prognozą."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from mixtracker import trends, weather


def _features(idx, rng):
    f = pd.DataFrame({"ws": rng.uniform(2, 14, len(idx)), "ghi": rng.uniform(0.5, 7, len(idx)),
                      "temp": 10 + 10 * np.sin(np.arange(len(idx)) / 58)}, index=idx)
    f["cf"] = weather.turbine_cf(f["ws"])
    f["hdd"] = (15.5 - f["temp"]).clip(lower=0)
    f["cdd"] = (f["temp"] - 22).clip(lower=0)
    return f


def test_forecast_country_ext_days():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-06-01", "2026-10-29", freq="D")
    f = _features(idx, rng)
    e = pd.DataFrame(index=idx)
    e["wind"] = 5 + 80 * f["cf"]
    e["solar"] = 6 * f["ghi"]
    e["load"] = 400 + 8 * f["hdd"]
    e["gen_total"] = e["wind"] + e["solar"] + 300
    fut = _features(pd.date_range("2026-10-30", periods=16, freq="D"), np.random.default_rng(5))
    fc = weather.forecast_country("PL", f[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 30), ext=7)
    assert len(fc["days"]) == 7 and fc["days"][-1] == "2026-11-05"
    assert fc["ext"]["days"][0] == "2026-11-06" and fc["ext"]["days"][-1] == "2026-11-12"
    # ten sam model: dzień 10 = 5 + 80·cf (korekta z ostatnich dni wygasa)
    assert fc["ext"]["wind"][3] == pytest.approx(5 + 80 * fut["cf"].iloc[10], rel=1e-3)
    assert len(fc["ext"]["load"]) == 7 and all(v is not None for v in fc["ext"]["solar"])
    # bez ext - jak dotąd
    fc0 = weather.forecast_country("PL", f[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 30))
    assert "ext" not in fc0 and fc0["wind"] == fc["wind"]


def test_forecast_country_ext_missing_weather():
    """Dni bez prognozy pogody w przedłużeniu zostają puste."""
    rng = np.random.default_rng(3)
    idx = pd.date_range("2025-06-01", "2026-10-29", freq="D")
    f = _features(idx, rng)
    e = pd.DataFrame(index=idx)
    e["wind"] = 5 + 80 * f["cf"]; e["solar"] = 6 * f["ghi"]; e["load"] = 400 + 8 * f["hdd"]
    e["gen_total"] = e["wind"] + e["solar"] + 300
    fut = _features(pd.date_range("2026-10-30", periods=10, freq="D"), rng)   # tylko 10 dni pogody
    fc = weather.forecast_country("PL", f[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 30), ext=7)
    assert fc["ext"]["wind"][:3] != [None] * 3 and fc["ext"]["wind"][3:] == [None] * 4


def test_weekly_forecast_sums():
    ref = dt.date(2026, 10, 29)   # czwartek tyg. 44
    idx = pd.date_range("2026-09-01", ref, freq="D")
    e = pd.DataFrame({"wind": 100.0, "solar": 10.0, "load": 500.0, "gen_total": 400.0}, index=idx)
    fc = {"days": [(ref + dt.timedelta(days=k)).isoformat() for k in range(1, 8)],   # 30.10–5.11
          "wind": [200.0] * 7, "solar": [20.0] * 7, "load": [600.0] * 7,
          "ext": {"days": [(ref + dt.timedelta(days=k)).isoformat() for k in range(8, 15)],   # 6.11–12.11
                  "wind": [300.0] * 7, "solar": [None] * 7, "load": [700.0] * 7}}
    r = trends.weekly_forecast(e, fc, ref, 2026, 43)
    w = r["wind"]
    # tydz. 44 (26.10–1.11): 4 dni znane po 100 + 3 dni prognozy po 200
    assert w["fc"][43] == 1000.0 and w["fc_days"][43] == 3
    # tydz. 45 (2–8.11): 4 dni modelu po 200 + 3 dni przedłużenia po 300
    assert w["fc"][44] == 1700.0 and w["fc_days"][44] == 7
    # tydz. 46: tylko 9–12.11 (4 dni) - za mało
    assert w["fc"][45] is None
    assert all(v is None for v in w["fc"][:43])
    # słońce bez przedłużenia: tydz. 45 ma 4 dni prognozy - pominięty
    assert r["solar"]["fc"][43] == pytest.approx((4 * 10 + 3 * 20), rel=1e-6) and r["solar"]["fc"][44] is None
    assert r["load"]["fc"][44] == 4 * 600 + 3 * 700


def test_weekly_forecast_without_forecast():
    idx = pd.date_range("2026-09-01", "2026-10-29", freq="D")
    e = pd.DataFrame({"wind": 1.0}, index=idx)
    assert trends.weekly_forecast(e, None, dt.date(2026, 10, 29), 2026, 43) == {}
