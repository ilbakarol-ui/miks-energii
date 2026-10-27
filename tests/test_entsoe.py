"""Testy U-20261027: ENTSO-E zamiast urwanego zużycia UK (Irlandia Północna) i zaniżonych danych NL."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from mixtracker import entsoe, store
from tests.test_pipeline import tmp_store  # noqa: F401 (fixture)

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


def _ts(points, start, end, res="PT15M", psr=None, out=False, curve="A01"):
    dom = "outBiddingZone_Domain.mRID" if out else "inBiddingZone_Domain.mRID"
    psr_xml = f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>" if psr else ""
    pts = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    return (f"<TimeSeries><mRID>1</mRID><curveType>{curve}</curveType>"
            f"<{dom} codingScheme=\"A01\">10YNL----------L</{dom}>{psr_xml}"
            f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{res}</resolution>{pts}</Period></TimeSeries>")


def _doc(*ts):
    return f'<?xml version="1.0" encoding="UTF-8"?><GL_MarketDocument xmlns="{NS}"><mRID>x</mRID>{"".join(ts)}</GL_MarketDocument>'


def _day_utc(d: dt.date, tz: str):
    a = pd.Timestamp(d).tz_localize(tz).tz_convert("UTC")
    b = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(tz).tz_convert("UTC")
    return a, b


def _flat(a, b, mw, step=15):
    n = int((b - a) / pd.Timedelta(minutes=step))
    return [(i + 1, mw) for i in range(n)], a.strftime("%Y-%m-%dT%H:%MZ"), b.strftime("%Y-%m-%dT%H:%MZ")


def test_parse_a03_ffill_and_directions():
    """Krzywa A03: pominięte pozycje = poprzednia wartość; serie „out” (ładowanie) osobno."""
    xml = _doc(_ts([(1, 100), (3, 300)], "2026-10-20T00:00Z", "2026-10-20T01:00Z", psr="B16", curve="A03"),
               _ts([(1, 50), (2, 50), (3, 50), (4, 50)], "2026-10-20T00:00Z", "2026-10-20T01:00Z", psr="B10", out=True))
    ts = entsoe.parse_xml(xml)
    assert [t["direction"] for t in ts] == ["in", "out"]
    assert ts[0]["series"].tolist() == [100, 100, 300, 300]
    mw = entsoe.to_mw(ts, "generation")
    assert set(mw.columns) == {"solar", "storage_charge"}


def test_ack_no_data_is_empty_and_other_ack_raises():
    ok = '<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'
    assert entsoe.parse_xml(ok) == []
    bad = '<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><code>999</code><text>Too many requests</text></Reason></Acknowledgement_MarketDocument>'
    with pytest.raises(entsoe.EntsoeError):
        entsoe.parse_xml(bad.replace("<code>999</code>", "<code>998</code>"))


def test_psr_mapping_covers_all_codes():
    """Każdy kod ENTSO-E trafia do konkretnej kategorii trackera (bez „misc” poza B20 „Other”)."""
    for code, name in entsoe.PSR.items():
        cat = entsoe.classify(name)
        assert cat in store.COLUMNS, (code, name, cat)
        if code != "B20":
            assert cat != "misc", (code, name)


class FakeEntsoe:
    """Zużycie 500 MW (A65) i produkcja: słońce 200 MW + gaz 300 MW, pompowanie 40 MW (A75), dla każdej doby."""
    def __init__(self, tz):
        self.tz, self.calls, self.requests_made = tz, [], 0

    def get(self, params):
        self.calls.append(params["documentType"])
        s = pd.Timestamp(dt.datetime.strptime(params["periodStart"], "%Y%m%d%H%M"), tz="UTC")
        e = pd.Timestamp(dt.datetime.strptime(params["periodEnd"], "%Y%m%d%H%M"), tz="UTC")
        if params["documentType"] == "A65":
            pts, a, b = _flat(s, e, 500)
            return _doc(_ts(pts, a, b))
        pts, a, b = _flat(s, e, 200)
        pg, _, _ = _flat(s, e, 300)
        pp, _, _ = _flat(s, e, 40)
        return _doc(_ts(pts, a, b, psr="B16"), _ts(pg, a, b, psr="B04"), _ts(pp, a, b, psr="B10", out=True))


def _ec_frame(days, load=100.0, solar=1.0):
    idx = pd.DatetimeIndex(days, name="date")
    df = pd.DataFrame(np.nan, index=idx, columns=store.COLUMNS)
    df["cov"] = 1.0
    df[[c for c in store.COLUMNS if c in entsoe.DETAIL_KEYS]] = 0.0
    df["solar"], df["misc"], df["load"], df["price_avg"], df["xborder"] = solar, 5.0, load, 80.0, 123.0
    return df


def test_update_overlays_from_date_and_keeps_prices(tmp_store, monkeypatch):
    monkeypatch.setattr(entsoe, "FOLDER", tmp_store / "entsoe")
    settings = store.load_settings()
    y = dt.date.today() - dt.timedelta(days=1)
    frm = y - dt.timedelta(days=40)
    days = pd.date_range(frm - pd.Timedelta(days=5), y, freq="D")
    store.write_daily("NL", _ec_frame(days))
    conf = {"NL": {"domain": "10YNL----------L", "from": frm.isoformat(), "load": True, "generation": True}}
    log = []
    res = entsoe.update({**settings, "entsoe": conf}, log, 5, client=FakeEntsoe("Europe/Amsterdam"))
    df = store.read_daily("NL")
    after, before = df.loc[pd.Timestamp(frm):], df.loc[:pd.Timestamp(frm) - pd.Timedelta(days=1)]
    full = after.iloc[1:-1]                                    # pełne doby (bez zmiany czasu – 23/25 h liczone osobno)
    assert (full["load"].round() .isin([12000, 11500, 12500])).all()     # 500 MW × 24 h (23/25 h przy zmianie czasu)
    assert (full["solar"] / full["load"]).round(3).eq(0.4).all()
    assert (full["storage_charge"] / full["load"]).round(3).eq(0.08).all()
    assert full["misc"].eq(0).all()                            # „inne” z ENTSO-E (brak B20 = 0), nie 5 z Energy-Charts
    assert after["price_avg"].eq(80).all() and after["xborder"].eq(123).all()   # ceny i saldo z Energy-Charts
    assert before["load"].eq(100).all() and before["solar"].eq(1).all()        # przed datą bez zmian
    assert res["NL"][0] >= 40 and res["NL"][1] >= 40
    assert any("NL: ENTSO-E od" in x and "load" in x for x in log)
    st = store.read_state()["NL"]["entsoe"]
    assert st["from"] == frm.isoformat() and st["last"] == y.isoformat()
    # drugi przebieg: tylko ostatnie doby (historia już pobrana)
    c2 = FakeEntsoe("Europe/Amsterdam")
    entsoe.update({**settings, "entsoe": conf}, [], 5, client=c2)
    # okno ponownego pobrania: 10 dni, w poniedziałki 45 (paczki po CHUNK_DAYS dób) – bez ponownej historii
    back = settings["refetch_days_weekly"] if dt.date.today().weekday() == settings["weekly_refetch_weekday"] else settings["refetch_days"]
    span = (y - max(frm, y - dt.timedelta(days=back))).days + 1
    chunks = -(-span // entsoe.CHUNK_DAYS)
    assert c2.calls == ["A65", "A75"] * chunks


def test_load_only_for_uk_keeps_generation(tmp_store, monkeypatch):
    monkeypatch.setattr(entsoe, "FOLDER", tmp_store / "entsoe")
    settings = store.load_settings()
    y = dt.date.today() - dt.timedelta(days=1)
    frm = y - dt.timedelta(days=10)
    ec = _ec_frame(pd.date_range(frm, y, freq="D"))
    ec["load"] = np.nan                                        # Energy-Charts nie podaje zużycia NI
    store.write_daily("UK", ec)
    conf = {"UK": {"domain": "10Y1001A1001A016", "from": frm.isoformat(), "load": True, "generation": False}}
    c = FakeEntsoe("Europe/London")
    entsoe.update({**settings, "entsoe": conf}, [], 5, client=c)
    df = store.read_daily("UK")
    assert set(c.calls) == {"A65"}
    assert df["load"].notna().all() and df["solar"].eq(1).all() and df["misc"].eq(5).all()


def test_no_token_is_noop(tmp_store, monkeypatch):
    monkeypatch.delenv("ENTSOE_TOKEN", raising=False)
    log = []
    assert entsoe.update(store.load_settings(), log) == {}
    assert log and "ENTSOE_TOKEN" in log[0]


def test_low_coverage_day_does_not_replace():
    idx = pd.DatetimeIndex(pd.date_range("2026-10-01", periods=2, freq="D"), name="date")
    ec = _ec_frame(idx)
    raw = pd.DataFrame({"load": [999.0, 888.0], "load_cov": [1.0, 0.5]}, index=idx)
    out, n_load, _ = entsoe.apply(ec, raw, {"from": "2026-10-01", "load": True}, 0.8)
    assert out["load"].tolist() == [999.0, 100.0] and n_load == 1
