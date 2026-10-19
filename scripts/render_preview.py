"""Buduje podgląd dashboardu (fragment HTML bez szkieletu <html>, do publikacji jako artefakt)
z danych zapisanych w repozytorium i wypisuje krótkie podsumowanie dnia.

python scripts/render_preview.py <folder_wyjściowy>
Wynik: <folder>/miks-energii.html oraz <folder>/brief.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mixtracker import build, eurostat, store  # noqa: E402


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    settings = store.load_settings()
    es = pd.read_csv(eurostat.OUT) if eurostat.OUT.exists() else None
    payload = build.build(settings, es, {"requests": 0, "log": []})
    tmp = out / "_render"
    build.render(payload, tmp, fragment=True)
    (out / "miks-energii.html").write_text((tmp / "index.html").read_text(encoding="utf-8"), encoding="utf-8")

    m = payload["meta"]
    brief = {"dane_do": m["ref"], "wygenerowano": m["generated"], "tydzien": m["periods"]["week"]["label"]}
    eu = payload["summary"].get("EU", {}).get("week", {})
    for k in ("load", "gen_total", "share_renewable", "intensity"):
        if k in eu:
            brief["UE_" + k] = {"biezacy": eu[k][0], "poprz_tydzien": eu[k][1], "rok_temu": eu[k][2], "sr_5_lat": eu[k][3]}
    brief["najwieksze_zmiany"] = payload.get("movers", [])[:5]
    pm = payload.get("price_map") or {}
    if pm.get("dates"):
        i = len(pm["dates"]) - 1
        vals = {c: v["avg"][i] for c, v in pm["countries"].items() if v.get("avg") and v["avg"][i] is not None}
        brief["ceny_dzien"] = pm["dates"][i]
        brief["ceny_eur_mwh"] = dict(sorted(vals.items(), key=lambda kv: -kv[1]))
    wt = payload.get("wind_tab") or {}
    if wt.get("countries"):
        def w(code):
            c = wt["countries"].get(code)
            if not c:
                return None
            tot = sum(v for v in c["wind"] if v is not None)
            avg = sum(v for v in (c.get("avg") or []) if v is not None)
            return {"dni": c["days"], "gwh": c["wind"], "vs_norma_proc": c.get("anom"),
                    "7_dni_vs_norma_proc": round(100 * (tot / avg - 1), 1) if avg else None}
        brief["wiatr_UE"] = w("EU")
        brief["wiatr_PL"] = w("PL")
        brief["wiatr_prognoza_wydana"] = wt.get("issued")
    gb = payload.get("control", {}).get("GB", {})
    brief["kontrola"] = {k: v for k, v in payload.get("control", {}).items() if v.get("error") or v.get("break")}
    brief["GB_ostatni_dzien"] = gb.get("last")
    (out / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(brief, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview"))
