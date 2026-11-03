"""Testy poprawek B-20261103-1, -2, -3 (osobny plik – bez konfliktu z innymi łatkami)."""
import datetime as dt

import pytest

from mixtracker import run_daily, store, update
from mixtracker.energycharts import parse_public_power, to_daily
from tests.test_pipeline import FakeClient, fake_payload, fake_price, tmp_store  # noqa: F401 (fixture)


def _load_gap(hours_missing: int):
    p = fake_payload("pl", dt.date(2025, 6, 1), dt.date(2025, 6, 1))
    n = len(p["unix_seconds"])
    per_h = max(1, n // 24)
    for pt in p["production_types"]:
        if pt["name"] == "Load":
            pt["data"][:hours_missing * per_h] = [None] * (hours_missing * per_h)
    return to_daily(parse_public_power(p)[0], "Europe/Warsaw")


def test_load_low_coverage_is_gap():
    """B-20261103-1: zużycie z doby z pokryciem < 0,8 to luka (NaN), przy ≥ 0,8 – przeskalowane do pełnej doby."""
    d = _load_gap(6)                                   # 18 z 24 h = 0,75
    assert d.loc["2025-06-01", "load"] != d.loc["2025-06-01", "load"]          # NaN
    assert d.loc["2025-06-01", "hard_coal"] == pytest.approx(2000 * 24)       # produkcja bez zmian
    d = _load_gap(1)                                   # 23 z 24 h
    assert d.loc["2025-06-01", "load"] == pytest.approx(4000 * 24)


def test_first_afternoon_no_retry(tmp_store, monkeypatch):
    """B-20261103-2: --no-retry (przebieg 12:37 UTC) nie czeka na brakujące strefy."""
    settings = store.load_settings()
    sleeps = []
    monkeypatch.setattr(update.time, "sleep", lambda s: sleeps.append(s))
    seen = {}

    def fake(settings, budget, only_missing=False, **kw):
        seen.update(kw, only_missing=only_missing)
        return {"requests": 0, "log": [], "zones": [], "zones_all": [], "missing_ahead": []}

    monkeypatch.setattr(update, "update_prices_ahead", fake)
    monkeypatch.setattr(run_daily.build, "build", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        run_daily.main(["--afternoon", "--no-retry", "--budget", "5"])
    assert seen == {"retries": 0, "only_missing": False}
    seen.clear()
    with pytest.raises(SystemExit):
        run_daily.main(["--afternoon", "--budget", "5"])
    assert "retries" not in seen                       # ręczne uruchomienie – domyślne 2 ponowienia


def test_partial_days_line():
    """B-20261103-3: linia o stale niepełnych dobach w podsumowaniu (≥ 7 z 14)."""
    payload = {"control": {"IE": {"partial14": 12, "below14": 1}, "UK": {"partial14": 11, "below14": 0},
                           "PL": {"partial14": 0, "below14": 0}, "BG": {"partial14": 1, "below14": 0}}}
    lines = run_daily._partial_lines(payload)
    assert len(lines) == 1 and "IE 12 (w tym 1 luka)" in lines[0] and "UK 11" in lines[0] and "PL" not in lines[0]
    assert run_daily._partial_lines({"control": {"PL": {"partial14": 2}}}) == []
