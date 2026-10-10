"""
Nabíjení baterie v nejlevnějších hodinách (podle hodinových spotových cen).

Postup: v zadaném časovém okně (např. 22:00 až 6:00) se vybere tolik nejlevnějších hodin, kolik je potřeba k dosažení
cílového stavu baterie. V těch hodinách se zapne nabíjení (stejnou cestou jako ruční "Nabít baterii", včetně zálohy a
návratu zpět) a po skončení hodiny se vrátí původní režim.

Plán se nevytvoří, když: je baterie nad cílem, nejsou ceny, žádná hodina není pod cenovým stropem, nebo rozdíl proti
cenám po skončení okna nepokryje ztráty baterie (nabíjení a vybíjení ztrácí asi 10 %).

Nastavení a stav jsou v nabijeni_stav.json (osobní, v .gitignore).
"""
import datetime as dt
import json
import math
import os

import ovladani

HERE = os.path.dirname(os.path.abspath(__file__))
STAV_PATH = os.path.join(HERE, "nabijeni_stav.json")

DEFAULTS = {"enabled": False, "target": 50, "start": 22, "end": 6, "cap_kc": 1.5, "spread": 0.3, "kapacita": 10.0}
P_EFF_KW = 4.5          # výkon nabíjení, se kterým se počítá (měřené maximum 5,4 kW, BMS omezuje na zhruba 5,9 kW)
EFF = 0.9               # účinnost nabití a vybití baterie dohromady
REF_HOURS = 16          # srovnávací ceny: 16 hodin po skončení okna (to, co by se jinak nakoupilo)
REPLAN_MIN = 30         # plán se přepočítá nejdřív po 30 minutách

_s = {"settings": dict(DEFAULTS), "plan": None, "planned_at": None, "done": [], "started": None, "notified": None}


# ---------------------------------------------------------------------------
# nastavení
# ---------------------------------------------------------------------------
def clean_settings(d):
    """Zkontroluje nastavení z rozhraní. Chyby jsou česky (klient je přeloží)."""
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        raise ValueError("Neplatné nastavení")
    out["enabled"] = bool(d.get("enabled", out["enabled"]))
    for k, (lo, hi, name, typ) in {"target": (20, 100, "Cílový stav", int), "start": (0, 23, "Začátek okna", int), "end": (0, 23, "Konec okna", int),
                                   "cap_kc": (0.2, 10.0, "Cenový strop", float), "spread": (0.0, 5.0, "Minimální rozdíl", float),
                                   "kapacita": (3.0, 30.0, "Kapacita baterie", float)}.items():
        if k not in d:
            continue
        try:
            v = typ(d[k])
        except (TypeError, ValueError):
            raise ValueError(f"{name}: neplatná hodnota")
        if not lo <= v <= hi:
            raise ValueError(f"{name}: povoleno je {lo} až {hi}")
        out[k] = v
    if (out["end"] - out["start"]) % 24 < 2:
        raise ValueError("Okno musí mít aspoň 2 hodiny")
    return out


# ---------------------------------------------------------------------------
# výpočet plánu (čistá funkce, nezávislá na měniči)
# ---------------------------------------------------------------------------
def window_instance(now, start, end):
    """První okno (začátek, konec), které ještě neskončilo."""
    length = (end - start) % 24
    for k in (-1, 0, 1):
        s = dt.datetime.combine(now.date() + dt.timedelta(days=k), dt.time(start))
        e = s + dt.timedelta(hours=length)
        if e > now:
            return s, e


def _price(prices, t):
    day = prices.get(t.date().isoformat())
    return day[t.hour] if day and len(day) == 24 else None


def make_plan(prices, now, soc, st):
    """prices: {'RRRR-MM-DD': [24 cen v Kč/kWh]}, now: místní čas, soc: stav baterie v %, st: nastavení."""
    if not st["enabled"]:
        return {"stav": "vypnuto"}
    need_kwh = max(0.0, st["target"] - soc) / 100 * st["kapacita"]
    if need_kwh < 0.3:
        return {"stav": "netreba", "soc": round(soc)}
    s, e = window_instance(now, st["start"], st["end"])
    slots, missing = [], False
    t = s
    while t < e:
        end_t = t + dt.timedelta(hours=1)
        if end_t > now and not (t < now and end_t - now < dt.timedelta(minutes=30)):
            p = _price(prices, t)
            if p is None:
                missing = True
            else:
                slots.append({"start": t, "cena": p})
        t = end_t
    if missing or not slots:
        return {"stav": "ceka"}
    hours = math.ceil(need_kwh / P_EFF_KW)
    cand = [x for x in slots if x["cena"] <= st["cap_kc"]]
    if not cand:
        return {"stav": "zadne", "nejlevnejsi": round(min(x["cena"] for x in slots), 2)}
    chosen = sorted(cand, key=lambda x: (x["cena"], x["start"]))[:hours]
    chosen.sort(key=lambda x: x["start"])
    avg = sum(x["cena"] for x in chosen) / len(chosen)
    ref_prices = [p for p in (_price(prices, e + dt.timedelta(hours=i)) for i in range(REF_HOURS)) if p is not None]
    ref = sum(ref_prices) / len(ref_prices) if ref_prices else None
    if ref is not None and ref - avg / EFF < st["spread"]:
        return {"stav": "nevyplati", "cena": round(avg, 2), "ref": round(ref, 2)}
    blocks = []
    for x in chosen:
        if blocks and blocks[-1]["_end"] == x["start"]:
            blocks[-1]["_end"] += dt.timedelta(hours=1)
            blocks[-1]["_ceny"].append(x["cena"])
        else:
            blocks.append({"_start": x["start"], "_end": x["start"] + dt.timedelta(hours=1), "_ceny": [x["cena"]]})
    out = []
    for b in blocks:
        out.append({"id": b["_start"].strftime("%Y-%m-%dT%H"), "start": b["_start"].isoformat(timespec="minutes"),
                    "end": b["_end"].isoformat(timespec="minutes"), "cena": round(sum(b["_ceny"]) / len(b["_ceny"]), 2)})
    return {"stav": "naplanovano", "bloky": out, "hodin": len(chosen), "cena": round(avg, 2), "ref": None if ref is None else round(ref, 2),
            "kwh": round(min(need_kwh, len(chosen) * P_EFF_KW), 1), "castecne": len(chosen) < hours}


# ---------------------------------------------------------------------------
# uložení a pohled pro rozhraní
# ---------------------------------------------------------------------------
def load():
    try:
        d = json.load(open(STAV_PATH, encoding="utf-8"))
        _s["settings"] = clean_settings(d.get("settings", {}))
        _s["plan"] = d.get("plan")
        _s["planned_at"] = d.get("planned_at")
        _s["done"] = list(d.get("done", []))[-30:]
        _s["started"] = d.get("started")
        _s["notified"] = d.get("notified")
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
            _s["started"] = None
            save()
        return
    if _s["started"]:
        if not charge:                                  # skončilo (cíl, konec hodiny nebo ruční zrušení)
            _s["done"].append(_s["started"])
            _s["started"] = None
            _s["plan"] = None
            save()
        return
    last = dt.datetime.fromisoformat(_s["planned_at"]) if _s["planned_at"] else None
    if _s["plan"] is None or last is None or (now - last).total_seconds() >= REPLAN_MIN * 60:
        _s["plan"] = make_plan(prices, now, soc, st)
        _s["planned_at"] = now.isoformat(timespec="seconds")
        save()
        p = _s["plan"]
        if p.get("stav") == "naplanovano" and notify:
            sig = ",".join(b["id"] for b in p["bloky"])
            if sig != _s["notified"]:
                _s["notified"] = sig
                save()
                b0 = p["bloky"][0]
                hm = lambda iso: dt.datetime.fromisoformat(iso).strftime("%H:%M")
                cs = f"{hm(b0['start'])}–{hm(b0['end'])}, průměrná cena {str(p['cena']).replace('.', ',')} Kč/kWh"
                en = f"{hm(b0['start'])}–{hm(b0['end'])}, average price {p['cena']} CZK/kWh"
                notify("Nabíjení naplánováno", cs + (f" (a další {len(p['bloky']) - 1} hodin. bloků)" if len(p["bloky"]) > 1 else ""), "plan",
                       en=("Charging planned", en + (f" (plus {len(p['bloky']) - 1} more blocks)" if len(p["bloky"]) > 1 else "")))
    p = _s["plan"]
    if not (p and p.get("stav") == "naplanovano") or charge:
        return
    for b in p["bloky"]:
        if b["id"] in _s["done"]:
            continue
        bs, be = dt.datetime.fromisoformat(b["start"]), dt.datetime.fromisoformat(b["end"])
        if bs <= now < be and soc < st["target"] - 1:
            await ovladani.execute(inv, "charge", {"target": st["target"], "power": 100}, "plán", soc, until=be.timestamp())
            _s["started"] = b["id"]
            ovladani._log("plán", "nabíjení v nejlevnější hodině", f"{b['start']}–{b['end']}, {b['cena']} Kč/kWh, cíl {st['target']} %")
            save()
            break
