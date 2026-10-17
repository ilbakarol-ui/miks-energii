# Miks energii w Europie

Codziennie aktualizowany dashboard: produkcja energii elektrycznej według źródeł, zużycie, ceny day-ahead, emisyjność i prognozy na 7 dni w krajach UE-27 oraz w Norwegii, Szwajcarii i Wielkiej Brytanii (osobno także Irlandia Północna). Pokazuje trendy tydzień do tygodnia, miesiąc do miesiąca, rok do roku i na tle średniej oraz zakresu z ostatnich 5 lat.

Działa w chmurze GitHub (GitHub Actions), raz dziennie, bez niczego na Twoim komputerze i bez kluczy API. Gotowa strona jest publikowana pod stałym adresem na GitHub Pages.

## Co jest na dashboardzie

- **Wybór kraju i okresu**: ostatni pełny tydzień (pon.–niedz.), ostatni pełny miesiąc albo rok do daty.
- **Wskaźniki**: zużycie, produkcja krajowa, udział OZE, saldo wymiany, cena day-ahead i emisyjność (g CO₂/kWh). Przy każdym: zmiana t/t lub m/m, r/r, względem średniej 5 lat i pasek z zakresem min–maks z 5 lat.
- **Miks produkcji**: udział 8 grup źródeł (gaz, jądrowa, węgiel, biomasa i inne OZE, ropa/odpady/inne, woda, wiatr, słońce), zmiana r/r w p.p. i pozycja względem 5 lat. Przełącznik **Szczegóły** rozbija grupy, np. węgiel brunatny i kamienny, wiatr na lądzie i na morzu.
- **Trend tygodniowy na tle 5 lat**: bieżący rok, poprzedni rok, średnia i pasmo min–maks. Do wyboru zużycie, produkcja, saldo, udział OZE, udział paliw kopalnych, każde źródło osobno, emisyjność, emisje CO₂, cena, ceny ważone produkcją słońca i wiatru oraz godziny z ujemną ceną.
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

Następne przebiegi startują same codziennie o 05:23 UTC (07:23 latem, 06:23 zimą w Polsce) i trwają ok. 20–30 minut. Godzinę zmienisz w `.github/workflows/daily.yml` (linia `cron`).

Jeśli pierwszy przebieg wpiszesz z mniejszym limitem, historia uzupełni się sama w kolejnych dniach. Do tego czasu porównania z 5 lat pokazują „–”, a Kontrola danych ma uwagę „historia 5 lat w trakcie uzupełniania”.

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
  - Dane są poprawiane przez operatorów, dlatego tracker codziennie pobiera ponownie ostatnie 10 dni, a w niedziele ostatnie 45.
- **Wielka Brytania**: [Elexon BMRS](https://bmrs.elexon.co.uk), Insights Solution API: FUELHH (produkcja wg paliwa i interkonektory, co 30 min) oraz AGPT (B1620, tylko słońce i podział wiatru). Zbiory AGPT i ATL nie są podstawą, bo mają długie luki (całe typy znikają na tygodnie), a ATL zmienił definicję w 2026 r. Contains BMRS data © Elexon Limited copyright and database right. Zapis w `data/daily/GB.csv`.
- **Ceny**: rynek dnia następnego (day-ahead) z Energy-Charts /price.
  - **Licencja**: Energy-Charts udostępnia na CC BY 4.0 (dane SMARD / Bundesnetzagentur) tylko strefy AT, BE, CH, CZ, DE-LU, DK1, DK2, FR, HU, IT-North, NL, NO2, PL, SE4 i SI. Ceny pozostałych stref są wyłącznie do użytku prywatnego i wewnętrznego, więc dashboard ich domyślnie nie pobiera.
  - Dla Włoch, Norwegii i Szwecji oznacza to tylko jedną strefę (IT-North, NO2, SE4), co dashboard opisuje przy cenie. Dla Danii to średnia DK1 i DK2.
  - Jeśli repozytorium i dashboard są prywatne i używasz ich tylko na własne potrzeby, możesz włączyć wszystkie strefy: w `config/settings.json` ustaw `"price_licenses": ["cc_by", "private"]`. Historia cen nowych stref dociągnie się sama w kolejnych przebiegach.
  - **Cena ważona produkcją** (capture price) = Σ(cena × produkcja) / Σ produkcja, z danych 15-minutowych lub godzinowych. Relacja do ceny średniej to capture rate.
- **Emisyjność**: szacunek emisji bezpośrednich CO₂ ze spalania na kWh produkcji krajowej netto, ze współczynników dla paliw w `config/emission_factors.json` (np. węgiel brunatny 1,10, kamienny 0,90, gaz 0,40 t CO₂/MWh; biomasa = 0 jak w EU ETS). Nie obejmuje emisji z importu ani cyklu życia. Współczynniki możesz zmienić; wynik przeliczy się przy następnym przebiegu dla całej historii.
- **Prognozy na 7 dni**: Energy-Charts ma prognozy tylko na 1–2 dni, więc tracker liczy własne.
  - Pogoda z [Open-Meteo](https://open-meteo.com) (CC BY 4.0) dla kilku punktów w każdym kraju (`config/weather_points.csv`): wiatr na 100 m, promieniowanie słoneczne, temperatura. Historia (ERA5) zapisuje się w `data/weather/`.
  - Model dla każdego kraju dopasowany do jego historii: wiatr z krzywej mocy turbiny (ostatnie 120 dni), słońce z promieniowania (60 dni), zużycie z dni grzewczych/chłodniczych i dnia tygodnia (365 dni). Prognoza UE-27 to suma krajów.
  - Błąd modelu: średni błąd z ostatnich 28 dni przy znanej pogodzie. Błąd samej prognozy pogody dochodzi do tego i rośnie z każdym dniem. Model nie zna świąt.
  - Opcjonalnie można włączyć prognozy operatorów z Energy-Charts (1–2 dni): `"forecasts_energycharts": true`, ale wydłuża to przebieg o ok. 50 minut.
- **Dane miesięczne oficjalne**: Eurostat `nrg_cb_pem` (produkcja netto wg paliwa) i `nrg_cb_em` (energia dostępna na rynek krajowy, import, eksport). Brak dla Szwajcarii, dla Wielkiej Brytanii tylko do 2020.
- **Średnia i zakres 5 lat**: ten sam tydzień ISO albo ten sam miesiąc w 5 poprzednich latach (w 2026: lata 2021–2025). Wymagane są co najmniej 3 lata danych.

Ustawienia (odstęp między zapytaniami, limit czasu, liczba dni do ponownego pobrania, próg pokrycia, liczba lat, licencje cen, prognozy) są w `config/settings.json`. Strefy cenowe krajów są w `config/price_zones.csv`. Lista krajów i stref czasowych jest w `config/countries.csv`.

## Ograniczenia

- Energy-Charts pozwala na ok. 2 zapytania na minutę na każdy rodzaj danych (produkcja, ceny, prognozy osobno), więc codzienny przebieg trwa ok. 20–30 minut. Nie skracaj `request_interval_seconds` poniżej 30.
- **Wielka Brytania**: dane z Elexon BMRS (bez klucza API). Produkcja z FUELHH obejmuje elektrownie przyłączone do sieci przesyłowej. Słońce pochodzi z AGPT (szacunek NESO dla instalacji rozproszonych). Rozproszona energetyka wiatrowa (ok. 2 GW średnio) nie jest ujęta, więc udział wiatru jest nieco zaniżony. Wiatr dzielony jest na ląd i morze według proporcji z AGPT. Zużycie liczone jest z bilansu: produkcja + saldo interkonektorów (oznaczone „szac.”), bez pompowania w elektrowniach szczytowo-pompowych. Kod UK w Energy-Charts obejmuje tylko Irlandię Północną, pokazaną osobno. Dla GB nie ma ceny day-ahead na wolnej licencji. Elexon ma osobny limit czasu w przebiegu (`elexon_budget_minutes`, domyślnie 40 min).
- **Holandia**: od 18.09.2026 źródło raportuje zużycie i „inne” źródła inaczej (skok o ok. −30%), a produkcja ze słońca jest w nim zaniżona. Dashboard wykrywa takie skoki automatycznie i ostrzega przy kraju; porównania r/r są wtedy niewiarygodne.
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
