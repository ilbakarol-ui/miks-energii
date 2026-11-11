"""B-20261111-1: ostatnie doby niepełne mimo pełnego pokrycia (agregat UE, słońce kraju)."""
import pandas as pd

from mixtracker import build, run_daily

COUNTRIES = [{"code": c, "group": "UE"} for c in ("DE", "FR", "PL", "ES", "IT")] + [{"code": "EU", "group": "agregat"}]


def _frames(eu_gas=1.0, eu_solar=1.0, de_solar_last=72000.0):
    idx = pd.date_range("2026-10-20", "2026-11-10", freq="D")
    fr = {}
    for c in ("DE", "FR", "PL", "ES", "IT"):
        fr[c] = pd.DataFrame({"cov": 1.0, "gas": 100000.0, "solar": 72000.0, "load": 400000.0}, index=idx)
    fr["DE"].loc[idx[-1], "solar"] = de_solar_last
    tot_g, tot_s = 5 * 100000.0, 4 * 72000.0 + de_solar_last
    fr["EU"] = pd.DataFrame({"cov": 1.0, "gas": 500000.0, "solar": 360000.0, "load": 2000000.0}, index=idx)
    fr["EU"].loc[idx[-1], ["gas", "solar"]] = [eu_gas * tot_g, eu_solar * tot_s]
    return fr


def test_eu_aggregate_below_sum_is_gap():
    fr = _frames(eu_gas=0.85, eu_solar=0.74)
    out = build._flag_partial_tail(fr, COUNTRIES, 0.8)
    day = pd.Timestamp("2026-11-10")
    assert out == {"EU": [(day, ["gaz", "słońce"])]}
    assert fr["EU"].at[day, "cov"] < 0.8 and build._ref_date(fr, 0.8).isoformat() == "2026-11-09"


def test_small_difference_unchanged():
    fr = _frames(eu_gas=0.99, eu_solar=0.985)
    assert build._flag_partial_tail(fr, COUNTRIES, 0.8) == {}
    assert fr["EU"].at[pd.Timestamp("2026-11-10"), "cov"] == 1.0


def test_country_solar_collapse_is_gap():
    fr = _frames(de_solar_last=5400.0)
    out = build._flag_partial_tail(fr, COUNTRIES, 0.8)
    assert out["DE"] == [(pd.Timestamp("2026-11-10"), ["słońce"])]
    # EU = suma krajów z pełną dobą (DE już luką, 4 z 5 krajów) – wyższa od sumy, więc bez oznaczenia
    assert "EU" not in out


def test_cloudy_day_unchanged():
    fr = _frames(de_solar_last=14000.0)   # 19% mediany – bardzo pochmurny dzień, nie brak danych
    assert build._flag_partial_tail(fr, COUNTRIES, 0.8) == {}


def test_too_many_missing_countries_skips_eu_check():
    fr = _frames(eu_gas=0.5)
    day = pd.Timestamp("2026-11-10")
    for c in ("FR", "PL", "ES", "IT"):
        fr[c] = fr[c].drop(index=day)
    assert "EU" not in build._flag_partial_tail(fr, COUNTRIES, 0.8)


def test_summary_line():
    p = {"control": {"EU": {"partial_last": {"series": {"2026-11-10": ["gaz", "słońce"]}}},
                     "DE": {"partial_last": {"series": {"2026-11-10": ["słońce"]}}}, "PL": {}}}
    lines = run_daily._partial_lines(p)
    assert "Ostatnie doby niepełne mimo pokrycia (do ponownego pobrania): DE 10.11 (słońce), EU 10.11 (gaz, słońce)" in lines
