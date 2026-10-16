"""Oficjalne dane miesięczne Eurostatu: produkcja netto wg paliwa (nrg_cb_pem)
i energia dostępna na rynek wewnętrzny = zużycie krajowe (nrg_cb_em, AIM).
Publikowane z opóźnieniem 2-3 miesięcy; obejmują też Maltę i agregat UE-27.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import requests

from . import store

BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
PEM_SIEC = ["TOTAL", "C0000", "G3000", "O4000XBIO", "N9000", "X9900", "RA100", "RA130",
            "RA200", "RA300", "RA400", "RA500_5160", "CF_R", "CF_NR_OTH"]
EM_BAL = ["AIM", "IMP", "EXP"]
OUT = store.DATA / "eurostat" / "monthly.csv"


def _get(dataset: str, params: list[tuple[str, str]]) -> dict:
    r = requests.get(f"{BASE}/{dataset}", params=[("format", "JSON"), ("lang", "en")] + params, timeout=120)
    r.raise_for_status()
    return r.json()


def _flatten(d: dict) -> pd.DataFrame:
    ids, sizes = d["id"], d["size"]
    cats = {k: d["dimension"][k]["category"]["index"] for k in ids}
    inv = {k: {i: c for c, i in v.items()} for k, v in cats.items()}
    rows = []
    for flat, val in d.get("value", {}).items():
        n = int(flat)
        pos = {}
        for k, s in zip(reversed(ids), reversed(sizes)):
            pos[k] = inv[k][n % s]
            n //= s
        pos["value"] = val
        rows.append(pos)
    return pd.DataFrame(rows)


def fetch(since: str) -> pd.DataFrame:
    geos = [c["eurostat"] for c in store.load_countries() if c["eurostat"]]
    geo_p = [("geo", g) for g in geos]
    pem = _flatten(_get("nrg_cb_pem", [("unit", "GWH"), ("sinceTimePeriod", since)] + geo_p
                        + [("siec", s) for s in PEM_SIEC]))
    em = _flatten(_get("nrg_cb_em", [("unit", "GWH"), ("siec", "E7000"), ("sinceTimePeriod", since)] + geo_p
                       + [("nrg_bal", b) for b in EM_BAL]))
    pem = pem.pivot_table(index=["geo", "time"], columns="siec", values="value", aggfunc="first")
    em = em.pivot_table(index=["geo", "time"], columns="nrg_bal", values="value", aggfunc="first")
    df = pem.join(em, how="outer").reset_index()
    return df


def to_groups(df: pd.DataFrame) -> pd.DataFrame:
    """Przelicza kody SIEC na 8 grup trackera (GWh)."""
    def g(c, fill=None):
        s = df[c] if c in df else pd.Series(float("nan"), index=df.index)
        return s.fillna(fill) if fill is not None else s

    out = pd.DataFrame({"geo": df["geo"], "month": df["time"]})
    out["total"] = g("TOTAL")
    out["coal"] = g("C0000")
    out["gas"] = g("G3000")
    out["nuclear"] = (g("N9000") - g("X9900", 0)).clip(lower=0)   # N9000 obejmuje też "inne paliwa n.e.c."
    out["hydro"] = g("RA100")
    out["wind"] = g("RA300")
    out["solar"] = g("RA400")
    out["bio"] = g("CF_R", 0) + g("RA200", 0) + g("RA500_5160", 0)
    known = out[["coal", "gas", "nuclear", "hydro", "wind", "solar", "bio"]].fillna(0).sum(axis=1)
    out["other"] = (out["total"] - known).clip(lower=0)
    out["renewable"] = (out["hydro"].fillna(0) - g("RA130", 0) + out["wind"].fillna(0)
                        + out["solar"].fillna(0) + out["bio"])
    out["consumption"] = g("AIM")
    out["imports"] = g("IMP")
    out["exports"] = g("EXP")
    out = out[out["total"].notna() | out["consumption"].notna()]
    return out.sort_values(["geo", "month"]).reset_index(drop=True)


def update(history_years: int = 5) -> tuple[pd.DataFrame, str | None]:
    since = f"{dt.date.today().year - history_years - 1}-01"
    try:
        df = to_groups(fetch(since))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        df.round(3).to_csv(OUT, index=False)
        return df, None
    except Exception as e:  # dane Eurostatu są uzupełniające - błąd nie zatrzymuje raportu
        err = f"Eurostat: {type(e).__name__}: {e}"
        if OUT.exists():
            return pd.read_csv(OUT), err
        return pd.DataFrame(), err
