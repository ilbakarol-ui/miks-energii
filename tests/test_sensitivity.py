"""Wrażliwość na temperaturę (sensitivity.py), wpływ temperatury i wagi wiatru wg mocy (weather_tab.py)."""
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from mixtracker import sensitivity as sen
from mixtracker import weather_tab as wt
from tests.test_weather_tab import REF, _derived, _write_base


def _temp(start="2022-01-01", end="2026-10-31"):
    d = pd.date_range(start, end)
    return pd.Series(10 - 9 * np.cos(2 * np.pi * (d.dayofyear - 15) / 365), index=d)


def test_fit_gas_recovers_slope():
    t = _temp()
    h = pd.Series(sen.hdd(t.values), index=t.index)
    mh = h.groupby(h.index.strftime("%Y-%m")).mean()
    monthly = pd.Series({m: (100 + 20 * v) * pd.Period(m).days_in_month for m, v in mh.items()})
    f = sen.fit_gas(monthly, t)
    assert f["b"] == pytest.approx(20, rel=1e-3) and f["a"] == pytest.approx(100, abs=0.5)
    assert f["from"] == "2023-01" and f["r2"] > 0.99
    assert sen.fit_gas(monthly.loc[:"2023-06"], t) is None   # za mało miesięcy


def test_fit_power_hdd_cdd_and_weekday():
    t = _temp("2024-01-01", "2026-11-10") + 6          # ciepłe lata: dużo dni powyżej 22 °C
    dow = np.where(t.index.dayofweek >= 5, -50.0, 0.0)
    y = pd.Series(500 + 5 * sen.hdd(t.values) + 8 * sen.cdd(t.values) + dow, index=t.index)
    f = sen.fit_power(y, t, dt.date(2026, 11, 10))
    assert f["bh"] == pytest.approx(5, rel=1e-3) and f["bc"] == pytest.approx(8, rel=1e-3) and f["n"] == 730
    # bez ciepłych dni składnik chłodzenia pomijany (bc = 0)
    t2 = _temp("2024-01-01", "2026-11-10") - 8
    y2 = pd.Series(500 + 5 * sen.hdd(t2.values), index=t2.index)
    assert sen.fit_power(y2, t2, dt.date(2026, 11, 10))["bc"] == 0.0


def test_update_gas_every_30_days(tmp_path, monkeypatch):
    monkeypatch.setattr(sen, "FOLDER", tmp_path)
    monkeypatch.setattr(sen, "GAS", tmp_path / "gas_monthly.csv")
    monkeypatch.setattr(sen, "STATE", tmp_path / "state.json")
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": ["geo", "time"], "size": [1, 2],
                    "dimension": {"geo": {"category": {"index": {"PL": 0}}},
                                  "time": {"category": {"index": {"2026-08": 0, "2026-09": 1}}}},
                    "value": {"0": 36000.0, "1": 72000.0}}

    def get(*a, **k):
        calls.append(k["params"])
        return R()
    log = []
    assert sen.update(log, today=dt.date(2026, 11, 12), get=get)
    df = sen.read_gas()
    assert df[df.code == "PL"]["gwh"].tolist() == [10000.0, 20000.0]
    assert ("nrg_bal", "IC_CAL_MG") in calls[0] and ("geo", "EU27_2020") in calls[0]
    assert not sen.update(log, today=dt.date(2026, 11, 30), get=get) and len(calls) == 1
    assert json.loads((tmp_path / "state.json").read_text())["gas_last_month"] == "2026-09"


def test_impact_sign_and_eu_sum(tmp_path):
    """Prognoza 3 °C poniżej normy (syntetyczna baza) → więcej gazu i prądu; UE = suma krajów."""
    _write_base(tmp_path)
    W = wt.load(tmp_path)
    conf = dict(wt.DEFAULTS)
    coefs = {"PL": {"gas": {"a": 100.0, "b": 30.0}, "power": {"a": 400.0, "bh": 5.0, "bc": 0.0}},
             "DE": {"gas": {"a": 300.0, "b": 200.0}, "power": {"a": 1300.0, "bh": 17.0, "bc": 0.0}}}
    blocks = {c: wt.impact_country(c, W, coefs[c], REF, conf) for c in coefs}
    pl = blocks["PL"]
    k = pl["split"] + 3                                   # listopad: zawsze poniżej 15,5 °C – ΔHDD = +3
    assert pl["gas"]["det"][k] == pytest.approx(90, abs=8) and pl["power"]["det"][k] == pytest.approx(15, abs=2)
    assert all(v is None for v in pl["gas"]["det"][:pl["split"]]) and pl["gas"]["act"][0] is not None
    eu = wt.impact_eu(blocks, coefs)
    j = eu["dates"].index(pl["dates"][k])
    de = blocks["DE"]
    assert eu["gas"]["det"][j] == pytest.approx(pl["_gas"][k] + de["_gas"][de["dates"].index(pl["dates"][k])], abs=1)
    assert eu["tw_gas"][j] == pytest.approx(-3, abs=0.3)  # ważona temperatura: 3 °C poniżej normy
    assert eu["countries"] == 2 and eu["gas"]["p10"][j] is not None


def test_impact_seas_and_eu(tmp_path):
    _write_base(tmp_path)
    W = wt.load(tmp_path)
    conf = dict(wt.DEFAULTS)
    coef = {"gas": {"a": 100.0, "b": 30.0}}
    s = wt.impact_seas("PL", W, coef, REF, conf)
    out = wt._seas_out(s)
    nov = out[0]
    assert nov["m"] == "2026-11"
    # warianty: temperatura o 1 − 0,1·m niższa od normy → ΔHDD ≈ 0,45 w medianie, 30 dni × 30 GWh/d
    assert nov["gas"]["p50"] == pytest.approx(0.45 * 30 * 30 / 1000, abs=0.15) and "power" not in nov
    eu = wt.seas_eu({"PL": s, "DE": wt.impact_seas("DE", W, coef, REF, conf)})
    assert eu[0]["gas"]["p50"] == pytest.approx(2 * nov["gas"]["p50"], rel=0.15)


def test_wind_capacity_with_cf_check(tmp_path):
    ec = pd.DataFrame([{"code": "DE", "year": 2025, "series": "wind_on_gw", "value": 68.0},
                       {"code": "DE", "year": 2025, "series": "wind_off_gw", "value": 10.0},
                       {"code": "PL", "year": 2025, "series": "wind_on_gw", "value": 10.0},
                       {"code": "IE", "year": 2025, "series": "wind_on_gw", "value": 1.9},
                       {"code": "DE", "year": 2025, "series": "pumped_gw", "value": 6.0}])
    p = tmp_path / "ec.csv"
    ec.to_csv(p, index=False)
    idx = pd.date_range("2025-11-01", periods=365)
    derived = {"DE": pd.DataFrame({"wind": 78 * 24 * 0.25}, index=idx),        # CF 0,25
               "PL": pd.DataFrame({"wind": 10 * 24 * 0.30}, index=idx),        # CF 0,30
               "IE": pd.DataFrame({"wind": 1.9 * 24 * 0.75}, index=idx)}       # CF 0,75 – moc niepełna
    cap = wt.wind_capacity(["DE", "PL", "IE"], derived, path=p)
    assert cap["DE"]["gw"] == 78.0 and cap["DE"]["src"] == "Energy-Charts" and cap["PL"]["cf"] == 0.3
    assert cap["IE"]["src"].startswith("szacunek z produkcji") and cap["IE"]["gw"] == pytest.approx(1.9 * 0.75 / 0.275, rel=0.01)
    w = wt.capacity_weights(cap, ["DE", "PL", "IE"])
    assert w.sum() == pytest.approx(1) and w["DE"] > 0.8


def test_build_has_impact(tmp_path, monkeypatch):
    _write_base(tmp_path)
    gas = pd.DataFrame([{"code": c, "month": m, "gwh": 1000.0} for c in ("PL", "DE")
                        for m in pd.period_range("2023-01", "2026-09", freq="M").astype(str)])
    monkeypatch.setattr(wt.sensitivity, "read_gas", lambda: gas)
    r = wt.build(_derived(), {}, ["EU", "PL", "DE"], REF, 5, 3, folder=tmp_path)
    # stałe zużycie gazu nie zależy od temperatury → brak wrażliwości gazu (b ≤ 0)
    assert "gas" not in r["sensitivity"].get("PL", {})
    assert "gas_source" in r and r["gas_source"]["last_month"] == "2026-09"
