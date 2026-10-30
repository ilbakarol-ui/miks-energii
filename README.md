# Miks energii w Europie

Codziennie aktualizowany dashboard: produkcja energii elektrycznej według źródeł, zużycie, ceny day-ahead, emisyjność i prognozy na 7 dni w krajach UE-27 oraz w Norwegii, Szwajcarii i Wielkiej Brytanii (osobno także Irlandia Północna). Pokazuje trendy tydzień do tygodnia, miesiąc do miesiąca, rok do roku i na tle średniej oraz zakresu z ostatnich 5 lat.

Działa w chmurze GitHub (GitHub Actions), raz dziennie, bez niczego na Twoim komputerze i bez kluczy API. Gotowa strona jest publikowana pod stałym adresem na GitHub Pages.

## Co jest na dashboardzie

- **Wybór kraju i okresu**: ostatni pełny tydzień (pon.–niedz.), ostatni pełny miesiąc albo rok do daty.
- **Wskaźniki**: zużycie, produkcja krajowa, udział OZE, saldo wymiany, cena day-ahead i emisyjność (g CO₂/kWh). Przy każdym: zmiana t/t lub m/m, r/r, względem średniej 5 lat i pasek z zakresem min–maks z 5 lat.
- **Miks produkcji**: udział 8 grup źródeł (gaz, jądrowa, węgiel, biomasa i inne OZE, ropa/odpady/inne, woda, wiatr, słońce), zmiana r/r w p.p. i pozycja względem 5 lat. Przełącznik **Szczegóły** rozbija grupy, np. węgiel brunatny i kamienny, wiatr na lądzie i na morzu.
- **Trend tygodniowy na tle 5 lat**: bieżący rok, poprzedni rok, średnia i pasmo min–maks. Do wyboru zużycie, produkcja, saldo, udział OZE, udział paliw kopalnych, każde źródło osobno, emisyjność, emisje CO₂, cena, ceny ważone produkcją słońca i wiatru oraz godziny z ujemną ceną.
- **Zakładka „Prognozy wiatru”** (przełącznik u góry strony):
  - wybór dnia z 7 dni prognozy; kafelki: produkcja z wiatru w wybranym dniu i w 7 dniach na tle normy z 5 lat oraz szczyt godzinowy,
  - mapa Europy: prognozowana produkcja z wiatru jako odchylenie od normy (te same dni kalendarza ±3 dni w ostatnich 5 latach), z rankingiem krajów,
  - wykres godzinowy (MW, czas polski) dla wybranego kraju oraz dzienny: ostatnie 14 dni rzeczywistej produkcji, prognoza i pasmo z 5 lat,
  - tabela wszystkich krajów × 7 dni, kolor = odchylenie od normy,
  - trafność: błąd archiwalnych prognoz względem rzeczywistej produkcji wg wyprzedzenia (ten sam dzień, 1 dzień, … 6 dni), liczony z prognoz z ostatnich 60 dni; pojawia się po zebraniu co najmniej 3 porównań.
  - Dane pogodowe: prędkość wiatru na 100 m z Open-Meteo, przeliczana przez krzywą mocy turbiny i model dopasowany do produkcji kraju z ostatnich 120 dni. Rozkład godzinowy to model dzienny zastosowany do każdej godziny (suma doby = prognoza dzienna). Przy normie poniżej 1 GWh/d (np. Słowenia, Szwajcaria) odchylenie procentowe nie jest pokazywane.
- **Zakładka „Pogoda”** (link bezpośredni: `#pogoda`) – najdłuższe dostępne prognozy wiatru i temperatury dla wybranego kraju i UE-27 (kraje UE; Norwegia, Szwajcaria i Wielka Brytania tylko z produkcją rzeczywistą):
  - kafelki: wiatr w najbliższych 7 dniach (% normy) i w dniach 8–12, temperatura w dniach 1–7 i 8–14 względem normy, najbliższy miesiąc i zima z SEAS5,
  - wiatr (GWh/dobę): 14 dni produkcji rzeczywistej, model 7-dniowy z zakładki „Prognozy wiatru”, mediana i pasmo 80% z 51 wariantów ECMWF ensemble (ok. 12 dni) i norma z 5 lat; prędkość wiatru wariantów jest przeliczana na produkcję zależnością liniową dopasowaną do ostatnich (do 120) dni z historii ensemble – kraj dostaje pasmo dopiero przy co najmniej 14 dniach historii i R² ≥ 0,6 (dopasowanie poprawia się z każdym dniem; dla UE prędkość to średnia krajów ważona produkcją z wiatru z 365 dni),
  - temperatura (°C): 14 dni rzeczywistych, prognoza ECMWF IFS (ok. 14 dni), pasmo 80% wariantów ensemble i norma dzienna z ostatnich 7 pełnych lat (ERA5, wygładzona ±15 dni); UE = średnia punktów sieci gazowej (więcej punktów w krajach o większym zużyciu gazu),
  - prognoza sezonowa ECMWF SEAS5 na 6 miesięcy: mediana i zakres 80% z 51 wariantów jako odchylenie średniej miesięcznej od normy tych samych punktów (surowy model, bez korekty błędu systematycznego),
  - tabela wszystkich krajów z odchyleniami od normy.
  - Dane: wspólna baza pogodowa `data/shared_weather` (workflow „Wspólna pogoda dla dashboardów”, nocą i ok. 18 UTC); zakładka niczego nie pobiera. Kod `mixtracker/weather_tab.py`, ustawienia opcjonalne w `"weather_tab"` (`calib_days`, `min_calib_days`, `min_r2`, `norm_years`, `seas_months`). Błąd zakładki nie zatrzymuje budowy strony.
- **Zakładka „Weryfikacja prognoz”** (link bezpośredni: `#weryfikacja`), osobno dla wiatru, słońca i zużycia:
  - kafelki dla wybranego kraju: błąd modelu (WAPE), odchylenie systematyczne i umiejętność względem dwóch metod odniesienia,
  - wykres: rzeczywistość, model i klimatologia z ostatnich 60 dni oraz błąd w kolejnych tygodniach,
  - tabela trafności wg wyprzedzenia (0–6 dni): test wsteczny obok prognoz faktycznie wydanych (błąd, odchylenie, błąd metody „jak wczoraj”, umiejętność),
  - tabela wszystkich krajów (UE-27 jako suma krajów) z kolorem umiejętności i opisem metody na dole strony.
  - Metoda: **test wsteczny** (ostatnie 90 dni; model dopasowany tylko do danych sprzed danego dnia, ponowne dopasowanie co 7 dni, pogoda rzeczywista z ERA5/analizy Open-Meteo) mierzy błąd samego modelu; **weryfikacja operacyjna** rozlicza prognozy zapisane w `data/forecast/archive.csv` w chwili wydania, więc obejmuje też błąd prognozy pogody. Miary: WAPE = Σ|prognoza − rzeczywistość| / Σ rzeczywistość; odchylenie = Σ prognoza / Σ rzeczywistość − 1; umiejętność = 1 − błąd modelu / błąd metody odniesienia. Metody odniesienia: persystencja (wartość z dnia przed wydaniem) i klimatologia (te same dni kalendarza ±3 dni z 5 lat).
  - Ustawienia: `"verify": false` wyłącza weryfikację, `"verify_days"` (domyślnie 90) zmienia długość testu, `"verify_archive_from"` (ustawione na 2026-10-20, pierwszy dzień prognoz z nowym modelem) to pierwszy dzień archiwum branego pod uwagę. Weryfikacja wydłuża przebieg o ok. 1,5 minuty.
- **Magazyny energii: moc zainstalowana** (sekcja w zakładce „Miks i ceny”, pod danymi Eurostatu): dla wybranego kraju moc elektrowni szczytowo-pompowych i magazynów bateryjnych rok po roku (od 2021 r., rok bieżący z gwiazdką), pojemność baterii, średni czas rozładowania i przyrost; tabela wszystkich krajów z mocą baterii z 5 ostatnich lat.
  - Źródła: elektrownie szczytowo-pompowe – Energy-Charts `/installed_power` (dane ENTSO-E, Szwajcaria – BFE); baterie – Eurostat `nrg_inf_bt` (moc i pojemność, kompletne od 2022 r., publikacja z ok. 10-miesięcznym opóźnieniem), dla Niemiec rejestr MaStR przez Energy-Charts (z magazynami domowymi – Eurostat podaje tylko część magazynów sieciowych); UE-27 = Eurostat z Niemcami wg MaStR. Brak danych dla Wielkiej Brytanii.
  - Odświeżanie w przebiegu nocnym: Eurostat co 7 dni, Energy-Charts co 30 dni (najwyżej 6 krajów na przebieg). Pliki: `data/storage/`, kod `mixtracker/storage_cap.py`.
- **Mapa cen day-ahead**: Europa pokolorowana średnią ceną rynku dnia następnego, z wyborem dnia (ostatnie 30 dni, także dziś i jutro, jeśli ceny są już opublikowane) albo średnią z 7 lub 30 dni. Obok ranking krajów. Po najechaniu widać strefy, min–maks i godziny z ujemną ceną; kliknięcie wybiera kraj. Skala barw jest stała (te same progi każdego dnia), więc kolory można porównywać między dniami. Szare kraje nie mają cen na wolnej licencji.
- **Ceny energii**: średnia dzienna z 90 dni z zakresem min–maks w ciągu dnia oraz tabela cen ważonych produkcją (słońce, wiatr, zużycie) z relacją do ceny średniej i liczbą godzin z ceną poniżej zera, r/r i względem 5 lat.
- **Prognoza na 7 dni**: wiatr, słońce i zużycie z modelu pogodowego, z ostatnimi 14 dniami rzeczywistymi i błędem modelu, na tle średniej i zakresu z tych samych dni kalendarza w ostatnich 5 latach, z podsumowaniem „następne 7 dni: X GWh, +Y% względem średniej”.
- **Produkcja dzienna** (90 dni) i **miks miesięczny** (25 miesięcy, w GWh albo %), z linią zużycia.
- **Porównanie krajów**: tabela z sortowaniem, z ceną i emisyjnością. Udziały poza zakresem z 5 lat są oznaczone ▲/▼.
- **Największe zmiany**: automatyczna lista, np. udział wiatru +10 p.p. r/r albo udział gazu najwyższy od 5 lat.
- **Dane oficjalne Eurostatu**: najnowszy opublikowany miesiąc (zwykle sprzed 2–3 miesięcy), r/r i względem średniej 5 lat, porównanie z danymi dziennymi. Tylko tutaj jest Malta, bo nie ma dla niej danych dziennych.
- **Kontrola danych**: ostatni dzień z danymi dla każdego kraju, luki, źródło zużycia, ceny, prognozy, nierozpoznane typy produkcji i wykryte nagłe skoki w danych źródła (np. zmiana sposobu raportowania).

## Uruchomienie (jednorazowo, ok. 15 minut)

1. **Repozytorium**: na GitHubie kliknij **New repository**, podaj nazwę (np. `miks-energii`) i wybierz **Public**, potem **Create**.
   - Publiczne repozytorium ma darmowe, nielimitowane minuty Actions i darmowe GitHub Pages. Dane są publiczne (Energy-Charts, Eurostat), więc nic poufnego nie wycieka.
   - Prywatne repozytorium też zadziała, ale: GitHub Pages wymaga wtedy płatnego planu, a przebiegi (ok. 30 min dziennie, ok. 900 min miesięcznie) dzielą limit 2000 minut z trackerem LNG, który zużywa ok. 1500. Razem przekroczą limit.
2. **Wgranie plików**: wybierz **uploading an existing file**, przeciągnij całą zawartość rozpakowanej paczki i wybierz **Commit changes**.
   - Folder `.github` jest ukryty i często się nie przenosi. Sprawdź, czy w repozytorium jest `.github/workflows/daily.yml`. Jeśli nie: **Add file → Create new file**, nazwa `.github/workflows/daily.yml`, treść z pliku `workflow_daily.yml` z paczki.
3. **Uprawnienia**: **Settings → Actions → General → Workflow permissions** → **Read and write permissions** → **Save**.
4. **Strona**: **Settings → Pages → Build and deployment → Source** → **GitHub Actions**.
5. **Pierwsze uruchomienie**: zakładka **Actions** → **Miks energii - aktualizacja dzienna** → **Run workflow**, zostaw domyślny limit czasu **240**. Pobranie 5 lat historii produkcji i cen dla 30 krajów oraz sprawdzenie, które kraje mają prognozy, trwa ok. 2,5 godziny, bo źródło pozwala na 2 zapytania na minutę.
6. **Adres dashboardu**: po zakończeniu przebiegu jest w **Settings → Pages** (np. `https://<twoja-nazwa>.github.io/miks-energii/`). Ten sam adres działa na telefonie. Kraj można podać w adresie, np. `…/#pl`.

Następne przebiegi startują same codziennie (także w weekendy) o 00:53 UTC (02:53 latem, 01:53 zimą w Polsce) i trwają ok. 20–40 minut (w poniedziałki dłużej – ponowne pobranie 45 dni). Wczesny start zostawia zapas na opóźnienia startów planowych GitHub (bywają do ok. 3 godzin), tak żeby dane były gotowe przed odświeżeniem podglądu o 5:40.

**Drugi przebieg po południu** (codziennie, 12:37 UTC, czyli 14:37 latem i 13:37 zimą; commit „Dane z RRRR-MM-DD (popołudnie)”): pobiera tylko ceny day-ahead na jutro (aukcja ok. 12:45; jedno zapytanie na strefę, ok. 10 minut z instalacją) i przebudowuje dashboard. Produkcji, pogody i prognoz nie pobiera – prognozy liczone są raz dziennie, w nocy. Podgląd dashboardu odświeża się po nim o 16:40 z krótką informacją o cenach na jutro. Jego podsumowanie trafia do `data/podsumowanie_popoludnie.md`, więc `data/podsumowanie.md` zostaje z diagnostyką przebiegu nocnego. Ręcznie: Actions → „Miks energii - aktualizacja dzienna” → Run workflow z zaznaczonym „Przebieg popołudniowy”.

Jeśli pierwszy przebieg wpiszesz z mniejszym limitem, historia uzupełni się sama w kolejnych dniach. Do tego czasu porównania z 5 lat pokazują „–”, a Kontrola danych ma uwagę „historia 5 lat w trakcie uzupełniania”.

### Opcjonalnie: token ENTSO-E (zużycie Irlandii Północnej, poprawione dane Holandii)

Energy-Charts przestało podawać zużycie Irlandii Północnej (od 24.10.2025), a dla Holandii od jesieni 2026 r. podaje zaniżone zużycie, słońce i „inne”. Z tokenem ENTSO-E przebieg nocny pobiera te serie wprost z ENTSO-E Transparency Platform i zastępuje nimi dane Energy-Charts od daty z `config/settings.json` (`"entsoe"`: UK – zużycie od 2025-10-24; NL – zużycie i produkcja od 2026-09-01; ceny i saldo wymiany zostają z Energy-Charts). Brakująca historia dociąga się w pierwszym przebiegu (limit `entsoe_budget_minutes`, domyślnie 15 min).

1. Załóż darmowe konto na [transparency.entsoe.eu](https://transparency.entsoe.eu) i poproś o dostęp do API (e-mail do transparency@entsoe.eu z tematem „Restful API access” i adresem konta); token wygenerujesz potem w **My Account Settings → Web API Security Token**.
2. W repozytorium: **Settings → Secrets and variables → Actions → New repository secret**, nazwa `ENTSOE_TOKEN`, wartość – token.

Bez sekretu nic się nie zmienia (w podsumowaniu przebiegu jest tylko jedna linia o braku tokenu). Surowe doby z ENTSO-E są w `data/entsoe/<KOD>.csv`, a w Kontroli danych źródło kraju ma dopisek „+ ENTSO-E od …”. Po pierwszym przebiegu w podsumowaniu jest porównanie z Energy-Charts (np. `load +28%, solar +900%`) – jeśli ENTSO-E ma ten sam błąd co Energy-Charts, różnica będzie bliska zera i warto wyłączyć dany kraj (`"load": false`, `"generation": false`). Kod: `mixtracker/entsoe.py`, testy: `tests/test_entsoe.py`.

Z tym samym tokenem przebieg nocny pobiera też **niedostępność elektrowni** (komunikaty UMM, dokument A80) dla Francji, Belgii, Hiszpanii i Holandii (`"outages"` w `config/settings.json`) – to wspólne dane dla dashboardu „Gaz UE” (gdy stoją bloki jądrowe, Europa spala więcej gazu). Okno: 400 dni wstecz i 180 dni naprzód; pierwsze pobieranie trwa kilka nocy (ENTSO-E podaje wszystkie rewizje komunikatów, odpowiada ok. 20 s na stronę 200 dokumentów; limit `backfill_budget_minutes`, domyślnie 60 min na noc) – najpierw okres bieżący i przyszły, potem historia do porównań r/r, wznawiane od zapisanej strony; potem tylko komunikaty zmienione od poprzedniego przebiegu (limit `budget_minutes`, 15 min). Stan pobierania jest w `meta.json` (`current_ok`, `history_ok`, `queue`). Pliki w `data/shared_outages/`: `daily.csv` (średnia niedostępna moc w dobie wg paliw, osobno planowe i awaryjne), `units.json` (bloki jądrowe i gazowe ≥ 400 MW wyłączone teraz albo w ciągu 30 dni), `events.csv` (ostatnie rewizje komunikatów) i `meta.json`. W podsumowaniu przebiegu jest linia „Niedostępność elektrowni (ENTSO-E, dla Gaz UE): …”. Kod: `mixtracker/shared_outages.py`, testy: `tests/test_shared_outages.py`.

## Na co dzień

- **Dashboard**: stały adres z GitHub Pages. Bez Pages pobierzesz go z wyników przebiegu w zakładce **Actions** (sekcja *Artifacts* → `dashboard`, plik `index.html`).
- **Krótkie podsumowanie** dnia (UE-27, największe zmiany) widać na stronie przebiegu w **Actions** i w pliku `data/podsumowanie.md`.
- **Dane źródłowe** (możesz je otworzyć w Excelu):
  - `data/daily/<KOD>.csv`: MWh dziennie wg kategorii w czasie lokalnym kraju, ceny dzienne i składniki cen ważonych,
  - `data/prices/<STREFA>.csv`: ceny dzienne strefy (średnia, min, maks, godziny z ceną < 0), łącznie z dniem dzisiejszym i jutrzejszym, jeśli były już opublikowane w chwili przebiegu,
  - `data/forecast/<KOD>.json`: najnowsza prognoza; `data/forecast/archive.csv`: archiwum prognoz z 400 dni (do oceny trafności),
  - `data/eurostat/monthly.csv`: GWh miesięcznie.
- **Nierozpoznany typ produkcji** (Kontrola danych): trafia do „Inne”. Dopisz regułę w `mixtracker/categories.py` (funkcja `classify`).

## Skąd są dane i co znaczą

- **Dane dzienne**: [Energy-Charts.info](https://energy-charts.info) (Fraunhofer ISE, licencja CC BY 4.0), które zbiera dane z ENTSO-E Transparency Platform i od operatorów sieci (dla Wielkiej Brytanii z Elexon). Rozdzielczość 15 min lub 1 h, sumowana do dni w czasie lokalnym kraju.
  - **Produkcja** to publiczna produkcja netto. Nie obejmuje części autoprodukcji przemysłowej, więc bywa o kilka procent niższa niż w Eurostacie (różnicę pokazuje panel Eurostatu).
  - **Zużycie** to obciążenie sieci według ENTSO-E (z pompowaniem i stratami sieciowymi). Gdy kraj go nie raportuje, liczone jest z bilansu i oznaczone „(szac.)”.
  - **Saldo wymiany** = zużycie − (produkcja − pompowanie). Dodatnie: import netto, ujemne: eksport netto.
  - **Udział OZE** = wiatr, słońce, woda (bez elektrowni szczytowo-pompowych), biomasa, geotermia, inne OZE.
  - Dni z pokryciem danych poniżej 80% są pomijane. Drobne luki w pojedynczej serii są uzupełniane proporcjonalnie.
  - Dane są poprawiane przez operatorów, dlatego tracker przy każdym przebiegu pobiera ponownie ostatnie 10 dni, a w poniedziałki ostatnie 45.
- **Wielka Brytania**: [Elexon BMRS](https://bmrs.elexon.co.uk), Insights Solution API: FUELHH (produkcja wg paliwa i interkonektory, co 30 min) oraz AGPT (B1620, tylko słońce i podział wiatru). Zbiory AGPT i ATL nie są podstawą, bo mają długie luki (całe typy znikają na tygodnie), a ATL zmienił definicję w 2026 r. Contains BMRS data © Elexon Limited copyright and database right. Zapis w `data/daily/GB.csv`.
- **Ceny**: rynek dnia następnego (day-ahead) z Energy-Charts /price.
  - **Licencja**: Energy-Charts udostępnia na CC BY 4.0 (dane SMARD / Bundesnetzagentur) tylko strefy AT, BE, CH, CZ, DE-LU, DK1, DK2, FR, HU, IT-North, NL, NO2, PL, SE4 i SI. Ceny pozostałych stref są wyłącznie do użytku prywatnego i wewnętrznego, więc dashboard ich domyślnie nie pobiera.
  - Dla Włoch, Norwegii i Szwecji oznacza to tylko jedną strefę (IT-North, NO2, SE4), co dashboard opisuje przy cenie. Dla Danii to średnia DK1 i DK2.
  - Jeśli repozytorium i dashboard są prywatne i używasz ich tylko na własne potrzeby, możesz włączyć wszystkie strefy: w `config/settings.json` ustaw `"price_licenses": ["cc_by", "private"]`. Historia cen nowych stref dociągnie się sama w kolejnych przebiegach.
  - **Cena ważona produkcją** (capture price) = Σ(cena × produkcja) / Σ produkcja, z danych 15-minutowych lub godzinowych. Relacja do ceny średniej to capture rate.
- **Emisyjność**: szacunek emisji bezpośrednich CO₂ ze spalania na kWh produkcji krajowej netto, ze współczynników dla paliw w `config/emission_factors.json` (np. węgiel brunatny 1,10, kamienny 0,90, gaz 0,40 t CO₂/MWh; biomasa = 0 jak w EU ETS). Nie obejmuje emisji z importu ani cyklu życia. Współczynniki możesz zmienić; wynik przeliczy się przy następnym przebiegu dla całej historii.
- **Prognozy na 7 dni**: Energy-Charts ma prognozy tylko na 1–2 dni, więc tracker liczy własne.
  - Pogoda z [Open-Meteo](https://open-meteo.com) (CC BY 4.0) dla kilku punktów w każdym kraju (`config/weather_points.csv`): wiatr na 100 m, promieniowanie słoneczne, temperatura. Historia (ERA5) zapisuje się w `data/weather/`.
  - Model dla każdego kraju dopasowany do jego historii (`mixtracker/model.py`): wiatr z krzywej mocy turbiny (ostatnie 120 dni, nowsze dni ważą więcej), słońce z promieniowania (90 dni, waga maleje o połowę co 15 dni), zużycie z dni grzewczych/chłodniczych, dnia tygodnia, świąt państwowych (biblioteka `holidays`), dni pomostowych i okresu 24.12–1.01 (365 dni). Prognoza UE-27 to suma krajów.
  - Korekta z ostatnich dni: błąd modelu z ostatniego znanego dnia (wiatr i słońce: średnia z 3 dni) przechodzi na prognozę z wagą szacowaną z historii błędów kraju, malejącą z wyprzedzeniem. Dopasowanie odporne: dni z błędem powyżej 4 odchyleń (MAD) są pomijane.
  - Luki w danych źródła (wiatr < 10% i słońce < 15% oczekiwanej produkcji przy danej pogodzie, zużycie poza 60–160% mediany z 4 tygodni) są pomijane przy uczeniu i w weryfikacji; lista jest w kontroli danych.
  - Błąd modelu: średni błąd z ostatnich 28 dni przy znanej pogodzie. Błąd samej prognozy pogody dochodzi do tego i rośnie z każdym dniem.
  - Opcjonalnie można włączyć prognozy operatorów z Energy-Charts (1–2 dni): `"forecasts_energycharts": true`, ale wydłuża to przebieg o ok. 50 minut.
- **Dane miesięczne oficjalne**: Eurostat `nrg_cb_pem` (produkcja netto wg paliwa) i `nrg_cb_em` (energia dostępna na rynek krajowy, import, eksport). Brak dla Szwajcarii, dla Wielkiej Brytanii tylko do 2020.
- **Średnia i zakres 5 lat**: ten sam tydzień ISO albo ten sam miesiąc w 5 poprzednich latach (w 2026: lata 2021–2025). Wymagane są co najmniej 3 lata danych.

Ustawienia (odstęp między zapytaniami, limit czasu, liczba dni do ponownego pobrania, próg pokrycia, liczba lat, licencje cen, prognozy) są w `config/settings.json`. Strefy cenowe krajów są w `config/price_zones.csv`. Lista krajów i stref czasowych jest w `config/countries.csv`.

## Wspólna pogoda dla innych dashboardów

Osobny przebieg (`.github/workflows/shared_weather.yml`, „Wspólna pogoda dla dashboardów”, codziennie o 00:17 UTC, także w weekendy; do tego lekki przebieg wieczorny o 17:47 UTC – patrz niżej) pobiera całą pogodę potrzebną raportowi Gaz UE i zapisuje ją w `data/shared_weather/`. Raport nie pobiera pogody sam – czyta te pliki. Dwa zadania idą równolegle na osobnych maszynach (każde ma własny limit Open-Meteo):

- **temp** – temperatura dla punktów z `uses` = temp w `config/shared_weather_points.csv` (49 miast UE i 14 punktów w Azji):
  - `temp_hist.csv` – średnia dobowa z archiwum ERA5 od 2019 r.,
  - `temp_recent.csv` – ostatnie 14 dni i 16 dni prognozy (`fc=1`),
  - `temp_prev.csv` – dawne prognozy: `l1` to prognoza na dany dzień wydana dzień wcześniej, `l7` – tydzień wcześniej (`l0` – bieżąca analiza), od 2024 r.; służy do liczenia, jak bardzo myli się prognoza pogody przy różnym wyprzedzeniu,
  - `meta.json` – czas aktualizacji, zakres danych, statystyki kroków (zapytania, odmowy 429, ostatni błąd).
- **extra** – energetyka i sezon:
  - `ensemble_power.csv.gz` – ensemble ECMWF IFS (51 wariantów): wiatr na 100 m, promieniowanie i temperatura, średnio po punktach kraju (miasta i rejony farm OZE, `uses` = ens), 5 dni wstecz i 12 dni prognozy; gdy serwer ensemble zawiedzie – deterministyczna prognoza ECMWF,
  - `ensemble_hist.csv` – minione dni z ensemble (średnia wariantów), 400 dni – do kalibracji pogoda → produkcja prądu,
  - `seasonal.csv.gz` – prognoza sezonowa ECMWF SEAS5 (51 wariantów, 183 dni) dla punktów z `uses` = seas, odświeżana co 7 dni,
  - `meta_extra.json` – jak `meta.json`.

- **evening** (17:47 UTC, czyli 18:47 zimą i 19:47 latem w Polsce; limit 12 min) – tylko świeża prognoza temperatury (`temp_recent.csv`) i ensemble do energetyki (`ensemble_power.csv.gz`) z nowszych przebiegów modeli, przed wieczornym raportem Gaz UE (20:41). Stan w `meta_evening.json` (forecast_issued, ensemble_issued, statusy kroków); `meta.json` i `meta_extra.json` zostają z nocy. Ręcznie: Actions → „Wspólna pogoda dla dashboardów” → Run workflow z zaznaczonym „Lekki przebieg wieczorny”.

Każdy krok ma limit czasu (35 min na zadanie) i zapisuje to, co zdążył zebrać; brakującą historię dociąga partiami w kolejnych dniach. Przebieg nie zmienia danych ani działania Miksu energii. Kod: `mixtracker/shared_weather.py`, testy: `tests/test_shared_weather.py`.

## Ograniczenia

- Energy-Charts pozwala na ok. 2 zapytania na minutę na każdy rodzaj danych (produkcja, ceny, prognozy osobno), więc codzienny przebieg trwa ok. 20–30 minut. Nie skracaj `request_interval_seconds` poniżej 30.
- **Wielka Brytania**: dane z Elexon BMRS (bez klucza API). Produkcja z FUELHH obejmuje elektrownie przyłączone do sieci przesyłowej. Słońce pochodzi z AGPT (szacunek NESO dla instalacji rozproszonych). Rozproszona energetyka wiatrowa (ok. 2 GW średnio) nie jest ujęta, więc udział wiatru jest nieco zaniżony. Wiatr dzielony jest na ląd i morze według proporcji z AGPT. Zużycie liczone jest z bilansu: produkcja + saldo interkonektorów (oznaczone „szac.”), bez pompowania w elektrowniach szczytowo-pompowych. Kod UK w Energy-Charts obejmuje tylko Irlandię Północną, pokazaną osobno. Dla GB nie ma ceny day-ahead na wolnej licencji. Elexon ma osobny limit czasu w przebiegu (`elexon_budget_minutes`, domyślnie 40 min).
- **Holandia**: od 18.09.2026 źródło raportuje zużycie i „inne” źródła inaczej (skok o ok. −30%), a produkcja ze słońca jest w nim zaniżona. Dashboard wykrywa takie skoki automatycznie i ostrzega przy kraju; porównania r/r są wtedy niewiarygodne. Z tokenem ENTSO-E (wyżej) te serie są zastępowane danymi ENTSO-E. Dane UE-27 to osobny agregat Energy-Charts, więc poprawka Holandii go nie zmienia.
- Dni z brakującymi lub niepełnymi danymi są pobierane ponownie automatycznie (każdy zakres najwyżej raz na 7 dni).
- Bez cen na wolnej licencji (np. Hiszpania, Rumunia, kraje bałtyckie) kafelek ceny i sekcja cen pokazują wyjaśnienie zamiast liczb.
- Dla Malty nie ma danych dziennych w żadnym darmowym źródle, jest tylko w panelu Eurostatu.
- Klasyfikacja źródeł opiera się na nazwach typów produkcji z Energy-Charts. Gdy źródło doda nową nazwę, pojawi się ona w Kontroli danych jako nierozpoznana.

## Dla zaawansowanych

```bash
pip install -r requirements.txt
python -m pytest -q                                     # testy
python -m mixtracker.run_daily --budget 10 --only PL,DE # krótki przebieg lokalnie
python -m mixtracker.run_daily --skip-fetch             # tylko przeliczenie i dashboard
python scripts/make_demo_data.py demo                   # dane demonstracyjne do podglądu
```

Dashboard to jeden plik `site/index.html` z wbudowanymi danymi (bez zewnętrznych skryptów). Szablon: `mixtracker/template.html`.
