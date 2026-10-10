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
- **Spotové ceny** z burzy OTE (hodinové, kurz ČNB) v levém sloupci. Shodují se s cenou v aplikaci dodavatele.
- **Ovládání měniče** v pravém sloupci: nabít baterii, přepnout režim, nejnižší stav baterie, limit prodeje. Chráněno přístupovým kódem, s možností "Vrátit na původní".
- **Nabíjení v nejlevnějších hodinách:** v zadaném okně (např. 22:00 až 6:00) se vybere tolik nejlevnějších hodin, kolik je potřeba k cílovému stavu baterie, a v nich se samo zapne nabíjení. Má cenový strop a hlídá, aby rozdíl proti pozdějším cenám pokryl ztráty baterie. Ve výchozím stavu je vypnuté.
- **Upozornění na iPhone** (baterie plná, nabíjení skončilo, měnič nedostupný).
- Čeština a angličtina (vlajky v záhlaví), tmavý, světlý a automatický vzhled.

## Spuštění (Windows)
```
pip install -r requirements.txt
python goodwe_dashboard.py
```
Otevři `http://localhost:8080`. IP adresu měniče v domácí síti je nutné zadat v proměnné `GW_IP` (zjistíte ji v routeru nebo v aplikaci SEMS), např. v PowerShellu `setx GW_IP "192.168.1.100"` a znovu se přihlásit.
Dvojklik na `solar.bat` spustí dashboard i prohlížeč.

Proměnné prostředí: `GW_IP` (povinná), `GW_PORT` (8080), `GW_INTERVAL` (5 s), `GW_COUNTER_EVERY` (300 s).

## Ovládání a upozornění
- Přístupový kód: v PowerShellu `setx GW_PIN "váš-kód"` (aspoň 6 znaků), pak se odhlaste a přihlaste. Bez kódu je ovládání vypnuté. Kód se nikam do souborů neukládá.
- Změny se zapisují do `ovladani.log`, původní hodnoty do `ovladani_zaloha.json`. Ostrovní režim v kódu není.
- Upozornění: `pip install pywebpush`, pak v dashboardu u Ovládání "Zapnout" (na iPhonu z ikony na ploše, přes https adresu).
- Ceny: marže dodavatele se nastavuje proměnnou `GW_MARZE` (Kč/kWh), výchozí 0 odpovídá čisté burzovní ceně, kterou ukazují i aplikace dodavatelů spotové elektřiny (ověřeno u jednoho českého dodavatele).

## Automatický start
`solar_autostart.bat` se spouští při přihlášení do Windows ze složky Po spuštění (`shell:startup`, zástupce "Solarni dashboard"). Druhá kopie dashboardu se nespustí (port je obsazený).

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
`goodwe_historie.csv`, `goodwe_pocitadla.csv`, `goodwe_sems.csv`, `ovladani*`, `push_*.json` jsou v `.gitignore`. Repozitář nech **privátní**.

## Bezpečnost a odpovědnost
- Neoficiální projekt, nesouvisí se společností GoodWe. **Použití na vlastní riziko, bez záruky.** Zápisy do měniče (ovládání) mohou ovlivnit jeho provoz a záruku výrobce.
- Dashboard nemá přihlášení pro čtení. **Nikdy neotevírejte jeho port do internetu** (žádné přesměrování portu na routeru). Pro přístup zvenku použijte VPN (např. Tailscale).
- Ovládání měniče je vypnuté, dokud nenastavíte přístupový kód `GW_PIN`. Kód ani klíče nepatří do souborů projektu ani do Gitu.
- Veřejná výloha (`vyloha/`) ukazuje živá data z domu. Z přesných hodnot jde poznat, kdy nejste doma. Zvažte zpoždění (`GW_VEREJNE_ZPOZDENI`).
- Ostrovní režim (OFF_GRID) v ovládání záměrně není.

## Licence
MIT, viz soubor `LICENSE`. Software je poskytován bez záruky (viz též Bezpečnost a odpovědnost výše).
