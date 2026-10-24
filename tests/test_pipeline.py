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


def _day_hours(d, tz="Europe/Warsaw"):
    """Liczba godzin doby lokalnej (23 lub 25 przy zmianie czasu)."""
    a = pd.Timestamp(d).tz_localize(tz)
    b = pd.Timestamp(d + dt.timedelta(days=1)).tz_localize(tz)
    return (b - a) / pd.Timedelta(hours=1)


def test_migration_adds_prices_and_forecast(tmp_store):
    """Dane z wersji bez cen: backfill dociąga ceny dla całej historii; prognoza zapisana."""
    settings = store.load_settings()
    start = dt.date(dt.date.today().year - 1, 6, 1)
    mw, _ = parse_public_power(fake_payload("pl", start, dt.date.today() - dt.timedelta(days=1)))
    old = to_daily(mw, "Europe/Warsaw")
    store.write_daily("PL", old)
    st = {"PL": {"floor": start.isoformat()}}
    store.write_state(st)
    settings["forecasts_energycharts"] = True
    client = FakeClient(data_from=start)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client)
    df = store.read_daily("PL")
    assert df.loc[pd.Timestamp(start):, "price_avg"].notna().all()
    assert store.read_state()["PL"]["history_complete"] is True
    fc = json.loads((store.FORECAST / "PL.json").read_text())
    # liczba godzin doby zależy od zmiany czasu (25.10.2026 ma 25 h)
    hours = _day_hours(dt.date.fromisoformat(fc["days"][0]))
    assert fc["solar"][0] == pytest.approx(hours) and fc["wind"][0] == pytest.approx(2 * hours)
    assert (store.FORECAST / "archive.csv").exists()


def test_mixed_resolution():
    """Zakres z danymi godzinowymi i 15-minutowymi (zmiana rozdzielczości w źródle)."""
    a = fake_payload("pl", dt.date(2025, 9, 28), dt.date(2025, 9, 30), step_min=60)
    b = fake_payload("pl", dt.date(2025, 10, 1), dt.date(2025, 10, 3), step_min=15)
    merged = {"unix_seconds": a["unix_seconds"] + b["unix_seconds"],
              "production_types": [{"name": x["name"], "data": x["data"] + y["data"]}
                                   for x, y in zip(a["production_types"], b["production_types"])]}
    mw, _ = parse_public_power(merged)
    d = to_daily(mw, "Europe/Warsaw")
    assert (d["cov"] == 1.0).all()
    assert d["hard_coal"].round(3).eq(2000 * 24).all()
    # ceny: godzinowe do 30.09, 15-minutowe od 1.10
    pa = fake_price(dt.date(2025, 9, 28), dt.date(2025, 9, 30), step_min=60)
    pb = fake_price(dt.date(2025, 10, 1), dt.date(2025, 10, 3), step_min=15)
    price = prices.parse_price({"unix_seconds": pa["unix_seconds"] + pb["unix_seconds"],
                                "price": pa["price"] + pb["price"], "unit": "EUR / MWh"})
    dp = prices.daily_prices(price, mw, "Europe/Warsaw")
    exp = (11 * 50 + 12 * 150 - 10) / 24
    assert dp["price_avg"].notna().all()
    assert dp.loc["2025-09-29", "price_avg"] == pytest.approx(exp)
    assert 1000 * dp.loc["2025-10-02", "pw_load"] / dp.loc["2025-10-02", "pq_load"] == pytest.approx(exp)
    assert 1000 * dp.loc["2025-09-29", "pw_solar"] / dp.loc["2025-09-29", "pq_solar"] == pytest.approx((11 * 50 - 10) / 12)


def test_repair_bad_ranges(tmp_store):
    """Dni zapisane z błędnym pokryciem są wykrywane i pobierane ponownie."""
    settings = store.load_settings()
    start = dt.date(dt.date.today().year - 1, 6, 1)
    client = FakeClient(data_from=start)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client)
    df = store.read_daily("PL")
    bad = df.index[(df.index >= pd.Timestamp(start) + pd.Timedelta(days=40)) & (df.index < pd.Timestamp(start) + pd.Timedelta(days=50))]
    df.loc[bad, "cov"] = 0.25
    df.loc[bad[:3], "price_avg"] = np.nan
    store.write_daily("PL", df)
    st = store.read_state(); st["PL"]["floor"] = start.isoformat(); store.write_state(st)
    ranges = update.bad_ranges(df, [{"zone": "PL"}], st["PL"], dt.date(2021, 1, 1), dt.date.today() - dt.timedelta(days=20), settings)
    assert ranges == [(bad[0].date(), bad[-1].date())]
    client2 = FakeClient(data_from=start)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client2)
    df2 = store.read_daily("PL")
    assert (df2.loc[bad, "cov"] == 1.0).all() and df2.loc[bad, "price_avg"].notna().all()
    # ponowny przebieg: nic do naprawy, brak dodatkowych zapytań poza bieżącymi
    client3 = FakeClient(data_from=start)
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client3)
    assert client3.calls.count("power") == 1


def test_weather_model():
    from mixtracker import weather
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-06-01", "2026-10-15", freq="D")
    f = pd.DataFrame({"ws": rng.uniform(2, 14, len(idx)), "ghi": rng.uniform(0.5, 7, len(idx)),
                      "temp": 10 + 10 * np.sin(np.arange(len(idx)) / 58)}, index=idx)
    f["cf"] = weather.turbine_cf(f["ws"])
    f["hdd"] = (15.5 - f["temp"]).clip(lower=0)
    f["cdd"] = (f["temp"] - 22).clip(lower=0)
    e = pd.DataFrame(index=idx)
    e["wind"] = 5 + 80 * f["cf"]
    e["solar"] = 6 * f["ghi"]
    e["load"] = 400 + 8 * f["hdd"] - 60 * (idx.dayofweek == 6)
    e["gen_total"] = e["wind"] + e["solar"] + 300
    hist = f.loc[:"2026-10-15"]
    fut = f.loc["2026-10-09":"2026-10-15"].copy()
    fut.index = pd.date_range("2026-10-16", periods=7, freq="D")
    fc = weather.forecast_country("PL", hist[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 16))
    assert len(fc["days"]) == 7 and fc["days"][0] == "2026-10-16"
    assert fc["wind"][0] == pytest.approx(5 + 80 * fut["cf"].iloc[0], rel=1e-3)
    assert fc["solar"][3] == pytest.approx(6 * fut["ghi"].iloc[3], rel=1e-3)
    assert fc["errors"]["load"] < 0.5 and fc["errors"]["wind"] < 0.5


def test_weather_daily_features():
    from mixtracker import weather
    idx = pd.date_range("2026-10-15 22:00", periods=48, freq="h", tz="UTC")
    df = pd.DataFrame({"wind_speed_100m": 12.0, "shortwave_radiation": 100.0, "temperature_2m": 5.0}, index=idx)
    pts = [{"lat": 0, "lon": 0, "kind": "all"}, {"lat": 0, "lon": 0, "kind": "wind"}]
    d = weather.daily_features([df, df.assign(wind_speed_100m=3.0)], pts, "Europe/Warsaw")
    assert list(d.index.strftime("%Y-%m-%d")) == ["2026-10-16", "2026-10-17"]
    assert d["cf"].iloc[0] == pytest.approx(0.5) and d["ghi"].iloc[0] == pytest.approx(2.4)
    assert d["hdd"].iloc[0] == pytest.approx(10.5)


def test_detect_break():
    idx = pd.date_range("2026-06-01", "2026-10-15", freq="D")
    e = pd.DataFrame({"load": np.where(idx < pd.Timestamp("2026-09-18"), 330.0, 220.0)}, index=idx)
    b = trends.detect_break(e, dt.date(2026, 10, 15))
    assert b["date"] == "2026-09-18" and b["change"] == pytest.approx(-33.3, abs=0.1)
    e2 = pd.DataFrame({"load": 300 + 10 * np.sin(np.arange(len(idx)) / 7)}, index=idx)
    assert trends.detect_break(e2, dt.date(2026, 10, 15)) is None


def fake_elexon(dataset, params):
    """Rekordy jak w Elexon Insights. FUELHH po dniach rozliczeniowych, AGPT po czasie publikacji."""
    if dataset == "FUELHH":
        a = pd.Timestamp(params["settlementDateFrom"]).tz_localize("Europe/London")
        b = (pd.Timestamp(params["settlementDateTo"]) + pd.Timedelta(days=1)).tz_localize("Europe/London")
    else:
        a, b = pd.Timestamp(params["publishDateTimeFrom"]), pd.Timestamp(params["publishDateTimeTo"])
    idx = pd.date_range(a.tz_convert("UTC").ceil("30min"), b.tz_convert("UTC"), freq="30min", inclusive="left")
    recs = []
    for t in idx:
        if dataset == "FUELHH":
            for name, q in [("CCGT", 10000.0), ("OCGT", 1000.0), ("NUCLEAR", 5000.0), ("WIND", 9000.0),
                            ("BIOMASS", 2000.0), ("PS", 0.0), ("INTFR", 2000.0), ("INTNSL", -500.0)]:
                recs.append({"dataset": "FUELHH", "publishTime": t.isoformat(), "startTime": t.isoformat(),
                             "fuelType": name, "generation": q})
            # korekta: późniejsza publikacja dla CCGT
            recs.append({"dataset": "FUELHH", "publishTime": (t + pd.Timedelta(hours=1)).isoformat(),
                         "startTime": t.isoformat(), "fuelType": "CCGT", "generation": 10500.0})
        else:
            for name, q in [("Solar", 1000.0), ("Wind Offshore", 6000.0), ("Wind Onshore", 3000.0), ("Fossil Gas", 1.0)]:
                recs.append({"dataset": "AGPT", "documentRevisionNumber": 1, "publishTime": t.isoformat(),
                             "businessType": "Production", "psrType": name, "quantity": q, "startTime": t.isoformat()})
    return recs


def test_elexon_gb(tmp_store, monkeypatch):
    from mixtracker import elexon
    monkeypatch.setattr(elexon, "_get", fake_elexon)
    monkeypatch.setattr(elexon.time, "sleep", lambda s: None)
    d, names = elexon.fetch_days(dt.date(2025, 10, 25), dt.date(2025, 10, 27))   # 26.10.2025 - zmiana czasu (25 h)
    assert "WIND" in names
    assert d.loc["2025-10-25", "gas"] == pytest.approx(11500 * 24)
    assert d.loc["2025-10-26", "gas"] == pytest.approx(11500 * 25)
    assert d.loc["2025-10-25", "wind_offshore"] == pytest.approx(9000 * 24 * 2 / 3)
    assert d.loc["2025-10-25", "solar"] == pytest.approx(1000 * 24)
    assert d.loc["2025-10-25", "xborder"] == pytest.approx(1500 * 24)
    assert d["load"].isna().all() and (d["cov"] == 1.0).all()
    e, est = trends.derive(d, 0.8)
    assert est and e.loc["2025-10-25", "load"] == pytest.approx((11500 + 5000 + 9000 + 2000 + 1000 + 1500) * 24 / 1000)
    settings = store.load_settings()
    settings["history_years"] = 0          # historia tylko od 1 stycznia bieżącego roku
    res = elexon.update(settings, [], budget_minutes=5)
    df = store.read_daily("GB")
    assert res["history_complete"] and df.index.min() == pd.Timestamp(dt.date.today().year, 1, 1)
    assert df["cov"].min() >= 0.8


def test_weather_update_first_run(tmp_store, monkeypatch):
    """Pierwszy przebieg: pusty cache pogody + historia z archiwum + prognoza (regresja błędu dtype)."""
    from mixtracker import weather
    monkeypatch.setattr(weather, "WEATHER", tmp_store / "weather")
    rng = np.random.default_rng(2)

    def fake_points(points, url, extra):
        if "start_date" in extra:
            a = pd.Timestamp(extra["start_date"]); b = pd.Timestamp(extra["end_date"]) + pd.Timedelta(days=1)
        else:
            today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
            a = today - pd.Timedelta(days=extra["past_days"]); b = today + pd.Timedelta(days=extra["forecast_days"])
        idx = pd.date_range(a, b, freq="h", inclusive="left", tz="UTC")
        return [pd.DataFrame({"wind_speed_100m": rng.uniform(2, 14, len(idx)),
                              "shortwave_radiation": rng.uniform(0, 300, len(idx)),
                              "temperature_2m": rng.uniform(0, 20, len(idx))}, index=idx) for _ in points]

    monkeypatch.setattr(weather, "fetch_points", fake_points)
    end = dt.date.today() - dt.timedelta(days=1)
    idx = pd.date_range(end - dt.timedelta(days=500), end, freq="D")
    df = pd.DataFrame(0.0, index=idx, columns=store.COLUMNS)
    df["cov"] = 1.0
    df["wind_onshore"] = rng.uniform(1e4, 9e4, len(idx)); df["solar"] = rng.uniform(1e4, 5e4, len(idx))
    df["gas"] = 2e5; df["load"] = 4e5
    df.index.name = "date"
    store.write_daily("PL", df)
    countries = [c for c in store.load_countries() if c["code"] == "PL"]
    log = []
    res = weather.update(store.load_settings(), {"PL": store.read_daily("PL")}, trends.derive, countries, log)
    assert not [m for m in log if "błąd" in m], log
    assert "PL" in res and len(res["PL"]["days"]) == 7 and all(v is not None for v in res["PL"]["load"])
    assert set(res["PL"]["errors"]) == {"wind", "solar", "load"}


def test_movers_intensity_threshold():
    def s(cur, ly):
        base = {k: [None] * 6 for k in ["load"] + ["share_" + g for g in trends.GROUP_KEYS] + ["share_renewable"]}
        base["intensity"] = [cur, None, ly, None, None, None]
        return {"week": {**base, "_complete": True}}
    m = trends.movers({"FR": s(16.0, 7.5), "BE": s(183.0, 79.0)}, {"FR": "Francja", "BE": "Belgia"}, {}, 10)
    assert [x["code"] for x in m] == ["BE"]


def test_price_ahead_and_map(tmp_store):
    """Ceny strefowe pobierane do jutra (rynek dnia następnego) i dane mapy cen."""
    settings = store.load_settings()
    client = FakeClient(data_from=dt.date.today() - dt.timedelta(days=60))
    update.update_all(settings, budget_minutes=5, only=["PL"], client=client)
    tomorrow = update._today("Europe/Warsaw") + dt.timedelta(days=1)
    z = store.read_zone("PL")
    assert z.index.max().date() == tomorrow
    # dane dzienne kraju kończą się na wczoraj (dziś i jutro nie mają jeszcze produkcji)
    assert store.read_daily("PL").index.max().date() < update._today("Europe/Warsaw")
    pm = build.price_map(prices.load_zones(["cc_by"]), prices.all_zones(), store.load_countries(), days=10)
    assert pm["dates"][-1] == tomorrow.isoformat() and len(pm["dates"]) == 10
    h = _day_hours(tomorrow)   # godzina dodana/usunięta przy zmianie czasu to godzina nocna (150)
    assert pm["countries"]["PL"]["avg"][-1] == pytest.approx((11 * 50 + (h - 12) * 150 - 10) / h, abs=0.1)
    assert pm["countries"]["ES"]["zones"] == [] and pm["countries"]["ES"]["all"] == ["ES"]
    assert "PL" in pm["geo"]["countries"] and pm["geo"]["w"] > 0


def test_gb_resplit_and_break_model():
    from mixtracker import elexon, weather
    idx = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    df = pd.DataFrame(0.0, index=idx, columns=store.COLUMNS)
    df["wind_offshore"] = 600.0; df["wind_onshore"] = 400.0
    df.loc["2024-05":"2024-10", ["wind_offshore", "wind_onshore"]] = [150.0, 850.0]   # luka offshore w AGPT
    r = elexon.resplit_stored(df)
    assert (r["wind_offshore"] + r["wind_onshore"]).eq(1000).all()
    assert r.loc["2024-07-01", "wind_offshore"] == pytest.approx(600.0)
    # model zużycia po skoku w danych uczy się tylko na nowym poziomie
    rng = np.random.default_rng(3)
    idx = pd.date_range("2025-09-01", "2026-10-15", freq="D")
    f = pd.DataFrame({"ws": 8.0, "ghi": 3.0, "temp": 10 + 8 * np.sin(np.arange(len(idx)) / 58)}, index=idx)
    f["cf"] = weather.turbine_cf(f["ws"]); f["hdd"] = (15.5 - f["temp"]).clip(lower=0); f["cdd"] = 0.0
    e = pd.DataFrame(index=idx)
    e["load"] = np.where(idx < pd.Timestamp("2026-09-18"), 330.0, 220.0) + 3 * f["hdd"] + rng.normal(0, 1, len(idx))
    e["wind"] = 50.0; e["solar"] = 10.0; e["gen_total"] = 300.0
    fut = f.iloc[-7:].copy(); fut.index = pd.date_range("2026-10-16", periods=7, freq="D")
    fc = weather.forecast_country("NL", f[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 16))
    assert 200 < fc["load"][0] < 270 and "load" in fc["notes"]


def test_wind_hourly_and_missing_weather():
    from mixtracker import weather
    rng = np.random.default_rng(5)
    idx = pd.date_range("2025-06-01", "2026-10-18", freq="D")
    f = pd.DataFrame({"ws": rng.uniform(2, 14, len(idx)), "ghi": 3.0, "temp": 10.0}, index=idx)
    f["cf"] = weather.turbine_cf(f["ws"]); f["hdd"] = 5.5; f["cdd"] = 0.0
    e = pd.DataFrame({"wind": 5 + 80 * f["cf"], "solar": 18.0, "load": 400.0, "gen_total": 500.0}, index=idx)
    fut = f.iloc[-7:].copy(); fut.index = pd.date_range("2026-10-19", periods=7, freq="D")
    fut.iloc[4:, :] = np.nan                                          # brak prognozy pogody od 5. dnia
    hidx = pd.date_range(pd.Timestamp("2026-10-19").tz_localize("Europe/Warsaw").tz_convert("UTC"), periods=168, freq="h")
    hws = rng.uniform(3, 15, 168); hws[96:] = np.nan
    hw = pd.DataFrame({"cf": weather.turbine_cf(hws), "ws": hws}, index=hidx)
    fc = weather.forecast_country("PL", f[weather.FEATURES], fut[weather.FEATURES], e, dt.date(2026, 10, 19),
                                  hourly=hw, tz="Europe/Warsaw")
    assert fc["wind"][4:] == [None, None, None] and all(v is not None for v in fc["wind"][:4])
    mw = fc["wind_hourly"]["mw"]
    assert len(mw) == 168 and mw[100] is None
    for d in range(4):
        assert sum(mw[d * 24:(d + 1) * 24]) / 1000 == pytest.approx(fc["wind"][d], rel=1e-3)


def test_verify_scores():
    from mixtracker import verify
    y = pd.Series([10.0, 20, 30, 40])
    f = pd.Series([12.0, 18, 33, 40])
    r = pd.Series([20.0, 10, 20, 30])
    sc = verify.scores(f, y, {"pers": r})
    assert sc["n"] == 4
    assert sc["wape"] == pytest.approx(100 * 7 / 100)          # Σ|f−y| = 2+2+3+0
    assert sc["bias"] == pytest.approx(100 * (103 / 100 - 1))
    assert sc["wape_pers"] == pytest.approx(100 * 40 / 100)
    assert sc["skill_pers"] == pytest.approx(100 * (1 - 7 / 40))
    assert verify.scores(f.iloc[:2], y.iloc[:2]) is None         # za mało dni


def _synthetic(code_shift=0.0):
    from mixtracker import weather
    rng = np.random.default_rng(3)
    idx = pd.date_range("2021-01-01", "2026-10-18", freq="D")
    f = pd.DataFrame({"ws": rng.uniform(2, 14, len(idx)), "ghi": 3.5 + 3 * np.sin(2 * np.pi * (idx.dayofyear - 80) / 365),
                      "temp": 10 + 10 * np.sin(2 * np.pi * (idx.dayofyear - 110) / 365)}, index=idx)
    f["cf"] = weather.turbine_cf(f["ws"])
    f["hdd"] = (15.5 - f["temp"]).clip(lower=0)
    f["cdd"] = (f["temp"] - 22).clip(lower=0)
    e = pd.DataFrame(index=idx)
    e["wind"] = 5 + 80 * f["cf"] + code_shift
    e["solar"] = 6 * f["ghi"]
    e["load"] = 400 + 8 * f["hdd"] - 60 * (idx.dayofweek == 6)
    return f[weather.FEATURES], e


def test_verify_backtest_and_eu():
    from mixtracker import verify
    f, e = _synthetic()
    r = verify.backtest(f, e, "wind", pd.Timestamp("2026-10-18"), days=60)
    assert r["n"] == 60 and r["period"] == ["2026-08-20", "2026-10-18"]
    assert r["wape"] < 1                                    # model idealny na danych syntetycznych
    assert r["skill_pers"] > 90 and r["skill_clim"] > 90    # losowy wiatr: persystencja i klimatologia słabe
    assert len(r["series"]["dates"]) == 60 and len(r["weekly"]["weeks"]) >= 8
    assert "_raw" in r
    f2, e2 = _synthetic(10.0)
    out = verify.verify_all({"PL": f, "DE": f2}, {"PL": e, "DE": e2}, None, dt.date(2026, 10, 18), days=30)
    assert set(out["backtest"]) == {"PL", "DE", "EU"}
    eu = out["backtest"]["EU"]["wind"]
    assert eu["countries"] == 2 and eu["n"] == 30
    pl, de = out["backtest"]["PL"]["wind"], out["backtest"]["DE"]["wind"]
    assert eu["series"]["act"][-1] == pytest.approx(pl["series"]["act"][-1] + de["series"]["act"][-1], abs=0.02)
    assert "_raw" not in pl
    json.dumps(out)                                          # payload musi być serializowalny


def test_verify_operational():
    from mixtracker import verify
    days = pd.date_range("2026-10-01", "2026-10-20", freq="D")
    act = {"PL": pd.DataFrame({"wind": np.arange(len(days), dtype=float) + 100}, index=days),
           "DE": pd.DataFrame({"wind": np.full(len(days), 300.0)}, index=days)}
    rows = []
    today = dt.date.today()
    for code in ("PL", "DE", "EU"):
        for i in range(5, 15):
            issued = days[i]
            for lead in range(3):
                d = issued + pd.Timedelta(days=lead)
                true = 0 if code == "EU" else act[code]["wind"][d]
                rows.append({"issued": issued.strftime("%Y-%m-%d"), "code": code, "date": d.strftime("%Y-%m-%d"),
                             "type": "wind", "gwh": true * (1 + 0.1 * lead)})
    arch = pd.DataFrame(rows)
    # okno wsteczne liczone od dziś - przesuwamy daty tak, by mieściły się w ostatnich 90 dniach
    shift = (pd.Timestamp(today) - pd.Timestamp("2026-10-20")).days
    for c in ("issued", "date"):
        arch[c] = (pd.to_datetime(arch[c]) + pd.Timedelta(days=shift)).dt.strftime("%Y-%m-%d")
    act = {k: v.set_axis(v.index + pd.Timedelta(days=shift)) for k, v in act.items()}
    out = verify.operational(arch, act)
    pl = out["PL"]["wind"]
    assert pl["leads"]["0"]["wape"] == pytest.approx(0)
    assert pl["leads"]["1"]["bias"] == pytest.approx(10)
    assert pl["leads"]["2"]["wape"] == pytest.approx(20)
    assert pl["issues"] == 10 and pl["leads"]["0"]["n"] == 10
    # persystencja: PL rośnie o 1 GWh dziennie, więc dla wyprzedzenia 0 jej błąd = 1 GWh na dzień
    assert pl["leads"]["0"]["skill_pers"] == pytest.approx(100)
    eu = out["EU"]["wind"]                                  # suma krajów, nie wiersze "EU" z archiwum
    assert eu["countries"] == 2 and eu["leads"]["1"]["bias"] == pytest.approx(10)


def test_model_calendar():
    from mixtracker import model
    idx = pd.date_range("2026-01-01", "2026-12-31", freq="D")
    c = model.calendar(idx, "PL")
    assert c.loc["2026-11-11", "hol"] == 1            # Święto Niepodległości (środa)
    assert c.loc["2026-01-05", "bridge"] == 1         # poniedziałek przed Trzech Króli (wtorek)
    assert c.loc["2026-12-28", "xmas"] == 1 and c.loc["2026-12-28", "hol"] == 0
    assert c.loc["2026-10-19"].sum() == 0
    assert model.calendar(idx, "GB").loc["2026-08-31", "hol"] == 1     # Summer Bank Holiday (Anglia)
    assert model.calendar(idx, "UK").loc["2026-07-13", "hol"] == 1     # Battle of the Boyne (Irlandia Płn., dzień wolny)


def test_model_gaps_and_correction():
    from mixtracker import model
    f, e = _synthetic()
    # luka w źródle: 40 dni produkcji ze słońca bliskiej zera przy normalnej pogodzie
    y = e["solar"].copy()
    gap = (y.index >= "2026-08-01") & (y.index < "2026-09-10")
    y[gap] = 0.05
    bad = model.bad_days(y, f, "solar")
    assert bad.isin(y.index[gap]).sum() >= 38 and len(bad) <= 42
    m = model.fit(f, y, "solar", pd.Timestamp("2026-09-20"))
    assert m.coef[1] == pytest.approx(6, rel=0.02)    # model nie uczy się luki
    # zużycie: trwały skok poziomu o 15% od 1.10 - korekta z ostatniego dnia przejmuje nowy poziom
    load = e["load"].copy()
    load[load.index >= "2026-10-01"] *= 1.15
    t = pd.Timestamp("2026-10-15")
    m1 = model.fit(f, load, "load", t, code="PL")
    m0 = model.fit(f, load, "load", t, code="PL", ar=False)
    p1 = model.forecast(m1, f, load[load.index < t], [t])
    p0 = model.forecast(m0, f, load[load.index < t], [t])
    err1, err0 = abs(p1.iloc[0] / load[t] - 1), abs(p0.iloc[0] / load[t] - 1)
    assert m1.rho[0] > 0.5 and err1 < 0.5 * err0
    assert all(m1.rho[i] >= m1.rho[i + 1] for i in range(len(m1.rho) - 1))


def test_verify_backtest_leads_and_members():
    from mixtracker import verify
    f, e = _synthetic()
    f2, e2 = _synthetic(10.0)
    out = verify.verify_all({"PL": f, "DE": f2, "NO": f}, {"PL": e, "DE": e2, "NO": e}, None,
                            dt.date(2026, 10, 18), days=30, members={"PL", "DE"})
    r = out["backtest"]["PL"]["load"]
    assert sorted(r["leads"]) == [str(k) for k in range(7)]
    assert out["backtest"]["EU"]["wind"]["countries"] == 2           # NO poza UE-27
    assert sorted(out["backtest"]["EU"]["wind"]["leads"]) == [str(k) for k in range(7)]
    json.dumps(out)


def test_flag_partial_last_day():
    idx = pd.date_range("2026-09-14", "2026-10-18", freq="D")
    df = pd.DataFrame({"cov": 1.0, "load": 30000.0 + 3000 * (idx.dayofweek < 5)}, index=idx)
    out, info = build._flag_partial_last(df.assign(load=df["load"].where(df.index < "2026-10-18", 15000.0)), 0.8)
    assert (info["date"], info["ratio"]) == ("2026-10-18", 0.5)
    assert out.at[pd.Timestamp("2026-10-18"), "cov"] < 0.8 and out.at[pd.Timestamp("2026-10-17"), "cov"] == 1.0
    out, info = build._flag_partial_last(df.assign(load=df["load"].where(df.index < "2026-10-18", 27000.0)), 0.8)
    assert info is None and out.at[pd.Timestamp("2026-10-18"), "cov"] == 1.0
    assert build._flag_partial_last(df.drop(columns="load"), 0.8)[1] is None
    # niepełna doba, która nie jest już ostatnia (kolejna doba przyszła pełna) - nadal oznaczona
    df2 = pd.concat([df, pd.DataFrame({"cov": 1.0, "load": 33000.0}, index=[pd.Timestamp("2026-10-19")])])
    out, info = build._flag_partial_last(df2.assign(load=df2["load"].where(df2.index != "2026-10-18", 15000.0)), 0.8)
    assert info["days"] == ["2026-10-18"] and out.at[pd.Timestamp("2026-10-18"), "cov"] < 0.8
    assert out.at[pd.Timestamp("2026-10-19"), "cov"] == 1.0
    # poza oknem dosyłania (ponad 10 dni) - bez zmian
    out, info = build._flag_partial_last(df2.assign(load=df2["load"].where(df2.index != "2026-10-05", 15000.0)), 0.8)
    assert info is None


def test_afternoon_prices_only(tmp_store):
    settings = store.load_settings()
    client = FakeClient(data_from=dt.date(2020, 1, 1))
    res = update.update_prices_ahead(settings, budget_minutes=5, client=client)
    assert set(client.calls) == {"price"}                       # bez produkcji i prognoz
    zones = {z["zone"] for zl in prices.load_zones(["cc_by"]).values() for z in zl}
    assert set(res["zones"]) == zones and client.requests_made == len(zones)
    pl = store.read_zone("PL")
    tomorrow = pd.Timestamp(dt.date.today() + dt.timedelta(days=1))
    assert tomorrow in pl.index and pl.loc[tomorrow, "price_avg"] > 0
    assert not store.DAILY.exists() or not any(store.DAILY.iterdir())   # dane dzienne krajów nietknięte


def test_load_status_stale_series():
    idx = pd.date_range("2026-08-01", "2026-10-20", freq="D")
    df = pd.DataFrame({"load": 1000.0}, index=idx)
    assert build._load_status(df, False, dt.date(2026, 10, 20)) == "ENTSO-E (obciążenie)"
    s = build._load_status(df.assign(load=df["load"].where(df.index <= "2026-09-20")), False, dt.date(2026, 10, 20))
    assert s.startswith("BRAK od 2026-09-21 (30 dni)")
    assert build._load_status(df, True, dt.date(2026, 10, 20)) == "szacowane z bilansu"
    assert build._load_status(df.assign(load=np.nan), False, dt.date(2026, 10, 20)) == "brak"


def test_movers_same_fact_once():
    def it(code, series, delta, score, kind="week", typ="yoy"):
        return {"code": code, "name": code, "period": kind, "series": series, "label": series, "type": typ,
                "cur": 50.0, "ref": 50.0 - delta, "delta": delta, "score": score}
    items = [it("UK", "wind", 35.4, 35.4), it("UK", "renewable", 35.4, 35.3), it("UK", "gas", -35.1, 35.1),
             it("UK", "solar", 10.0, 10.0), it("UK", "coal", 9.0, 9.0), it("PL", "wind", 5.0, 5.0)]
    out = trends._dedupe_movers(items, 8)
    codes = [(x["code"], x["series"]) for x in out]
    assert codes == [("UK", "wind"), ("UK", "solar"), ("PL", "wind")]


def test_storage_capacity(tmp_path, monkeypatch):
    from mixtracker import storage_cap as sc
    for k, v in {"DIR": tmp_path, "EC_RAW": tmp_path / "ec.csv", "ES_RAW": tmp_path / "es.csv",
                 "OUT": tmp_path / "out.csv", "STATE": tmp_path / "st.json"}.items():
        monkeypatch.setattr(sc, k, v)
    js = {"time": ["2021", "2022", "2023", "2024", "2025", "2026"], "production_types": [
        {"name": "Hydro pumped storage", "data": [10, 10, 10, 9.9, 9.9, 2.0]},          # 2026 niepełny agregat - pominięty
        {"name": "Battery storage (power)", "data": [2.4, 4.2, 8.4, 12.7, 16.9, 21.8]},
        {"name": "Battery storage (capacity)", "data": [3.7, 6.4, 12.5, 18.7, 25.5, 34.2]},
        {"name": "Wind onshore", "data": [1, 2, 3, 4, 5, 6]}]}
    de = sc.parse_ec("DE", js, 2026)
    assert set(de["series"]) == {"pumped_gw", "bat_gw", "bat_gwh"}
    assert 2026 not in de[de.series == "pumped_gw"].year.tolist() and 2026 in de[de.series == "bat_gw"].year.tolist()
    pl = sc.parse_ec("PL", {"time": ["2024"], "production_types": [{"name": "Hydro pumped storage", "data": [1.5]}]}, 2026)
    es = pd.DataFrame([
        ("EU", 2024, "bat_gw", 10.0), ("EU", 2024, "bat_gwh", 19.0), ("DE", 2024, "bat_gw", 0.8), ("DE", 2024, "bat_gwh", 0.8),
        ("PL", 2024, "bat_gw", 0.27), ("PL", 2021, "bat_gw", 0.0), ("PL", 2021, "bat_gwh", 0.0)],
        columns=["code", "year", "series", "value"])
    out = sc.combine(pd.concat([de, pl]), es, ["EU", "DE", "PL"]).set_index(["code", "year"])
    assert out.at[("DE", 2024), "bat_gw"] == 12.7 and out.at[("DE", 2024), "bat_src"].startswith("MaStR")
    assert out.at[("EU", 2024), "bat_gw"] == pytest.approx(10.0 - 0.8 + 12.7)            # Niemcy wg MaStR
    assert out.at[("PL", 2024), "bat_gw"] == 0.27 and out.at[("PL", 2024), "pumped_gw"] == 1.5
    assert pd.isna(out.at[("PL", 2021), "bat_gw"])                                        # zero sprzed 2022 = brak danych
