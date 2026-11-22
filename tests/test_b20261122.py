"""B-20261122-1: świeże zawyżone doby zużycia CH są luką do czasu poprawki źródła; starsze, inne kraje i DK – bez zmian."""
import pandas as pd

from mixtracker import model


def _series():
    idx = pd.date_range("2026-09-28", "2026-11-21", freq="D")
    return pd.Series(190.0, index=idx)


def test_fresh_high_ch_day_is_bad():
    y = _series()
    y[pd.Timestamp("2026-11-21")] = 286.1                      # ×1,5 – jak 21.11.2026
    assert pd.Timestamp("2026-11-21") in model.bad_days(y, None, "load", "CH")


def test_old_high_ch_day_is_kept():
    y = _series()
    y[pd.Timestamp("2026-11-17")] = 285.0                      # 4 dni temu – poza oknem 3 dni
    assert pd.Timestamp("2026-11-17") not in model.bad_days(y, None, "load", "CH")


def test_small_rise_and_other_countries_unchanged():
    y = _series()
    y[pd.Timestamp("2026-11-21")] = 205.0                      # ×1,08 – poniżej progu 1,12
    assert pd.Timestamp("2026-11-21") not in model.bad_days(y, None, "load", "CH")
    y[pd.Timestamp("2026-11-21")] = 286.1
    assert pd.Timestamp("2026-11-21") not in model.bad_days(y, None, "load", "PL")
    assert pd.Timestamp("2026-11-21") not in model.bad_days(y, None, "load")


def test_dk_low_rule_unchanged_and_no_high_rule():
    y = _series()
    y[pd.Timestamp("2026-11-20")] = 130.0                      # świeża zaniżona – jak dotąd luka
    y[pd.Timestamp("2026-11-12")] = 130.0                      # 9 dni temu – poza oknem 8 dni
    y[pd.Timestamp("2026-11-21")] = 286.1                      # zawyżona – dla DK reguły w górę nie ma
    bad = model.bad_days(y, None, "load", "DK")
    assert pd.Timestamp("2026-11-20") in bad and pd.Timestamp("2026-11-12") not in bad
    assert pd.Timestamp("2026-11-21") not in bad
