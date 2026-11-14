"""Opcjonalny „sztuczny zegar” dla testów: FAKE_NOW=2026-10-26T00:55:00+00:00 python -m pytest -q
(sprawdza, czy testy przechodzą zimą/latem i przy zmianie czasu - wiele testów używa dzisiejszej daty)."""
import datetime as dt
import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _fake_now():
    v = os.environ.get("FAKE_NOW")
    if not v:
        yield
        return
    import time_machine
    with time_machine.travel(dt.datetime.fromisoformat(v), tick=True):
        yield


@pytest.fixture(autouse=True)
def _no_power_step(monkeypatch, tmp_path):
    """U-20261114-3: krok „power” Wspólnej pogody (173 punkty z config/weather_points.csv) jest w testach wyłączony;
    testy, które go sprawdzają, ustawiają sw.POWER_POINTS same."""
    from mixtracker import shared_weather as sw
    monkeypatch.setattr(sw, "POWER_POINTS", tmp_path / "brak_punktow.csv")
    from mixtracker import weather
    monkeypatch.setattr(weather, "SHARED", tmp_path / "brak_wspolnej_pogody")


@pytest.fixture(autouse=True)
def _no_real_weather(monkeypatch, tmp_path):
    """Testy nie pytają prawdziwego Open-Meteo i nie zapisują prawdziwego data/weather.

    test_first_afternoon_no_retry (run_daily --afternoon) od B-20261113-1 liczył prognozy naprawdę: 173 punkty
    z Open-Meteo w kroku „Testy” przed każdym przebiegiem dziennym i zapis do data/weather repozytorium."""
    from mixtracker import weather

    def no_network(*a, **k):
        raise RuntimeError("sieć wyłączona w testach")
    monkeypatch.setattr(weather, "_get", no_network)
    monkeypatch.setattr(weather, "WEATHER", tmp_path / "weather_cache")
