# Veřejná výloha (jen pro čtení)

Stránka, kterou lze sdílet s kamarády. Ukazuje živá data z elektrárny, spotové ceny a graf. **Nemá žádné ovládání.**

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

## Soukromí a vypínače
- Zpoždění dat: `setx GW_VEREJNE_ZPOZDENI "3600"` (sekundy, výchozí 0 = živě). Stránka pak ukáže, že data jsou stará.
- Okamžité vypnutí: ve Vercelu proměnná `PUBLIC_OFF=1` (stránka přestane ukazovat data) nebo na PC smazat `GW_VEREJNE_*`.
- Odesílají se jen výkony, stav baterie, denní energie a ceny. Nikdy IP adresa, ovládání ani osobní soubory.
