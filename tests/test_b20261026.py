"""Testy poprawek B-20261026-1, -2, -3 (osobny plik – bez konfliktu z innymi łatkami do test_pipeline.py)."""
import datetime as dt

from mixtracker import store, trends, update
from tests.test_pipeline import FakeClient, fake_price, tmp_store  # noqa: F401 (fixture)


def test_afternoon_missing_details_and_early_stop(tmp_store, monkeypatch):
    """B-20261026-1: przy braku jutra – godziny jutra w logu, plik debug, a ponowienie bez zmian kończy czekanie."""
    settings = store.load_settings()
    sleeps = []
    monkeypatch.setattr(update.time, "sleep", lambda s: sleeps.append(s))

    class Never(FakeClient):
        def price(self, zone, start, end):
            self.requests_made += 1
            return fake_price(start, start, data_from=self.data_from) if zone == "PL" else super().price(zone, start, end)

    res = update.update_prices_ahead(settings, budget_minutes=10, client=Never(data_from=dt.date(2020, 1, 1)),
                                     retries=3, retry_wait=600)
    assert res["missing_ahead"] == ["PL"]
    det = [x for x in res["log"] if x.startswith("szczegóły braków:")]
    assert det and "PL: 0 h na" in det[0]
    assert (store.PRICES.parent / "debug" / "price_PL.csv").exists()
    assert len(sleeps) == 1                                     # drugie czekanie pominięte – ponowienie nic nie zmieniło
    assert any("ponowienie bez zmian" in x for x in res["log"])


def test_movers_intensity_and_record_once():
    """B-20261026-3: emisyjność + rekord udziału (min5) tego samego kraju i okresu oraz ta sama seria w 2 okresach."""
    def it(code, series, delta, typ, kind="month"):
        return {"code": code, "name": code, "period": kind, "series": series, "label": series, "type": typ,
                "cur": 10.0, "ref": 10.0 - delta, "delta": delta, "score": abs(delta)}
    out = trends._dedupe_movers([it("BE", "intensity", 131.3, "int_yoy"), it("BE", "nuclear", -30.0, "min5")], 8)
    assert [(x["code"], x["type"]) for x in out] == [("BE", "int_yoy")]
    out = trends._dedupe_movers([it("LV", "intensity", -84.2, "int_yoy"), it("LV", "intensity", -72.7, "int_yoy", "week")], 8)
    assert len(out) == 1 and out[0]["period"] == "month"
    out = trends._dedupe_movers([it("LV", "intensity", -72.7, "int_yoy", "week"), it("LV", "gas", -32.0, "yoy")], 8)
    assert len(out) == 2                                       # różne okresy i typy – obie


def test_summary_period_label():
    """B-20261026-2: w „Największych zmianach” nazwa okresu zamiast „mies.”."""
    from mixtracker import run_daily
    mv = [{"code": "BE", "name": "Belgia", "period": "month", "series": "nuclear", "label": "Jądrowa", "type": "min5",
           "cur": 0.0, "ref": 5.0, "delta": -5.0, "score": 5.0}]
    meta = {"ref": "2026-10-25", "periods": {"week": {"label": "tydz. 43/2026", "short": "tydz. 43"},
                                             "month": {"label": "wrzesień 2026", "short": "wrz 2026"}}}
    md = run_daily.summary_md({"meta": meta, "summary": {}, "movers": mv}, {"requests": 1})
    assert "(wrz 2026)" in md and "mies." not in md
