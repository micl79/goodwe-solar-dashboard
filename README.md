# Solární dashboard pro GoodWe (GW10K-ET)

Lokální dashboard pro hybridní měnič GoodWe ET. Čte data přímo z měniče přes LAN (UDP 8899, knihovna `goodwe`),
bez cloudu. Jeden soubor, bez dalších závislostí kromě `goodwe`.

## Co umí
- **Živý tok energie** (solár, síť, dům, spotřeba, baterie) s animovanými toky.
- **Živý graf výkonu** (1 h / 6 h / 24 h) s tooltipem a tabulkou.
- **Analýza** za týden, měsíc, rok a celkem: výroba, spotřeba, prodej, nákup, soběstačnost, vlastní spotřeba.
- **Historie ze SEMS** (export `.xlsx` -> `sems_import.py` -> `goodwe_sems.csv`).
- **Odhad za výpadek dat ve SEMS** (rozdíl celoživotních počítadel měniče + sezónní průběh z minulých let), v grafu šrafovaně.
- **Telefon / PWA**: ikona na domovské obrazovce, přístup z venku přes Tailscale.
- Tmavé "luxusní" téma.

## Spuštění (Windows)
```
pip install -r requirements.txt
python goodwe_dashboard.py
```
Otevři `http://localhost:8080`. IP měniče se nastavuje v proměnné `GW_IP` (výchozí `192.168.68.113`).
Dvojklik na `solar.bat` spustí dashboard i prohlížeč.

Proměnné prostředí: `GW_IP`, `GW_PORT` (8080), `GW_INTERVAL` (5 s), `GW_COUNTER_EVERY` (300 s).

## Historie ze SEMS
1. Na semsportal.com stáhni exporty *Station Statistical Report* (období Day / Month / Year).
2. Nech je ve složce Stažené soubory nebo vedle skriptů.
3. `python sems_import.py` -> vznikne `goodwe_sems.csv`; dashboard restartuj.

## Telefon
- Doma: `http://<IP počítače>:8080` (adresa se vypíše při startu).
- Odkudkoli: Tailscale + `tailscale serve --bg 8080`, pak "Přidat na plochu" v prohlížeči.
- Dashboard nemá přihlášení. Nikdy nepřesměrovávej port na routeru do internetu.

## Ikona na ploše
Zástupce se zlatou ikonou:
```
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+'\Solar.lnk'); $s.TargetPath=$d+'\solar.bat'; $s.WorkingDirectory=$d; $s.IconLocation=$d+'\solar.ico'; $s.WindowStyle=7; $s.Save()"
```
(předpokládá `solar.bat` a `solar.ico` na ploše)

## Osobní data
`goodwe_historie.csv`, `goodwe_pocitadla.csv` a `goodwe_sems.csv` jsou v `.gitignore`. Repozitář nech **privátní**.
