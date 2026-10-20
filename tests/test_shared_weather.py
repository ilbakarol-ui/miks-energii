import datetime as dt
import json

import pandas as pd

from mixtracker import shared_weather as sw


def fake_get_factory(calls):
    def get(url, params):
        calls.append((url, dict(params)))
        n = len(params["latitude"].split(","))
        out = []
        for i in range(n):
            if "hourly" in params:
                a = pd.Timestamp(params["start_date"])
                b = pd.Timestamp(params["end_date"]) + pd.Timedelta(hours=23)
                t = pd.date_range(a, b, freq="h")
                h = {"time": [x.strftime("%Y-%m-%dT%H:%M") for x in t]}
                for k, v in enumerate(params["hourly"].split(",")):
                    h[v] = [float(k + i)] * len(t)
                out.append({"hourly": h})
            else:
                if "start_date" in params:
                    days = pd.date_range(params["start_date"], params["end_date"])
                else:
                    today = pd.Timestamp(TODAY)
                    days = pd.date_range(today - pd.Timedelta(days=params.get("past_days", 0)),
                                         today + pd.Timedelta(days=params["forecast_days"] - 1))
                dd = {"time": [d.strftime("%Y-%m-%d") for d in days]}
                for k, var in enumerate(params["daily"].split(",")):
                    dd[var] = [5.0 + i + k] * len(days)
                    if url in (sw.ENSEMBLE_URL, sw.SEASONAL_URL):
                        for m in (1, 2):
                            dd[f"{var}_member{m:02d}"] = [5.0 + i + k + m] * len(days)
                out.append({"daily": dd})
        return out
    return get


TODAY = dt.date(2026, 10, 19)


def setup(tmp_path, monkeypatch, n=3):
    pts = tmp_path / "pts.csv"
    pts.write_text("pt,group,cc,lat,lon,tz\n" + "".join(
        f"P{i},eu_gas,DE,5{i}.0,1{i}.0,Europe/Berlin\n" for i in range(n)) + "A1,asia,JP,35.68,139.69,GMT\n")
    monkeypatch.setattr(sw, "OUT", tmp_path / "out")
    monkeypatch.setattr(sw, "POINTS", pts)
    monkeypatch.setattr(sw, "HIST_START", "2026-01-01")
    monkeypatch.setattr(sw, "PREV_START", "2026-09-01")
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)


def test_quota_weight():
    assert sw.Quota.weight(10, 14) == 10
    assert sw.Quota.weight(1, 28, 8) == 2
    assert sw.Quota.weight(1, 28, 20) == 4


def test_daily_from_hourly_requires_20_hours():
    t = [f"2026-01-01T{h:02d}:00" for h in range(24)] + [f"2026-01-02T{h:02d}:00" for h in range(10)]
    d = sw.daily_from_hourly(t, {"l0": [1.0] * 24 + [2.0] * 10})
    assert d.loc["2026-01-01", "l0"] == 1.0
    assert pd.isna(d.loc["2026-01-02", "l0"])


def test_full_run_and_incremental(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    calls = []
    meta = sw.run(budget=1e6, get=fake_get_factory(calls), today=TODAY)
    assert meta["errors"] == []
    out = tmp_path / "out"
    rec = pd.read_csv(out / "temp_recent.csv")
    assert rec.fc.sum() == 4 * 16 and set(rec.pt) == {"P0", "P1", "P2", "A1"}
    hist = pd.read_csv(out / "temp_hist.csv")
    assert hist.date.max() == "2026-10-13" and hist.date.min() == "2026-01-01"
    prev = pd.read_csv(out / "temp_prev.csv")
    assert list(prev.columns) == ["pt", "date"] + [f"l{k}" for k in range(8)]
    assert prev.date.max() == "2026-10-18" and prev.date.min() == "2026-09-01"
    assert (prev[prev.pt == "P1"].l3 == 4.0).all()           # zmienna nr 3 + indeks punktu 1
    # strefy czasowe: Europa i Azja w osobnych zapytaniach
    assert {c[1]["timezone"] for c in calls} == {"Europe/Berlin", "GMT"}
    m = json.loads((out / "meta.json").read_text())
    assert m["forecast_issued"] == "2026-10-19" and m["coverage"]["prev"]["points"] == 4
    # następny dzień: tylko przyrost
    calls.clear()
    sw.run(budget=1e6, get=fake_get_factory(calls), today=TODAY + dt.timedelta(days=1))
    hist_calls = [c for c in calls if c[0] == sw.ARCHIVE_URL]
    assert all(c[1]["start_date"] == "2026-10-14" for c in hist_calls)
    prev_calls = [c for c in calls if c[0] == sw.PREV_URL]
    assert all(c[1]["start_date"] == "2026-10-15" for c in prev_calls)
    assert pd.read_csv(out / "temp_prev.csv").date.max() == "2026-10-19"


def test_budget_leaves_pending(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    meta = sw.run(budget=30, get=fake_get_factory([]), today=TODAY)
    assert meta["pending"].get("hist", 0) > 0 or meta["pending"].get("prev", 0) > 0


def test_step_error_does_not_stop_others(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    good = fake_get_factory([])

    def get(url, params):
        if url == sw.PREV_URL:
            raise RuntimeError("HTTP 403")
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY)
    assert any(e.startswith("prev") for e in meta["errors"])
    assert (tmp_path / "out" / "temp_hist.csv").exists()


def test_deadline_saves_partial(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sw, "PREV_CHUNK_DAYS", 10)
    good = fake_get_factory([])
    n = {"prev": 0}

    def get(url, params):
        if url == sw.PREV_URL:
            n["prev"] += 1
            if n["prev"] > 1:
                sw.DEADLINE[0] = 0      # czas minął w trakcie kroku
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY)
    assert meta["steps"]["prev"]["status"] == "limit czasu"
    assert meta["steps"]["hist"]["status"] == "OK"
    assert len(pd.read_csv(tmp_path / "out" / "temp_prev.csv")) > 0     # pierwsza paczka zapisana
    m = json.loads((tmp_path / "out" / "meta.json").read_text())
    assert "prev" in m["steps"] and m["coverage"]["prev"]["points"] >= 1


def setup_extra(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    (tmp_path / "pts.csv").write_text("pt,group,cc,lat,lon,tz,uses\n"
                                      "EU_DE_1,eu_gas,DE,52.5,13.4,Europe/Berlin,temp;ens;seas\n"
                                      "EU_DE_2,eu_gas,DE,50.1,8.7,Europe/Berlin,temp;ens\n"
                                      "RES_DE_1,power_res,DE,54.3,9.0,Europe/Berlin,ens\n"
                                      "EU_PL_1,eu_gas,PL,52.2,21.0,Europe/Berlin,temp;ens;seas\n"
                                      "JP_tokyo,asia,JP,35.68,139.69,GMT,temp;seas\n")


def test_extra_job(tmp_path, monkeypatch):
    setup_extra(tmp_path, monkeypatch)
    calls = []
    meta = sw.run(budget=1e6, get=fake_get_factory(calls), today=TODAY, job="extra")
    assert meta["errors"] == [] and meta["ensemble_kind"] == "ensemble" and meta["seasonal_refreshed_points"] == 3
    out = tmp_path / "out"
    e = pd.read_csv(out / "ensemble_power.csv.gz")
    assert set(e.code) == {"DE", "PL"} and set(e.member) == {0, 1, 2}
    assert e.date.min() == "2026-10-14" and e.date.max() == "2026-10-30"      # 5 dni wstecz, 12 dni prognozy
    de = e[(e.code == "DE") & (e.member == 0)].iloc[0]
    assert abs(de.ws - 6.0) < 1e-9                       # średnia z 3 punktów DE (5, 6, 7 dla pierwszej zmiennej)
    h = pd.read_csv(out / "ensemble_hist.csv")
    assert h.date.max() == "2026-10-18" and set(h.columns) == {"code", "date", "ws", "sr", "t"}
    s = pd.read_csv(out / "seasonal.csv.gz")
    assert set(s.pt) == {"EU_DE_1", "EU_PL_1", "JP_tokyo"} and set(s.member) == {0, 1, 2} and (s.fetched == "2026-10-19").all()
    assert not (out / "meta.json").exists() and (out / "meta_extra.json").exists()
    # dzień później: SEAS5 nie jest pobierany ponownie (kopia < 7 dni), ensemble tak; historia ensemble rośnie
    calls.clear()
    m2 = sw.run(budget=1e6, get=fake_get_factory(calls), today=TODAY + dt.timedelta(days=1), job="extra")
    assert m2["seasonal_refreshed_points"] == 0 and not [c for c in calls if c[0] == sw.SEASONAL_URL]
    assert pd.read_csv(out / "ensemble_hist.csv").date.max() == "2026-10-19"


def test_ensemble_fallback_deterministic(tmp_path, monkeypatch):
    setup_extra(tmp_path, monkeypatch)
    good = fake_get_factory([])

    def get(url, params):
        if url == sw.ENSEMBLE_URL:
            raise RuntimeError("HTTP 502")
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY, job="extra")
    assert meta["ensemble_kind"] == "deterministic"
    assert set(pd.read_csv(tmp_path / "out" / "ensemble_power.csv.gz").member) == {0}


def test_prev_increment_first_and_pending(tmp_path, monkeypatch):
    """Punkty z aktualną historią są pobierane przed zaległymi; po końcu czasu pending pokazuje zaległe punkty."""
    setup(tmp_path, monkeypatch)
    sw.run(budget=1e6, get=fake_get_factory([]), today=TODAY)
    out = tmp_path / "out"
    prev = pd.read_csv(out / "temp_prev.csv", dtype={"date": str})
    # P2 ma zaległość: historia urwana 2026-09-10
    prev = prev[~((prev.pt == "P2") & (prev.date > "2026-09-10"))]
    prev.to_csv(out / "temp_prev.csv", index=False)
    calls = []
    good = fake_get_factory(calls)

    def get(url, params):
        r = good(url, params)
        if url == sw.PREV_URL:
            sw.DEADLINE[0] = 0          # czas kończy się po pierwszym zapytaniu dawnych prognoz
        return r
    meta = sw.run(budget=1e6, get=get, today=TODAY + dt.timedelta(days=1))
    first = next(c for c in calls if c[0] == sw.PREV_URL)
    assert first[1]["start_date"] >= "2026-10-15"     # najpierw przyrost, nie zaległość z września
    assert meta["steps"]["prev"]["status"] == "limit czasu"
    assert meta["pending"]["prev"] >= 1               # P2 (i ew. punkty, na które nie starczyło czasu)
    m = json.loads((out / "meta.json").read_text())
    assert m["coverage"]["prev"]["points_to_end"] < m["coverage"]["prev"]["points"]


def test_meta_step_counters_and_extra_coverage(tmp_path, monkeypatch):
    setup_extra(tmp_path, monkeypatch)
    sw.STATS["http429"] = 5
    sw.STATS["last_error"] = "stary błąd"
    meta = sw.run(budget=1e6, get=fake_get_factory([]), today=TODAY, job="extra")
    for step in meta["steps"].values():
        assert step["http429"] == 0 and step["last_error"] is None
    m = json.loads((tmp_path / "out" / "meta_extra.json").read_text())
    assert "coverage" not in m                         # zakres plików temperatury tylko w meta.json
