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
- Na iPhonu fungují jen u stránky přidané na plochu (iOS 16.4+) a přes https (Tailscale serve). Zatím neověřeno na skutečném telefonu.
- Provoz: dashboard běží z projektové složky (data CSV jsou tady). Staré soubory z plochy jsou v `Desktop\solar_stare_soubory`. Druhá instance se nespustí (`allow_reuse_address = False`).

