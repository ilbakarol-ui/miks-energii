"""Testy poprawek B-20261028-1 i B-20261028-3."""
import datetime as dt

from mixtracker import run_daily, shared_weather as sw, store, update
from tests.test_b20261027 import _Never
from tests.test_pipeline import FakeClient, tmp_store  # noqa: F401 (fixture)


def test_only_missing_summary_lists_all_zones(tmp_store):
    """B-20261028-1: podsumowanie drugiego przebiegu pokazuje wszystkie strefy, a nie tylko uzupełnione."""
    settings = store.load_settings()
    update.update_prices_ahead(settings, budget_minutes=5, client=_Never(data_from=dt.date(2020, 1, 1)), retry_wait=0)
    res = update.update_prices_ahead(settings, budget_minutes=5, client=FakeClient(data_from=dt.date(2020, 1, 1)), only_missing=True)
    assert res["zones"] == ["PL"] and len(res["zones_all"]) > 1
    md = "\n".join(run_daily._prices_md(res))
    n = len(res["zones_all"])
    assert f"{n} z {n} stref" in md and "uzupełniono: PL" in md
    assert md.count("\n| ") - 1 == n          # wiersz nagłówka tabeli + n stref


def test_prev_timeout_short_for_daily_increment():
    """B-20261028-3: przyrost dzienny dawnych prognoz z krótkim limitem, duże paczki – 150 s."""
    u = sw.PREV_URL
    assert sw._timeout(u, {"start_date": "2026-10-24", "end_date": "2026-10-28"}) == 40
    assert sw._timeout(u, {"start_date": "2026-08-30", "end_date": "2026-10-28"}) == 150
    assert sw._timeout(u, {}) == 150
    assert sw._timeout("https://api.open-meteo.com/v1/forecast", {"start_date": "2026-10-24", "end_date": "2026-10-28"}) == 90
