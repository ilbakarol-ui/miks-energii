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
