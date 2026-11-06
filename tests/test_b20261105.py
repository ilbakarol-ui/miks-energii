"""Testy poprawek B-20261105-1, -2, -3."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from mixtracker import run_daily, shared_weather as sw, trends


def _block(load_days: int, n: int = 7):
    idx = pd.date_range("2026-10-26", periods=n, freq="D")
    cols = {c: np.full(n, 10.0) for c in trends.ADDITIVE}
    df = pd.DataFrame(cols, index=idx)
    df.loc[df.index[load_days:], ["load", "net_import"]] = np.nan
    return df


def test_scaled_sum_load_gaps():
    """B-20261105-1: zużycie z 4 z 7 dób → nieznane (NaN), z 6 z 7 → przeskalowane 7/6; produkcja bez zmian."""
    s = trends._scaled_sum(_block(4), 7)
    assert s["gen_total"] == pytest.approx(70.0)
    assert np.isnan(s["load"]) and np.isnan(s["net_import"])
    s = trends._scaled_sum(_block(6), 7)
    assert s["load"] == pytest.approx(70.0)
    s = trends._scaled_sum(_block(7), 7)
    assert s["load"] == pytest.approx(70.0)


def test_weekly_profile_load_gaps():
    """B-20261105-1: tydzień z 3 lukami zużycia nie trafia do serii tygodniowej zużycia (ale produkcja tak)."""
    idx = pd.date_range("2026-10-26", periods=14, freq="D")      # tygodnie ISO 44 i 45
    e = pd.DataFrame({c: np.full(14, 10.0) for c in trends.ADDITIVE}, index=idx)
    e.loc[idx[1:4], ["load", "net_import"]] = np.nan              # tydz. 44: 4 z 7 dób zużycia
    e.loc[idx[8], "load"] = np.nan                                # tydz. 45: 6 z 7
    iso = e.index.isocalendar()
    g = e.copy()
    g["_y"], g["_w"] = iso["year"].to_numpy(), iso["week"].to_numpy()
    cnt = g.groupby(["_y", "_w"])["gen_total"].count()
    sums = g.groupby(["_y", "_w"]).sum(min_count=1)
    lraw = {c: (sums[c].copy(), g.groupby(["_y", "_w"])[c].count()) for c in trends.LOAD_COLS}
    sums = sums.mul(7 / cnt, axis=0)
    for c, (ls, lc) in lraw.items():
        sums[c] = (ls * 7 / lc.where(lc > 0)).where(lc >= 6).reindex(sums.index)
    assert np.isnan(sums.loc[(2026, 44), "load"])
    assert sums.loc[(2026, 45), "load"] == pytest.approx(70.0)
    assert sums.loc[(2026, 44), "gen_total"] == pytest.approx(70.0)
    prof = trends.weekly_profile(e, 2026, 5, 1, 45)                # funkcja działa z lukami zużycia
    assert isinstance(prof, dict)


def test_partial_lines_load_gaps():
    """B-20261105-2: linia o lukach samego zużycia (DK 5 z 14); 14 z 14 (UK) bez wpisu – ma już „BRAK od …”."""
    p = {"control": {"DK": {"partial14": 0, "below14": 0, "load_gaps14": 5},
                     "UK": {"partial14": 11, "below14": 1, "load_gaps14": 14},
                     "PL": {"partial14": 0, "below14": 0, "load_gaps14": 0}}}
    lines = run_daily._partial_lines(p)
    assert any(l.startswith("Niepełne doby") and "UK 11" in l for l in lines)
    lg = [l for l in lines if l.startswith("Zużycie bez danych")]
    assert lg and "DK 5" in lg[0] and "UK" not in lg[0] and "PL" not in lg[0]


def test_seasonal_timeout():
    """B-20261105-3: dłuższy limit dla SEAS5, ensemble 120 s (od 6.11), reszta 90 s."""
    assert sw._timeout(sw.SEASONAL_URL, {}) == 180
    assert sw._timeout("https://ensemble-api.open-meteo.com/v1/ensemble", {}) == 120
    assert sw._timeout("https://api.open-meteo.com/v1/forecast", {}) == 90
