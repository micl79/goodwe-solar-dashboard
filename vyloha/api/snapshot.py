"""
Veřejná výloha: funkce pro Vercel (Python).

  POST /api/snapshot              domácí PC pošle aktuální data (vyžaduje tajný klíč PUSH_TOKEN)
  GET  /api/snapshot              vrátí poslední data a historii pro graf (to čte stránka)
  GET  /api/snapshot?co=ceny      vrátí spotové ceny
  GET  /api/snapshot?co=stats     vrátí analýzu energie (týden, měsíc, rok)

Data jsou v Redisu (Upstash, přidává se k projektu ve Vercelu). Nic jiného se neukládá.
Proměnné prostředí: PUSH_TOKEN, KV_REST_API_URL + KV_REST_API_TOKEN (nebo UPSTASH_REDIS_REST_URL/TOKEN),
volitelně PUBLIC_OFF=1 (stránka přestane ukazovat data) a FORCE_DEMO=1 (ukazuje se jen virtuální elektrárna).
Když domácí PC pošle data do 10 minut, ukazují se skutečná, jinak virtuální (simulace) a ceny si funkce stáhne sama.
"""
import datetime as _dt
import hmac
import json
import math
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HIST_MAX = 1440      # 24 h po minutě
MAX_BODY = 100000

FIELDS = {"pv": (0, 100000), "load": (0, 100000), "grid": (-100000, 100000), "bat": (-100000, 100000),
          "soc": (0, 100), "e_day": (0, 5000), "e_load_day": (0, 5000), "bat_ch_day": (0, 5000), "bat_dis_day": (0, 5000)}


def redis(cmds):
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not (url and tok):
        raise RuntimeError("Chybí připojení k Redisu")
    req = urllib.request.Request(url.rstrip("/") + "/pipeline", data=json.dumps(cmds).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        out = json.loads(r.read())
    for o in out:
        if isinstance(o, dict) and o.get("error"):
            raise RuntimeError("Redis: " + str(o["error"])[:100])
    return [o.get("result") for o in out]


def _f(v, lo, hi, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{name}: neplatná hodnota")
    return float(v)


def clean_now(d, now=None):
    """Z přijatých dat vezme jen vyjmenovaná pole a zkontroluje meze."""
    now = time.time() if now is None else now
    if not isinstance(d, dict):
        raise ValueError("now: očekává se objekt")
    ts = _f(d.get("ts"), now - 86400, now + 300, "ts")
    if ts is None:
        raise ValueError("ts chybí")
    out = {"ts": ts, "model": str(d.get("model") or "")[:30]}
    for k, (lo, hi) in FIELDS.items():
        out[k] = _f(d.get(k), lo, hi, k)
    if out["soc"] is None or out["pv"] is None:
        raise ValueError("chybí soc nebo pv")
    return out


def clean_point(p):
    if not (isinstance(p, list) and len(p) == 6):
        raise ValueError("point: očekává se 6 čísel")
    r = [_f(p[0], 1e9, 4e9, "point.ts")]
    for i, (a, b) in enumerate([(0, 100000), (0, 100000), (-100000, 100000), (-100000, 100000), (0, 100)], 1):
        v = _f(p[i], a, b, "point")
        if v is None:
            raise ValueError("point: prázdná hodnota")
        r.append(v)
    return r


def clean_prices(d):
    if not isinstance(d, dict):
        raise ValueError("ceny: očekává se objekt")
    date = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    today = d.get("today")
    if not (isinstance(today, str) and date.match(today)):
        raise ValueError("ceny.today")
    days = {}
    for k, v in (d.get("days") or {}).items():
        if len(days) >= 3 or not date.match(str(k)) or not isinstance(v, list) or not 20 <= len(v) <= 26:
            raise ValueError("ceny.days")
        days[k] = [_f(x, -10000, 100000, "cena") for x in v]
    err = d.get("err")
    return {"today": today, "days": days, "kurz": _f(d.get("kurz"), 1, 1000, "kurz"),
            "marze": _f(d.get("marze") or 0, -100, 100, "marze"),
            "err": str(err)[:200] if err else None, "ts": _f(d.get("ts"), 0, 4e9, "ts")}


def _plain(v, depth=0):
    """Analýza smí obsahovat jen čísla, krátké texty, seznamy a objekty."""
    if depth > 6:
        raise ValueError("stats: moc hluboké")
    if v is None or isinstance(v, (bool, int)):
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("stats: neplatné číslo")
        return
    if isinstance(v, str):
        if len(v) > 40:
            raise ValueError("stats: dlouhý text")
        return
    if isinstance(v, list):
        if len(v) > 500:
            raise ValueError("stats: dlouhý seznam")
        for x in v:
            _plain(x, depth + 1)
        return
    if isinstance(v, dict):
        if len(v) > 50:
            raise ValueError("stats: moc klíčů")
        for k, x in v.items():
            if not isinstance(k, str) or len(k) > 30:
                raise ValueError("stats: klíč")
            _plain(x, depth + 1)
        return
    raise ValueError("stats: nepovolený typ")


def clean_stats(d):
    if not (isinstance(d, dict) and isinstance(d.get("periods"), dict)):
        raise ValueError("stats: očekává se objekt s periods")
    _plain(d)
    if len(json.dumps(d)) > 80000:
        raise ValueError("stats: moc velké")
    return d


def store(body, now=None):
    """Přijme data z domácího PC a uloží je. Vrací seznam příkazů pro Redis."""
    cmds = [["SET", "solar:now", json.dumps(clean_now(body.get("now"), now))]]
    if body.get("ceny") is not None:
        cmds.append(["SET", "solar:ceny", json.dumps(clean_prices(body["ceny"]))])
    if body.get("stats") is not None:
        cmds.append(["SET", "solar:stats", json.dumps(clean_stats(body["stats"]))])
    if body.get("point") is not None:
        cmds.append(["LPUSH", "solar:hist", json.dumps(clean_point(body["point"]))])
        cmds.append(["LTRIM", "solar:hist", "0", str(HIST_MAX - 1)])
    return cmds



# ---------------------------------------------------------------------------
# Virtuální data pro dobu, kdy je domácí PC vypnuté (nebo když je FORCE_DEMO=1)
# ---------------------------------------------------------------------------
DEMO_AFTER_S = 600            # starší data než 10 minut = domácí PC je offline, ukáže se simulace
CAP_WH = 10000.0              # virtuální baterie
LAT = math.radians(49.8)
OTE_URL = "https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/denni-trh/chart-data?report_date="
CNB_URL = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"


def _utc(ts):
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).replace(tzinfo=None)


def _last_sunday(year, month):
    d = _dt.date(year, month, 31)           # březen i říjen mají 31 dní
    while d.weekday() != 6:
        d -= _dt.timedelta(days=1)
    return d


def prague_offset_h(ts):
    """1 h v zimě, 2 h v létě (EU: od poslední neděle v březnu do poslední neděle v říjnu, vždy 01:00 UTC)."""
    u = _utc(ts)
    start = _dt.datetime.combine(_last_sunday(u.year, 3), _dt.time(1))
    end = _dt.datetime.combine(_last_sunday(u.year, 10), _dt.time(1))
    return 2 if start <= u < end else 1


def _noise(x):
    """Hladký pseudonáhodný šum 0 až 1. Stejný vstup dá stejný výsledek, takže všichni návštěvníci vidí totéž."""
    def r(k):
        v = math.sin(k * 12.9898 + 78.233) * 43758.5453
        return v - math.floor(v)
    i = math.floor(x)
    f = x - i
    f = f * f * (3 - 2 * f)
    return r(i) * (1 - f) + r(i + 1) * f


def _pv_w(ts):
    u = _utc(ts)
    doy = u.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + doy) / 365)
    ha = math.radians(15 * (u.hour + u.minute / 60 - 11.04))      # sluneční poledne ve 14,4° v. d. je v 11:04 UTC
    cosz = math.sin(LAT) * math.sin(decl) + math.cos(LAT) * math.cos(decl) * math.cos(ha)
    if cosz <= 0:
        return 0.0
    den = 0.35 + 0.65 * _noise(doy * 1.7 + 3)                      # jak je ten den oblačný
    mraky = 0.4 + 0.6 * _noise(ts / 1500.0 + doy)                  # oblačnost během dne
    return 8200.0 * cosz ** 1.2 * den * mraky


def _load_w(ts):
    h = (ts / 3600.0 + prague_offset_h(ts)) % 24

    def g(c, w):
        return math.exp(-((h - c) / w) ** 2)
    return max(180.0, 300 + 520 * g(7.3, 1.0) + 260 * g(12.8, 1.4) + 950 * g(19.2, 2.0) + 320 * (_noise(ts / 600.0) - 0.5))


def simulate(now, minutes=HIST_MAX):
    """Virtuální elektrárna: vrací (aktuální stav, historie po minutě za posledních `minutes`).
    Počítá se bez uložení stavu: od půlnoci před dvěma dny, po minutě."""
    off = prague_offset_h(now)
    midnight = (now + off * 3600) // 86400 * 86400 - off * 3600       # dnešní půlnoc (pražského času) jako UTC
    t = midnight - 2 * 86400
    soc = 40.0
    cnt = [0.0, 0.0, 0.0, 0.0]            # kWh od půlnoci: výroba, spotřeba, nabito, vybito
    pts, last = [], None
    keep_from = now - minutes * 60
    while t <= now:
        if (t + prague_offset_h(t) * 3600) % 86400 < 60:
            cnt = [0.0, 0.0, 0.0, 0.0]
        pv, load = _pv_w(t), _load_w(t)
        surplus = pv - load
        if surplus >= 0:
            ch = min(surplus, 3500.0, (100.0 - soc) / 100 * CAP_WH * 60)
            soc += ch / 60.0 / CAP_WH * 100 * 0.96
            dis, grid = 0.0, -(surplus - ch)                               # přebytek jde do sítě (záporné)
        else:
            need = -surplus
            dis = min(need, 4000.0, (soc - 15.0) / 100 * CAP_WH * 60)
            soc -= dis / 60.0 / CAP_WH * 100
            ch, grid = 0.0, need - dis                                     # zbytek se nakoupí (kladné)
        bat = dis - ch                                                     # + vybíjení, − nabíjení
        cnt[0] += pv / 60000.0
        cnt[1] += load / 60000.0
        cnt[2] += ch / 60000.0
        cnt[3] += dis / 60000.0
        if t >= keep_from:
            pts.append([round(t, 1), round(pv), round(load), round(grid), round(bat), round(soc, 1)])
        last = (pv, load, grid, bat, soc, list(cnt))
        t += 60
    pv, load, grid, bat, soc, c = last
    cur = {"ts": float(now), "model": "Demo", "pv": round(pv), "load": round(load), "grid": round(grid), "bat": round(bat),
           "soc": round(soc, 1), "e_day": round(c[0], 2), "e_load_day": round(c[1], 2), "bat_ch_day": round(c[2], 2),
           "bat_dis_day": round(c[3], 2)}
    return cur, pts


def _http(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "solar-vyloha"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def prague_today(now):
    return _dt.datetime.fromtimestamp(now + prague_offset_h(now) * 3600, _dt.timezone.utc).date()


def fetch_prices(now):
    """Burzovní ceny z OTE a kurz ČNB přímo z Vercelu (veřejná data), když je domácí PC vypnuté."""
    today = prague_today(now)
    days = {}
    for d in (today, today + _dt.timedelta(days=1)):
        try:
            raw = json.loads(_http(OTE_URL + d.isoformat()).decode("utf-8"))
            for line in raw["data"]["dataLine"]:
                if str(line.get("title", "")).startswith("60min"):
                    pts = [float(p["y"]) for p in line.get("point", [])]
                    if pts and len(pts) % 4 == 0:
                        days[d.isoformat()] = pts[::4]
        except Exception:  # noqa: BLE001
            pass
    kurz = None
    try:
        for line in _http(CNB_URL).decode("cp1250").splitlines():
            p = line.split("|")
            if len(p) > 4 and p[3] == "EUR":
                kurz = float(p[4].replace(",", ".")) / float(p[2].replace(",", "."))
    except Exception:  # noqa: BLE001
        pass
    return {"today": today.isoformat(), "days": days, "kurz": kurz, "marze": 0,
            "err": None if days else "OTE zatím nevrátilo žádné ceny", "ts": float(now)}


def pick_prices(stored, auto_cached, now, fetch=fetch_prices):
    """Vrátí (ceny, nové ceny k uložení do cache nebo None). Ceny z domácího PC mají přednost, když jsou dnešní a čerstvé."""
    today = prague_today(now).isoformat()
    if stored and stored.get("today") == today and (stored.get("days") or {}).get(today) and stored.get("ts") \
            and now - stored["ts"] < 6 * 3600:
        return stored, None
    if auto_cached and auto_cached.get("today") == today and (auto_cached.get("days") or {}).get(today):
        return auto_cached, None
    got = fetch(now)
    if got["days"]:
        return got, got
    return (stored or got), None


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, cache="no-store"):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        co = (q.get("co") or [""])[0]
        force = bool(os.environ.get("FORCE_DEMO")) or (q.get("demo") or [""])[0] == "1"
        if os.environ.get("PUBLIC_OFF"):
            return self._send(200, {"now": None, "hist": [], "err": "Stránka je dočasně vypnutá."} if co != "ceny"
                              else {"today": None, "days": {}, "kurz": None, "marze": 0, "err": "vypnuto", "ts": None})
        try:
            if co == "stats":
                (r,) = redis([["GET", "solar:stats"]])
                return self._send(200, json.loads(r) if r else {"periods": {}, "since": None, "outage": None},
                                  "public, s-maxage=60, stale-while-revalidate=300")
            if co == "ceny":
                r, auto = redis([["GET", "solar:ceny"], ["GET", "solar:ceny_auto"]])
                prices, to_cache = pick_prices(json.loads(r) if r else None, json.loads(auto) if auto else None, time.time())
                if to_cache:
                    redis([["SET", "solar:ceny_auto", json.dumps(to_cache), "EX", "900"]])
                if not prices.get("days"):
                    prices = dict(prices, err=prices.get("err") or "Ceny zatím nejsou.")
                return self._send(200, prices, "public, s-maxage=60, stale-while-revalidate=300")
            real = None
            if not force:
                now, hist = redis([["GET", "solar:now"], ["LRANGE", "solar:hist", "0", str(HIST_MAX - 1)]])
                real = json.loads(now) if now else None
            if real and time.time() - real["ts"] <= DEMO_AFTER_S:
                return self._send(200, {"now": real, "hist": [json.loads(h) for h in reversed(hist or [])], "err": None, "demo": False},
                                  "public, s-maxage=10, stale-while-revalidate=30")
            cur, pts = simulate(time.time())                      # domácí PC je vypnuté: virtuální elektrárna
            self._send(200, {"now": cur, "hist": pts, "err": None, "demo": True}, "public, s-maxage=10, stale-while-revalidate=30")
        except Exception:  # noqa: BLE001
            self._send(502, {"now": None, "hist": [], "err": "Data se teď nepodařilo načíst."})

    def do_POST(self):
        token = os.environ.get("PUSH_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if len(token) < 20 or not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
            return self._send(401, {"ok": False})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= MAX_BODY:
                raise ValueError("velikost")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("tělo")
            cmds = store(body)
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"ok": False, "error": str(e)[:100]})
        try:
            redis(cmds)
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"ok": False, "error": str(e)[:100]})
        self._send(200, {"ok": True})
