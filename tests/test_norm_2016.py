"""26.11.2026: norma temperatury 2016–2025 w zakładce „Pogoda” (ERA5 2016–2018 w osobnym temp_hist_old.csv)."""
import datetime as dt

import numpy as np
import pandas as pd

from mixtracker import shared_weather as sw
from mixtracker import weather_tab as wt
from tests.test_shared_weather import TODAY, fake_get_factory, setup_trial
from tests.test_weather_tab import REF, _derived, _write_base


def _old_hist(folder, pts=("EU_PL_1", "EU_PL_2", "EU_DE_1"), until="2018-12-31", off=4.0):
    days = pd.date_range("2016-01-01", until)
    base = 10 - 9 * np.cos(2 * np.pi * (days.dayofyear - 15) / 365) + off       # 2016–2018 o `off` °C cieplej
    pd.concat([pd.DataFrame({"pt": pt, "date": days.strftime("%Y-%m-%d"), "temp": np.round(base + (2 if pt == "EU_DE_1" else 0), 2)})
               for pt in pts]).to_csv(folder / "temp_hist_old.csv", index=False)


def test_norm_years_fixed_period():
    days = pd.date_range("2014-01-01", "2026-10-24").strftime("%Y-%m-%d")
    hist = pd.DataFrame({"pt": "A", "date": days, "temp": 5.0})
    pts = pd.DataFrame({"pt": ["A"]})
    assert wt.norm_years(hist, pts, REF, dict(wt.DEFAULTS)) == list(range(2016, 2026))
    assert wt.norm_years(hist, pts, dt.date(2025, 6, 1), dict(wt.DEFAULTS)) == list(range(2016, 2025))
    assert wt.norm_years(hist[hist.date >= "2019"], pts, REF, dict(wt.DEFAULTS)) == list(range(2019, 2026))


def test_old_hist_used_only_when_complete(tmp_path):
    _write_base(tmp_path)
    _old_hist(tmp_path, until="2018-06-30")                              # niekompletne – norma bez zmian
    t = wt.temp_block("PL", wt.load(tmp_path), REF, dict(wt.DEFAULTS))
    assert t["norm_years"] == "2019–2025"
    _old_hist(tmp_path, pts=("EU_PL_1", "EU_PL_2"))                     # bez punktu DE – też bez zmian
    assert wt.temp_block("PL", wt.load(tmp_path), REF, dict(wt.DEFAULTS))["norm_years"] == "2019–2025"
    _old_hist(tmp_path)                                                  # komplet – norma 2016–2025
    W = wt.load(tmp_path)
    t2 = wt.temp_block("PL", W, REF, dict(wt.DEFAULTS))
    assert t2["norm_years"] == "2016–2025"
    # 3 z 10 lat o 4 °C cieplej → norma wyższa o ok. 1,2 °C
    j = t2["dates"].index(REF.isoformat())
    assert abs(t2["norm"][j] - t["norm"][j] - 1.2) < 0.15
    r = wt.build(_derived(), {}, ["EU", "PL", "DE"], REF, 5, 3, folder=tmp_path)
    assert r["countries"]["PL"]["temp"]["norm_years"] == "2016–2025"
    assert r["countries"]["PL"]["seas"]["norm_years"] == "2016–2025"


def test_shared_weather_hist_old_backfill(tmp_path, monkeypatch):
    setup_trial(tmp_path, monkeypatch)
    calls = []
    meta = sw.run(budget=1e6, get=fake_get_factory(calls), today=TODAY)
    out = tmp_path / "out"
    old = pd.read_csv(out / "temp_hist_old.csv")
    # tylko punkty sieci gazowej i próbne, pełne 2016–2018, w kawałkach ≤ 365 dni
    assert set(old.pt) == {"P0", "P1", "P2", "EX_FR_1", "EX_FR_2"}
    assert old.date.min() == "2016-01-01" and old.date.max() == "2018-12-31"
    assert old.groupby("pt").size().eq(1096).all()
    hc = [c[1] for c in calls if c[0] == sw.ARCHIVE_URL and c[1]["start_date"] < "2019-01-01"]
    assert all((dt.date.fromisoformat(c["end_date"]) - dt.date.fromisoformat(c["start_date"])).days < sw.HIST_CHUNK_DAYS for c in hc)
    assert meta["hist_old"]["pending"] == 0 and meta["hist_old"]["status"] == "OK"
    # temp_hist.csv (czytany przez raport Gaz UE) bez lat 2016–2018; errors / steps bez kroku hist_old
    assert pd.read_csv(out / "temp_hist.csv").date.min() >= sw.HIST_START
    assert "hist_old" not in meta["steps"] and meta["errors"] == []
    # następny przebieg niczego nie pobiera
    calls2 = []
    meta2 = sw.run(budget=1e6, get=fake_get_factory(calls2), today=TODAY + dt.timedelta(days=1))
    assert not [c for c in calls2 if c[0] == sw.ARCHIVE_URL and c[1]["start_date"] < "2019-01-01"]
    assert meta2["hist_old"]["status"] == "komplet"
