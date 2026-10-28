"""Testy poprawek B-20261027-1, -2, -3."""
import datetime as dt

import numpy as np
import pandas as pd

from mixtracker import build, store, update
from tests.test_pipeline import FakeClient, fake_price, tmp_store  # noqa: F401 (fixture)


class _Never(FakeClient):
    """PL bez ceny na jutro (tylko dzisiejsza doba)."""
    def price(self, zone, start, end):
        self.requests_made += 1
        return fake_price(start, start, data_from=self.data_from) if zone == "PL" else super().price(zone, start, end)


def test_only_missing_second_run(tmp_store):
    """B-20261027-1: drugi przebieg pyta tylko strefy bez ceny na jutro; przy komplecie – 0 zapytań."""
    settings = store.load_settings()
    res = update.update_prices_ahead(settings, budget_minutes=5, client=_Never(data_from=dt.date(2020, 1, 1)), retry_wait=0)
    assert res["missing_ahead"] == ["PL"]
    c = FakeClient(data_from=dt.date(2020, 1, 1))
    res = update.update_prices_ahead(settings, budget_minutes=5, client=c, only_missing=True)
    assert c.requests_made == 1 and res["zones"] == ["PL"] and res["missing_ahead"] == []
    assert pd.Timestamp(dt.date.today() + dt.timedelta(days=1)) in store.read_zone("PL").dropna().index
    c2 = FakeClient(data_from=dt.date(2020, 1, 1))
    res = update.update_prices_ahead(settings, budget_minutes=5, client=c2, only_missing=True)
    assert c2.requests_made == 0 and res["requests"] == 0 and "komplet" in res["log"][0]


def test_debug_files_cleared(tmp_store):
    """B-20261027-3: stare pliki diagnostyczne znikają; nowe mają datę w nazwie."""
    settings = store.load_settings()
    dbg = store.PRICES.parent / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    (dbg / "price_XX.csv").write_text("stary")
    update.update_prices_ahead(settings, budget_minutes=5, client=_Never(data_from=dt.date(2020, 1, 1)), retry_wait=0)
    names = sorted(p.name for p in dbg.glob("price_*.csv"))
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).strftime("%Y%m%d")
    assert names == [f"price_PL_{tomorrow}.csv"]
    update.update_prices_ahead(settings, budget_minutes=5, client=FakeClient(data_from=dt.date(2020, 1, 1)))
    assert not list(dbg.glob("price_*.csv"))


def test_known_break_shifts_country_and_aggregate():
    """B-20261027-2: poziom przed skokiem przesunięty w kraju i agregacie; r/r bez sztucznego spadku."""
    idx = pd.date_range("2025-06-01", "2026-12-15", freq="D")
    nl = pd.DataFrame({"load": 300.0, "cov": 1.0}, index=idx)
    b = pd.Timestamp("2026-09-28")
    nl.loc[nl.index >= b, "load"] = 220.0                       # zmiana definicji: −80
    eu = pd.DataFrame({"load": 8000.0 + np.where(idx >= b, -80.0, 0.0), "cov": 1.0}, index=idx)
    frames = {"NL": nl, "EU": eu, "PL": pd.DataFrame({"load": 500.0, "cov": 1.0}, index=idx)}
    info = build.apply_known_breaks(frames, [{"code": "NL", "series": "load", "date": "2026-09-28", "aggs": "EU"}])
    assert abs(info["NL"]["shift_per_day"] + 80) < 1e-6 and info["NL"]["known"]
    assert frames["NL"]["load"].nunique() == 1 and frames["NL"]["load"].iloc[0] == 220.0
    assert frames["EU"]["load"].nunique() == 1 and frames["EU"]["load"].iloc[0] == 7920.0
    assert frames["PL"]["load"].iloc[0] == 500.0                 # inne kraje bez zmian
    assert build.apply_known_breaks({"NL": nl.loc[:"2026-10-05"]}, [{"code": "NL", "series": "load", "date": "2026-09-28"}]) == {}


def test_known_breaks_config():
    kb = store.read_known_breaks()
    assert any(r["code"] == "NL" and r["series"] == "load" and "EU" in r["aggs"] for r in kb)


def test_known_break_with_entsoe_country_corrects_only_aggregate():
    """Gdy NL pochodzi z ENTSO-E (U-20261027), seria NL bez korekty, a EU – stałym skokiem z konfiguracji."""
    idx = pd.date_range("2026-08-01", "2026-11-15", freq="D")
    b = pd.Timestamp("2026-09-28")
    nl = pd.DataFrame({"load": 300.0, "cov": 1.0}, index=idx)
    eu = pd.DataFrame({"load": 8000.0 + np.where(idx >= b, -80.0, 0.0), "cov": 1.0}, index=idx)
    frames = {"NL": nl, "EU": eu}
    kb = [{"code": "NL", "series": "load", "date": "2026-09-28", "aggs": "EU", "shift_per_day": "-80"}]
    info = build.apply_known_breaks(frames, kb, replaced={"NL"})
    assert info["NL"]["entsoe"] and frames["NL"]["load"].nunique() == 1 and frames["NL"]["load"].iloc[0] == 300.0
    assert frames["EU"]["load"].nunique() == 1
    kb[0]["shift_per_day"] = ""
    frames = {"NL": nl, "EU": eu}
    assert build.apply_known_breaks(frames, kb, replaced={"NL"}) == {} and frames["EU"]["load"].iloc[0] == 8000.0
