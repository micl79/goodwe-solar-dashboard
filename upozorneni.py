"""
Upozornění na iPhone (Web Push). Nepovinné: bez balíčku pywebpush dashboard běží dál, jen bez upozornění.

Soubory vedle skriptu (osobní, v .gitignore):
  push_klic.json        - klíč, kterým dashboard podepisuje upozornění (vytvoří se sám při prvním spuštění)
  push_odberatele.json  - telefony, které si upozornění zapnuly

Co se oznamuje: baterie je plná, nabíjení z dashboardu skončilo, měnič je 10 minut nedostupný a zase online,
jednou denně souhrn zítřejších spotových cen (nejlevnější hodiny).
"""
import base64
import json
import os
import threading
import time

try:
    from pywebpush import WebPushException, webpush
    from py_vapid import Vapid02
    from cryptography.hazmat.primitives import serialization
    OK = True
except Exception:  # noqa: BLE001 - balíček není nainstalovaný, upozornění zůstanou vypnutá
    OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "push_klic.json")
SUBS_PATH = os.path.join(HERE, "push_odberatele.json")
STAV_PATH = os.path.join(HERE, "push_stav.json")
CLAIMS = {"sub": "mailto:dashboard@example.com"}   # povinný údaj protokolu, skutečná adresa se nikam neposílá
OFFLINE_AFTER_S = 600

_lock = threading.Lock()
_vapid = None
_pub = None
_st = {"last_ok": time.time(), "offline_sent": False, "armed": None}


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def init():
    """Načte nebo vytvoří klíč. Vrací True, když upozornění fungují."""
    global _vapid, _pub
    if not OK:
        return False
    try:
        if os.path.exists(KEY_PATH):
            pem = json.load(open(KEY_PATH, encoding="utf-8"))["pem"].encode()
            _vapid = Vapid02.from_pem(pem)
        else:
            _vapid = Vapid02()
            _vapid.generate_keys()
            pem = _vapid.private_pem()
            with open(KEY_PATH, "w", encoding="utf-8") as f:
                json.dump({"pem": pem.decode()}, f)
        _pub = _b64u(_vapid.public_key.public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint))
        return True
    except Exception:  # noqa: BLE001
        return False


def public_key():
    return _pub


def _load():
    try:
        return json.load(open(SUBS_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(subs):
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump(subs, f)


def count():
    with _lock:
        return len(_load())


def subscribe(sub, lang="cs"):
    if not (isinstance(sub, dict) and isinstance(sub.get("endpoint"), str) and sub["endpoint"].startswith("https://")
            and isinstance(sub.get("keys"), dict) and sub["keys"].get("p256dh") and sub["keys"].get("auth")):
        raise ValueError("Neplatné přihlášení k upozorněním")
    with _lock:
        subs = [s for s in _load() if s["endpoint"] != sub["endpoint"]]
        subs.append({"endpoint": sub["endpoint"], "lang": "en" if lang == "en" else "cs",
                     "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}})
        _save(subs)


def unsubscribe(endpoint):
    with _lock:
        _save([s for s in _load() if s["endpoint"] != endpoint])


def _send_all(title, body, tag, en=None):
    with _lock:
        subs = _load()
    dead, sent = [], 0
    for s in subs:
        try:
            t, b = en if (en and s.get("lang") == "en") else (title, body)    # jazyk, který si telefon zvolil
            webpush(s, json.dumps({"title": t, "body": b, "tag": tag}), vapid_private_key=_vapid,
                    vapid_claims=dict(CLAIMS), ttl=3600, timeout=15)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):      # telefon upozornění zrušil
                dead.append(s["endpoint"])
        except Exception:  # noqa: BLE001
            pass
    if dead:
        with _lock:
            _save([s for s in _load() if s["endpoint"] not in dead])
    return sent


def notify(title, body, tag=None, wait=False, en=None):
    """Pošle upozornění všem přihlášeným telefonům. Standardně na pozadí, ať nezdržuje čtení měniče.
    en = (titulek, text) v angličtině pro telefony, které mají zvolenou angličtinu."""
    if not (OK and _vapid):
        return 0
    if wait:
        return _send_all(title, body, tag, en)
    threading.Thread(target=_send_all, args=(title, body, tag, en), daemon=True).start()
    return 0


# --- sledování událostí; volá se při každém čtení měniče ---
def tick(soc, now=None, send=None):
    """soc = stav baterie v %, nebo None, když se měnič nepodařilo přečíst."""
    now = time.time() if now is None else now
    send = send or notify
    if soc is None:
        if not _st["offline_sent"] and now - _st["last_ok"] > OFFLINE_AFTER_S:
            _st["offline_sent"] = True
            send("Měnič je nedostupný", "Dashboard ho nemůže přečíst už 10 minut.", "offline",
                 en=("Inverter unavailable", "The dashboard has not been able to read it for 10 minutes."))
        return
    _st["last_ok"] = now
    if _st["offline_sent"]:
        _st["offline_sent"] = False
        send("Měnič je zase online", "Spojení s měničem funguje.", "offline",
             en=("Inverter is back online", "The connection to the inverter works."))
    if _st["armed"] is None:                 # první čtení po startu: neoznamuj, jen se nastav
        _st["armed"] = soc < 90
    elif soc < 90:
        _st["armed"] = True
    elif soc >= 100 and _st["armed"]:
        _st["armed"] = False
        send("Baterie je plná", "Stav baterie dosáhl 100 %.", "plna",
                 en=("Battery full", "The battery level has reached 100%."))


# --- výpadek a návrat sítě; volá se při každém čtení měniče ---
GRID_DEBOUNCE_S = 30        # krátké poklesy napětí se neoznamují
CAPACITY_KWH = 10.66        # Dyness Tower T10 (jmenovitě)
RESERVE_PCT = 10            # pod touto hodnotou se s baterií nepočítá


def odhad_hodin(soc, load_w, capacity=CAPACITY_KWH):
    """Kolik hodin vydrží baterie při dané spotřebě (hrubý odhad), nebo None."""
    if soc is None or not load_w or load_w < 50:
        return None
    return max(0.0, (soc - RESERVE_PCT) / 100 * capacity / (load_w / 1000))


def _hm(h):
    m = int(round(h * 60))
    return (f"{m // 60} h {m % 60:02d} min", f"{m // 60} h {m % 60:02d} min")


def siet(ok, soc, load_w, now=None, send=None):
    """ok: True = měnič je připojen k síti, False = síť vypadla, None = neznámo (nic se neděje)."""
    now = time.time() if now is None else now
    send = send or notify
    if ok is None:
        return
    s = _st
    if not ok:
        s.setdefault("grid_bad_since", now)
        if s["grid_bad_since"] is None:
            s["grid_bad_since"] = now
        if not s.get("grid_down") and now - s["grid_bad_since"] >= GRID_DEBOUNCE_S:
            s["grid_down"] = True
            s["grid_down_at"] = s["grid_bad_since"]
            h = odhad_hodin(soc, load_w)
            cs = f"Baterie {round(soc)} %." if soc is not None else ""
            en = f"Battery {round(soc)}%." if soc is not None else ""
            if h is not None:
                cs += f" Při současné spotřebě vydrží zhruba {_hm(h)[0]}."
                en += f" At the current consumption it lasts about {_hm(h)[1]}."
            send("Výpadek sítě", cs.strip() or "Měnič nevidí síť.", "siet",
                 en=("Grid outage", en.strip() or "The inverter does not see the grid."))
        return
    s["grid_bad_since"] = None
    if s.get("grid_down"):
        s["grid_down"] = False
        dur = max(0.0, now - s.get("grid_down_at", now)) / 3600
        send("Síť je zpět", f"Výpadek trval asi {_hm(dur)[0]}.", "siet",
             en=("Grid is back", f"The outage lasted about {_hm(dur)[1]}."))


# --- souhrn zítřejších cen ---
def _kc(v):
    return f"{v:.2f}".replace(".", ",")


def souhrn_cen(vals, okno=3, lang="cs"):
    """Text upozornění z hodinových cen (Kč/kWh): nejlevnější okno hodin po sobě, nejdražší hodina, záporné ceny."""
    n = len(vals)
    okno = min(okno, n)
    best = min(range(n - okno + 1), key=lambda i: sum(vals[i:i + okno]))
    prum = sum(vals[best:best + okno]) / okno
    drahe = max(range(n), key=lambda i: vals[i])
    zap = [i for i, v in enumerate(vals) if v <= 0]
    hodiny = ", ".join(f"{i}–{i + 1} h" for i in zap[:4])
    if lang == "en":
        s = (f"Cheapest {okno} hours in a row: {best}–{best + okno} h, average {prum:.2f} CZK/kWh. "
             f"Most expensive hour: {drahe}–{drahe + 1} h ({vals[drahe]:.2f} CZK).")
        return s + (f" Negative price: {hodiny}." if zap else "")
    s = f"Nejlevnější {okno} hodiny po sobě: {best}–{best + okno} h, průměr {_kc(prum)} Kč/kWh. Nejdražší hodina: {drahe}–{drahe + 1} h ({_kc(vals[drahe])} Kč)."
    return s + (f" Záporná cena: {hodiny}." if zap else "")


def ceny_zitra(den, vals, send=None):
    """Jednou za den (podle data, i po restartu) pošle souhrn cen. den = 'RRRR-MM-DD' zítřka."""
    send = send or notify
    try:
        last = json.load(open(STAV_PATH, encoding="utf-8")).get("ceny_zitra")
    except (OSError, ValueError):
        last = None
    if last is not None and last >= den:
        return False
    send("Zítřejší ceny elektřiny", souhrn_cen(vals), "ceny",
         en=("Tomorrow’s electricity prices", souhrn_cen(vals, lang="en")))
    with open(STAV_PATH, "w", encoding="utf-8") as f:
        json.dump({"ceny_zitra": den}, f)
    return True
