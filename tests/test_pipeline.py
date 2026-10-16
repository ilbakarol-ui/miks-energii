import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from mixtracker import build, forecast, prices, store, trends, update
from mixtracker.categories import classify
from mixtracker.energycharts import parse_public_power, to_daily


@pytest.mark.parametrize("name,cat", [
    ("Hydro Run-of-River", "hydro_ror"),
    ("Hydro water reservoir", "hydro_reservoir"),
    ("Hydro pumped storage", "hydro_pumped"),
    ("Hydro pumped storage consumption", "storage_charge"),
    ("Fossil brown coal / lignite", "lignite"),
    ("Fossil hard coal", "hard_coal"),
    ("Fossil coal-derived gas", "hard_coal"),
    ("Fossil gas", "gas"),
    ("Fossil oil", "oil"),
    ("Fossil oil shale", "peat_shale"),
    ("Fossil peat", "peat_shale"),
    ("Biomass", "biomass"),
    ("Nuclear", "nuclear"),
    ("Wind offshore", "wind_offshore"),
    ("Wind onshore", "wind_onshore"),
    ("Solar", "solar"),
    ("Geothermal", "geothermal"),
    ("Waste", "waste"),
    ("Others", "misc"),
    ("Other renewables", "other_renewable"),
    ("Battery Storage (Discharge)", "battery"),
    ("Battery Storage (Charge)", "storage_charge"),
    ("Load", "load"),
    ("Residual load", None),
    ("Renewable share of generation", None),
    ("Renewable share of load", None),
    ("Cross border electricity trading", "xborder"),
])
def test_classify(name, cat):
    assert classify(name) == cat


def fake_payload(country, start, end, tz="Europe/Warsaw", step_min=15, data_from=None):
    a = pd.Timestamp(start).tz_localize(tz)
    b = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(tz)
    if data_from is not None:
        a = max(a, pd.Timestamp(data_from).tz_localize(tz))
    if a >= b:
        return {"unix_seconds": [], "production_types": [], "deprecated": False}
    idx = pd.date_range(a, b, freq=f"{step_min}min", inclusive="left")
    n = len(idx)
    hour = idx.tz_convert(tz).hour.to_numpy()
    solar = np.where((hour >= 7) & (hour < 19), 1000.0, 0.0)
    pt = [
        {"name": "Fossil hard coal", "data": [2000.0] * n},
        {"name": "Fossil brown coal / lignite", "data": [1000.0] * n},
        {"name": "Wind onshore", "data": [500.0] * n},
        {"name": "Solar", "data": solar.tolist()},
        {"name": "Hydro pumped storage consumption", "data": [-100.0] * n},
        {"name": "Load", "data": [4000.0] * n},
        {"name": "Residual load", "data": [3000.0] * n},
        {"name": "Cross border electricity trading", "data": [600.0] * n},
        {"name": "Renewable share of load", "data": [20.0] * n},
    ]
    return {"unix_seconds": idx.as_unit("s").asi8.tolist(), "production_types": pt, "deprecated": False}


def test_daily_integration_and_dst():
    mw, names = parse_public_power(fake_payload("pl", dt.date(2025, 10, 25), dt.date(2025, 10, 27)))
    assert "Residual load" in names
    d = to_daily(mw, "Europe/Warsaw")
    # 26.10.2025 ma 25 godzin (zmiana czasu)
    assert d.loc["2025-10-26", "hard_coal"] == pytest.approx(2000 * 25)
    assert d.loc["2025-10-25", "hard_coal"] == pytest.approx(2000 * 24)
    assert d.loc["2025-10-25", "solar"] == pytest.approx(1000 * 12)
    assert d.loc["2025-10-25", "storage_charge"] == pytest.approx(100 * 24)   # wartość dodatnia
    assert d.loc["2025-10-25", "load"] == pytest.approx(4000 * 24)
    assert (d["cov"] == 1.0).all()


def test_gap_scaling():
    p = fake_payload("pl", dt.date(2025, 6, 1), dt.date(2025, 6, 1))
    # usuń 2 godziny danych węgla
    for pt in p["production_types"]:
        if pt["name"] == "Fossil hard coal":
            pt["data"][40:48] = [None] * 8
    d = to_daily(parse_public_power(p)[0], "Europe/Warsaw")
    assert d.loc["2025-06-01", "hard_coal"] == pytest.approx(2000 * 24)


def fake_price(start, end, tz="Europe/Warsaw", data_from=None, step_min=60):
    a = pd.Timestamp(start).tz_localize(tz)
    b = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(tz)
    if data_from is not None:
        a = max(a, pd.Timestamp(data_from).tz_localize(tz))
    if a >= b:
        return {}
    idx = pd.date_range(a, b, freq=f"{step_min}min", inclusive="left")
    hour = idx.tz_convert(tz).hour.to_numpy()
    price = np.where((hour >= 7) & (hour < 19), 50.0, 150.0)   # tanio w dzień (słońce), drogo w nocy
    price = np.where(hour == 13, -10.0, price)
    return {"unix_seconds": idx.as_unit("s").asi8.tolist(), "price": price.tolist(), "unit": "EUR / MWh"}


class FakeClient:
    interval = 0.0

    def __init__(self, data_from, price_from=None, forecasts=True):
        self.requests_made = 0
        self.calls = []
        self.data_from = data_from
        self.price_from = price_from
        self.forecasts = forecasts
        self.by_endpoint = {}

    def public_power(self, country, start, end):
        self.requests_made += 1
        self.calls.append("power")
        return fake_payload(country, start, end, data_from=self.data_from)

    def price(self, zone, start, end):
        self.requests_made += 1
        self.calls.append("price")
        return fake_price(start, end, data_from=self.price_from or self.data_from)

    def forecast(self, country, ptype, ftype, start=None, end=None, v2=False):
        self.requests_made += 1
        self.calls.append("forecast")
        if not self.forecasts or v2:
            return {}
        idx = pd.date_range(pd.Timestamp(start).tz_localize("UTC"), periods=24 * 8, freq="h")
        return {"unix_seconds": idx.as_unit("s").asi8.tolist(), "forecast_values": [1000.0] * len(idx)}


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DAILY", tmp_path / "daily")
    monkeypatch.setattr(store, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(store, "PRICES", tmp_path / "prices")
    monkeypatch.setattr(store, "FORECAST", tmp_path / "forecast")
    monkeypatch.setattr(forecast, "ARCHIVE", tmp_path / "forecast" / "archive.csv")
    return tmp_path


def test_update_backfill_and_floor(tmp_store):
    settings = store.load_settings()
    client = FakeClient(data_from=dt.date(dt.date.today().year - 2, 3, 1))
    res = update.update_all(settings, budget_minutes=5, only=["PL"], client=client)
    df = store.read_daily("PL")
    assert df.index.min().date() == dt.date(dt.date.today().year - 2, 3, 1)
    assert df.index.max().date() == dt.date.today() - dt.timedelta(days=1) or \
        df.index.max().date() == update._today("Europe/Warsaw") - dt.timedelta(days=1)
    st = store.read_state()["PL"]
    assert st["floor"] <= "%d-03-01" % (dt.date.today().year - 2)
    assert st["history_complete"] is True
    assert res["requests"] >= 3
    # ceny i ceny ważone zapisane razem z produkcją
    assert df["price_avg"].notna().all()
    # drugi przebieg: tylko ostatnie dni (produkcja + cena), bez backfillu, prognozy znane
    client2 = FakeClient(data_from=None)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client2)
    assert client2.calls.count("power") == 1 and client2.calls.count("price") == 1


def test_prices_capture():
    p = fake_payload("pl", dt.date(2025, 6, 2), dt.date(2025, 6, 2))
    mw, _ = parse_public_power(p)
    price = prices.parse_price(fake_price(dt.date(2025, 6, 2), dt.date(2025, 6, 2)))
    d = prices.daily_prices(price, mw, "Europe/Warsaw")
    row = d.loc["2025-06-02"]
    assert row["price_avg"] == pytest.approx((11 * 50 + 12 * 150 - 10) / 24)
    assert row["price_neg_h"] == pytest.approx(1.0)
    # słońce 7-19: 11 h po 50 i 1 h po -10
    assert 1000 * row["pw_solar"] / row["pq_solar"] == pytest.approx((11 * 50 - 10) / 12)
    # zużycie stałe: ważona = średnia
    assert 1000 * row["pw_load"] / row["pq_load"] == pytest.approx(row["price_avg"])
    # cena 15-min przy danych godzinowych - uśrednianie
    q = prices.parse_price(fake_price(dt.date(2025, 6, 2), dt.date(2025, 6, 2), step_min=15))
    hourly = mw.resample("1h").mean()
    d2 = prices.daily_prices(q, hourly, "Europe/Warsaw")
    assert 1000 * d2.loc["2025-06-02", "pw_load"] / d2.loc["2025-06-02", "pq_load"] == pytest.approx(row["price_avg"])


def test_price_licenses():
    z = prices.load_zones(["cc_by"])
    assert [x["zone"] for x in z["PL"]] == ["PL"]
    assert "ES" not in z and [x["zone"] for x in z["IT"]] == ["IT-North"]
    assert len(prices.load_zones(["cc_by", "private"])["IT"]) > 1


def test_forecast_parse_and_daily():
    idx = pd.date_range("2026-10-17", periods=48, freq="h", tz="Europe/Warsaw")
    s = forecast.parse_forecast({"unix_seconds": idx.as_unit("s").asi8.tolist(), "forecast_values": [500.0] * 48})
    e = forecast.daily_energy(s, "Europe/Warsaw")
    assert list(e.round(3)) == [12.0, 12.0]
    v2 = {"data": [{"timestamp": t.isoformat(), "values": {"solar": 1000.0}} for t in idx[:24]], "series": [{"id": "solar"}]}
    assert forecast.daily_energy(forecast.parse_forecast(v2), "Europe/Warsaw").iloc[0] == pytest.approx(24.0)


def make_constant(days_back=6 * 366):
    end = dt.date(2026, 10, 15)
    idx = pd.date_range(end - dt.timedelta(days=days_back), end, freq="D")
    df = pd.DataFrame(0.0, index=idx, columns=store.COLUMNS)
    df["cov"] = 1.0
    df["hard_coal"] = 2000.0
    df["wind_onshore"] = 1000.0
    df["load"] = 3300.0
    df[list(prices.PRICE_COLS)] = np.nan
    df.index.name = "date"
    return df


def test_trends_constant():
    e, est = trends.derive(make_constant(), 0.8)
    assert not est
    ref = dt.date(2026, 10, 15)
    p = trends.reference_periods(ref)
    assert p["week"]["start"] == dt.date(2026, 10, 5) and p["week"]["end"] == dt.date(2026, 10, 11)
    assert p["month"]["start"] == dt.date(2026, 9, 1) and p["month"]["end"] == dt.date(2026, 9, 30)
    w = trends.compare(e, "week", p["week"], 5, 3)
    assert w["gen_total"][0] == pytest.approx(21.0)          # 3 GWh x 7 dni
    assert w["gen_total"][2] == pytest.approx(21.0)          # rok temu
    assert w["gen_total"][3] == pytest.approx(21.0)          # średnia 5 lat
    assert w["share_renewable"][0] == pytest.approx(100 / 3, abs=0.01)
    assert w["net_import"][0] == pytest.approx(2.1)           # 0.3 GWh x 7
    # emisyjność: 2 GWh x 0,9 t/MWh = 1,8 kt dziennie na 3 GWh produkcji = 600 g/kWh
    assert w["intensity"][0] == pytest.approx(600.0)
    assert w["co2"][0] == pytest.approx(1.8 * 7)
    assert w["price_avg"][0] is None   # brak cen w danych
    m = trends.compare(e, "month", p["month"], 5, 3)
    assert m["load"][0] == pytest.approx(3.3 * 30)
    assert m["load"][1] == pytest.approx(3.3 * 31)            # sierpień


def test_build_and_render(tmp_path, monkeypatch):
    folder = tmp_path / "daily"
    store.write_daily("PL", make_constant(), folder)
    monkeypatch.setattr(store, "load_countries", lambda path=None: [
        {"code": "PL", "name": "Polska", "ec": "pl", "eurostat": "PL", "tz": "Europe/Warsaw", "group": "UE"},
        {"code": "MT", "name": "Malta", "ec": "", "eurostat": "MT", "tz": "Europe/Malta", "group": "UE"},
    ])
    monkeypatch.setattr(store, "STATE", tmp_path / "state.json")
    settings = store.load_settings()
    payload = build.build(settings, None, {}, folder=folder)
    assert payload["meta"]["ref"] == "2026-10-15"
    assert "PL" in payload["summary"] and "MT" not in payload["summary"]
    out = build.render(payload, tmp_path / "site")
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "/*__DATA__*/" not in html
    json.loads((tmp_path / "site" / "data.json").read_text(encoding="utf-8"))


def test_migration_adds_prices_and_forecast(tmp_store):
    """Dane z wersji bez cen: backfill dociąga ceny dla całej historii; prognoza zapisana."""
    settings = store.load_settings()
    start = dt.date(dt.date.today().year - 1, 6, 1)
    mw, _ = parse_public_power(fake_payload("pl", start, dt.date.today() - dt.timedelta(days=1)))
    old = to_daily(mw, "Europe/Warsaw")
    store.write_daily("PL", old)
    st = {"PL": {"floor": start.isoformat()}}
    store.write_state(st)
    client = FakeClient(data_from=start)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client)
    df = store.read_daily("PL")
    assert df.loc[pd.Timestamp(start):, "price_avg"].notna().all()
    assert store.read_state()["PL"]["history_complete"] is True
    fc = json.loads((store.FORECAST / "PL.json").read_text())
    assert fc["solar"][0] == pytest.approx(24.0) and fc["wind"][0] == pytest.approx(48.0)
    assert (store.FORECAST / "archive.csv").exists()
