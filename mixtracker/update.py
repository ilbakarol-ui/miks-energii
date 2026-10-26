"""Aktualizacja danych z Energy-Charts.

1) Ostatnie dni dla wszystkich krajów: produkcja + ceny stref + prognozy.
2) W ramach limitu czasu: historia wstecz do 5 lat (produkcja i ceny razem,
   bo ceny ważone produkcją wymagają danych z tego samego okresu).

Przerwany backfill jest kontynuowany przy następnym przebiegu. Dni, które mają
produkcję, ale nie mają cen (np. dane z wersji trackera bez cen), są pobierane
ponownie w ramach backfillu.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import forecast, prices, store
from .energycharts import Client, FetchError, parse_public_power, to_daily, unknown_names


class Budget:
    def __init__(self, minutes: float, per_request: float):
        self.end = time.monotonic() + minutes * 60
        self.per_request = per_request

    def can_request(self) -> bool:
        return time.monotonic() + self.per_request < self.end


def _today(tz: str) -> dt.date:
    return dt.datetime.now(ZoneInfo(tz)).date()


def chunks(start: dt.date, end: dt.date, days: int):
    cur = start
    while cur <= end:
        stop = min(end, cur + dt.timedelta(days=days - 1))
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def _fetch_power(client, c, start, end, settings, budget, log):
    """Zwraca listę (start, end, ramka MW, nazwy) - przy błędzie dzieli zakres na połowy."""
    if not budget.can_request():
        return [], False
    try:
        payload = client.public_power(c["ec"], start, end)
    except FetchError as e:
        span = (end - start).days + 1
        if span > settings["min_chunk_days"]:
            mid = start + dt.timedelta(days=span // 2)
            a, oka = _fetch_power(client, c, start, mid - dt.timedelta(days=1), settings, budget, log)
            b, okb = _fetch_power(client, c, mid, end, settings, budget, log)
            return a + b, oka and okb
        log.append(f"{c['code']}: błąd pobierania produkcji {start}..{end}: {e}")
        return [], False
    mw, names = parse_public_power(payload)
    return [(start, end, mw, names)], True


def _fetch_prices(client, zones, start, end, budget, log, code):
    """{strefa: seria cen} dla zakresu; brak danych -> pusta seria."""
    out = {}
    for z in zones:
        if not budget.can_request():
            return out, False
        try:
            out[z["zone"]] = prices.parse_price(client.price(z["zone"], start, end))
        except FetchError as e:
            log.append(f"{code}: błąd pobierania cen {z['zone']} {start}..{end}: {e}")
            return out, False
    return out, True


def fetch_range(client: Client, c: dict, zones: list[dict], start: dt.date, end: dt.date, settings: dict,
                budget: Budget, log: list, price_ahead: int = 0) -> tuple[pd.DataFrame, list[str], bool, dict]:
    """Produkcja i ceny dla zakresu. Zwraca (dni, nazwy typów, czy kompletnie, {strefa: ceny dzienne}).

    price_ahead > 0: ceny strefowe pobierane dodatkowo na tyle dni po `end` (dziś, jutro -
    rynek dnia następnego publikuje ceny dzień wcześniej); trafiają tylko do plików stref."""
    parts, ok = _fetch_power(client, c, start, end, settings, budget, log)
    frames, names, zone_daily = [], [], {}
    for a, b, mw, nm in parts:
        names += nm
        daily = to_daily(mw, c["tz"], settings["min_day_coverage"])
        if zones:
            pb = b + dt.timedelta(days=price_ahead) if (price_ahead and b == end) else b
            zp, okp = _fetch_prices(client, zones, a, pb, budget, log, c["code"])
            ok = ok and okp
            country_price = prices.combine_zones(list(zp.values()))
            pdaily = prices.daily_prices(country_price, mw, c["tz"])
            if not daily.empty and not pdaily.empty:
                for col in pdaily.columns:
                    daily[col] = pdaily[col].reindex(daily.index)
            for zname, ser in zp.items():
                zd = prices.zone_daily(ser, c["tz"])
                if not zd.empty:
                    zone_daily[zname] = pd.concat([zone_daily.get(zname, pd.DataFrame()), zd])
        if not daily.empty:
            daily = daily[(daily.index >= pd.Timestamp(a)) & (daily.index <= pd.Timestamp(b))]
            frames.append(daily)
    df = pd.concat(frames) if frames else pd.DataFrame()
    return df, names, ok, zone_daily


def _complete_from(df: pd.DataFrame, has_zones: bool, price_floor: str | None) -> dt.date | None:
    """Pierwszy dzień nieprzerwanego (od końca) bloku kompletnych dni."""
    if df.empty:
        return None
    have = df["cov"].fillna(0) > 0
    if has_zones:
        pf = pd.Timestamp(price_floor) if price_floor else None
        priced = df["price_avg"].notna()
        if pf is not None:
            priced = priced | (df.index < pf)
        have = have & priced
    if not have.any():
        return None
    arr = have.to_numpy()
    last = len(arr) - 1 - int(np.argmax(arr[::-1]))
    i = last
    while i > 0 and arr[i - 1]:
        i -= 1
    return df.index[i].date()


def bad_ranges(df: pd.DataFrame, zones: list[dict], st: dict, target: dt.date, until: dt.date,
               settings: dict) -> list[tuple[dt.date, dt.date]]:
    """Zakresy dni z brakującymi lub niepełnymi danymi (produkcja albo ceny)."""
    start = target
    if st.get("floor"):
        start = max(start, dt.date.fromisoformat(st["floor"]))
    if df.empty or start > until:
        return []
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(until), freq="D")
    d = df.reindex(idx)
    bad = d["cov"].fillna(0) < settings["min_day_coverage"]
    if zones:
        pf = pd.Timestamp(st["price_floor"]) if st.get("price_floor") else None
        nop = d["price_avg"].isna()
        if pf is not None:
            nop = nop & (d.index >= pf)
        bad = bad | nop
    days = list(d.index[bad.to_numpy()])
    out, cur = [], None
    max_len = settings["backfill_chunk_days"]
    for t in days:
        t = t.date()
        if cur and (t - cur[1]).days <= 4 and (t - cur[0]).days < max_len:
            cur[1] = t
        else:
            if cur:
                out.append(tuple(cur))
            cur = [t, t]
    if cur:
        out.append(tuple(cur))
    return out


def _cursor(st: dict, df: pd.DataFrame, zones: list[dict], c: dict) -> dt.date:
    """Data, od której historia została już przetworzona (backfill idzie wstecz od niej).
    Przy pierwszym użyciu albo po zmianie listy stref cenowych - wyliczana z danych."""
    sig = ",".join(sorted(z["zone"] for z in zones))
    if st.get("cursor") and st.get("zones_sig", "") == sig:
        return dt.date.fromisoformat(st["cursor"])
    ec = _complete_from(df, bool(zones), st.get("price_floor")) or _today(c["tz"])
    st["cursor"] = ec.isoformat()
    st["zones_sig"] = sig
    return ec


def update_all(settings: dict, budget_minutes: float | None = None, only: list[str] | None = None,
               client: Client | None = None, with_forecasts: bool = True) -> dict:
    countries = [c for c in store.load_countries() if c["ec"]]
    if only:
        countries = [c for c in countries if c["code"] in only]
    client = client or Client(base_url=settings["energycharts_base_url"],
                              interval=settings["request_interval_seconds"],
                              timeout=settings["request_timeout_seconds"],
                              max_retries=settings["max_retries"])
    budget = Budget(budget_minutes or settings["budget_minutes"], client.interval)
    zones_by_country = prices.load_zones(settings.get("price_licenses", ["cc_by"]))
    state = store.read_state()
    log: list[str] = []
    data = {c["code"]: store.read_daily(c["code"]) for c in countries}
    zone_data: dict[str, pd.DataFrame] = {}

    def save(c, new, names, zd):
        code = c["code"]
        data[code] = store.merge_daily(data[code], new)
        store.write_daily(code, data[code])
        st = state.setdefault(code, {})
        if names:
            st["names"] = sorted(set(st.get("names", [])) | set(names))
            st["unknown"] = unknown_names(st["names"])
        for z, df in zd.items():
            if z not in zone_data:
                zone_data[z] = store.read_zone(z)
            old = zone_data[z]
            merged = pd.concat([old.drop(index=df.index.intersection(old.index), errors="ignore"), df])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            zone_data[z] = merged
            store.write_zone(z, merged)

    # 1) ostatnie dni (z korektami wstecz) + prognozy
    weekly = dt.date.today().weekday() == settings["weekly_refetch_weekday"]
    back = settings["refetch_days_weekly"] if weekly else settings["refetch_days"]
    forecasts = {}
    for c in countries:
        code = c["code"]
        df = data[code]
        yesterday = _today(c["tz"]) - dt.timedelta(days=1)
        good = df.index[df["cov"] >= settings["min_day_coverage"]] if not df.empty else []
        last = good.max().date() if len(good) else None
        start = (last - dt.timedelta(days=back)) if last else yesterday - dt.timedelta(days=35)
        st = state.setdefault(code, {})
        zones = zones_by_country.get(code, [])
        for a, b in chunks(start, yesterday, settings["backfill_chunk_days"]):
            new, names, ok, zd = fetch_range(client, c, zones, a, b, settings, budget, log,
                                             price_ahead=2 if b == yesterday else 0)
            save(c, new, names, zd)
            if ok:
                st["last_ok"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
                st.pop("last_error", None)
            elif budget.can_request():
                st["last_error"] = log[-1] if log else "błąd"
        if with_forecasts and settings.get("forecasts_energycharts", False) and code != "EU":
            try:
                fc = forecast.update_country(client, c, state, log, budget.can_request)
                if fc:
                    forecasts[code] = fc
            except Exception as e:  # prognozy są dodatkiem - błąd nie zatrzymuje przebiegu
                log.append(f"{code}: błąd prognoz: {type(e).__name__}: {e}")
        store.write_state(state)

    # 2) historia wstecz (round-robin, aż do wyczerpania limitu czasu)
    target = dt.date(dt.date.today().year - settings["history_years"], 1, 1)
    pending = list(countries)
    while pending and budget.can_request():
        nxt = []
        for c in pending:
            if not budget.can_request():
                break
            code = c["code"]
            st = state.setdefault(code, {})
            zones = zones_by_country.get(code, [])
            df = data[code]
            earliest = _cursor(st, df, zones, c)
            floor = dt.date.fromisoformat(st["floor"]) if st.get("floor") else None
            if earliest <= target or (floor and earliest <= floor):
                continue
            b = earliest - dt.timedelta(days=1)
            a = max(target, b - dt.timedelta(days=settings["backfill_chunk_days"] - 1))
            if floor and a < floor:
                a = floor
            new, names, ok, zd = fetch_range(client, c, zones, a, b, settings, budget, log)
            if ok:
                st["cursor"] = a.isoformat()
            if ok and (new.empty or new["cov"].max() == 0):
                st["floor"] = earliest.isoformat()   # źródło nie ma starszych danych
                log.append(f"{code}: brak danych produkcji przed {earliest}")
                continue
            if ok and zones and (new.empty or new["price_avg"].isna().all()):
                st["price_floor"] = (b + dt.timedelta(days=1)).isoformat()
                log.append(f"{code}: brak cen przed {st['price_floor']}")
            if not new.empty:
                save(c, new, names, zd)
            if ok:
                nxt.append(c)
        store.write_state(state)
        pending = nxt

    # 3) naprawa luk w historii: dni bez danych lub z niepełnym pokryciem (np. po
    #    zmianie rozdzielczości w źródle) - każdy zakres próbowany najwyżej raz na 7 dni
    today = dt.date.today()
    repaired = 0
    for c in countries:
        if not budget.can_request():
            break
        code = c["code"]
        st = state.setdefault(code, {})
        zones = zones_by_country.get(code, [])
        tried = {k: v for k, v in st.get("repair", {}).items()
                 if (today - dt.date.fromisoformat(v)).days < 30}
        until = _today(c["tz"]) - dt.timedelta(days=settings["refetch_days"] + 1)
        for a, b in bad_ranges(data[code], zones, st, target, until, settings):
            key = f"{a}_{b}"
            if key in tried and (today - dt.date.fromisoformat(tried[key])).days < 7:
                continue
            if not budget.can_request():
                break
            new, names, ok, zd = fetch_range(client, c, zones, a, b, settings, budget, log)
            if ok:
                tried[key] = today.isoformat()
                repaired += 1
            if not new.empty:
                save(c, new, names, zd)
        st["repair"] = tried
        store.write_state(state)

    done = {}
    for c in countries:
        code = c["code"]
        st = state.get(code, {})
        zones = zones_by_country.get(code, [])
        ec = _cursor(st, data[code], zones, c) if data[code].shape[0] else None
        floor = dt.date.fromisoformat(st["floor"]) if st.get("floor") else None
        complete = bool(ec) and (ec <= target or bool(floor and ec <= floor))
        done[code] = complete
        st["history_complete"] = complete
    store.write_state(state)
    return {"requests": client.requests_made, "by_endpoint": dict(getattr(client, "by_endpoint", {})),
            "log": log, "history_complete": done, "forecasts": sorted(forecasts)}


def _ahead_hours(ser, tz: str, day: dt.date) -> tuple[float, int | None]:
    """B-20261026-1: ile godzin ceny przyszło dla doby `day` (czas lokalny strefy) i jaki krok czasowy (min)."""
    if ser is None or len(ser.dropna()) == 0:
        return 0.0, None
    loc = ser.dropna().tz_convert(tz)
    step = int(pd.Series(loc.index).diff().median().total_seconds() // 60) if len(loc) > 1 else 60
    return round(float((loc.index.date == day).sum()) * step / 60, 2), step


def update_prices_ahead(settings: dict, budget_minutes: float | None = None, client: Client | None = None,
                        days_ahead: int = 1, retries: int = 2, retry_wait: float = 600) -> dict:
    """Przebieg popołudniowy: tylko ceny day-ahead stref (dziś i jutro - aukcja ok. 12:45 publikuje ceny
    na następny dzień). Bez produkcji, pogody i prognoz; jedno zapytanie na strefę."""
    client = client or Client(base_url=settings["energycharts_base_url"],
                              interval=settings["request_interval_seconds"],
                              timeout=settings["request_timeout_seconds"],
                              max_retries=settings["max_retries"])
    budget = Budget(budget_minutes or 20, client.interval)
    tz_of = {c["code"]: c["tz"] for c in store.load_countries()}
    zones: dict[str, str] = {}
    for code, zl in prices.load_zones(settings.get("price_licenses", ["cc_by"])).items():
        for z in zl:
            zones.setdefault(z["zone"], tz_of.get(code, "Europe/Brussels"))
    log: list[str] = []
    done = []
    last_ser: dict = {}
    for zone, tz in sorted(zones.items()):
        if not budget.can_request():
            log.append(f"ceny: limit czasu, pominięto strefy od {zone}")
            break
        today = _today(tz)
        try:
            # B-20261025-1: +1 dzień zapasu – przy dobie 23/25 h (zmiana czasu) koniec zakresu nie ucina jutra
            ser = prices.parse_price(client.price(zone, today, today + dt.timedelta(days=days_ahead + 1)))
        except FetchError as e:
            log.append(f"ceny {zone}: {e}")
            continue
        last_ser[zone] = ser
        zd = prices.zone_daily(ser, tz)
        if zd.empty:
            continue
        _merge_zone(zone, zd)
        done.append(zone)
    # B-20261025-1: strefy bez ceny na jutro (aukcja ok. 12:45, Energy-Charts bywa spóźnione) – 2 ponowienia co 10 min
    want = {z: pd.Timestamp(_today(tz) + dt.timedelta(days=days_ahead)) for z, tz in zones.items()}
    extra = 0
    prev_hrs = None
    for _ in range(retries):
        late = [z for z in zones if _last_price_day(z) < want[z]]
        if not late or not budget.can_request():
            break
        # B-20261026-1: gdy ponowienie nic nie zmieniło (te same godziny jutra), kolejne czekanie nic nie da
        hrs = {z: _ahead_hours(last_ser.get(z), zones[z], want[z].date())[0] for z in late}
        if prev_hrs is not None and hrs == prev_hrs:
            log.append(f"ceny: ponowienie bez zmian dla {', '.join(late)} – koniec ponowień")
            break
        prev_hrs = hrs
        time.sleep(retry_wait)
        for zone in late:
            if not budget.can_request():
                break
            tz = zones[zone]
            try:
                ser = prices.parse_price(client.price(zone, _today(tz), _today(tz) + dt.timedelta(days=days_ahead + 1)))
            except FetchError as e:
                log.append(f"ceny {zone} (ponowienie): {e}")
                continue
            extra += 1
            last_ser[zone] = ser
            zd = prices.zone_daily(ser, tz)
            if not zd.empty:
                _merge_zone(zone, zd)
                if zone not in done:
                    done.append(zone)
    missing = sorted(z for z in zones if _last_price_day(z) < want[z])
    if missing:
        log.append(f"BRAK cen day-ahead na jutro: {', '.join(missing)} ({len(missing)} z {len(zones)} stref; "
                   f"mapa pokaże dla nich ostatni dzień z ceną)")
        # B-20261026-1: ile godzin jutra przyszło (diagnoza: niepełna doba < 18 h, inna rozdzielczość, brak danych)
        det = []
        for z in missing:
            h, step = _ahead_hours(last_ser.get(z), zones[z], want[z].date())
            det.append(f"{z}: {h:g} h na {want[z]:%d.%m} (krok {step if step is not None else '–'} min)")
            if last_ser.get(z) is not None:
                try:
                    (store.PRICES.parent / "debug").mkdir(parents=True, exist_ok=True)
                    last_ser[z].to_csv(store.PRICES.parent / "debug" / f"price_{z}.csv")   # ostatnia odpowiedź, nadpisywana
                except OSError:
                    pass
        log.append("szczegóły braków: " + "; ".join(det))
    return {"requests": len(done) + extra, "log": log, "zones": done, "missing_ahead": missing}


def _merge_zone(zone: str, zd: pd.DataFrame):
    """Dopisuje doby z ceną (doby niepełne – NaN – nie nadpisują zapisanych wartości)."""
    zd = zd.dropna(subset=["price_avg"])
    if zd.empty:
        return
    old = store.read_zone(zone)
    merged = pd.concat([old.drop(index=zd.index.intersection(old.index), errors="ignore"), zd])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    store.write_zone(zone, merged)


def _last_price_day(zone: str) -> pd.Timestamp:
    s = store.read_zone(zone)["price_avg"].dropna()
    return s.index.max() if len(s) else pd.Timestamp("1900-01-01")
