"""Codzienny przebieg: pobranie danych -> Eurostat -> trendy -> dashboard.

python -m mixtracker.run_daily [--budget 70] [--only PL,DE] [--skip-fetch]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import build, elexon, entsoe, eurostat, storage_cap, store, trends, update, weather


def _prices_md(run: dict) -> list[str]:
    """B-20261025-2: przebieg popołudniowy – tabela cen stref (ostatni dzień z ceną) zamiast powtórzonej tabeli tygodnia."""
    import datetime as dt
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    rows = []
    for z in sorted(run.get("zones") or []):
        d = store.read_zone(z).dropna(subset=["price_avg"])
        if len(d):
            last = d.index.max()
            mark = "" if last.strftime("%Y-%m-%d") >= tomorrow else " (brak jutra)"
            rows.append(f"| {z} | {last:%Y-%m-%d}{mark} | {d.at[last, 'price_avg']:.1f} | {d.at[last, 'price_min']:.1f} | {d.at[last, 'price_max']:.1f} |")
    miss = run.get("missing_ahead") or []
    head = (f"Ceny na {tomorrow}: {len(rows) - len(miss)} z {len(rows)} stref" if rows else "Brak danych cen stref")
    return [f"## Ceny day-ahead – przebieg popołudniowy", "", head, "",
            "| strefa | dzień | śr. €/MWh | min | maks |", "|---|---|---|---|---|", *rows, ""]


def summary_md(payload: dict, run: dict) -> str:
    m = payload["meta"]
    if run.get("zones") is not None:          # tylko przebieg popołudniowy (update_prices_ahead) zwraca "zones"
        lines = _prices_md(run)
        lines += [f"Dane produkcji do {m['ref']}; zapytania do Energy-Charts: {run.get('requests', 0)}; czas: {run.get('minutes', 0):.0f} min"]
        lines += [f"- {msg}" for msg in (run.get("log") or [])[:15]]
        return "\n".join(lines) + "\n"
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
            # B-20261026-2: nazwa okresu („tydz. 43”, „wrz 2026”) zamiast „mies.”, żeby wrzesień nie wyglądał na stan bieżący
            per = ((m.get("periods") or {}).get(it["period"]) or {}).get("short") or ("tydz." if it["period"] == "week" else "mies.")
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
    ap.add_argument("--only-missing", action="store_true",
                    help="z --afternoon: tylko strefy bez ceny na jutro (drugi przebieg popołudniowy, B-20261027-1)")
    args = ap.parse_args(argv)
    settings = store.load_settings()
    if args.afternoon:
        args.skip_fetch = args.skip_weather = args.skip_eurostat = True
    t0 = time.time()
    run = {"requests": 0, "log": []}
    only = [x.strip().upper() for x in args.only.split(",") if x.strip()] or None
    if args.afternoon:
        run = update.update_prices_ahead(settings, args.budget, only_missing=args.only_missing)
    elif not args.skip_fetch:
        run = update.update_all(settings, args.budget, only)
    # B-20261027-1: przy komplecie cen drugi przebieg nie nadpisuje podsumowania z 12:37 (strona i tak jest budowana – Pages)
    nothing_to_do = bool(args.only_missing and run.get("requests", 0) == 0 and not run.get("missing_ahead"))
    if not args.skip_fetch and any(c.get("source") == "elexon" for c in store.load_countries()):
        if not only or "GB" in only:
            try:
                gb = elexon.update(settings, run.setdefault("log", []), settings.get("elexon_budget_minutes", 40))
                run.setdefault("history_complete", {})["GB"] = gb["history_complete"]
            except Exception as e:
                run.setdefault("log", []).append(f"GB: błąd Elexon: {type(e).__name__}: {e}")
    # U-20261027: ENTSO-E zamiast urwanych/zaniżonych serii Energy-Charts (zużycie UK/NI, NL) – po pobraniu z EC,
    # bo nocny przebieg nadpisuje ostatnie 10/45 dni; bez sekretu ENTSOE_TOKEN tylko linia w logu
    if not args.skip_fetch and settings.get("entsoe"):
        conf = {k: v for k, v in settings["entsoe"].items() if not only or k in only}
        try:
            entsoe.update({**settings, "entsoe": conf}, run.setdefault("log", []), settings.get("entsoe_budget_minutes", 15))
        except Exception as e:
            run.setdefault("log", []).append(f"ENTSO-E: {type(e).__name__}: {e}")
    if settings.get("weather_forecast", True) and not args.skip_weather:
        countries = [c for c in store.load_countries() if store.has_daily(c)]
        frames = {c["code"]: store.read_daily(c["code"]) for c in countries}
        wlog = run.setdefault("log", [])
        fcs = weather.update(settings, frames, trends.derive, countries, wlog)
        run["forecasts"] = sorted(fcs)
    if not args.afternoon and not args.skip_fetch:
        try:   # moc zainstalowana magazynów: Eurostat co 7 dni, Energy-Charts co 30 dni (po 6 krajów na przebieg)
            storage_cap.update(settings, run.setdefault("log", []))
        except Exception as e:
            run.setdefault("log", []).append(f"Magazyny: {type(e).__name__}: {e}")
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
    if nothing_to_do:
        print("\n".join(run.get("log", [])))
        return
    (store.DATA / name).write_text(md, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    print(f"Dashboard: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
