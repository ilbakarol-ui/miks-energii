"""Zakładka „Pogoda”: prognoza sprzed 3 dni (wiatr z archiwum prognoz Miksu, temperatura z archiwum temperatury)."""
import datetime as dt

import pandas as pd

from mixtracker import weather_tab as wt
from tests.test_weather_tab import REF, _derived, _write_base


def test_archive_temp_keeps_first_issue(tmp_path):
    _write_base(tmp_path)
    W = wt.load(tmp_path)
    path = tmp_path / "temp_archive.csv"
    n = wt.archive_temp(["EU", "PL", "DE"], W=W, path=path, today=REF)
    a = pd.read_csv(path, dtype={"issued": str, "date": str})
    assert n == len(a) > 0 and set(a.issued) == {"2026-10-30"} and set(a.code) == {"EU", "PL", "DE"}
    # temperatura kraju = ta sama średnia punktów co linia prognozy w temp_block
    tb = wt.temp_block("PL", W, REF, dict(wt.DEFAULTS))
    d = tb["dates"][tb["split"] + 2]
    assert a[(a.code == "PL") & (a.date == d)]["temp"].iloc[0] == round(tb["det"][tb["split"] + 2], 1) or \
        abs(a[(a.code == "PL") & (a.date == d)]["temp"].iloc[0] - tb["det"][tb["split"] + 2]) < 0.06
    # drugi przebieg tego samego dnia (np. po południu z wieczorną prognozą) niczego nie zmienia
    W["recent"] = W["recent"].assign(temp=W["recent"]["temp"] + 5)
    assert wt.archive_temp(["EU", "PL", "DE"], W=W, path=path, today=REF) == 0
    assert pd.read_csv(path, dtype={"issued": str, "date": str}).equals(a)
    # nowe wydanie dopisywane, wydania starsze niż 60 dni usuwane
    assert wt.archive_temp(["PL"], W=W, path=path, issued="2026-10-31", today=REF) > 0
    wt.archive_temp(["PL"], W=W, path=path, issued="2027-01-05", today=dt.date(2027, 1, 5))
    assert set(pd.read_csv(path, dtype=str).issued) == {"2027-01-05"}


def test_prev_issue_picks_lag_3():
    arch = pd.DataFrame([{"issued": i, "code": "PL", "date": d, "type": t, "gwh": v}
                         for i in ("2026-10-26", "2026-10-27") for d, v in (("2026-10-28", 1.0), ("2026-10-29", 2.0))
                         for t in ("wind", "solar")])
    iss, prev = wt._prev_issue(arch, "gwh", "2026-10-30T02:15Z", type_="wind")
    assert iss == "2026-10-27" and prev == {"PL": {"2026-10-28": 1.0, "2026-10-29": 2.0}}
    assert wt._prev_issue(arch, "gwh", "2026-11-05", type_="wind") == (None, {})


def test_build_adds_fc3_for_wind_and_temp(tmp_path, monkeypatch):
    _write_base(tmp_path)
    fdir = tmp_path / "forecast"
    fdir.mkdir()
    monkeypatch.setattr(wt.store, "FORECAST", fdir)
    issued = REF + dt.timedelta(days=1)                      # bieżące wydanie Miksu 30.10 → archiwum z 27.10
    days = [(REF + dt.timedelta(days=k)).isoformat() for k in range(1, 8)]
    fcs = {"PL": {"issued": f"{issued}T02:15Z", "days": days, "wind": [100.0] * 7}}
    old = [{"issued": "2026-10-27", "code": "PL", "date": (dt.date(2026, 10, 27) + dt.timedelta(days=k)).isoformat(),
            "type": "wind", "gwh": 80.0} for k in range(7)]
    pd.DataFrame(old).to_csv(fdir / "archive.csv", index=False)
    # archiwum temperatury: wydanie 27.10 (bieżąca prognoza temperatury z meta.json: 30.10), +1 °C względem dziś
    W = wt.load(tmp_path)
    wt.archive_temp(["PL"], W=dict(W, recent=W["recent"].assign(temp=W["recent"]["temp"] + 1.0)),
                    path=tmp_path / "temp_archive.csv", issued="2026-10-27", today=REF)
    r = wt.build(_derived(), fcs, ["EU", "PL"], REF, 5, 3, folder=tmp_path)
    w = r["countries"]["PL"]["wind"]
    assert w["fc3_issued"] == "2026-10-27"
    j = w["dates"].index("2026-10-31")
    assert w["fc3"][j] == 80.0 and w["det"][j] == 100.0
    assert w["fc3"][w["dates"].index(REF.isoformat())] == 80.0          # miniony dzień – porównanie z produkcją
    t = r["countries"]["PL"]["temp"]
    k = t["dates"].index("2026-11-01")
    assert t["fc3_issued"] == "2026-10-27" and abs(t["fc3"][k] - t["det"][k] - 1.0) < 0.06
    assert r["prev_issued"] == {"wind": "2026-10-27", "temp": "2026-10-27", "lag": 3}
    assert "fc3" not in (r["countries"].get("EU", {}).get("wind") or {})   # brak archiwum UE – bez linii


def test_build_without_archives(tmp_path, monkeypatch):
    _write_base(tmp_path)
    monkeypatch.setattr(wt.store, "FORECAST", tmp_path / "brak")
    r = wt.build(_derived(), {}, ["EU", "PL"], REF, 5, 3, folder=tmp_path)
    assert "fc3" not in r["countries"]["PL"]["temp"] and r["prev_issued"]["temp"] is None


def test_impact_fc3_country_and_eu(tmp_path, monkeypatch):
    """Wpływ na gaz i prąd z prognozy sprzed 3 dni: ta sama wrażliwość, 1 °C cieplej → mniej gazu i prądu; UE = suma."""
    _write_base(tmp_path)
    monkeypatch.setattr(wt.store, "FORECAST", tmp_path / "brak")
    W = wt.load(tmp_path)
    wt.archive_temp(["PL", "DE"], W=dict(W, recent=W["recent"].assign(temp=W["recent"]["temp"] + 1.0)),
                    path=tmp_path / "temp_archive.csv", issued="2026-10-27", today=REF)
    conf = dict(wt.DEFAULTS)
    coefs = {"PL": {"gas": {"a": 100.0, "b": 30.0}, "power": {"a": 400.0, "bh": 5.0, "bc": 0.0}},
             "DE": {"gas": {"a": 300.0, "b": 200.0}, "power": {"a": 1300.0, "bh": 17.0, "bc": 0.0}}}
    arch = pd.read_csv(tmp_path / "temp_archive.csv", dtype={"issued": str, "date": str})
    iss, prev = wt._prev_issue(arch, "temp", "2026-10-30")
    blocks = {c: wt.impact_country(c, W, coefs[c], REF, conf, prev=(iss, prev.get(c))) for c in coefs}
    pl = blocks["PL"]
    k = pl["split"] + 3
    # listopad: zawsze poniżej 15,5 °C – 1 °C cieplej = 30 GWh/d gazu i 5 GWh/d prądu mniej
    assert pl["fc3_issued"] == "2026-10-27"
    assert abs(pl["gas"]["fc3"][k] - (pl["gas"]["det"][k] - 30)) <= 1 and abs(pl["power"]["fc3"][k] - (pl["power"]["det"][k] - 5)) <= 1
    eu = wt.impact_eu(blocks, coefs)
    j = eu["dates"].index(pl["dates"][k])
    de = blocks["DE"]
    assert eu["fc3_issued"] == "2026-10-27"
    assert abs(eu["gas"]["fc3"][j] - (pl["_gas3"][k] + de["_gas3"][de["dates"].index(pl["dates"][k])])) <= 1
    # bez archiwum – brak serii
    b0 = wt.impact_country("PL", W, coefs["PL"], REF, conf)
    assert "fc3" not in b0["gas"] and "fc3_issued" not in b0
    assert "fc3" not in wt.impact_eu({"PL": b0}, coefs)["gas"]
