"""
Veřejná výloha: funkce pro Vercel (Python).

  POST /api/snapshot              domácí PC pošle aktuální data (vyžaduje tajný klíč PUSH_TOKEN)
  GET  /api/snapshot              vrátí poslední data a historii pro graf (to čte stránka)
  GET  /api/snapshot?co=ceny      vrátí spotové ceny
  GET  /api/snapshot?co=stats     vrátí analýzu energie (týden, měsíc, rok)

Data jsou v Redisu (Upstash, přidává se k projektu ve Vercelu). Nic jiného se neukládá.
Proměnné prostředí: PUSH_TOKEN, KV_REST_API_URL + KV_REST_API_TOKEN (nebo UPSTASH_REDIS_REST_URL/TOKEN),
volitelně PUBLIC_OFF=1 (stránka přestane ukazovat data).
"""
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
        co = (parse_qs(urlparse(self.path).query).get("co") or [""])[0]
        if os.environ.get("PUBLIC_OFF"):
            return self._send(200, {"now": None, "hist": [], "err": "Stránka je dočasně vypnutá."} if co != "ceny"
                              else {"today": None, "days": {}, "kurz": None, "marze": 0, "err": "vypnuto", "ts": None})
        try:
            if co == "stats":
                (r,) = redis([["GET", "solar:stats"]])
                return self._send(200, json.loads(r) if r else {"periods": {}, "since": None, "outage": None},
                                  "public, s-maxage=60, stale-while-revalidate=300")
            if co == "ceny":
                (r,) = redis([["GET", "solar:ceny"]])
                return self._send(200, json.loads(r) if r else {"today": None, "days": {}, "kurz": None, "marze": 0,
                                                                "err": "Ceny zatím nejsou.", "ts": None},
                                  "public, s-maxage=60, stale-while-revalidate=300")
            now, hist = redis([["GET", "solar:now"], ["LRANGE", "solar:hist", "0", str(HIST_MAX - 1)]])
            if not now:
                return self._send(200, {"now": None, "hist": [], "err": "Zatím žádná data."}, "public, s-maxage=10")
            self._send(200, {"now": json.loads(now), "hist": [json.loads(h) for h in reversed(hist or [])], "err": None},
                       "public, s-maxage=10, stale-while-revalidate=30")
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
