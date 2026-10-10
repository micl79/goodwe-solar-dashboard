"""
Odesílání živých dat na veřejnou stránku (Vercel). Vypnuto, dokud nejsou nastavené proměnné prostředí:

  GW_VEREJNE_URL       adresa funkce, např. https://moje-domena.cz/api/snapshot
  GW_VEREJNE_TOKEN     tajný klíč (stejný jako PUSH_TOKEN ve Vercelu; nikdy ho nezapisujte do souborů)
  GW_VEREJNE_INTERVAL  sekund mezi odesláními (výchozí 30)
  GW_VEREJNE_ZPOZDENI  zpoždění zveřejněných dat v sekundách (výchozí 0 = živě; třeba 3600 = hodinu zpátky)

Odesílá se jen: výkony, stav baterie, denní energie, spotové ceny a analýza energie (součty po dnech a měsících). Žádná IP adresa, ovládání ani osobní soubory.
"""
import collections
import json
import os
import threading
import time
import urllib.request

URL = os.environ.get("GW_VEREJNE_URL", "").strip()
TOKEN = os.environ.get("GW_VEREJNE_TOKEN", "").strip()
EVERY = max(10.0, float(os.environ.get("GW_VEREJNE_INTERVAL", "30")))
DELAY = max(0.0, min(float(os.environ.get("GW_VEREJNE_ZPOZDENI", "0")), 20 * 3600))
POINT_EVERY = 60.0     # do grafu na stránce se přidává jeden bod za minutu
STATS_EVERY = 300.0    # analýza energie (týden, měsíc, rok) se posílá jednou za 5 minut
KEYS = ("ts", "model", "pv", "load", "grid", "bat", "soc", "e_day", "e_load_day", "bat_ch_day", "bat_dis_day")

_buf = collections.deque()
_lock = threading.Lock()
_st = {"ok": None, "last_point": 0.0, "last_stats": 0.0}


def enabled():
    local = URL.startswith("http://127.0.0.1") or URL.startswith("http://localhost")
    return bool(TOKEN and (URL.startswith("https://") or local))


def feed(now):
    """Zavolá se při každém přečtení měniče. Drží krátkou paměť, aby šlo data zpozdit."""
    if not enabled():
        return
    item = {k: now.get(k) for k in KEYS}
    with _lock:
        _buf.append(item)
        limit = time.time() - DELAY - 120
        while _buf and _buf[0]["ts"] < limit:
            _buf.popleft()


def _pick(now_t=None):
    now_t = time.time() if now_t is None else now_t
    with _lock:
        ok = [x for x in _buf if x["ts"] <= now_t - DELAY]
        return ok[-1] if ok else None


def payload(get_prices, get_stats=None, now_t=None):
    snap = _pick(now_t)
    if snap is None:
        return None
    out = {"now": snap, "ceny": get_prices()}
    if snap["ts"] - _st["last_point"] >= POINT_EVERY - 5:
        out["point"] = [snap["ts"], snap["pv"], snap["load"], snap["grid"], snap["bat"], snap["soc"]]
    if get_stats and time.time() - _st["last_stats"] >= STATS_EVERY:
        out["stats"] = get_stats()
    return out


def _post(body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _loop(get_prices, get_stats):
    while True:
        try:
            body = payload(get_prices, get_stats)
            if body:
                _post(body)
                if "point" in body:
                    _st["last_point"] = body["point"][0]
                if "stats" in body:
                    _st["last_stats"] = time.time()
                if _st["ok"] is not True:
                    print("Veřejná stránka: odesílání dat funguje.", flush=True)
                _st["ok"] = True
        except Exception as e:  # noqa: BLE001 - výpadek internetu nesmí shodit dashboard
            if _st["ok"] is not False:
                print(f"Veřejná stránka: odeslání se nepodařilo ({type(e).__name__}), zkusím znovu.", flush=True)
            _st["ok"] = False
        time.sleep(EVERY)


def start(get_prices, get_stats=None):
    """Spustí odesílání na pozadí. Vrací text pro výpis při startu."""
    if not (URL or TOKEN):
        return "vypnuta (nenastaveno GW_VEREJNE_URL a GW_VEREJNE_TOKEN)"
    if not enabled():
        return "vypnuta (adresa musí začínat https:// a musí být nastaven klíč GW_VEREJNE_TOKEN)"
    threading.Thread(target=_loop, args=(get_prices, get_stats), daemon=True).start()
    return f"zapnuta, každých {int(EVERY)} s" + (f", zpoždění {int(DELAY)} s" if DELAY else ", živě")
