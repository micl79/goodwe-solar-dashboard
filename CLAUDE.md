# Kontext projektu pro Claude Code

Lokální dashboard pro GoodWe GW10K-ET (hybrid, 3fáze, baterie). Hlavní soubor `goodwe_dashboard.py` obsahuje
čtení měniče, server (http.server, vše v jednom souboru), HTML/CSS/JS (inline řetězec `HTML`) a výpočty analýzy.
Komunikace s uživatelem je česky.

## Důležitá zjištění o datech (ověřeno na reálném měniči)
- Čtení: `goodwe.connect(IP)` + `read_runtime_data()`, UDP 8899.
- Znaménka: `active_power_total` záporné = odběr ze sítě (v dashboardu `grid = -active_power_total`, + je odběr);
  `pbattery1` kladné = vybíjení, záporné = nabíjení.
- Spotřeba domu = `house_consumption`. Nákup/prodej ze sítě brát z elektroměru: `meter_e_total_imp` / `meter_e_total_exp`
  (NE `e_total_imp`/`e_total_exp`, ty vycházejí nesmyslně). Denní `e_day_imp`/`e_day_exp` se nepoužívají.
- Celoživotní počítadla (ukládají se po 5 min do `goodwe_pocitadla.csv`): `e_total`, `e_load_total`,
  `meter_e_total_imp`, `meter_e_total_exp`, `e_bat_charge_total`, `e_bat_discharge_total`. Pořadí v=[výroba, spotřeba, nákup, prodej, nabito, vybito].
- SEMS export (`sems_import.py`): sloupce `dd/mm/yyyy` (den), `mm/yyyy` (měsíc), `yyyy` (rok); řádky Energy Generation,
  Import Energy, Grid Export Energy, Charged Energy, Discharge Energy. Spotřeba v exportu chybí, dopočítává se
  `pv + import + vybito - nabito - prodej`.
- SEMS měl výpadek dat 9. 7. až 1. 10. 2026 (samé nuly); `estimate_outage()` to dopočítává z rozdílu celoživotních
  počítadel a sezónního průběhu a značí šrafováním (`e` v sloupcích).

## Konvence
- Barvy dat jsou validované (modrá, oranžová, zelená, jantarová, magenta) pro tmavý povrch; zlatá je jen ozdobná.
- Žádný rm -rf ani mazání v uživatelově složce; osobní CSV jsou v `.gitignore`.

## Ovládání měniče (`ovladani.py`)
- Jediný zápis do měniče je `POST /api/ovladani`, vyžaduje přístupový kód z proměnné prostředí `GW_PIN` (bez ní je ovládání vypnuté; kód se nikdy neukládá do souborů). Po 5 chybách za sebou zámek na 5 minut.
- Zápisy provádí smyčka `poll()` přes frontu (`cmd_q`), stejným spojením jako čtení. Příkaz starší než 25 s se zahodí.
- Před první změnou skupiny (`mode`, `charge`, `min_soc`, `export`) se uloží původní hodnota do `ovladani_zaloha.json`; "Vrátit na původní" ji obnoví. Nabíjení má vlastní zálohu (stav těsně před nabíjením) a samo se vrací zpět po dosažení cíle nebo po 8 h.
- Ostrovní režim (OFF_GRID) je záměrně vynechán.
- Zálohy 1bajtových nastavení se vracejí přes `write_setting` (sdílejí 2bajtový registr se sousedem), jen Eco plán 1 (12 B) po bajtech.
- Každá změna se zapisuje do `ovladani.log` (bez kódu).
- Ceny: burza OTE (hodinová řada 60min, `report_date=RRRR-MM-DD`), kurz ČNB; shodují se s cenou v aplikaci dodavatele (ověřeno 9. 10. 9:00 = 3,93 Kč/kWh).

## Upozornění (`upozorneni.py`)
- Web Push přes `pywebpush` (nepovinný balíček). Klíč VAPID se při prvním startu vytvoří v `push_klic.json`, přihlášené telefony jsou v `push_odberatele.json` (obojí v .gitignore, nikdy nenahrávat).
- Přihlášení k upozornění vyžaduje stejný přístupový kód jako ovládání. Události: baterie plná (po poklesu pod 90 %), nabíjení skončilo, měnič 10 min nedostupný a znovu online.
- Na iPhonu fungují jen u stránky přidané na plochu (iOS 16.4+) a přes https (Tailscale serve). Ověřeno na skutečném iPhonu 10. 10. 2026 (zkouška přišla).
- Provoz: dashboard běží z projektové složky (data CSV jsou tady). Druhá instance se nespustí (`allow_reuse_address = False`).

## Veřejná výloha (`vyloha/`, `verejne.py`, `sestav_vyloha.py`)
- Veřejná stránka je sestavená z HTML dashboardu: `sestav_vyloha.py` vyřízne bloky mezi značkami `PUBLIC-STRIP-START/END` (síťová část ovládání, upozornění na telefon) a přepíše adresy na `/api/snapshot`. Analýza energie na ní zůstává. Ovládání je na veřejné stránce jen ukázka v prohlížeči (`DEMO` v `sestav_vyloha.py`), nic neodesílá. Nový kód, který něco odesílá na server, OBALIT značkami, jinak sestavení selže (hlídá `method:'POST'`, `/api/ovladani`, `/api/push`).
- Když data z PC nechodí déle než 10 min (nebo je `FORCE_DEMO=1`), `vyloha/api/snapshot.py` vrací simulaci (`simulate()`, deterministická, bez uloženého stavu) a ceny si stahuje sám (`pick_prices`); stránka ukáže stav `DEMO`. Domácí PC data posílá (`verejne.py`, vypnuto bez proměnných `GW_VEREJNE_URL`/`GW_VEREJNE_TOKEN`), Vercel je ukládá do Redisu (`vyloha/api/snapshot.py`, Python). Zpoždění: `GW_VEREJNE_ZPOZDENI`. Návod k nasazení: `vyloha/README.md`.
- Nasazení: Vercel (Root Directory `vyloha`, produkce po pushi do `main`) + Upstash Redis. Klíč `PUSH_TOKEN` je jen ve Vercelu a v proměnné `GW_VEREJNE_TOKEN` na PC, nikdy v souborech.
- Veřejné ovládání NENÍ hotové a nedělá se bez přihlášení přes e-mail (Cloudflare Access): samotný šestimístný kód je na internet slabý. Ovládání zatím jen přes Tailscale.

## Jazyky (čeština, angličtina)
- Rozhraní je česky (zdrojový text) a anglicky. Slovník `EN` je v HTML dashboardu (klíč = český text, `{0}` = proměnná část). Texty na stránce překládá překladač (`MutationObserver` + `T()`), věty složené za běhu se píší přes `L('český text {0}',hodnota)`, hodnoty ze serveru přes `T()`.
- Nový text v rozhraní: přidat do `EN`, jinak zůstane česky. Kontrola: v prohlížeči přepnout na angličtinu a projít stránku (hledat texty s diakritikou) ve všech stavech (zamčeno, odemčeno, nabíjení, okno, analýza se všemi obdobími).
- Čísla a data se formátují podle jazyka (`LOC()`, `PC()`, `dm()`); Kč se v angličtině píše CZK. Odkaz `?lang=en` otevře stránku anglicky.
- Oznámení na telefon mají obě verze (`en=(titulek,text)` v `notify`), jazyk si ukládá každý telefon při zapnutí.
- Web `blackbirds-web` má vlastní, jednoduchý přepínač (`data-en`).

## Nabíjení v nejlevnějším bloku (`nabijeni.py`)
- `make_plan()` je čistá funkce (ceny v Kč/kWh po hodinách, místní čas, stav baterie, nastavení, konec posledního nabíjení) a vrací stav `vypnuto|netreba|ceka|naplanovano`. V příštích 24 hodinách hledá nejlevnější SOUVISLÝ blok `hodin` hodin (výchozí 3). Bez cenového stropu a bez minimálního rozdílu (uživatel to tak chtěl). Mezi nabíjeními je odstup 12 h (`MIN_GAP_H`). Nastavení: `enabled` (výchozí VYPNUTO), `target` (výchozí 100), `hodin`, `kapacita`, `vykon` (výkon nabíjení v %, 10 až 100, výchozí 100; uživatel si na svém zařízení nastavil 60 %). Posuvníky výkonu ukazují i kW (`PWR`, 100 % ≈ 5,4 kW).
- Počítá se s kapacitou 10 kWh (odhad z dat: 10,1 až 10,9 kWh) a výkonem 4,5 kW. Dny s 23 nebo 25 hodinami (změna času) se přeskakují.
- `tick()` volá `run_control` při každém čtení měniče: spustí nabíjení stejnou cestou jako ruční (`ovladani.execute("charge", until=konec bloku)`), takže platí záloha, návrat zpět, konec při dosažení cíle nebo na konci bloku. Ruční zrušení blok uzavře a plán ho znovu nespustí. Stav je v `nabijeni_stav.json` (v .gitignore).
- Když je PC vypnuté během nabíjení, nabíjení v měniči zůstane do cíle; po restartu dashboardu se režim vrátí (stav `charge` se načítá ze zálohy).
- Nastavení jde přes `POST /api/ovladani` s akcí `plan` (jen uloží, do měniče nezapisuje). Na veřejné ukázce je stejné ovládání jako maketa (`demoPlanCalc` v `sestav_vyloha.py`).

## Baterie (zjištěno ze štítků 17. 10. 2026)
- **Dyness Tower T10**: 3 moduly HV9637 (každý 37 Ah, 96 V, 3,552 kWh, LFP), dohromady 288 V a 10,66 kWh. Měnič hlásí `battery_modules` = 6, fyzicky jsou 3.
- Podle návodu Dyness (z internetu, štítky to neuvádějí): standardní proud 7,4 A (asi 2,1 kW), doporučený 18,5 A (asi 5,3 kW), při spojení s měničem nejvýš 22,5 A, absolutní maximum 37 A. Nabíjet jen při 0 až 50 °C.
- Naměřených 5,4 kW při výkonu 100 % odpovídá doporučenému proudu. Šetrný výkon je 40 až 60 % (asi 2 až 3,2 kW); nižší výkon znamená delší blok nebo nižší cíl.
