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
