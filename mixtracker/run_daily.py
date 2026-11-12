"""Codzienny przebieg: pobranie danych -> Eurostat -> trendy -> dashboard.

python -m mixtracker.run_daily [--budget 70] [--only PL,DE] [--skip-fetch]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import build, elexon, entsoe, eurostat, sensitivity, shared_outages, storage_cap, store, trends, update, weather


def _pl(v, nd: int = 1, sign: bool = False) -> str:
    """B-20261028-6: liczba z przecinkiem dziesiętnym i spacją tysięcy (jak w tekstach dla użytkowniczki)."""
    s = f"{v:{'+' if sign else ''},.{nd}f}"
    return s.replace(",", " ").replace(".", ",")


def _prices_md(run: dict) -> list[str]:
    """B-20261025-2: przebieg popołudniowy – tabela cen stref (ostatni dzień z ceną) zamiast powtórzonej tabeli tygodnia."""
    import datetime as dt
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    rows = []
    # B-20261028-1: drugi przebieg (--only-missing) zwraca w "zones" tylko uzupełniane strefy – tabela ze wszystkich
    for z in (run.get("zones_all") or sorted(run.get("zones") or [])):
        d = store.read_zone(z).dropna(subset=["price_avg"])
        if len(d):
            last = d.index.max()
            mark = "" if last.strftime("%Y-%m-%d") >= tomorrow else " (brak jutra)"
            rows.append(f"| {z} | {last:%Y-%m-%d}{mark} | {_pl(d.at[last, 'price_avg'])} | {_pl(d.at[last, 'price_min'])} | {_pl(d.at[last, 'price_max'])} |")
    miss = run.get("missing_ahead") or []
    head = (f"Ceny na {tomorrow}: {len(rows) - len(miss)} z {len(rows)} stref" if rows else "Brak danych cen stref")
    if rows and run.get("only_missing") and run.get("zones"):
        head += f" (w tym przebiegu uzupełniono: {', '.join(sorted(run['zones']))})"
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
            return "–" if v is None else f"{_pl(v)}%"
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
                lines.append(f"- {it['name']}: emisyjność {_pl(it['cur'], 0)} g/kWh ({_pl(it['delta'], 1, True)}% r/r, {per})")
            elif it["type"] == "load_yoy":
                lines.append(f"- {it['name']}: zużycie {_pl(it['delta'], 1, True)}% r/r ({per})")
            elif it["type"] == "yoy":
                lines.append(f"- {it['name']}: {it['label']} {_pl(it['cur'])}% ({_pl(it['delta'], 1, True)} p.p. r/r, {per})")
            else:
                word = "powyżej maks." if it["type"] == "max5" else "poniżej min."
                lines.append(f"- {it['name']}: {it['label']} {_pl(it['cur'])}% – {word} z 5 lat ({per})")
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
    lines += _partial_lines(payload)
    for msg in (run.get("log") or [])[:15]:
        lines.append(f"- {msg}")
    return "\n".join(lines) + "\n"


def _partial_lines(payload: dict, min_days: int = 7) -> list[str]:
    """B-20261103-3: kraje, w których co najmniej połowa z 14 ostatnich dób ma niepełne godziny ze źródła."""
    ctl = payload.get("control") or {}
    out = []
    # B-20261109-1: kraje, którym źródło nie dosłało dób (brak wierszy) – np. DK bez danych od 5.11.2026
    miss = sorted(((k, v.get("missing14") or 0, v.get("last")) for k, v in ctl.items() if (v.get("missing14") or 0) >= 2),
                  key=lambda x: -x[1])
    if miss:
        out.append("Brak dób w 14 dniach (źródło nie dosłało danych): "
                   + ", ".join(f"{k} {n} (ostatnia pełna doba {d})" for k, n, d in miss))
    bad = sorted(((k, v.get("partial14") or 0, v.get("below14") or 0) for k, v in ctl.items()
                  if (v.get("partial14") or 0) >= min_days), key=lambda x: -x[1])
    if bad:
        out.append("Niepełne doby w 14 dniach (brakujące godziny uzupełnione proporcjonalnie; poniżej 80% – luka): "
                   + ", ".join(f"{k} {n}" + (f" (w tym {b} {_luk(b)})" if b else "") for k, n, b in bad))
    # B-20261111-1: ostatnie doby z pełnym pokryciem godzin, ale wyraźnie zaniżonymi seriami (dosyłane później)
    ser = [(k, d, cols) for k, v in sorted(ctl.items()) for d, cols in sorted(((v.get("partial_last") or {}).get("series") or {}).items())]
    if ser:
        out.append("Ostatnie doby niepełne mimo pokrycia (do ponownego pobrania): "
                   + ", ".join(f"{k} {d[8:10]}.{d[5:7]} ({', '.join(cols)})" for k, d, cols in ser))
    # B-20261105-2: luki samego zużycia (produkcja pełna); 14 z 14 (np. UK, GB) ma już linię „BRAK od …”
    lg = sorted(((k, v.get("load_gaps14") or 0) for k, v in ctl.items() if 0 < (v.get("load_gaps14") or 0) < 14),
                key=lambda x: -x[1])
    if lg:
        out.append("Zużycie bez danych w 14 dniach (źródło podało < 80% godzin – luka do uzupełnienia): "
                   + ", ".join(f"{k} {n}" for k, n in lg))
    return out


def _luk(n: int) -> str:
    return "luka" if n == 1 else ("luki" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "luk")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=None, help="limit czasu pobierania (min)")
    ap.add_argument("--only", default="", help="lista kodów krajów, np. PL,DE")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-eurostat", action="store_true")
    ap.add_argument("--skip-weather", action="store_true")
    ap.add_argument("--afternoon", action="store_true",
                    help="przebieg popołudniowy: tylko ceny day-ahead na jutro (bez produkcji, pogody, prognoz i Eurostatu)")
    ap.add_argument("--no-retry", action="store_true",
                    help="z --afternoon: bez ponowień brakujących stref (przebieg 12:37 UTC; braki uzupełnia przebieg 14:07, B-20261103-2)")
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
        kw = {"retries": 0} if args.no_retry else {}
        run = update.update_prices_ahead(settings, args.budget, only_missing=args.only_missing, **kw)
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
    # niedostępność elektrowni (ENTSO-E A80) – wspólne dane dla dashboardu „Gaz UE” (data/shared_outages)
    if not args.skip_fetch and not only and settings.get("outages"):
        try:
            shared_outages.update(settings, run.setdefault("log", []))
        except Exception as e:
            run.setdefault("log", []).append(f"Niedostępność elektrowni (ENTSO-E): {type(e).__name__}: {e}"[:300])
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
        try:   # wrażliwość na temperaturę: zużycie gazu z Eurostatu (nrg_cb_gasm) co 30 dni – zakładka „Pogoda”
            sensitivity.update(run.setdefault("log", []))
        except Exception as e:
            run.setdefault("log", []).append(f"Wrażliwość na temperaturę: {type(e).__name__}: {e}")
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
    from . import model as _model   # B-20261031-1: model zużycia bez świąt (brak biblioteki holidays albo błąd kraju)
    if _model.HOLIDAY_ERRORS:
        run.setdefault("log", []).insert(0, "BŁĄD: model zużycia bez świąt – " +
                                         "; ".join(f"{k}: {v}" for k, v in sorted(_model.HOLIDAY_ERRORS.items())))
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
