"""B-20261119-2: archiwum prognoz – pierwsze wydanie dnia zostaje, spóźnione powtórzenia go nie nadpisują."""
import pandas as pd

from mixtracker import forecast


def _fc(issued, wind, solar=None):
    days = ["2026-11-19", "2026-11-20", "2026-11-21"]
    out = {"issued": issued, "days": days, "wind": wind, "load": [500.0, 510.0, 520.0]}
    if solar is not None:
        out["solar"] = solar
    return out


def test_first_issue_of_day_is_kept(tmp_path):
    p = tmp_path / "archive.csv"
    forecast._archive("PL", _fc("2026-11-19T05:37Z", [100.0, 110.0, 120.0]), path=p)
    # spóźniony przebieg tego samego dnia (np. 20:35 UTC) z inną pogodą – bez zmian w archiwum
    forecast._archive("PL", _fc("2026-11-19T20:35Z", [130.0, 140.0, 150.0]), path=p)
    a = pd.read_csv(p, dtype={"issued": str, "date": str})
    assert a[a.type == "wind"]["gwh"].tolist() == [100.0, 110.0, 120.0]
    assert len(a) == 6
    # brakująca wielkość (słońce) z późniejszego przebiegu jest dopisywana, reszta zostaje
    forecast._archive("PL", _fc("2026-11-19T20:35Z", [130.0, 140.0, 150.0], solar=[5.0, 6.0, 7.0]), path=p)
    a = pd.read_csv(p, dtype={"issued": str, "date": str})
    assert a[a.type == "solar"]["gwh"].tolist() == [5.0, 6.0, 7.0]
    assert a[a.type == "wind"]["gwh"].tolist() == [100.0, 110.0, 120.0] and len(a) == 9
    # inny kraj i następny dzień – zapisywane normalnie
    forecast._archive("DE", _fc("2026-11-19T20:35Z", [1.0, 2.0, 3.0]), path=p)
    forecast._archive("PL", _fc("2026-11-20T02:10Z", [7.0, 8.0, 9.0]), path=p)
    a = pd.read_csv(p, dtype={"issued": str, "date": str})
    assert set(a.code) == {"PL", "DE"} and set(a.issued) == {"2026-11-19", "2026-11-20"}


def test_replace_when_flag_off(tmp_path, monkeypatch):
    p = tmp_path / "archive.csv"
    monkeypatch.setattr(forecast, "ARCHIVE_KEEP_FIRST", False)
    forecast._archive("PL", _fc("2026-11-19T05:37Z", [100.0, 110.0, 120.0]), path=p)
    forecast._archive("PL", _fc("2026-11-19T20:35Z", [130.0, 140.0, 150.0]), path=p)
    a = pd.read_csv(p, dtype={"issued": str, "date": str})
    assert a[a.type == "wind"]["gwh"].tolist() == [130.0, 140.0, 150.0] and len(a) == 6
