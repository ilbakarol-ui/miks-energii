"""B-20261109-1, -2: doby, których źródło nie dosłało (Dania od 5.11.2026), i Dania z ENTSO-E."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from mixtracker import build, run_daily

ROOT = Path(__file__).resolve().parents[1]


def _frame(last: str, n: int = 40, cov_last: float | None = None) -> pd.DataFrame:
    idx = pd.date_range(end=last, periods=n, freq="D")
    df = pd.DataFrame({"cov": 1.0, "load": 100.0}, index=idx)
    if cov_last is not None:
        df.iloc[-1, df.columns.get_loc("cov")] = cov_last
        df.iloc[-1, df.columns.get_loc("load")] = np.nan
    return df


def test_window14_counts_missing_days():
    ref = "2026-11-08"
    dk = _frame("2026-11-04", cov_last=0.375)          # jak DK.csv 9.11: ostatni wiersz 4.11, cov 0,375
    w = build.window14(dk, ref, 0.8)
    assert w["missing14"] == 4                         # 5–8.11
    assert w["below14"] == 1 and w["partial14"] == 1   # 4.11
    assert w["load_gaps14"] == 1
    # okno nie cofa się z danymi: dzień później brak rośnie
    assert build.window14(dk, "2026-11-09", 0.8)["missing14"] == 5


def test_window14_full_country_unchanged():
    w = build.window14(_frame("2026-11-08"), "2026-11-08", 0.8)
    assert w == {"partial14": 0, "below14": 0, "missing14": 0, "load_gaps14": 0}


def test_partial_lines_missing_days():
    p = {"control": {"DK": {"missing14": 4, "last": "2026-11-03"}, "CY": {"missing14": 2, "last": "2026-11-05"},
                     "BG": {"missing14": 1, "last": "2026-11-07"}, "PL": {"missing14": 0}}}
    lines = run_daily._partial_lines(p)
    assert lines[0] == ("Brak dób w 14 dniach (źródło nie dosłało danych): DK 4 (ostatnia pełna doba 2026-11-03), "
                        "CY 2 (ostatnia pełna doba 2026-11-05)")
    assert run_daily._partial_lines({"control": {"PL": {"missing14": 1}}}) == []


def test_settings_dk_entsoe():
    s = json.loads((ROOT / "config" / "settings.json").read_text())
    dk = s["entsoe"]["DK"]
    assert dk["domain"] == "10Y1001A1001A65H" and dk["load"] and dk["generation"]
    assert dk["from"] <= "2026-11-01"
