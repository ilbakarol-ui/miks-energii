"""Testy U-20261114: Dania z Energinet (Energi Data Service) – uzupełnianie dób bez danych z Energy-Charts / ENTSO-E."""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from mixtracker import energinet, store
from tests.test_pipeline import tmp_store  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[1]
TZ = "Europe/Copenhagen"


class FakeEDS:
    """DK1 i DK2 co godzinę: wiatr 1000/200 MW, węgiel 100/0, biogaz 50/10, zużycie 3000/2000 MW, eksport.

    bad_load: {data: MW dodatkowo w DK1} – jak błąd SCADA 13.11.2026; version: wersja danych; skip_area_hours: godziny
    bez DK2."""
    def __init__(self, version="Initial", bad_load=None, skip_area_hours=0):
        self.version, self.bad_load, self.skip = version, bad_load or {}, skip_area_hours
        self.requests_made, self.calls = 0, []

    def records(self, s, e, areas):
        self.calls.append((s, e))
        out = []
        for i, t in enumerate(pd.date_range(s, e, freq="h", inclusive="left")):
            local_day = t.tz_convert(TZ).date()
            for a in areas:
                if a == "DK2" and i < self.skip:
                    continue
                dk1 = a == "DK1"
                out.append({"TimeUTC": t.strftime("%Y-%m-%dT%H:%M:%S"), "PriceArea": a, "Version": self.version,
                            "GrossCon": (3000.0 + self.bad_load.get(local_day, 0)) if dk1 else 2000.0,
                            "OffshoreWindPower": 600.0 if dk1 else 100.0, "OnshoreWindPower": 400.0 if dk1 else 100.0,
                            "SolarPower": 0.0, "SolarPowerSelfCon": 5.0, "HydroPower": 0.0,
                            "Biomass": 300.0, "Biogas": 50.0 if dk1 else 10.0, "Waste": 100.0, "FossilGas": 50.0,
                            "FossilOil": 1.0, "FossilHardCoal": 100.0 if dk1 else 0.0,
                            "ExchangeGreatBelt": -70.0 if dk1 else 70.0, "ExchangeGermany": -500.0 if dk1 else -100.0,
                            "ExchangeSweden": 200.0, "ExchangeNorway": 0.0, "ExchangeNetherlands": 0.0,
                            "ExchangeGreatBritain": 0.0, "CO2PerkWh": 50.0})
        return out


def _ec(days, cov=1.0, load=110000.0):
    df = pd.DataFrame(np.nan, index=pd.DatetimeIndex(days, name="date"), columns=store.COLUMNS)
    df["cov"] = cov
    df[[c for c in store.COLUMNS if c in energinet.DETAIL_KEYS]] = 0.0
    df["wind_onshore"], df["load"], df["price_avg"] = 1000.0, load, 90.0
    return df


def test_to_mw_sums_areas_and_picks_latest_version():
    recs = FakeEDS().records(pd.Timestamp("2026-11-05T00:00Z"), pd.Timestamp("2026-11-05T02:00Z"), ["DK1", "DK2"])
    newer = [dict(r, Version="Preliminary", OffshoreWindPower=9999.0) for r in recs[:2]]   # godzina 0, obie strefy
    mw = energinet.to_mw(recs + newer, ["DK1", "DK2"])
    assert len(mw) == 2
    assert mw["wind_offshore"].tolist() == [19998.0, 700.0]
    assert mw["wind_onshore"].iloc[1] == 500.0 and mw["hard_coal"].iloc[1] == 100.0
    assert mw["biomass"].iloc[1] == 660.0                       # biomasa + biogaz obu stref
    assert mw["load"].iloc[1] == 5000.0
    assert mw["xborder"].iloc[1] == -200.0                      # Wielki Bełt się znosi: −500 −100 + 2×200
    assert mw["ver"].tolist() == [1, 0]
    assert "solar" in mw and mw["solar"].eq(0).all()            # autokonsumpcja PV pominięta


def test_load_needs_exchange_data():
    recs = FakeEDS().records(pd.Timestamp("2026-11-13T00:00Z"), pd.Timestamp("2026-11-13T02:00Z"), ["DK1", "DK2"])
    for r in recs[:2]:                                          # godzina 0: wymiana pusta (jak 13.11.2026)
        for k in [k for k in r if k.startswith("Exchange")]:
            r[k] = None
    mw = energinet.to_mw(recs, ["DK1", "DK2"])
    assert pd.isna(mw["load"].iloc[0]) and mw["load"].iloc[1] == 5000.0
    assert mw["wind_onshore"].iloc[0] == 500.0                  # produkcja zostaje


def test_hours_without_both_areas_lower_coverage():
    c = FakeEDS(skip_area_hours=6)
    d = energinet.fetch_days(c, ["DK1", "DK2"], TZ, dt.date(2026, 11, 5), dt.date(2026, 11, 5), 0.8)
    assert round(d.at[pd.Timestamp("2026-11-05"), "cov"], 3) == 0.75


def test_update_fills_only_missing_days(tmp_store, monkeypatch):
    monkeypatch.setattr(energinet, "FOLDER", tmp_store / "energinet")
    settings = store.load_settings()
    y = dt.date.today() - dt.timedelta(days=1)
    frm = y - dt.timedelta(days=12)
    good = pd.date_range(frm - pd.Timedelta(days=30), frm + pd.Timedelta(days=2), freq="D")   # dobre doby z EC
    ec = _ec(good)
    partial = pd.Timestamp(frm + dt.timedelta(days=3))
    ec = pd.concat([ec, _ec([partial], cov=0.375)])            # jak DK 4.11: niepełna doba
    load_only = pd.Timestamp(frm + dt.timedelta(days=5))
    lo = _ec([load_only]); lo.loc[:, [c for c in store.COLUMNS if c != "load"]] = np.nan
    ec = pd.concat([ec, lo]).sort_index()                      # jak DK 5.11+: tylko zużycie z ENTSO-E
    store.write_daily("DK", ec)
    zp = pd.DataFrame({"price_avg": 100.0, "price_min": 50.0, "price_max": 150.0, "price_neg_h": 0.0},
                      index=pd.DatetimeIndex(pd.date_range(frm, y), name="date"))
    store.write_zone("DK1", zp); store.write_zone("DK2", zp.assign(price_avg=120.0))
    bad_day = y - dt.timedelta(days=1)
    conf = {"DK": {"from": frm.isoformat(), "areas": ["DK1", "DK2"]}}
    log = []
    res = energinet.update({**settings, "energinet": conf}, log, client=FakeEDS(bad_load={bad_day: 1700.0}))
    df = store.read_daily("DK")
    # dobre doby z Energy-Charts bez zmian
    assert df.loc[pd.Timestamp(frm):pd.Timestamp(frm) + pd.Timedelta(days=2), "wind_onshore"].eq(1000).all()
    # niepełna doba i doby bez produkcji uzupełnione (24 h × 500 MW wiatru na lądzie)
    filled = df.loc[pd.Timestamp(frm) + pd.Timedelta(days=3):pd.Timestamp(y)]
    assert filled["cov"].eq(1).all() and filled["wind_onshore"].eq(12000).all()
    assert filled["hard_coal"].eq(2400).all() and filled["xborder"].eq(-4800).all()
    # zużycie: ENTSO-E zostaje, brakujące z Energinet (5000 MW × 24 h), podejrzana doba odrzucona
    assert df.at[load_only, "load"] == 110000
    assert df.at[partial, "load"] == 110000                    # doba miała zużycie – bez zmian
    assert pd.isna(df.at[pd.Timestamp(bad_day), "load"])
    assert df.at[pd.Timestamp(y), "load"] == 120000
    # ceny dzienne ze stref (średnia DK1 i DK2), tylko w pustych miejscach
    assert df.at[pd.Timestamp(y), "price_avg"] == 110 and df.at[partial, "price_avg"] == 90
    assert res["DK"][0] == 10
    assert any("DK: Energinet" in x and "odrzucone" in x for x in log)
    st = store.read_state()["DK"]["energinet"]
    assert st["gen_days"] == 10 and st["last"] == y.isoformat()
    # drugi przebieg z nowszą wersją: doby wpisane przez moduł nadpisane, doby EC nie
    c2 = FakeEDS(version="Preliminary")
    rec = c2.records
    c2.records = lambda s, e, a: [dict(r, OnshoreWindPower=r["OnshoreWindPower"] * 2) for r in rec(s, e, a)]
    energinet.update({**settings, "energinet": conf}, [], client=c2)
    df2 = store.read_daily("DK")
    assert df2.loc[pd.Timestamp(y), "wind_onshore"] == 24000
    assert df2.loc[pd.Timestamp(frm), "wind_onshore"] == 1000
    assert df2.at[load_only, "load"] == 110000


def test_ec_day_returning_is_not_overwritten():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-11-05")], name="date")
    raw = pd.DataFrame({"cov": [1.0], "wind_onshore": [5.0], "load": [1.0], "load_cov": [1.0]}, index=idx)
    owned = {"2026-11-05": {"gen": 123.0}}                    # wcześniej wpisane, ale EC dosłało prawdziwą dobę
    out, gen, _, new_owned = energinet.apply(_ec(idx), raw, {"from": "2026-11-01"}, 0.8, owned=owned)
    assert gen == [] and out.at[idx[0], "wind_onshore"] == 1000 and "2026-11-05" not in new_owned


def test_settings_dk_energinet():
    s = json.loads((ROOT / "config" / "settings.json").read_text())
    dk = s["energinet"]["DK"]
    assert dk["areas"] == ["DK1", "DK2"] and dk["from"] <= "2026-11-04"
