"""Zakładka „Pogoda” (weather_tab.py) na syntetycznej bazie data/shared_weather."""
import datetime as dt

import numpy as np
import pandas as pd

from mixtracker import weather_tab as wt

REF = dt.date(2026, 10, 29)


def _write_base(folder, members=12, hist_days=20, slope=10.0):
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"pt": "EU_PL_1", "group": "eu_gas", "cc": "PL", "lat": 52, "lon": 21, "tz": "Europe/Warsaw", "uses": "temp;ens;seas"},
        {"pt": "EU_PL_2", "group": "eu_gas", "cc": "PL", "lat": 50, "lon": 19, "tz": "Europe/Warsaw", "uses": "temp;ens"},
        {"pt": "EU_DE_1", "group": "eu_gas", "cc": "DE", "lat": 52, "lon": 13, "tz": "Europe/Berlin", "uses": "temp;ens;seas"},
        {"pt": "PR_DE_1", "group": "power_res", "cc": "DE", "lat": 54, "lon": 9, "tz": "Europe/Berlin", "uses": "ens"},
        {"pt": "AS_JP_1", "group": "asia", "cc": "JP", "lat": 35, "lon": 139, "tz": "Asia/Tokyo", "uses": "temp;seas"},
    ]).to_csv(folder / "points.csv", index=False)
    # historia temperatury 2019-2026: sinusoida roczna, DE o 2 stopnie cieplej
    days = pd.date_range("2019-01-01", "2026-10-24")
    base = 10 - 9 * np.cos(2 * np.pi * (days.dayofyear - 15) / 365)
    rows = []
    for pt, off in (("EU_PL_1", 0), ("EU_PL_2", 0), ("EU_DE_1", 2), ("AS_JP_1", 5)):
        rows.append(pd.DataFrame({"pt": pt, "date": days.strftime("%Y-%m-%d"), "temp": np.round(base + off, 2)}))
    pd.concat(rows).to_csv(folder / "temp_hist.csv", index=False)
    # bieżące: 14 dni wstecz (fc=0) + 14 dni prognozy (fc=1), prognoza o 3 stopnie poniżej normy
    rec = []
    for d in pd.date_range(REF - dt.timedelta(days=13), REF + dt.timedelta(days=14)):
        fc = int(d.date() > REF)
        b = 10 - 9 * np.cos(2 * np.pi * (d.dayofyear - 15) / 365) - 3 * fc
        for pt, off in (("EU_PL_1", 0), ("EU_PL_2", 0), ("EU_DE_1", 2)):
            if pt == "EU_PL_2" and d.date() == REF + dt.timedelta(days=14):
                continue   # ostatni dzień niekompletny – ma wypaść
            rec.append({"pt": pt, "date": d.strftime("%Y-%m-%d"), "temp": round(b + off, 2), "fc": fc})
    pd.DataFrame(rec).to_csv(folder / "temp_recent.csv", index=False)
    # ensemble: 3 dni wstecz + 12 naprzód, prędkość wiatru rośnie z numerem wariantu
    ens = []
    for d in pd.date_range(REF - dt.timedelta(days=2), REF + dt.timedelta(days=12)):
        for code, ws0 in (("PL", 20.0), ("DE", 30.0)):
            for m in range(members):
                ens.append({"code": code, "date": d.strftime("%Y-%m-%d"), "member": m, "sr": 5.0, "t": 5.0 + 0.1 * m, "ws": ws0 + m})
    pd.DataFrame(ens).to_csv(folder / "ensemble_power.csv.gz", index=False)
    eh = []
    for i, d in enumerate(pd.date_range(REF - dt.timedelta(days=hist_days - 1), REF)):
        for code, ws0 in (("PL", 15.0), ("DE", 25.0)):
            eh.append({"code": code, "date": d.strftime("%Y-%m-%d"), "ws": ws0 + (i % 7), "sr": 5.0, "t": 8.0})
    pd.DataFrame(eh).to_csv(folder / "ensemble_hist.csv", index=False)
    # SEAS5: 6 miesięcy, wariant m = norma - 1 + 0.1*m (stała dla dnia)
    se = []
    for d in pd.date_range("2026-10-26", "2027-04-29"):
        nb = 10 - 9 * np.cos(2 * np.pi * (d.dayofyear - 15) / 365)
        for pt, off in (("EU_PL_1", 0), ("EU_DE_1", 2), ("AS_JP_1", 5)):
            for m in range(members):
                se.append({"pt": pt, "member": m, "date": d.strftime("%Y-%m-%d"), "temp": round(nb + off - 1 + 0.1 * m, 2), "fetched": "2026-10-29"})
    pd.DataFrame(se).to_csv(folder / "seasonal.csv.gz", index=False)
    (folder / "meta.json").write_text('{"forecast_issued": "2026-10-30", "updated": "2026-10-30T01:19Z"}', encoding="utf-8")
    (folder / "meta_extra.json").write_text('{"ensemble_issued": "2026-10-30", "updated": "2026-10-30T01:16Z"}', encoding="utf-8")
    (folder / "meta_evening.json").write_text('{"forecast_issued": "2026-10-29", "ensemble_issued": "2026-10-29", "updated": "2026-10-29T18:25Z"}', encoding="utf-8")


def _derived(slope=10.0, hist_days=20):
    """Produkcja z wiatru = slope·(ws z ensemble_hist) - 50 (PL) i w 5 latach historii (do normy)."""
    idx = pd.date_range("2021-01-01", REF)
    out = {}
    for code, ws0 in (("PL", 15.0), ("DE", 25.0)):
        w = pd.Series(300.0, index=idx)
        last = pd.date_range(REF - dt.timedelta(days=hist_days - 1), REF)
        w.loc[last] = [slope * (ws0 + (i % 7)) - 50 for i in range(hist_days)]
        out[code] = pd.DataFrame({"wind": w})
    out["EU"] = pd.DataFrame({"wind": out["PL"]["wind"] + out["DE"]["wind"]})
    return out


def test_wind_calibrated_band_and_norm(tmp_path):
    _write_base(tmp_path)
    fcs = {"PL": {"days": [(REF + dt.timedelta(days=k)).isoformat() for k in range(1, 8)], "wind": [100.0] * 7}}
    r = wt.build(_derived(), fcs, ["EU", "PL", "DE", "NO"], REF, 5, 3, folder=tmp_path)
    pl = r["countries"]["PL"]["wind"]
    assert pl["fit"]["ok"] and pl["fit"]["r2"] > 0.99 and pl["fit"]["n"] == 20
    assert pl["split"] == 14 and pl["dates"][14] == "2026-10-30" and pl["dates"][-1] == "2026-11-10"
    # pasmo tylko w prognozie; mediana = 10·(20 + mediana 0..11) - 50 = 205
    assert all(v is None for v in pl["p50"][:14])
    assert abs(pl["p50"][14] - 205.0) < 0.6 and pl["p10"][14] < pl["p50"][14] < pl["p90"][14]
    assert pl["det"][14:21] == [100.0] * 7 and pl["det"][21] is None
    assert pl["avg"][14] == 300.0   # norma z poprzednich lat (stała 300)
    eu = r["countries"]["EU"]["wind"]
    assert eu["fit"]["ok"] and eu["p50"][14] is not None
    assert "NO" not in r["countries"]   # brak danych pogodowych i produkcji
    assert r["issued"] == {"forecast": "2026-10-30", "ensemble": "2026-10-30", "seasonal": "2026-10-29",
                           "updated": "2026-10-30T01:19Z"}


def test_wind_no_band_when_too_little_history(tmp_path):
    _write_base(tmp_path, hist_days=10)
    r = wt.build(_derived(hist_days=10), {}, ["PL"], REF, 5, 3, folder=tmp_path)
    w = r["countries"]["PL"]["wind"]
    assert not w["fit"]["ok"] and "za mało dni" in w["fit"]["why"]
    assert all(v is None for v in w["p50"])
    # bez pasma i bez prognozy modelu zostaje tylko historia
    assert w["split"] == len(w["dates"]) == 14


def test_temperature_forecast_and_norm(tmp_path):
    _write_base(tmp_path)
    r = wt.build(_derived(), {}, ["EU", "PL"], REF, 5, 3, folder=tmp_path)
    t = r["countries"]["PL"]["temp"]
    assert t["points"] == 2 and t["norm_years"] == "2019–2025"
    assert t["split"] == 14 and t["dates"][-1] == "2026-11-11"   # 12.11 niekompletny (brak punktu) - pominięty
    fut = range(t["split"], t["split"] + 12)
    # prognoza 3 stopnie poniżej normy (norma wygładzona – tolerancja)
    assert all(abs((t["det"][i] - t["norm"][i]) + 3) < 0.3 for i in fut)
    assert abs(t["act"][0] - t["norm"][0]) < 0.3
    assert t["p10"][14] < t["p90"][14]
    eu = r["countries"]["EU"]["temp"]
    assert eu["points"] == 3   # tylko punkty sieci gazowej z temperaturą (bez power_res i Azji)
    assert eu["det"][14] > t["det"][14]   # DE cieplejsze o 2 stopnie podnosi średnią UE


def test_seasonal_monthly_anomaly(tmp_path):
    _write_base(tmp_path)
    r = wt.build(_derived(), {}, ["EU", "PL", "DE"], REF, 5, 3, folder=tmp_path)
    s = r["countries"]["PL"]["seas"]
    ms = [m["m"] for m in s["months"]]
    assert ms == ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04"]   # październik (6 dni) pominięty
    nov = s["months"][0]
    # anomalia wariantu m = -1 + 0.1·m (m = 0..11), mediana ≈ -0.45
    assert abs(nov["p50"] + 0.45) < 0.15 and nov["p10"] < nov["p50"] < nov["p90"] and nov["n"] == 12
    assert nov["warm"] <= 20   # tylko warianty bliskie zera (m = 10, 11)
    eu = r["countries"]["EU"]["seas"]["months"][0]
    assert abs(eu["p50"] + 0.45) < 0.15   # ta sama anomalia, mimo innej normy DE


def test_missing_folder_returns_empty(tmp_path):
    assert wt.build({}, {}, ["PL"], REF, 5, 3, folder=tmp_path / "brak") == {}


def test_country_error_does_not_break_build(tmp_path, monkeypatch):
    _write_base(tmp_path)

    def boom(*a, **k):
        raise ValueError("test")
    monkeypatch.setattr(wt, "seas_block", boom)
    r = wt.build(_derived(), {}, ["PL", "DE"], REF, 5, 3, folder=tmp_path)
    assert r["countries"]["PL"]["error"] == "ValueError: test"
