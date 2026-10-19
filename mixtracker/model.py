"""Model przeliczający pogodę na produkcję z wiatru, słońca i zużycie (dziennie, GWh).

Jeden model dla prognozy dziennej, błędu modelu i weryfikacji, żeby weryfikacja sprawdzała
dokładnie to, co jest publikowane.

1. Część pogodowa - regresja liniowa na oknie z ostatnich dni:
   - wiatr: 1, cf, cf² (cf - wskaźnik mocy turbiny z prędkości wiatru na 100 m), 120 dni,
     nowsze dni ważą więcej (waga spada o połowę co 30 dni),
   - słońce: 1, promieniowanie, 90 dni, waga spada o połowę co 15 dni (model nadąża za
     zmianą wysokości słońca w ciągu roku),
   - zużycie: 1, dni grzewcze, dni chłodnicze, sobota, niedziela, święto, dzień pomostowy,
     okres świąteczny 24.12-1.01 (dni robocze), 365 dni, wagi równe.
   Dopasowanie odporne: po pierwszym dopasowaniu dni z błędem większym niż 4 odchylenia
   (MAD) są pomijane i model jest dopasowywany ponownie.
2. Korekta z ostatniego znanego dnia: błąd części pogodowej z ostatniego dnia z danymi
   (średnia z `ar_days` dni) przenosi się na kolejne dni z wagą ρ(h), gdzie h to liczba dni
   od ostatniego znanego dnia. ρ(h) jest szacowane z historii błędów w oknie uczenia
   (współczynnik autokorelacji, ograniczony do 0-0,95). Korekta usuwa systematyczne
   odchylenie (np. przyrost mocy zainstalowanej, zmiana zachowania odbiorców) i zanika
   z wyprzedzeniem.
3. Dni z ewidentną luką w danych źródła (produkcja bliska zera przy dobrej pogodzie,
   zużycie poniżej 60% albo powyżej 160% mediany z 4 tygodni) są pomijane przy uczeniu,
   w korekcie i w weryfikacji, a ich liczba pokazywana jest w kontroli danych.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd

WINDOWS = {"wind": 120, "solar": 90, "load": 365}
AR_DAYS = {"wind": 3, "solar": 3, "load": 1}
HALFLIFE: dict[str, float | None] = {"wind": 30, "solar": 15, "load": None}
MAX_LAG = 10
NEED = {"wind": ["cf"], "solar": ["ghi"], "load": ["hdd", "cdd"]}

# kody krajów trackera -> (kraj, region) w bibliotece holidays
HOLIDAY_CODES = {"UK": ("GB", "NIR"), "GB": ("GB", "ENG")}


# ---------- kalendarz ----------

@lru_cache(maxsize=64)
def _holiday_set(code: str, y0: int, y1: int) -> frozenset:
    try:
        import holidays
    except ImportError:          # bez biblioteki model działa jak wcześniej (bez świąt)
        return frozenset()
    c, sub = HOLIDAY_CODES.get(code, (code, None))
    try:
        h = holidays.country_holidays(c, subdiv=sub, years=range(y0, y1 + 1))
    except Exception:
        return frozenset()
    return frozenset(pd.Timestamp(d) for d in h.keys())


def calendar(index: pd.DatetimeIndex, code: str | None) -> pd.DataFrame:
    """Święta (dni robocze), dni pomostowe i okres świąteczny 24.12-1.01."""
    idx = pd.DatetimeIndex(index)
    out = pd.DataFrame(0.0, index=idx, columns=["hol", "bridge", "xmas"])
    if len(idx) == 0:
        return out
    dow = idx.dayofweek
    hs = _holiday_set(code, int(idx.year.min()) - 1, int(idx.year.max()) + 1) if code else frozenset()
    if hs:
        is_h = np.array([d in hs for d in idx])
        nxt = np.array([(d + pd.Timedelta(days=1)) in hs for d in idx])
        prv = np.array([(d - pd.Timedelta(days=1)) in hs for d in idx])
        out["hol"] = (is_h & (dow < 5)).astype(float)
        out["bridge"] = ((~is_h) & (((dow == 0) & nxt) | ((dow == 4) & prv))).astype(float)
    xm = ((idx.month == 12) & (idx.day >= 24)) | ((idx.month == 1) & (idx.day == 1))
    out["xmas"] = (xm & (dow < 5) & (out["hol"].to_numpy() == 0)).astype(float)
    return out


# ---------- luki w danych źródła ----------

def bad_days(y: pd.Series, feat: pd.DataFrame | None, kind: str) -> pd.DatetimeIndex:
    """Dni, w których wartość ze źródła jest ewidentnie błędna (luka, zero przy dobrej pogodzie)."""
    y = y.dropna()
    if y.empty:
        return pd.DatetimeIndex([])
    bad = y < 0
    if kind == "load":
        med = y.rolling("29D", min_periods=10).median()
        med = med.fillna(y.median())
        bad |= (y < 0.6 * med) | (y > 1.6 * med)
    elif feat is not None and not feat.empty:
        x = feat["cf" if kind == "wind" else "ghi"].reindex(y.index)
        lo_x = 0.15 if kind == "wind" else 1.0
        k = (y / x).where(x > lo_x)
        # typowa wydajność (90. percentyl z roku) - odporna na pojedyncze luki i przyrost mocy
        kref = k.rolling("365D", min_periods=20).quantile(0.9).bfill()
        frac = 0.1 if kind == "wind" else 0.15
        bad |= (x > lo_x) & (y < frac * kref * x)
    return y.index[bad.fillna(False).to_numpy()]


# ---------- model ----------

@dataclass
class Model:
    kind: str
    code: str | None
    coef: np.ndarray
    rho: np.ndarray = field(default_factory=lambda: np.zeros(MAX_LAG))
    ar_days: int = 1
    fitted_to: pd.Timestamp | None = None
    n: int = 0
    cal: bool = True


def design(f: pd.DataFrame, kind: str, code: str | None, cal: bool = True) -> np.ndarray:
    if kind == "wind":
        return np.column_stack([np.ones(len(f)), f["cf"], f["cf"] ** 2])
    if kind == "solar":
        return np.column_stack([np.ones(len(f)), f["ghi"]])
    dow = f.index.dayofweek
    cols = [np.ones(len(f)), f["hdd"], f["cdd"], (dow == 5).astype(float), (dow == 6).astype(float)]
    if cal:
        c = calendar(f.index, code)
        cols += [c["hol"], c["bridge"], c["xmas"]]
    return np.column_stack(cols)


def _lstsq(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> np.ndarray:
    # kolumny bez zmienności (np. brak świąt w oknie) nie mogą dostać przypadkowego współczynnika
    keep = np.ones(X.shape[1], dtype=bool)
    keep[1:] = X[:, 1:].std(axis=0) > 1e-9
    coef = np.zeros(X.shape[1])
    sw = np.sqrt(w) if w is not None else np.ones(len(y))
    coef[keep], *_ = np.linalg.lstsq(X[:, keep] * sw[:, None], y * sw, rcond=None)
    return coef


def base(m: Model, feat: pd.DataFrame) -> pd.Series:
    X = design(feat.astype(float), m.kind, m.code, m.cal).astype(float)
    return pd.Series(X @ m.coef, index=feat.index)


def _ar_state(r: pd.Series, at: pd.Timestamp, days: int) -> float | None:
    """Średni błąd z `days` dni kończących się w `at` (tylko dni z danymi)."""
    w = r.loc[at - pd.Timedelta(days=days - 1):at].dropna()
    return float(w.mean()) if len(w) else None


def fit(feat: pd.DataFrame, y: pd.Series, kind: str, end: pd.Timestamp, code: str | None = None,
        min_n: int | None = None, robust: bool = True, ar: bool = True, gaps: bool = True,
        cal: bool = True, bad: pd.DatetimeIndex | None = None, window: int | None = None,
        halflife: float | None = None) -> Model | None:
    """Dopasowanie na oknie [end - okno, end). Dane od `end` nie są używane.
    bad - dni z luką w danych (gdy nie podano, wyznaczane z historii sprzed `end`)."""
    end = pd.Timestamp(end)
    window = window or WINDOWS[kind]
    halflife = HALFLIFE.get(kind) if halflife is None else (halflife or None)
    start = end - pd.Timedelta(days=window)
    y = y.dropna()
    y = y[y.index < end]
    if gaps:
        y = y.drop(bad if bad is not None else bad_days(y, feat, kind), errors="ignore")
    y = y[y.index >= start - pd.Timedelta(days=MAX_LAG + AR_DAYS[kind])]
    df = feat.join(y.rename("y"), how="inner")
    df = df[df.index >= start].dropna(subset=NEED[kind] + ["y"])
    if len(df) < (min_n or max(20, window // 4)) or df["y"].abs().sum() == 0:
        return None
    X = design(df, kind, code, cal).astype(float)
    yy = df["y"].to_numpy(dtype=float)
    # nowsze dni ważą więcej (połowa wagi po `halflife` dniach) - model nadąża za porą roku
    w = np.ones(len(df)) if not halflife else 0.5 ** ((end - df.index).days.to_numpy() / halflife)
    coef = _lstsq(X, yy, w)
    if robust:
        res = yy - X @ coef
        mad = 1.4826 * np.median(np.abs(res - np.median(res)))
        ok = np.abs(res) <= 4 * mad if mad > 0 else np.ones(len(res), dtype=bool)
        if ok.sum() >= 0.8 * len(res) and ok.sum() < len(res):
            coef = _lstsq(X[ok], yy[ok], w[ok])
    m = Model(kind, code, coef, ar_days=AR_DAYS[kind], fitted_to=end, n=len(df), cal=cal)
    if ar:
        f2 = feat.reindex(y.index)
        r = (y - base(m, f2.fillna(0)).where(f2[NEED[kind]].notna().all(axis=1))).dropna()
        r = r.asfreq("D") if len(r) else r
        if len(r) > 30:
            z = r.rolling(m.ar_days, min_periods=1).mean().where(r.notna())   # stan: średni błąd z ar_days dni
            rho = np.zeros(MAX_LAG)
            for h in range(1, MAX_LAG + 1):
                a = pd.DataFrame({"z": z, "r": r.shift(-h)}).dropna()
                a = a[a.index >= start]
                den = (a["z"] ** 2).sum()
                if len(a) >= 20 and den > 0:
                    rho[h - 1] = np.clip((a["z"] * a["r"]).sum() / den, 0.0, 0.95)
            # ρ nie może rosnąć z wyprzedzeniem
            m.rho = np.minimum.accumulate(rho)
    return m


def forecast(m: Model, feat: pd.DataFrame, y_known: pd.Series, dates: list[pd.Timestamp],
             gaps: bool = True, bad: pd.DatetimeIndex | None = None) -> pd.Series:
    """Prognoza na `dates`: część pogodowa + korekta z ostatniego znanego dnia.
    y_known - wartości znane w chwili wydania prognozy (bez dni z przyszłości)."""
    dates = pd.DatetimeIndex(dates)
    f = feat.reindex(dates)
    ok = f[NEED[m.kind]].notna().all(axis=1)
    p = base(m, f.fillna(0)).where(ok)
    if m.rho.any():
        y = y_known.dropna()
        if gaps and len(y):
            y = y.drop(bad if bad is not None else bad_days(y, feat, m.kind), errors="ignore")
        if len(y):
            last = y.index.max()
            hist = y.loc[last - pd.Timedelta(days=m.ar_days - 1):last]
            fh = feat.reindex(hist.index)
            r = (hist - base(m, fh.fillna(0)).where(fh[NEED[m.kind]].notna().all(axis=1))).dropna()
            if len(r):
                z = float(r.mean())
                h = np.array([(d - last).days for d in dates])
                w = np.array([m.rho[k - 1] if 1 <= k <= MAX_LAG else 0.0 for k in h])
                p = p + w * z
    return p.clip(lower=0)


def model_error(feat: pd.DataFrame, y: pd.Series, kind: str, last: pd.Timestamp, days: int = 28,
                code: str | None = None, min_n: int | None = None) -> float | None:
    """Błąd (suma |błędów| / suma wartości, %) na ostatnich `days` dniach przy znanej pogodzie.
    Model dopasowany bez tych dni; korekta z dnia poprzedzającego każdy dzień (wyprzedzenie 1 dzień)."""
    last = pd.Timestamp(last)
    cut = last - pd.Timedelta(days=days - 1)
    yc = y.dropna()
    bad = bad_days(yc, feat, kind)
    m = fit(feat, yc, kind, cut, code, min_n, bad=bad)
    if m is None:
        return None
    yc = yc.drop(bad, errors="ignore")
    test = yc[(yc.index >= cut) & (yc.index <= last)]
    preds = {}
    for t in test.index:
        p = forecast(m, feat, yc[yc.index < t], [t], gaps=False)
        if pd.notna(p.iloc[0]):
            preds[t] = float(p.iloc[0])
    if len(preds) < days // 2 or test.sum() <= 0:
        return None
    pr = pd.Series(preds)
    a = test.reindex(pr.index)
    return round(float(100 * (pr - a).abs().sum() / a.sum()), 1)
