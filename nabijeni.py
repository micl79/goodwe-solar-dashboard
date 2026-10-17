"""
Nabíjení baterie v nejlevnějším bloku hodin (podle hodinových spotových cen).

Postup: v příštích 24 hodinách se najde nejlevnější SOUVISLÝ blok hodin (výchozí 3 hodiny) a v něm se baterie dobije
na cílový stav. Nabíjení se zapne stejnou cestou jako ruční "Nabít baterii" (záloha původního stavu, návrat zpět) a po
dosažení cíle nebo na konci bloku se vrátí původní režim. Mezi dvěma nabíjeními je aspoň 12 hodin, takže se nabíjí
nejvýš jednou denně. Žádný cenový strop se nepoužívá: nabíjí se vždy v nejlevnějším bloku, který je k dispozici.

Plán se nevytvoří, když je baterie už na cíli, nebo když ještě nejsou ceny pro celý blok.

Nastavení a stav jsou v nabijeni_stav.json (osobní, v .gitignore).
"""
import datetime as dt
import json
import math
import os

import ovladani

HERE = os.path.dirname(os.path.abspath(__file__))
STAV_PATH = os.path.join(HERE, "nabijeni_stav.json")

DEFAULTS = {"enabled": False, "target": 100, "hodin": 3, "kapacita": 10.0, "vykon": 100}
P_EFF_KW = 4.5          # výkon nabíjení, se kterým se počítá (měřené maximum 5,4 kW, BMS omezuje na zhruba 5,9 kW)
MIN_GAP_H = 12          # nejkratší odstup mezi dvěma nabíjeními
HORIZON_H = 24          # kam dopředu se nejlevnější blok hledá
REPLAN_MIN = 30         # plán se přepočítá nejdřív po 30 minutách

_s = {"settings": dict(DEFAULTS), "plan": None, "planned_at": None, "done": [], "started": None, "started_end": None,
      "last_end": None, "notified": None}
HOUR = dt.timedelta(hours=1)


# ---------------------------------------------------------------------------
# nastavení
# ---------------------------------------------------------------------------
def clean_settings(d):
    """Zkontroluje nastavení z rozhraní. Chyby jsou česky (klient je přeloží). Neznámé klíče se ignorují."""
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        raise ValueError("Neplatné nastavení")
    out["enabled"] = bool(d.get("enabled", out["enabled"]))
    for k, (lo, hi, name, typ) in {"target": (20, 100, "Cílový stav", int), "hodin": (1, 8, "Počet hodin", int),
                                   "kapacita": (3.0, 30.0, "Kapacita baterie", float),
                                   "vykon": (10, 100, "Výkon nabíjení", int)}.items():
        if k not in d:
            continue
        try:
            v = typ(d[k])
        except (TypeError, ValueError):
            raise ValueError(f"{name}: neplatná hodnota")
        if not lo <= v <= hi:
            raise ValueError(f"{name}: povoleno je {lo} až {hi}")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# výpočet plánu (čistá funkce, nezávislá na měniči)
# ---------------------------------------------------------------------------
def _price(prices, t):
    day = prices.get(t.date().isoformat())
    return day[t.hour] if day and len(day) == 24 else None


def _ceil_hour(t):
    h = t.replace(minute=0, second=0, microsecond=0)
    return h if h == t else h + HOUR


def make_plan(prices, now, soc, st, last_end=None):
    """prices: {'RRRR-MM-DD': [24 cen v Kč/kWh]}, now: místní čas, soc: stav baterie v %, st: nastavení,
    last_end: konec posledního nabíjení (odstup aspoň 12 hodin)."""
    if not st["enabled"]:
        return {"stav": "vypnuto"}
    need_kwh = max(0.0, st["target"] - soc) / 100 * st["kapacita"]
    if need_kwh < 0.3:
        return {"stav": "netreba", "soc": round(soc)}
    start = now.replace(minute=0, second=0, microsecond=0)
    if now - start > dt.timedelta(minutes=30):          # aktuální hodina se použije, jen když zbývá aspoň 30 minut
        start += HOUR
    if last_end:
        start = max(start, _ceil_hour(last_end + dt.timedelta(hours=MIN_GAP_H)))
    end = start + dt.timedelta(hours=HORIZON_H)
    by = {}
    t = start
    while t < end:
        p = _price(prices, t)
        if p is not None:
            by[t] = p
        t += HOUR
    n = st["hodin"]
    best = None
    for t in sorted(by):
        hrs = [t + i * HOUR for i in range(n)]
        if all(h in by for h in hrs):
            tot = sum(by[h] for h in hrs)
            if best is None or tot < best[0] - 1e-9:      # při shodě vyhrává dřívější blok
                best = (tot, t)
    if best is None:
        return {"stav": "ceka"}
    cap = n * P_EFF_KW * st.get("vykon", 100) / 100      # kolik se do baterie v bloku vejde při zvoleném výkonu
    s0 = best[1]
    e0 = s0 + n * HOUR
    avg = best[0] / n
    return {"stav": "naplanovano", "hodin": n, "cena": round(avg, 2),
            "bloky": [{"id": s0.strftime("%Y-%m-%dT%H"), "start": s0.isoformat(timespec="minutes"),
                       "end": e0.isoformat(timespec="minutes"), "cena": round(avg, 2)}],
            "kwh": round(min(need_kwh, cap), 1), "castecne": need_kwh > cap}


# ---------------------------------------------------------------------------
# uložení a pohled pro rozhraní
# ---------------------------------------------------------------------------
def load():
    try:
        d = json.load(open(STAV_PATH, encoding="utf-8"))
        _s["settings"] = clean_settings(d.get("settings", {}))
        for k in ("plan", "planned_at", "started", "started_end", "last_end", "notified"):
            _s[k] = d.get(k)
        _s["done"] = list(d.get("done", []))[-30:]
    except (OSError, ValueError):
        pass


def save():
    with open(STAV_PATH, "w", encoding="utf-8") as f:
        json.dump(_s, f, ensure_ascii=False, indent=1)


def view():
    plan = _s["plan"]
    if plan and plan.get("stav") == "naplanovano":
        plan = dict(plan, bloky=[dict(b, hotovo=b["id"] in _s["done"]) for b in plan["bloky"]])
        if all(b["hotovo"] for b in plan["bloky"]):
            plan = dict(plan, stav="hotovo")
    return {"settings": _s["settings"], "plan": plan, "started": _s["started"]}


def set_settings(d):
    """Uloží nastavení z rozhraní. Plán se přepočítá při dalším čtení měniče."""
    _s["settings"] = clean_settings(d)
    _s["plan"] = None
    _s["planned_at"] = None
    save()
    return view()


# ---------------------------------------------------------------------------
# provádění plánu (volá se při každém čtení měniče)
# ---------------------------------------------------------------------------
def _den(now, iso):
    d = dt.datetime.fromisoformat(iso)
    delta = (d.date() - now.date()).days
    return ("", "") if delta == 0 else ("zítra ", "tomorrow ") if delta == 1 else (d.strftime("%d.%m. "), d.strftime("%d/%m "))


async def tick(inv, soc, prices, now=None, notify=None):
    """prices: {'datum': [24 cen v Kč/kWh]}. Spouští a ukončuje nabíjení podle plánu."""
    now = now or dt.datetime.now()
    st = _s["settings"]
    charge = ovladani._b.get("charge")
    if not st["enabled"]:
        if _s["started"]:                               # plán se vypnul za běhu: nabíjení z plánu skončí
            if charge:
                await ovladani._restore(inv, "charge")
                ovladani._log("plán", "plán vypnut, nabíjení zrušeno", "")
            _s["started"] = _s["started_end"] = None
            save()
        return
    if _s["started"]:
        if not charge:                                  # skončilo (cíl, konec bloku nebo ruční zrušení)
            _s["done"].append(_s["started"])
            be = dt.datetime.fromisoformat(_s["started_end"]) if _s["started_end"] else now
            _s["last_end"] = min(be, now).isoformat(timespec="seconds")
            _s["started"] = _s["started_end"] = None
            _s["plan"] = None
            save()
        return
    last = dt.datetime.fromisoformat(_s["planned_at"]) if _s["planned_at"] else None
    if _s["plan"] is None or last is None or (now - last).total_seconds() >= REPLAN_MIN * 60:
        le = dt.datetime.fromisoformat(_s["last_end"]) if _s["last_end"] else None
        _s["plan"] = make_plan(prices, now, soc, st, le)
        _s["planned_at"] = now.isoformat(timespec="seconds")
        save()
        p = _s["plan"]
        if p.get("stav") == "naplanovano" and notify:
            sig = p["bloky"][0]["id"]
            if sig != _s["notified"]:
                _s["notified"] = sig
                save()
                b0 = p["bloky"][0]
                hm = lambda iso: dt.datetime.fromisoformat(iso).strftime("%H:%M")
                d_cs, d_en = _den(now, b0["start"])
                notify("Nabíjení naplánováno",
                       f"{d_cs}{hm(b0['start'])}–{hm(b0['end'])}, průměrná cena {str(p['cena']).replace('.', ',')} Kč/kWh", "plan",
                       en=("Charging planned", f"{d_en}{hm(b0['start'])}–{hm(b0['end'])}, average price {p['cena']} CZK/kWh"))
    p = _s["plan"]
    if not (p and p.get("stav") == "naplanovano") or charge:
        return
    for b in p["bloky"]:
        if b["id"] in _s["done"]:
            continue
        bs, be = dt.datetime.fromisoformat(b["start"]), dt.datetime.fromisoformat(b["end"])
        if bs <= now < be and soc < st["target"] - 1:
            await ovladani.execute(inv, "charge", {"target": st["target"], "power": st.get("vykon", 100)}, "plán", soc, until=be.timestamp())
            _s["started"], _s["started_end"] = b["id"], b["end"]
            ovladani._log("plán", "nabíjení v nejlevnějším bloku", f"{b['start']}–{b['end']}, {b['cena']} Kč/kWh, cíl {st['target']} %")
            save()
            break
