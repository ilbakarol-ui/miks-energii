"""Testy poprawek B-20261031-1, -2, -3."""
import datetime as dt

import pandas as pd
import pytest

from mixtracker import model, shared_weather as sw, store, update
from tests.test_pipeline import FakeClient, tmp_store  # noqa: F401 (fixture)


def test_holiday_error_recorded(monkeypatch):
    """B-20261031-1: błąd kalendarza świąt kraju nie znika po cichu – trafia do HOLIDAY_ERRORS."""
    holidays = pytest.importorskip("holidays")

    def boom(*a, **k):
        raise NotImplementedError("subdiv NIR")
    monkeypatch.setattr(holidays, "country_holidays", boom)
    model._holiday_set.cache_clear()
    model.HOLIDAY_ERRORS.clear()
    try:
        assert model._holiday_set("UK", 2026, 2026) == frozenset()
        assert "UK" in model.HOLIDAY_ERRORS and "NIR" in model.HOLIDAY_ERRORS["UK"]
    finally:
        model._holiday_set.cache_clear()
        model.HOLIDAY_ERRORS.clear()


def test_every_country_has_holidays():
    """B-20261031-1: każdy kraj z config/countries.csv (poza EU) ma kalendarz świąt (wykrywa zmianę podregionów)."""
    pytest.importorskip("holidays")
    model._holiday_set.cache_clear()
    for c in store.load_countries():
        if c["code"] == "EU":
            continue
        # Szwajcaria ma w bibliotece holidays tylko 4 święta ogólnokrajowe (reszta kantonalna)
        assert len(model._holiday_set(c["code"], 2026, 2026)) >= (4 if c["code"] == "CH" else 5), c["code"]


def test_repair_gives_up_after_three_tries(tmp_store, monkeypatch):
    """B-20261031-2: zakres, który po 3 próbach nadal jest luką, nie jest już pobierany co tydzień."""
    settings = store.load_settings()
    update.update_all(settings, budget_minutes=5, only=["PL"], client=FakeClient(data_from=dt.date(2025, 1, 1)))
    st = store.read_state()
    st["PL"]["repair"] = {}
    st["PL"]["repair_tries"] = {"2025-03-01_2025-03-05": 3, "2025-04-01_2025-04-02": 1}
    store.write_state(st)
    ranges = [(dt.date(2025, 3, 1), dt.date(2025, 3, 5)), (dt.date(2025, 4, 1), dt.date(2025, 4, 2))]
    monkeypatch.setattr(update, "bad_ranges", lambda *a, **k: list(ranges))
    called = []

    def fake_fetch(client, c, zones, a, b, *args, **kw):
        called.append((c["code"], a, b))
        return pd.DataFrame(), [], True, {}
    monkeypatch.setattr(update, "fetch_range", fake_fetch)
    res = update.update_all(settings, budget_minutes=5, only=["PL"], client=FakeClient(data_from=None))
    st = store.read_state()["PL"]
    repairs = [x for x in called if x[1].year == 2025]
    assert ("PL", dt.date(2025, 3, 1), dt.date(2025, 3, 5)) not in repairs
    assert ("PL", dt.date(2025, 4, 1), dt.date(2025, 4, 2)) in repairs
    assert st["repair_gave_up"] == ["2025-03-01_2025-03-05"]
    assert st["repair_tries"]["2025-04-01_2025-04-02"] == 2
    assert any("trwałą lukę źródła" in m for m in res.get("log", []))


def test_ensemble_timeout_and_pause(monkeypatch):
    """B-20261031-3: krótszy limit dla ensemble i odstęp między zapytaniami ensemble."""
    assert sw._timeout("https://ensemble-api.open-meteo.com/v1/ensemble", {}) == 45
    assert sw._timeout("https://api.open-meteo.com/v1/forecast", {}) == 90
    sleeps = []

    class R:
        status_code = 200
        text = "[]"
        content = b"[]"

        def json(self):
            return [{"ok": 1}]
    monkeypatch.setattr(sw.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(sw.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(sw, "LAST_ENS", [0.0])
    sw.http_get("https://ensemble-api.open-meteo.com/v1/ensemble", {})
    sw.http_get("https://ensemble-api.open-meteo.com/v1/ensemble", {})
    sw.http_get("https://api.open-meteo.com/v1/forecast", {})
    assert len(sleeps) == 1 and 3.0 < sleeps[0] <= sw.ENSEMBLE_PAUSE_S
