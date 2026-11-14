"""B-20261104-1 / B-20261112-3 (dawne prognozy: ponowienie pojedynczymi punktami, limit 90 s),
B-20261113-1 (prognozy Miksu po południu, gdy nocny przebieg ich nie policzył),
B-20261113-2 (krok recent odporny na jedną nieudaną paczkę)."""
import datetime as dt
import json

import pandas as pd

from mixtracker import run_daily
from mixtracker import shared_weather as sw
from tests.test_shared_weather import TODAY, fake_get_factory, setup


def test_prev_timeout_90_for_increment():
    u = sw.PREV_URL
    assert sw._timeout(u, {"start_date": "2026-11-09", "end_date": "2026-11-13"}) == 90
    assert sw._timeout(u, {"start_date": "2026-09-01", "end_date": "2026-10-30"}) == 150


def test_prev_failed_batch_retried_point_by_point(tmp_path, monkeypatch):
    """Paczka kilku punktów odrzucona – pojedyncze punkty przechodzą; jeden punkt stale z błędem trafia do errors."""
    setup(tmp_path, monkeypatch, n=3)
    good = fake_get_factory([])
    calls = []

    def get(url, params):
        if url == sw.PREV_URL:
            n = len(params["latitude"].split(","))
            calls.append(n)
            if n > 1 or params["latitude"] == "52.00":      # paczka i punkt P2 zawodzą
                raise RuntimeError("ReadTimeout")
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY)
    prev = pd.read_csv(tmp_path / "out" / "temp_prev.csv")
    assert set(prev.pt) == {"P0", "P1", "A1"}
    assert meta["pending"]["prev"] == 1
    err = [e for e in meta["errors"] if e.startswith("prev")]
    assert len(err) == 1 and "P2" in err[0] and "paczek punktów bez danych" in err[0]
    assert 3 in calls and calls.count(1) >= 4                 # paczka 3 punktów, potem pojedyncze

    # wszystkie pojedyncze też zawodzą → jak dotąd cała paczka w błędach
    (tmp_path / "b").mkdir()
    setup(tmp_path / "b", monkeypatch, n=3)

    def bad(url, params):
        if url == sw.PREV_URL and params.get("timezone") == "Europe/Berlin":
            raise RuntimeError("ReadTimeout")
        return good(url, params)
    meta = sw.run(budget=1e6, get=bad, today=TODAY)
    assert meta["pending"]["prev"] >= 3 and any("P0,P1,P2" in e for e in meta["errors"])


def test_recent_one_failed_batch_keeps_other_points(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, n=3)
    good = fake_get_factory([])
    sw.run(budget=1e6, get=good, today=TODAY)                     # wczorajszy plik z kompletem punktów
    out = tmp_path / "out"
    old = pd.read_csv(out / "temp_recent.csv", dtype={"date": str})
    calls = {"n": 0}

    def get(url, params):
        if url == sw.FORECAST_URL and params["timezone"] == "GMT":
            calls["n"] += 1
            raise RuntimeError("ReadTimeout")
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY + dt.timedelta(days=1))
    rec = pd.read_csv(out / "temp_recent.csv", dtype={"date": str})
    assert set(rec.pt) == {"P0", "P1", "P2", "A1"}
    a1 = rec[rec.pt == "A1"]
    d0 = TODAY.isoformat()      # fc liczone wobec dnia przebiegu: A1 z wczorajszego pliku, P0 z dzisiejszego
    assert a1[a1.date == d0].fc.iloc[0] == 1 and rec[(rec.pt == "P0") & (rec.date == d0)].fc.iloc[0] == 0
    assert len(a1) == len(old[old.pt == "A1"])
    assert calls["n"] == 2                                        # paczka + jedna ponowna próba na końcu
    assert meta["steps"]["recent"]["status"] == "OK" and meta["forecast_issued"] == (TODAY + dt.timedelta(days=1)).isoformat()
    assert any(e.startswith("recent (częściowo)") and "A1" in e for e in meta["errors"])


def test_recent_most_points_failed_raises(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, n=3)
    good = fake_get_factory([])

    def get(url, params):
        if url == sw.FORECAST_URL and params["timezone"] == "Europe/Berlin":
            raise RuntimeError("ReadTimeout")
        return good(url, params)
    meta = sw.run(budget=1e6, get=get, today=TODAY)
    assert meta["steps"]["recent"]["status"] != "OK"
    assert any(e.startswith("recent:") and "1 z 4" in e for e in meta["errors"])


def test_forecast_issued_today(tmp_path):
    p = tmp_path / "EU.json"
    today = dt.datetime.now(dt.timezone.utc).date()
    p.write_text(json.dumps({"issued": f"{today - dt.timedelta(days=1)}T02:20Z"}))
    assert not run_daily._forecast_issued_today(p)
    p.write_text(json.dumps({"issued": f"{today}T02:23Z"}))
    assert run_daily._forecast_issued_today(p)
    assert not run_daily._forecast_issued_today(tmp_path / "brak.json")


def _afternoon(monkeypatch, issued_today, extra=()):
    """Przebieg popołudniowy bez sieci: zwraca, czy weather.update było wywołane."""
    seen = {"weather": False}
    monkeypatch.setattr(run_daily, "_forecast_issued_today", lambda path=None: issued_today)
    monkeypatch.setattr(run_daily.update, "update_prices_ahead",
                        lambda *a, **k: {"requests": 1, "log": [], "missing_ahead": []})
    monkeypatch.setattr(run_daily.weather, "update", lambda *a, **k: seen.update(weather=True) or {"PL": {}})
    monkeypatch.setattr(run_daily.store, "load_countries", lambda: [])
    monkeypatch.setattr(run_daily.build, "build", lambda *a, **k: {})
    monkeypatch.setattr(run_daily.build, "render", lambda *a, **k: "")
    monkeypatch.setattr(run_daily, "summary_md", lambda p, r: "\n".join(r.get("log", [])))
    monkeypatch.setattr(run_daily.store, "DATA", run_daily.store.DATA.__class__(monkeypatch._tmp))
    run_daily.main(["--afternoon", *extra])
    return seen["weather"]


def test_afternoon_recomputes_forecasts_only_when_missing(tmp_path, monkeypatch):
    monkeypatch._tmp = str(tmp_path)
    monkeypatch.setattr(run_daily.eurostat, "OUT", tmp_path / "es.csv")
    assert _afternoon(monkeypatch, issued_today=False)
    assert "Prognozy przeliczone po południu" in (tmp_path / "podsumowanie_popoludnie.md").read_text()
    assert not _afternoon(monkeypatch, issued_today=True)
    assert not _afternoon(monkeypatch, issued_today=False, extra=["--only-missing"])
