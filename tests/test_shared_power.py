"""Testy U-20261114-3: pogoda do prognoz Miksu energii we wspólnej bazie (krok „power” Wspólnej pogody)
i odczyt z niej w mixtracker/weather.py (własne zapytanie do Open-Meteo tylko awaryjnie)."""
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from mixtracker import run_daily, shared_weather as sw, store, trends, weather
from tests.test_pipeline import tmp_store  # noqa: F401 (fixture)
from tests.test_shared_weather import fake_get_factory, setup as sw_setup

POINTS = "code,place,lat,lon,kind\nPL,A,52.23,21.01,all\nPL,B,54.50,18.00,wind\nPL,C,50.06,19.94,solar\nDE,D,52.52,13.40,all\n"


def _hourly(lat, lon, past, days):
    """Godzinowa pogoda zależna tylko od punktu i godziny (ta sama w obu ścieżkach pobierania)."""
    today = pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.date_range(today - pd.Timedelta(days=past), today + pd.Timedelta(days=days), freq="h", inclusive="left")
    x = np.arange(len(idx)) + idx[0].value // 3_600_000_000_000        # numer godziny od epoki
    return idx, {"wind_speed_100m": 8 + 5 * np.sin(x / 9.0 + lat), "shortwave_radiation": 150 + 140 * np.sin(x / 3.8 + lon),
                 "temperature_2m": 8 + 6 * np.sin(x / 40.0 + lat)}


def om_get(calls=None, fail=None):
    """Sztuczne Open-Meteo dla kroku „power” (hourly, bez start_date); reszta jak w test_shared_weather."""
    other = fake_get_factory([])

    def get(url, params, **kw):
        if "hourly" in params and "start_date" not in params:
            if calls is not None:
                calls.append(dict(params))
            if fail and fail(params):
                raise RuntimeError("ReadTimeout")
            out = []
            for la, lo in zip(params["latitude"].split(","), params["longitude"].split(",")):
                idx, cols = _hourly(float(la), float(lo), params["past_days"], params["forecast_days"])
                out.append({"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in idx],
                                       **{k: [float(v) for v in vals] for k, vals in cols.items()}}})
            return out
        return other(url, params)
    return get


def fake_points(points, url, extra):
    """To samo co om_get, w formacie weather.fetch_points (ramki godzinowe)."""
    assert "start_date" not in extra
    out = []
    for p in points:
        idx, cols = _hourly(float(f"{p['lat']:.2f}"), float(f"{p['lon']:.2f}"), extra["past_days"], extra["forecast_days"])
        out.append(pd.DataFrame(cols, index=idx))
    return out


@pytest.fixture
def power(tmp_path, monkeypatch):
    pts = tmp_path / "weather_points.csv"
    pts.write_text(POINTS, encoding="utf-8")
    sw_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sw, "POWER_POINTS", pts)
    monkeypatch.setattr(sw, "POWER_BATCH", 2)
    monkeypatch.setattr(weather, "POINTS", pts)
    monkeypatch.setattr(weather, "SHARED", tmp_path / "out")
    return tmp_path / "out"


def _today():
    return dt.datetime.now(dt.timezone.utc).date()      # w teście, nie przy imporcie (FAKE_NOW)


def test_power_step_writes_country_features(power):
    calls = []
    meta = sw.run(budget=1e6, get=om_get(calls), today=_today())
    assert meta["errors"] == [] and set(meta["steps"]) == {"recent", "hist", "prev"}     # raporty czytają to jak dotąd
    assert meta["power"]["status"] == "OK" and meta["power"]["countries"] == 2 and meta["power"]["missing"] == []
    assert len(calls) == 2 and all(c["timezone"] == "GMT" and c["wind_speed_unit"] == "ms" for c in calls)
    d = pd.read_csv(power / "power_daily.csv", parse_dates=["date"])
    assert list(d.columns) == ["code", "date"] + weather.FEATURES and set(d.code) == {"DE", "PL"}
    pl = d[d.code == "PL"].set_index("date")
    assert pl.index.min() <= pd.Timestamp(_today()) - pd.Timedelta(days=9)
    assert pl.index.max() >= pd.Timestamp(_today()) + pd.Timedelta(days=14)
    # te same liczby co w ścieżce bezpośredniej (weather.daily_features)
    wp = weather.load_points()["PL"]
    ref = weather.daily_features(fake_points(wp, "", {"past_days": 10, "forecast_days": 16}), wp, "Europe/Warsaw")
    np.testing.assert_allclose(pl.loc[ref.index, weather.FEATURES].to_numpy(), ref.to_numpy(), atol=1e-4)
    h = pd.read_csv(power / "power_hourly.csv.gz")
    assert set(h.code) == {"DE", "PL"} and h.time.min().startswith((_today() - dt.timedelta(days=1)).isoformat())
    assert h[h.code == "PL"].time.max() >= (_today() + dt.timedelta(days=8)).isoformat()
    m = json.loads((power / "power_meta.json").read_text())
    assert m["issued"][:10] == _today().isoformat() and m["countries"] == 2
    # przebieg wieczorny też odświeża pogodę do prognoz; zadanie extra – nie
    ev = sw.run(budget=1e6, get=om_get(), today=_today(), minutes=10, job="evening")
    assert ev["power"]["status"] == "OK" and set(ev["steps"]) == {"recent", "ensemble"}
    assert "power" not in sw.run(budget=1e6, get=om_get(), today=_today(), job="extra")


def test_power_step_failed_batch_leaves_country_out(power, monkeypatch):
    monkeypatch.setattr(sw, "POWER_BATCH", 1)

    def fail(params):
        return params["latitude"] == "52.52"                    # punkt DE
    calls = []
    meta = sw.run(budget=1e6, get=om_get(calls, fail), today=_today())
    assert meta["errors"] == []                                  # błąd kroku power nie trafia do errors (czyta je Gaz UE)
    assert meta["power"]["status"] == "częściowo" and meta["power"]["missing"] == ["DE"] and "ReadTimeout" in meta["power"]["last_error"]
    assert len(calls) == 5                                       # 4 paczki + jedna ponowna próba nieudanej
    assert set(pd.read_csv(power / "power_daily.csv").code) == {"PL"}


def test_power_step_keeps_old_files_when_most_countries_fail(power):
    sw.run(budget=1e6, get=om_get(), today=_today())
    before = (power / "power_daily.csv").read_text(), (power / "power_meta.json").read_text()
    meta = sw.run(budget=1e6, get=om_get(fail=lambda p: True), today=_today())
    assert meta["power"]["status"] == "błąd" and "0 z 2" in meta["power"]["error"] and meta["errors"] == []
    assert ((power / "power_daily.csv").read_text(), (power / "power_meta.json").read_text()) == before


def _pl_history():
    rng = np.random.default_rng(3)
    end = dt.date.today() - dt.timedelta(days=1)
    idx = pd.date_range(end - dt.timedelta(days=500), end, freq="D")
    df = pd.DataFrame(0.0, index=idx, columns=store.COLUMNS)
    df["cov"] = 1.0
    df["wind_onshore"] = rng.uniform(1e4, 9e4, len(idx)); df["solar"] = rng.uniform(1e4, 5e4, len(idx))
    df["gas"] = 2e5; df["load"] = 4e5
    df.index.name = "date"
    store.write_daily("PL", df)
    return [c for c in store.load_countries() if c["code"] == "PL"]


def _history_points(points, url, extra):
    """Archiwum ERA5 (pierwszy przebieg) – zawsze dostępne; prognoza przez fake_points."""
    if "start_date" not in extra:
        return fake_points(points, url, extra)
    idx = pd.date_range(extra["start_date"], pd.Timestamp(extra["end_date"]) + pd.Timedelta(days=1), freq="h",
                        inclusive="left", tz="UTC")
    x = np.arange(len(idx))
    return [pd.DataFrame({"wind_speed_100m": 8 + 5 * np.sin(x / 9.0), "shortwave_radiation": 150 + 140 * np.sin(x / 3.8),
                          "temperature_2m": 8 + 6 * np.sin(x / 40.0)}, index=idx) for _ in points]


def test_forecast_from_shared_store_equals_direct_fetch(power, tmp_store, monkeypatch):
    countries = _pl_history()
    settings = store.load_settings()
    monkeypatch.setattr(weather, "fetch_points", _history_points)
    # a) bez wspólnej bazy: własne zapytanie (jak dotąd)
    monkeypatch.setattr(weather, "WEATHER", tmp_store / "weather_a")
    log_a = []
    a = weather.update(settings, {"PL": store.read_daily("PL")}, trends.derive, countries, log_a,
                       archive_path=tmp_store / "arch_a.csv")
    assert not any("wspólna baza" in m for m in log_a)
    # b) ze wspólnej bazy: prognozy Open-Meteo nie wolno już pytać
    sw.run(budget=1e6, get=om_get(), today=_today())

    def no_forecast(points, url, extra):
        assert "start_date" in extra, "prognoza miała przyjść ze wspólnej bazy"
        return _history_points(points, url, extra)
    monkeypatch.setattr(weather, "fetch_points", no_forecast)
    monkeypatch.setattr(weather, "WEATHER", tmp_store / "weather_b")
    log_b = []
    b = weather.update(settings, {"PL": store.read_daily("PL")}, trends.derive, countries, log_b,
                       archive_path=tmp_store / "arch_b.csv")
    assert any("wspólna baza" in m and "1 krajów" in m for m in log_b), log_b
    assert a["PL"]["days"] == b["PL"]["days"] and len(b["PL"]["ext"]["days"]) == 7
    for k in ("wind", "solar", "load"):
        np.testing.assert_allclose(b["PL"][k], a["PL"][k], rtol=2e-3, atol=0.05)
        np.testing.assert_allclose(b["PL"]["ext"][k], a["PL"]["ext"][k], rtol=2e-3, atol=0.05)
    assert b["PL"]["wind_hourly"]["start"] == a["PL"]["wind_hourly"]["start"]
    np.testing.assert_allclose(b["PL"]["wind_hourly"]["mw"], a["PL"]["wind_hourly"]["mw"], rtol=5e-3, atol=2)
    np.testing.assert_allclose(b["PL"]["wind_ws"], a["PL"]["wind_ws"], atol=0.11)


def test_country_missing_in_shared_store_is_fetched_directly(power, tmp_store, monkeypatch):
    countries = _pl_history()
    sw.run(budget=1e6, get=om_get(), today=_today())
    d = pd.read_csv(power / "power_daily.csv")
    d[d.code != "PL"].to_csv(power / "power_daily.csv", index=False)       # we wspólnej bazie tylko DE
    monkeypatch.setattr(weather, "fetch_points", _history_points)
    monkeypatch.setattr(weather, "WEATHER", tmp_store / "weather")
    log = []
    res = weather.update(store.load_settings(), {"PL": store.read_daily("PL")}, trends.derive, countries, log)
    assert "PL" in res and not any("wspólna baza" in m for m in log)


def test_yesterdays_shared_weather_used_only_when_open_meteo_fails(power, tmp_store, monkeypatch):
    countries = _pl_history()
    sw.run(budget=1e6, get=om_get(), today=_today())
    meta = json.loads((power / "power_meta.json").read_text())
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=26)        # wydanie z wczorajszej nocy
    meta["issued"] = old.strftime("%Y-%m-%dT%H:%MZ")
    (power / "power_meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(weather, "WEATHER", tmp_store / "weather")
    seen = {"fc": 0}

    def ok(points, url, extra):
        seen["fc"] += "start_date" not in extra
        return _history_points(points, url, extra)
    monkeypatch.setattr(weather, "fetch_points", ok)
    log = []
    assert "PL" in weather.update(store.load_settings(), {"PL": store.read_daily("PL")}, trends.derive, countries, log)
    assert seen["fc"] == 1 and not any("wspólna baza" in m for m in log)   # świeże zapytanie ma pierwszeństwo

    def down(points, url, extra):
        if "start_date" not in extra:
            raise RuntimeError("ReadTimeout")
        return _history_points(points, url, extra)
    monkeypatch.setattr(weather, "fetch_points", down)
    log = []
    res = weather.update(store.load_settings(), {"PL": store.read_daily("PL")}, trends.derive, countries, log)
    assert "PL" in res and any("prognoza niedostępna" in m and "ze wspólnej bazy" in m for m in log), log
    # plik starszy niż 36 h nie jest używany – jak dotąd brak prognoz
    meta["issued"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=40)).strftime("%Y-%m-%dT%H:%MZ")
    (power / "power_meta.json").write_text(json.dumps(meta))
    assert weather.read_shared() is None
    log = []
    assert weather.update(store.load_settings(), {"PL": store.read_daily("PL")}, trends.derive, countries, log) == {}
    assert any("prognoza niedostępna" in m for m in log)


def test_refresh_shared_weather_only_in_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    called = []
    monkeypatch.setattr(run_daily.subprocess, "run", lambda *a, **k: called.append(a))
    assert run_daily._refresh_shared_weather([]) is False and called == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert run_daily._refresh_shared_weather([]) is True and len(called) == 2
    assert called[1][0][-1] == "data/shared_weather"

    def boom(*a, **k):
        raise OSError("git")
    monkeypatch.setattr(run_daily.subprocess, "run", boom)
    log = []
    assert run_daily._refresh_shared_weather(log) is False and "Wspólna pogoda" in log[0]


def test_real_points_config_covers_all_forecast_countries():
    """Krok „power” liczy cechy dla każdego kraju z config/weather_points.csv, który jest w config/countries.csv."""
    wp = weather.load_points(store.ROOT / "config" / "weather_points.csv")
    codes = {c["code"] for c in store.load_countries()}
    assert set(wp) <= codes, sorted(set(wp) - codes)
