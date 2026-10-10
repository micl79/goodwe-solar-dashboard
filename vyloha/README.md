# Veřejná výloha (jen pro čtení)

Stránka, kterou lze sdílet s kamarády. Ukazuje živá data z elektrárny, spotové ceny, graf a analýzu energie (týden, měsíc, rok).
Sloupec **Ovládání** je jen ukázka: tlačítka pracují v prohlížeči návštěvníka a **nic se z nich neodesílá**. Skutečné ovládání na stránce není.

Jak to funguje: domácí PC (dashboard, `verejne.py`) posílá každých 30 s vybraná čísla na Vercel (`api/snapshot.py`),
ten je uloží do Redisu a stránka (`public/index.html`) si je odtud čte. K měniči ani k domácí síti se nikdo nepřipojuje.

## Sestavení stránky
Po změně vzhledu dashboardu: `python sestav_vyloha.py` (z kořene projektu), pak změny nahrát na GitHub.

## Nasazení (jednou)
1. Vercel: Add New, Project, importovat repozitář `goodwe-solar-dashboard`. **Root Directory: `vyloha`**. Framework: Other, bez build příkazu.
2. V projektu: Storage / Marketplace, přidat **Upstash Redis** (zdarma). Vercel sám doplní proměnné `KV_REST_API_URL` a `KV_REST_API_TOKEN`.
3. Settings, Environment Variables: přidat `PUSH_TOKEN` = dlouhý náhodný řetězec (aspoň 32 znaků). Nikam jinam ho nepište.
4. Deploy. Settings, Domains: připojit vlastní doménu.
5. Na domácím PC v PowerShellu (stejný klíč jako `PUSH_TOKEN`):
   ```
   setx GW_VEREJNE_URL "https://vase-domena.cz/api/snapshot"
   setx GW_VEREJNE_TOKEN "stejný-klíč"
   ```
   pak se odhlaste a přihlaste. Při startu dashboardu se vypíše „Veřejná stránka: zapnuta“.

## Když je domácí PC vypnuté
Funkce na Vercelu pozná, že data nechodí (starší než 10 minut), a **sama přepne na virtuální elektrárnu**: simulovaný
průběh slunce, spotřeby domu, baterie a sítě (jiný v létě a v zimě, stejný pro všechny návštěvníky). Ve stavovém řádku
je „simulovaná ukázka · virtuální data“. Burzovní ceny a kurz si funkce stáhne sama (veřejná data). Jakmile PC znovu
pošle data, ukáže se zase skutečná elektrárna. Analýza energie zůstává z posledních odeslaných dat.
- `?demo=1` na konci adresy vynutí virtuální data (na zkoušku).
- `FORCE_DEMO=1` ve Vercelu: ukazuje se **vždy jen virtuální elektrárna**, žádná skutečná data z domu nejdou ven.

## Soukromí a vypínače
- Zpoždění dat: `setx GW_VEREJNE_ZPOZDENI "3600"` (sekundy, výchozí 0 = živě). Stránka pak ukáže, že data jsou stará.
- Okamžité vypnutí: ve Vercelu proměnná `PUBLIC_OFF=1` (stránka přestane ukazovat data) nebo na PC smazat `GW_VEREJNE_*`.
- Odesílají se jen výkony, stav baterie, denní energie, ceny a analýza energie (součty po dnech a měsících, jednou za 5 minut). Nikdy IP adresa, ovládání ani osobní soubory.
- Stránka umí jen číst (GET). `sestav_vyloha.py` při sestavení selže, kdyby na ní zůstal odesílací kód (`method:'POST'`) nebo skutečné adresy ovládání.
