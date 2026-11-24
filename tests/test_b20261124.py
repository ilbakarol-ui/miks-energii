"""B-20261124-4: pełny przebieg odświeża stan cen na jutro w podsumowaniu popołudniowym."""
import datetime as dt

from mixtracker.run_daily import refresh_afternoon_prices_note


def _prices(d, zone, last):
    (d / "prices").mkdir(parents=True, exist_ok=True)
    (d / "prices" / f"{zone}.csv").write_text(f"date,price_avg\n2026-11-23,100\n{last},120\n", encoding="utf-8")


def test_full_run_rewrites_stale_afternoon_note(tmp_path):
    today = dt.date(2026, 11, 24)
    _prices(tmp_path, "PL", "2026-11-25")
    _prices(tmp_path, "DE-LU", "2026-11-25")
    _prices(tmp_path, "FR", "2026-11-24")
    pp = tmp_path / "podsumowanie_popoludnie.md"
    pp.write_text("Ceny na 2026-11-25: 0 z 3 stref\n", encoding="utf-8")
    assert refresh_afternoon_prices_note(tmp_path, today)
    txt = pp.read_text(encoding="utf-8")
    assert "Ceny na 2026-11-25: 2 z 3 stref; brak: FR" in txt
    assert not refresh_afternoon_prices_note(tmp_path, today)      # bez zmian – bez ponownego zapisu


def test_no_tomorrow_prices_keeps_file(tmp_path):
    today = dt.date(2026, 11, 24)
    _prices(tmp_path, "PL", "2026-11-24")
    pp = tmp_path / "podsumowanie_popoludnie.md"
    pp.write_text("stare\n", encoding="utf-8")
    assert not refresh_afternoon_prices_note(tmp_path, today)
    assert pp.read_text(encoding="utf-8") == "stare\n"
