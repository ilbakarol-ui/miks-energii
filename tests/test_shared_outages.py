"""Testy: niedostępność elektrowni z ENTSO-E (A80) dla dashboardu Gaz UE."""
import datetime as dt
import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from mixtracker import shared_outages as so, store
from tests.test_pipeline import tmp_store  # noqa: F401 (fixture)

NS = "urn:iec62325.351:tc57wg16:451-6:outagedocument:3:0"


def _doc(mrid, rev, unit, psr, nominal, periods, btype="A53", status=None, curve="A03"):
    """periods: [(start_iso, end_iso, resolution, [(pos, qty), ...])]."""
    st = f"<docStatus><value>{status}</value></docStatus>" if status else ""
    per = "".join(
        f"<Available_Period><timeInterval><start>{a}</start><end>{b}</end></timeInterval><resolution>{res}</resolution>"
        + "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in pts)
        + "</Available_Period>" for a, b, res, pts in periods)
    return (f'<?xml version="1.0" encoding="UTF-8"?><Unavailability_MarketDocument xmlns="{NS}"><mRID>{mrid}</mRID>'
            f"<revisionNumber>{rev}</revisionNumber><createdDateTime>2026-10-20T10:00:00Z</createdDateTime>{st}"
            f"<TimeSeries><mRID>1</mRID><businessType>{btype}</businessType><curveType>{curve}</curveType>"
            f"<start_DateAndOrTime.date>2026-10-01</start_DateAndOrTime.date><start_DateAndOrTime.time>00:00:00Z</start_DateAndOrTime.time>"
            f"<end_DateAndOrTime.date>2026-12-01</end_DateAndOrTime.date><end_DateAndOrTime.time>00:00:00Z</end_DateAndOrTime.time>"
            f"<production_RegisteredResource.mRID>{unit}P</production_RegisteredResource.mRID>"
            f"<production_RegisteredResource.name>{unit}</production_RegisteredResource.name>"
            f"<production_RegisteredResource.pSRType.psrType>{psr}</production_RegisteredResource.pSRType.psrType>"
            f"<production_RegisteredResource.pSRType.powerSystemResources.mRID>{unit}</production_RegisteredResource.pSRType.powerSystemResources.mRID>"
            f"<production_RegisteredResource.pSRType.powerSystemResources.name>{unit}</production_RegisteredResource.pSRType.powerSystemResources.name>"
            f'<production_RegisteredResource.pSRType.powerSystemResources.nominalP unit="MAW">{nominal}</production_RegisteredResource.pSRType.powerSystemResources.nominalP>'
            f"{per}</TimeSeries></Unavailability_MarketDocument>")


def _zip(*docs):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i, d in enumerate(docs):
            z.writestr(f"doc{i}.xml", d)
    return b.getvalue()


def test_parse_a03_points_and_zip():
    """A03: wartość obowiązuje do następnego punktu; PT1M z pozycjami jako minuty od początku."""
    d = _doc("m1", 3, "CHOOZ 2", "B14", 1500,
             [("2026-10-10T22:00Z", "2026-10-12T22:00Z", "PT1M", [(1, 0), (1441, 750)])])
    rows, n = so.parse_response(_zip(d), "FR")
    assert n == 1 and len(rows) == 2
    assert rows[0]["available_mw"] == 0 and rows[1]["available_mw"] == 750
    assert rows[0]["end"] == pd.Timestamp("2026-10-11T22:00Z")
    assert rows[1]["end"] == pd.Timestamp("2026-10-12T22:00Z")
    assert rows[0]["psr"] == "B14" and rows[0]["nominal_mw"] == 1500 and rows[0]["revision"] == 3


def test_ack_no_data_and_error():
    ok = b'<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'
    assert so.parse_response(ok, "FR") == ([], 0)
    bad = b'<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><code>B11</code><text>Too many</text></Reason></Acknowledgement_MarketDocument>'
    with pytest.raises(so.EntsoeError):
        so.parse_response(bad, "FR")


def test_merge_keeps_latest_revision_and_drops_cancelled():
    r1 = so.parse_doc(_doc("m1", 1, "U1", "B14", 1000, [("2026-10-10T00:00Z", "2026-10-20T00:00Z", "PT60M", [(1, 0)])]), "FR")
    r2 = so.parse_doc(_doc("m1", 2, "U1", "B14", 1000, [("2026-10-10T00:00Z", "2026-10-15T00:00Z", "PT60M", [(1, 0)])]), "FR")
    ev = so.merge_events(pd.DataFrame(columns=so.EVENT_COLS), r1)
    ev = so.merge_events(ev, r2)
    assert len(ev) == 1 and ev.iloc[0].revision == 2 and ev.iloc[0].end == pd.Timestamp("2026-10-15T00:00Z")
    old_rev = so.merge_events(ev, r1)          # starsza rewizja przychodząca później nie nadpisuje
    assert old_rev.iloc[0].revision == 2
    canc = so.parse_doc(_doc("m1", 3, "U1", "B14", 1000, [("2026-10-10T00:00Z", "2026-10-15T00:00Z", "PT60M", [(1, 0)])],
                             status="A09"), "FR")
    assert so.merge_events(ev, canc).empty


def test_daily_overlap_counted_once_and_split_planned_forced():
    """Dwa komunikaty tego samego bloku na siebie: liczy się większa niedostępność, nie suma."""
    plan = so.parse_doc(_doc("p", 1, "U1", "B14", 1000, [("2026-10-09T22:00Z", "2026-10-11T22:00Z", "PT60M", [(1, 600)])]), "FR")
    forced = so.parse_doc(_doc("f", 1, "U1", "B14", 1000, [("2026-10-10T22:00Z", "2026-10-11T22:00Z", "PT60M", [(1, 0)])],
                               btype="A54"), "FR")
    gas = so.parse_doc(_doc("g", 1, "G1", "B04", 400, [("2026-10-09T22:00Z", "2026-10-10T10:00Z", "PT60M", [(1, 0)])]), "FR")
    ev = so.merge_events(pd.DataFrame(columns=so.EVENT_COLS), plan + forced + gas)
    d, _ = so.daily(ev, "FR", "Europe/Paris", dt.date(2026, 10, 10), dt.date(2026, 10, 12))
    nuc = d[d.group == "nuclear"].set_index("date")
    assert nuc.loc["2026-10-10", "planned_mw"] == pytest.approx(400) and nuc.loc["2026-10-10", "forced_mw"] == 0
    assert nuc.loc["2026-10-11", "forced_mw"] == pytest.approx(1000) and nuc.loc["2026-10-11", "planned_mw"] == 0
    assert nuc.loc["2026-10-12", "planned_mw"] == 0
    assert nuc.loc["2026-10-11", "units_out"] == 1 and nuc.loc["2026-10-10", "units_out"] == 0
    g = d[d.group == "gas"].set_index("date")
    assert g.loc["2026-10-10", "planned_mw"] == pytest.approx(400 * 12 / 24)


def test_daily_dst_day_has_25_hours():
    ev = so.merge_events(pd.DataFrame(columns=so.EVENT_COLS), so.parse_doc(
        _doc("m", 1, "U1", "B14", 900, [("2026-10-24T22:00Z", "2026-10-25T23:00Z", "PT60M", [(1, 0)])]), "FR"))
    d, _ = so.daily(ev, "FR", "Europe/Paris", dt.date(2026, 10, 25), dt.date(2026, 10, 25))
    assert d.iloc[0].planned_mw == pytest.approx(900)


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls, self.requests_made = pages, [], 0

    def get(self, params):
        self.calls.append(params)
        self.requests_made += 1
        return self.pages.pop(0) if self.pages else b'<Acknowledgement_MarketDocument><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'


def test_update_writes_files_and_uses_update_filter_next_time(tmp_store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(so, "FOLDER", tmp_store / "shared_outages")
    now = pd.Timestamp("2026-10-29T01:00Z")
    doc = _doc("m1", 1, "CATTENOM 3", "B14", 1300, [("2026-10-20T00:00Z", "2026-12-04T22:00Z", "PT1M", [(1, 0)])])
    fx = FakeFetcher([_zip(doc)])
    log = []
    settings = {"outages": {"zones": {"FR": "10YFR-RTE------C"}, "history_days": 40, "horizon_days": 60}}
    res = so.update(settings, log, fetcher=fx, now=now)
    assert res["FR"][1] is True
    assert "periodStartUpdate" not in fx.calls[0]
    d = pd.read_csv(so.FOLDER / "daily.csv")
    row = d[(d.date == "2026-10-29") & (d.group == "nuclear")].iloc[0]
    assert row.planned_mw == pytest.approx(1300)
    units = json.loads((so.FOLDER / "units.json").read_text())
    assert units[0]["unit"] == "CATTENOM 3" and units[0]["now"] is True and units[0]["unavail_mw"] == 1300
    assert "atom dziś niedostępny: FR 1.3 GW" in log[-1]
    fx2 = FakeFetcher([])
    so.update(settings, [], fetcher=fx2, now=now + pd.Timedelta(days=1))
    assert all("periodStartUpdate" in c for c in fx2.calls)
    d2 = pd.read_csv(so.FOLDER / "daily.csv")       # zdarzenia z poprzedniego przebiegu zostają
    assert d2[(d2.date == "2026-10-30") & (d2.group == "nuclear")].iloc[0].planned_mw == pytest.approx(1300)


def test_paging_offset(tmp_store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(so, "FOLDER", tmp_store / "shared_outages")
    docs = [_doc(f"m{i}", 1, f"U{i}", "B04", 450, [("2026-10-20T00:00Z", "2026-10-21T00:00Z", "PT60M", [(1, 0)])])
            for i in range(so.PAGE)]
    fx = FakeFetcher([_zip(*docs), _zip(docs[0])])
    so.update({"outages": {"zones": {"FR": "10YFR-RTE------C"}, "history_days": 30, "horizon_days": 30}}, [],
              fetcher=fx, now=pd.Timestamp("2026-10-29T01:00Z"))
    assert [c["offset"] for c in fx.calls[:2]] == [0, so.PAGE]


def test_no_token_only_logs(tmp_store, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ENTSOE_TOKEN", raising=False)
    log = []
    assert so.update({"outages": {"zones": {"FR": "x"}}}, log) == {}
    assert "brak sekretu" in log[0]


def test_split_when_too_many_documents(monkeypatch):
    """Kawałek z ponad MAX_OFFSET + PAGE dokumentami jest dzielony na pół zamiast ucinany."""
    monkeypatch.setattr(so, "MAX_OFFSET", so.PAGE)       # 2 strony na kawałek
    full = _zip(*[_doc(f"m{i}", 1, f"U{i}", "B04", 450, [("2026-10-20T00:00Z", "2026-10-21T00:00Z", "PT60M", [(1, 0)])])
                  for i in range(so.PAGE)])
    small = _zip(_doc("x", 1, "X", "B04", 450, [("2026-10-20T00:00Z", "2026-10-21T00:00Z", "PT60M", [(1, 0)])]))
    fx = FakeFetcher([full, full, small, small])
    import time as _t
    rows, complete, note = so.fetch_zone(fx, "d", "FR", pd.Timestamp("2026-09-01", tz="UTC"),
                                         pd.Timestamp("2026-11-01", tz="UTC"), None, pd.Timestamp.now(tz="UTC"),
                                         _t.monotonic() + 60)
    assert complete and note is None
    assert len(fx.calls) == 4 and fx.calls[2]["periodEnd"] < fx.calls[0]["periodEnd"]
    assert len(rows) == 2
