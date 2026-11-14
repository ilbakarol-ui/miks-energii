"""Kontrola listy punktów Wspólnej pogody (config/shared_weather_points.csv).

Punkty próbne (group = eu_gas_x, pt = EX_…) służą raportowi Gaz UE do sprawdzenia, czy gęstsza siatka miast
poprawia prognozę zużycia gazu. Muszą być „niewidoczne” dla wszystkiego, co już działa:
- tylko uses = temp (bez ens – ensemble_power uśrednia punkty kraju i zmieniłby prognozę energetyki;
  bez seas – SEAS5 to drogie zapytania),
- inna grupa niż eu_gas (zakładka „Pogoda” Miksu bierze tylko group == eu_gas),
- prefiks EX_ zamiast EU_ (raport gazowy uznaje pogodę za nieświeżą, gdy błąd przebiegu wymienia paczkę EU_DE_…, EU_FR_… itd.;
  zaległe dociąganie historii punktów próbnych nie może tego wywołać),
- najwyżej 15 punktów (raport gazowy ostrzega, gdy dawne prognozy są aktualne dla < 80% punktów; 63 / 78 = 81%).
"""
from mixtracker import shared_weather as sw

EU_BOX = (34.0, 72.0, -11.0, 35.0)   # lat min, lat max, lon min, lon max


def _pts():
    return sw.load_points()


def test_ids_and_coordinates_unique():
    p = _pts()
    assert p.pt.is_unique
    # raport gazowy dopasowuje punkty po współrzędnych zaokrąglonych do 2 miejsc
    key = list(zip(p.lat.round(2), p.lon.round(2)))
    assert len(key) == len(set(key))


def test_columns_and_uses_valid():
    p = _pts()
    assert list(p.columns) == ["pt", "group", "cc", "lat", "lon", "tz", "uses"]
    allowed = {"temp", "ens", "seas"}
    assert all(set(u.split(";")) <= allowed for u in p.uses)


def test_trial_points_isolated():
    p = _pts()
    x = p[p.group == "eu_gas_x"]
    assert 0 < len(x) <= 15
    assert (x.uses == "temp").all()
    assert x.pt.str.startswith("EX_").all() and not p[p.group != "eu_gas_x"].pt.str.startswith("EX_").any()
    assert (x.tz == "Europe/Berlin").all()
    assert set(x.cc) <= set(p[p.group == "eu_gas"].cc)
    a, b, c, d = EU_BOX
    assert x.lat.between(a, b).all() and x.lon.between(c, d).all()


def test_existing_groups_unchanged():
    p = _pts()
    assert (p.group == "eu_gas").sum() == 49
    assert (p.group == "asia").sum() == 14
    assert not p[p.group == "eu_gas"].pt.str.startswith("EX_").any()
