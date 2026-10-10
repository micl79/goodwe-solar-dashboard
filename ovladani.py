"""
Ovládání měniče GoodWe z dashboardu.

Každá změna: kontrola mezí, záloha původní hodnoty (jednou za skupinu), zápis, přečtení zpět, záznam do souboru.
Zapisuje se jen to, co je tady výslovně vyjmenováno. Ostrovní režim (OFF_GRID) schválně chybí.

Soubory vedle skriptu:
  ovladani_zaloha.json  - původní hodnoty z měniče před první změnou (podle nich funguje "Vrátit zpět")
  ovladani.log          - záznam, co a kdy se změnilo (kód se nikdy nezapisuje)

Přístupový kód se bere z proměnné prostředí GW_PIN. Bez ní je ovládání vypnuté.
"""
import datetime as dt
import hmac
import json
import os
import threading
import time

from goodwe import OperationMode

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_PATH = os.path.join(HERE, "ovladani_zaloha.json")
LOG_PATH = os.path.join(HERE, "ovladani.log")

PIN = os.environ.get("GW_PIN", "")

# kód režimu v měniči -> (název v knihovně, český popis)
MODE_NAMES = {0: "GENERAL", 2: "BACKUP", 3: "ECO", 4: "PEAK_SHAVING", 5: "SELF_USE", 1: "OFF_GRID"}
MODES = {  # povolené režimy (OFF_GRID je záměrně vynechán)
    "GENERAL": OperationMode.GENERAL,
    "ECO": OperationMode.ECO,
    "SELF_USE": OperationMode.SELF_USE,
    "PEAK_SHAVING": OperationMode.PEAK_SHAVING,
    "BACKUP": OperationMode.BACKUP,
}
MIN_SOC = (10, 50)          # nejnižší stav baterie při vybíjení, %
EXPORT_W = (0, 10000)       # limit prodeje do sítě, W
CHARGE_SOC = (20, 100)      # cílový stav při nabíjení, %
CHARGE_POWER = (10, 100)    # výkon nabíjení, % maxima
MAX_CHARGE_S = 8 * 3600     # nabíjení se po této době samo vrátí zpět

# co se při změně zálohuje; pořadí = pořadí obnovy (work_mode až nakonec)
_MODE_IDS = ["eco_mode_1", "eco_mode_2_switch", "eco_mode_3_switch", "eco_mode_4_switch", "work_mode"]
GROUPS = {
    "charge": _MODE_IDS,    # stav těsně před nabíjením (na něj se vrací konec nabíjení)
    "mode": _MODE_IDS,      # stav před první změnou režimu (na něj se vrací "Vrátit na původní")
    "min_soc": ["battery_discharge_depth"],
    "export": ["grid_export_limit"],
}

oznam = None   # volitelná funkce (titulek, text, značka) pro upozornění na telefon; nastavuje dashboard

_pin_lock = threading.Lock()
_fails = {"n": 0, "until": 0.0}
_b = {"groups": {}, "charge": None}


# ---------------------------------------------------------------------------
# přístupový kód
# ---------------------------------------------------------------------------
def enabled():
    return bool(PIN)


def check_pin(pin):
    """(ok, zpráva). Po 5 chybách za sebou se ovládání na 5 minut zamkne."""
    if not PIN:
        return False, "Ovládání je vypnuté (není nastaven přístupový kód GW_PIN)."
    with _pin_lock:
        now = time.time()
        if now < _fails["until"]:
            return False, f"Příliš mnoho chybných pokusů. Zkuste to za {int(_fails['until'] - now) + 1} s."
        if isinstance(pin, str) and hmac.compare_digest(pin.encode("utf-8"), PIN.encode("utf-8")):
            _fails["n"] = 0
            return True, ""
        _fails["n"] += 1
        if _fails["n"] >= 5:
            _fails["n"], _fails["until"] = 0, now + 300
        return False, "Chybný přístupový kód."


# ---------------------------------------------------------------------------
# záloha a záznam
# ---------------------------------------------------------------------------
def load_backup():
    try:
        with open(BACKUP_PATH, encoding="utf-8") as f:
            d = json.load(f)
        _b["groups"] = d.get("groups", {})
        _b["charge"] = d.get("charge")
    except (OSError, ValueError):
        pass


def _save():
    with open(BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(_b, f, ensure_ascii=False, indent=1)


def _log(who, action, detail):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}\t{who}\t{action}\t{detail}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# nízká úroveň: přesné bajty registru (záloha a obnova beze ztráty)
# ---------------------------------------------------------------------------
async def _raw(inv, setting_id):
    s = inv._settings[setting_id]
    count = (s.size_ + (s.size_ % 2)) // 2
    resp = await inv._read_from_socket(inv._read_command(s.offset, count))
    return resp.response_data().hex()


async def _raw_write(inv, setting_id, hexval):
    s = inv._settings[setting_id]
    await inv._read_from_socket(inv._write_multi_command(s.offset, bytes.fromhex(hexval)))


async def _snap(inv, setting_id):
    """Eco plán 1 (12 bajtů) se zálohuje přesně po bajtech. Jednoduchá nastavení po hodnotě: některá sdílejí
    2bajtový registr se sousedním nastavením a obnova celého registru by sousedovi přepsala bajt."""
    if setting_id == "eco_mode_1":
        return {"raw": await _raw(inv, setting_id)}
    return {"v": int(await inv.read_setting(setting_id))}


async def _put(inv, setting_id, item):
    if "raw" in item:
        await _raw_write(inv, setting_id, item["raw"])
    else:
        await inv.write_setting(setting_id, item["v"])


async def _human(inv, group):
    """Čitelná hodnota skupiny pro zobrazení."""
    if group in ("mode", "charge"):
        return MODE_NAMES.get(await inv.read_setting("work_mode"), "?")
    if group == "min_soc":
        return int(await inv.read_setting("battery_discharge_depth"))
    return int(await inv.read_setting("grid_export_limit"))


async def _take(inv, group):
    """Před první změnou skupiny uloží původní hodnoty. Když už záloha je, nechá ji být."""
    if group in _b["groups"]:
        return
    vals = {sid: await _snap(inv, sid) for sid in GROUPS[group]}
    _b["groups"][group] = {"vals": vals, "puvodne": await _human(inv, group), "cas": time.time()}
    _save()


async def _restore(inv, group):
    if group == "mode" and "charge" in _b["groups"]:
        await _restore(inv, "charge")     # nejdřív skonči nabíjení, pak původní režim
    b = _b["groups"].get(group)
    if not b:
        return False
    for sid in GROUPS[group]:
        await _put(inv, sid, b["vals"][sid])
    del _b["groups"][group]
    if group == "charge":
        _b["charge"] = None
    _save()
    return True


# ---------------------------------------------------------------------------
# veřejné funkce (spouští je smyčka, která drží spojení s měničem)
# ---------------------------------------------------------------------------
async def status(inv):
    return {
        "work_mode": MODE_NAMES.get(await inv.read_setting("work_mode"), "?"),
        "min_soc": int(await inv.read_setting("battery_discharge_depth")),
        "export_limit": int(await inv.read_setting("grid_export_limit")),
        "puvodne": {g: b["puvodne"] for g, b in _b["groups"].items()},
        "charge": _b["charge"],
    }


def _num(v, lo_hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: neplatná hodnota")
    if not lo_hi[0] <= v <= lo_hi[1]:
        raise ValueError(f"{name}: povoleno je {lo_hi[0]} až {lo_hi[1]}")
    return v


async def execute(inv, action, params, who, soc=None):
    params = params or {}
    if action == "overit":
        return await status(inv)

    if action == "mode":
        name = params.get("mode")
        if name not in MODES:
            raise ValueError("Neznámý režim")
        if "charge" in _b["groups"]:
            await _restore(inv, "charge")     # změna režimu ukončí případné nabíjení
        await _take(inv, "mode")
        await inv.set_operation_mode(MODES[name])
        _save()
        _log(who, "režim", name)

    elif action == "charge":
        target = _num(params.get("target"), CHARGE_SOC, "Cílový stav")
        power = _num(params.get("power"), CHARGE_POWER, "Výkon")
        if soc is not None and soc >= target:
            raise ValueError(f"Baterie už je na {int(soc)} %, cíl je {target} %")
        if "charge" in _b["groups"]:
            await _restore(inv, "charge")
        await _take(inv, "charge")
        await inv.set_operation_mode(OperationMode.ECO_CHARGE, power, target)
        _b["charge"] = {"target": target, "power": power, "t0": time.time()}
        _save()
        _log(who, "nabíjení", f"cíl {target} %, výkon {power} %")

    elif action == "min_soc":
        v = _num(params.get("value"), MIN_SOC, "Nejnižší stav baterie")
        await _take(inv, "min_soc")
        await inv.set_ongrid_battery_dod(100 - v)
        _log(who, "nejnižší stav baterie", f"{v} %")

    elif action == "export":
        v = _num(params.get("value"), EXPORT_W, "Limit prodeje")
        await _take(inv, "export")
        await inv.set_grid_export_limit(v)
        _log(who, "limit prodeje", f"{v} W")

    elif action == "revert":
        g = params.get("group")
        groups = [x for x in GROUPS if x in _b["groups"]] if g == "all" else [g]
        if g != "all" and g not in GROUPS:
            raise ValueError("Neznámá skupina")
        done = [x for x in groups if await _restore(inv, x)]
        if not done:
            raise ValueError("Není co vracet, původní hodnoty nejsou uložené")
        _log(who, "zpět na původní", ",".join(done))

    else:
        raise ValueError("Neznámá akce")

    return await status(inv)


async def tick(inv, soc):
    """Volá se při každém čtení měniče. Když nabíjení dosáhlo cíle nebo trvá moc dlouho, vrátí režim zpět."""
    c = _b.get("charge")
    if not c:
        return
    if soc >= c["target"]:
        reason = f"cíl {c['target']} % dosažen"
    elif time.time() - c["t0"] > MAX_CHARGE_S:
        reason = "uplynul časový limit 8 h"
    else:
        return
    await _restore(inv, "charge")
    _log("automaticky", "nabíjení skončilo, režim vrácen zpět", reason)
    if oznam:
        oznam("Nabíjení skončilo", f"{reason[0].upper()}{reason[1:]}. Režim měniče je vrácený zpět.", "nabijeni")
