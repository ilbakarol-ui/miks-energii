"""Dane DEMONSTRACYJNE do podglądu dashboardu bez dostępu do Energy-Charts.

Rozkłada miesięczne dane Eurostatu (data/eurostat/monthly.csv) na dni z losową
zmiennością wiatru i słońca. NIE są to prawdziwe dane dzienne.

python scripts/make_demo_data.py <folder_wyjściowy>
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mixtracker import store  # noqa: E402
from mixtracker import prices  # noqa: E402
from mixtracker.categories import AUX, DETAIL_KEYS, PRICE_COLS  # noqa: E402

LIGNITE = {"DE": .55, "PL": .4, "CZ": .85, "BG": .9, "GR": 1, "SK": .3, "SI": 1, "HU": 1, "RO": .9, "EU": .6}
OFFSHORE = {"DE": .2, "DK": .4, "NL": .45, "BE": .55, "UK": .5, "FR": .05, "PL": .0, "EU": .12}
PROXY = {"CH": ("AT", {"nuclear": 0.33}), "UK": ("NL", {"nuclear": 0.14})}
BASE_PRICE = {"PL": 105, "DE": 88, "LU": 88, "FR": 62, "NL": 86, "BE": 80, "AT": 95, "CH": 92, "CZ": 92,
              "HU": 110, "SI": 105, "IT": 110, "DK": 75, "NO": 45, "SE": 55}


def main(out: Path, end: dt.date):
    es = pd.read_csv(store.DATA / "eurostat" / "monthly.csv")
    geo2code = {c["eurostat"]: c["code"] for c in store.load_countries() if c["eurostat"]}
    es["code"] = es["geo"].map(geo2code)
    rng = np.random.default_rng(7)
    start = dt.date(end.year - 5, 1, 1)
    days = pd.date_range(start, end, freq="D")
    countries = [c for c in store.load_countries() if store.has_daily(c)]
    zones = prices.load_zones(["cc_by"])
    for c in countries:
        code = c["code"]
        src, override = PROXY.get(code, (code, {}))
        g = es[es["code"] == src].set_index("month")
        if g.empty:
            continue
        rows = []
        wind_noise = np.clip(rng.normal(1, 0.45, len(days)), 0.15, 2.4)
        sun_noise = np.clip(rng.normal(1, 0.25, len(days)), 0.3, 1.5)
        load_noise = rng.normal(1, 0.03, len(days)) * np.where(days.dayofweek >= 5, 0.9, 1.04)
        for i, d in enumerate(days):
            key = f"{d.year}-{d.month:02d}"
            yy = d.year
            while key not in g.index and yy > end.year - 7:
                yy -= 1
                key = f"{yy}-{d.month:02d}"
            if key not in g.index:
                continue
            r = g.loc[key]
            ndays = pd.Period(key).days_in_month
            trend = 1 + 0.06 * (d.year - yy)
            f = lambda k: (0 if pd.isna(r[k]) else r[k]) * 1000 / ndays  # noqa: E731
            wind = f("wind") * wind_noise[i] * trend
            solar = f("solar") * sun_noise[i] * trend
            coal = f("coal") * (2 - trend) * (1.25 - 0.25 * wind_noise[i])
            gas = f("gas") * (1.3 - 0.3 * wind_noise[i])
            row = {k: 0.0 for k in DETAIL_KEYS}
            lf = LIGNITE.get(src, 0.0)
            row["lignite"], row["hard_coal"] = coal * lf, coal * (1 - lf)
            row["gas"] = gas
            row["nuclear"] = f("nuclear")
            off = OFFSHORE.get(code, 0.0)
            row["wind_onshore"], row["wind_offshore"] = wind * (1 - off), wind * off
            row["solar"] = solar
            row["hydro_ror"] = f("hydro") * 0.6
            row["hydro_reservoir"] = f("hydro") * 0.4
            row["biomass"] = f("bio")
            row["misc"] = f("other")
            gen = sum(row.values())
            if override:
                for k, share in override.items():
                    row[k] = gen * share
            gen = sum(row.values())
            load = (f("consumption") if not pd.isna(r["consumption"]) else gen) * load_noise[i]
            row["load"] = load
            row["storage_charge"] = 0.0
            row["xborder"] = load - gen
            row["cov"] = 1.0
            if code in zones:
                yr = d.year + d.dayofyear / 366
                level = BASE_PRICE.get(code, 90) * (2.2 if 2022 <= yr < 2022.9 else 1.0 if yr > 2023.3 else 1.4)
                sol_sh = solar / max(gen, 1)
                wnd_sh = wind / max(gen, 1)
                pa = level * (1 - 0.9 * wnd_sh) * (1.05 if d.dayofweek < 5 else 0.82) * rng.normal(1, 0.08)
                row["price_avg"] = pa
                row["price_min"] = pa * (0.45 - 1.2 * sol_sh)
                row["price_max"] = pa * (1.5 + 0.8 * sol_sh)
                row["price_neg_h"] = max(0.0, (sol_sh - 0.18) * 30 * rng.uniform(0.3, 1.3)) if d.year >= 2023 else 0.0
                for k, q, rate in (("solar", solar, 0.95 - 1.6 * sol_sh), ("wind", wind, 0.92 - 0.5 * wnd_sh),
                                   ("gen", gen, 0.99), ("load", load, 1.02)):
                    row["pq_" + k] = q
                    row["pw_" + k] = q * pa * max(rate, 0.15) / 1000
            else:
                for k in PRICE_COLS:
                    row[k] = float("nan")
            row["date"] = d
            rows.append(row)
        df = pd.DataFrame(rows).set_index("date")
        df = df[["cov"] + DETAIL_KEYS + AUX + PRICE_COLS]
        if code in zones and len(zones[code]) > 1:
            (out.parent / "prices").mkdir(parents=True, exist_ok=True)
            for j, z in enumerate(zones[code]):
                zd = df[["price_avg", "price_min", "price_max", "price_neg_h"]] * (1 + 0.06 * (j - 0.5))
                zd.index = zd.index.strftime("%Y-%m-%d")
                zd.index.name = "date"
                zd.round(2).to_csv(out.parent / "prices" / f"{z['zone']}.csv")
        store.write_daily(code, df, out)
    print(f"Zapisano dane demonstracyjne do {out}")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo_daily")
    end = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.date.today() - dt.timedelta(days=1)
    main(out, end)
