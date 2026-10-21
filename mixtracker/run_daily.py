"""Codzienny przebieg: pobranie danych -> Eurostat -> trendy -> dashboard.

python -m mixtracker.run_daily [--budget 70] [--only PL,DE] [--skip-fetch]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import build, elexon, eurostat, store, trends, update, weather


def summary_md(payload: dict, run: dict) -> str:
    m = payload["meta"]
    lines = [f"## Miks energii UE - dane do {m['ref']}", ""]
    s = payload["summary"].get("EU") or next(iter(payload["summary"].values()), None)
    if s:
        w = s["week"]
        def f(v):
            return "–" if v is None else f"{v:,.0f}".replace(",", " ")
        def pp(v):
            return "–" if v is None else f"{v:.1f}%"
        lines += [f"**{m['periods']['week']['label']}** (UE-27)", "",
                  "| | bieżący | poprz. tydz. | rok temu | śr. 5 lat |", "|---|---|---|---|---|",
                  f"| Zużycie, GWh | {f(w['load'][0])} | {f(w['load'][1])} | {f(w['load'][2])} | {f(w['load'][3])} |",
                  f"| Produkcja, GWh | {f(w['gen_total'][0])} | {f(w['gen_total'][1])} | {f(w['gen_total'][2])} | {f(w['gen_total'][3])} |",
                  f"| Udział OZE | {pp(w['share_renewable'][0])} | {pp(w['share_renewable'][1])} | {pp(w['share_renewable'][2])} | {pp(w['share_renewable'][3])} |",
                  f"| Emisyjność, g/kWh | {f(w['intensity'][0])} | {f(w['intensity'][1])} | {f(w['intensity'][2])} | {f(w['intensity'][3])} |",
                  ""]
    if payload["movers"]:
        lines += ["**Największe zmiany**", ""]
        for it in payload["movers"][:8]:
            per = "tydz." if it["period"] == "week" else "mies."
            if it["type"] == "int_yoy":
                lines.append(f"- {it['name']}: emisyjność {it['cur']:.0f} g/kWh ({it['delta']:+.1f}% r/r, {per})")
            elif it["type"] == "load_yoy":
                lines.append(f"- {it['name']}: zużycie {it['delta']:+.1f}% r/r ({per})")
            elif it["type"] == "yoy":
                lines.append(f"- {it['name']}: {it['label']} {it['cur']:.1f}% ({it['delta']:+.1f} p.p. r/r, {per})")
            else:
                word = "powyżej maks." if it["type"] == "max5" else "poniżej min."
                lines.append(f"- {it['name']}: {it['label']} {it['cur']:.1f}% - {word} z 5 lat ({per})")
        lines.append("")
    be = run.get("by_endpoint") or {}
    detail = f" ({', '.join(f'{k}: {v}' for k, v in be.items())})" if be else ""
    lines += [f"Zapytania do Energy-Charts: {run.get('requests', 0)}{detail}; czas: {run.get('minutes', 0):.0f} min"]
    if run.get("forecasts") is not None:
        lines.append(f"Prognozy pobrane dla: {', '.join(run['forecasts']) or 'żadnego kraju'}")
    hc = run.get("history_complete") or {}
    missing = [k for k, v in hc.items() if not v]
    if missing:
        lines.append(f"Historia 5 lat jeszcze niekompletna dla: {', '.join(missing)} (uzupełni się w kolejnych przebiegach)")
    for msg in (run.get("log") or [])[:15]:
        lines.append(f"- {msg}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=None, help="limit czasu pobierania (min)")
    ap.add_argument("--only", default="", help="lista kodów krajów, np. PL,DE")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-eurostat", action="store_true")
    ap.add_argument("--skip-weather", action="store_true")
    ap.add_argument("--afternoon", action="store_true",
                    help="przebieg popołudniowy: tylko ceny day-ahead na jutro (bez produkcji, pogody, prognoz i Eurostatu)")
    args = ap.parse_args(argv)
    settings = store.load_settings()
    if args.afternoon:
        args.skip_fetch = args.skip_weather = args.skip_eurostat = True
    t0 = time.time()
    run = {"requests": 0, "log": []}
    only = [x.strip().upper() for x in args.only.split(",") if x.strip()] or None
    if args.afternoon:
        run = update.update_prices_ahead(settings, args.budget)
    elif not args.skip_fetch:
        run = update.update_all(settings, args.budget, only)
    if not args.skip_fetch and any(c.get("source") == "elexon" for c in store.load_countries()):
        if not only or "GB" in only:
            try:
                gb = elexon.update(settings, run.setdefault("log", []), settings.get("elexon_budget_minutes", 40))
                run.setdefault("history_complete", {})["GB"] = gb["history_complete"]
            except Exception as e:
                run.setdefault("log", []).append(f"GB: błąd Elexon: {type(e).__name__}: {e}")
    if settings.get("weather_forecast", True) and not args.skip_weather:
        countries = [c for c in store.load_countries() if store.has_daily(c)]
        frames = {c["code"]: store.read_daily(c["code"]) for c in countries}
        wlog = run.setdefault("log", [])
        fcs = weather.update(settings, frames, trends.derive, countries, wlog)
        run["forecasts"] = sorted(fcs)
    es_df, es_err = (None, None) if args.skip_eurostat else eurostat.update(settings["history_years"])
    if args.skip_eurostat:
        import pandas as pd
        p = eurostat.OUT
        es_df = pd.read_csv(p) if p.exists() else None
    if es_err:
        run.setdefault("log", []).append(es_err)
    run["minutes"] = (time.time() - t0) / 60
    payload = build.build(settings, es_df, {"requests": run.get("requests", 0), "by_endpoint": run.get("by_endpoint", {}),
                                            "log": run.get("log", [])[:30],
                                            "kind": "popołudniowy" if args.afternoon else "poranny"})
    out = build.render(payload)
    md = summary_md(payload, run)
    # przebieg popołudniowy (tylko ceny) nie nadpisuje diagnostyki nocnego
    name = "podsumowanie_popoludnie.md" if args.afternoon else "podsumowanie.md"
    (store.DATA / name).write_text(md, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    print(f"Dashboard: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
