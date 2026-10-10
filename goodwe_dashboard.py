"""
Živý dashboard pro měnič GoodWe GW10K-ET.

Spuštění:   python goodwe_dashboard.py      (nebo dvojklik na solar.bat)
Otevři:     http://localhost:8080
Ukončení:   Ctrl+C v okně s příkazovou řádkou

Skript jen ČTE data z měniče (nic v něm nemění). Ukládá vedle sebe dva soubory:
  goodwe_historie.csv   - výkony po 5 s (pro živý graf)
  goodwe_pocitadla.csv  - počítadla energií po 5 min (pro týdenní/měsíční/roční analýzu)
"""
import asyncio
import calendar
import csv
import datetime as dt
import concurrent.futures
import json
import math
import os
import queue
import struct
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import goodwe

import ovladani
import upozorneni
import verejne

IP = os.environ.get("GW_IP", "192.168.68.113")
PORT = int(os.environ.get("GW_PORT", "8080"))
INTERVAL = float(os.environ.get("GW_INTERVAL", "5"))   # sekund mezi čteními
COUNTER_EVERY = float(os.environ.get("GW_COUNTER_EVERY", "300"))  # sekund mezi uložením počítadel
KEEP_SECONDS = 24 * 3600                                # historie v živém grafu
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "goodwe_historie.csv")
CNT_PATH = os.path.join(HERE, "goodwe_pocitadla.csv")
SEMS_PATH = os.path.join(HERE, "goodwe_sems.csv")   # historie ze SEMS (vytvoří sems_import.py)

state = {"now": None, "hist": [], "err": None, "cnt": []}
lock = threading.Lock()

# --- spotové ceny (burza OTE, kurz ČNB) ---
OTE_URL = "https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/denni-trh/chart-data?report_date="
CNB_URL = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
MARZE = float(os.environ.get("GW_MARZE", "0"))          # přirážka dodavatele v Kč/kWh (výchozí 0 = čistá burza)
PRICE_EVERY = 900                                       # sekund mezi stažením cen
prices = {"days": {}, "kurz": None, "err": None, "ts": None}


def prices_payload():
    with lock:
        return {"today": dt.date.today().isoformat(), "days": dict(prices["days"]), "kurz": prices["kurz"],
                "marze": MARZE, "err": prices["err"], "ts": prices["ts"]}


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "goodwe-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_day(day):
    """Hodinové ceny OTE v EUR/MWh pro daný den, nebo None, když je ještě nezveřejnila."""
    raw = json.loads(http_get(OTE_URL + day.isoformat()).decode("utf-8"))
    for line in raw["data"]["dataLine"]:
        if str(line.get("title", "")).startswith("60min"):
            pts = [float(p["y"]) for p in line.get("point", [])]
            if pts and len(pts) % 4 == 0:
                return pts[::4]          # řada je po čtvrthodinách, hodinová cena se opakuje 4x
    return None


def fetch_kurz():
    for line in http_get(CNB_URL).decode("cp1250").splitlines():
        p = line.split("|")
        if len(p) > 4 and p[3] == "EUR":
            return float(p[4].replace(",", ".")) / float(p[2].replace(",", "."))
    return None


def price_thread():
    while True:
        wait = PRICE_EVERY
        today = dt.date.today()
        days, err = {}, None
        for d in (today, today + dt.timedelta(days=1)):
            try:
                h = fetch_day(d)
                if h:
                    days[d.isoformat()] = h
            except Exception as e:
                if d == today:
                    err = f"{type(e).__name__}: {e}"
        try:
            kurz = fetch_kurz()
        except Exception:
            kurz = None
        with lock:
            if days:
                prices["days"] = days
            else:   # při výpadku nech jen dnešek a budoucnost, včerejší ceny by byly zavádějící
                prices["days"] = {k: v for k, v in prices["days"].items() if k >= today.isoformat()}
            if kurz:
                prices["kurz"] = kurz
            prices["err"] = None if prices["days"] else (err or "OTE zatím nevrátilo žádné ceny")
            prices["ts"] = time.time()
        with lock:
            kurz_ted = prices["kurz"]
            zitra = prices["days"].get((today + dt.timedelta(days=1)).isoformat())
        if zitra and kurz_ted:
            try:
                upozorneni.ceny_zitra((today + dt.timedelta(days=1)).isoformat(), [e / 1000 * kurz_ted + MARZE for e in zitra])
            except Exception:  # noqa: BLE001 - upozornění nesmí shodit stahování cen
                pass
        if not days:
            wait = 300
        time.sleep(wait)


# --- příkazy pro měnič: handler je jen zařadí do fronty, provádí je smyčka poll(), která už má spojení ---
cmd_q = queue.Queue()
CMD_VALID_S = 25      # po této době se nevyřízený příkaz zahodí a nic se nezmění


def run_cmd(fn, timeout=40):
    fut = concurrent.futures.Future()
    cmd_q.put((fn, fut, time.time() + CMD_VALID_S))
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise RuntimeError("Měnič neodpověděl včas. Nevím, jestli se změna provedla, zkontrolujte stav.")


def fail_pending(msg):
    while True:
        try:
            _, fut, _ = cmd_q.get_nowait()
        except queue.Empty:
            return
        fut.set_exception(RuntimeError(msg))


async def run_control(inv, soc):
    try:
        await ovladani.tick(inv, soc)
    except Exception as e:  # noqa: BLE001
        ovladani._log("automaticky", "CHYBA při návratu zpět", str(e))
    while True:
        try:
            fn, fut, deadline = cmd_q.get_nowait()
        except queue.Empty:
            return
        if time.time() > deadline:
            fut.set_exception(RuntimeError("Příkaz vypršel dřív, než ho měnič mohl provést. Nic se nezměnilo."))
            continue
        try:
            fut.set_result(await fn(inv, soc))
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)


def read_csv(path, ncols, min_ts=0.0):
    out = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                try:
                    r = [float(x) for x in row[:ncols]]
                except ValueError:
                    continue
                if len(r) == ncols and r[0] >= min_ts:
                    out.append(r)
    except OSError:
        pass
    return out


def append_csv(path, row):
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
    except OSError:
        pass


def load_history():
    state["hist"] = read_csv(CSV_PATH, 6, time.time() - KEEP_SECONDS)
    state["cnt"] = read_csv(CNT_PATH, 7)


async def poll():
    inv = None
    last_cnt = state["cnt"][-1][0] if state["cnt"] else 0
    while True:
        try:
            if inv is None:
                inv = await goodwe.connect(IP)
            d = await inv.read_runtime_data()
            g = d.get
            ts = time.time()
            pv = float(g("ppv", 0) or 0)
            load = float(g("house_consumption", 0) or 0)
            grid = -float(g("active_power_total", 0) or 0)   # + odběr ze sítě, - dodávka
            bat = float(g("pbattery1", 0) or 0)              # + vybíjení, - nabíjení
            soc = float(g("battery_soc", 0) or 0)
            now = {
                "ts": ts, "pv": pv, "load": load, "grid": grid, "bat": bat, "soc": soc,
                "e_day": g("e_day"), "e_load_day": g("e_load_day"),
                "bat_ch_day": g("e_bat_charge_day"), "bat_dis_day": g("e_bat_discharge_day"),
                "e_total": g("e_total"), "model": inv.model_name,
                "bat_mode": g("battery_mode_label"), "work_mode": g("work_mode_label"),
                "temp": g("temperature"),
            }
            row = [round(ts, 1), pv, load, grid, bat, soc]
            cnt = None
            if ts - last_cnt >= COUNTER_EVERY:
                vals = [g("e_total"), g("e_load_total"), g("meter_e_total_imp"),
                        g("meter_e_total_exp"), g("e_bat_charge_total"), g("e_bat_discharge_total")]
                if all(v is not None for v in vals):
                    cnt = [round(ts, 1)] + [float(v) for v in vals]
                    last_cnt = ts
            with lock:
                state["now"] = now
                state["err"] = None
                state["hist"].append(row)
                limit = ts - KEEP_SECONDS
                while state["hist"] and state["hist"][0][0] < limit:
                    state["hist"].pop(0)
                if cnt:
                    state["cnt"].append(cnt)
            append_csv(CSV_PATH, row)
            if cnt:
                append_csv(CNT_PATH, cnt)
            verejne.feed(now)
            upozorneni.tick(soc)
            await run_control(inv, soc)
        except Exception as e:  # noqa: BLE001 - chceme přežít jakýkoli výpadek
            with lock:
                state["err"] = str(e) or type(e).__name__
            inv = None
            upozorneni.tick(None)
            fail_pending("Měnič teď není dostupný. Nic se nezměnilo.")
        await asyncio.sleep(INTERVAL)


def poll_thread():
    asyncio.run(poll())


# ---------------------------------------------------------------------------
# Analýza: rozdíly počítadel energií (výroba, spotřeba, nákup, prodej, baterie)
# pořadí hodnot v:  0 výroba | 1 spotřeba | 2 nákup ze sítě | 3 prodej do sítě | 4 nabito | 5 vybito
# ---------------------------------------------------------------------------
def load_sems():
    """Historie ze SEMS: {'d': {datum: v}, 'm': {měsíc: v}, 'y': {rok: v}}, v = [výroba, spotřeba, nákup, prodej, nabito, vybito]."""
    out = {"d": {}, "m": {}, "y": {}}
    if not os.path.exists(SEMS_PATH):
        return out
    try:
        with open(SEMS_PATH, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                try:
                    kind, key = row[0], row[1]
                    pv, imp, exp, chg, dis = [float(x) for x in row[2:7]]
                except (ValueError, IndexError):
                    continue
                load = max(0.0, pv + imp + dis - chg - exp)   # spotřeba se ve SEMS exportu nenachází, dopočítáno
                if kind in out:
                    out[kind][key] = [pv, load, imp, exp, chg, dis]
    except OSError:
        pass
    return out


def estimate_outage(merged, weights, life):
    """Odhad za výpadek dat ve SEMS (dny bez dat nebo samé nuly po sobě).
    Chybějící energii spočítá jako rozdíl celoživotních počítadel měniče a součtu známých dat
    a rozdělí ji podle sezónního průběhu stejných kalendářních měsíců z ostatních let."""
    monthly_cover = {k[:7] for k in merged if weights.get(k, 1) > 1}
    dated = sorted(k for k in merged if weights.get(k, 1) == 1)
    nonzero = [k for k in dated if sum(merged[k]) > 0.001]
    if not nonzero or not life:
        return {}, None
    d, end = dt.date.fromisoformat(nonzero[0]), dt.date.today()
    runs, cur = [], []
    while d <= end:
        k = d.isoformat()
        empty = (k[:7] not in monthly_cover) and (k not in merged or sum(merged[k]) <= 0.001)
        if empty:
            cur.append(d)
        else:
            if len(cur) >= 3:
                runs.append(cur)
            cur = []
        d += dt.timedelta(days=1)
    if len(cur) >= 3 and cur[-1] < end:       # dnešní den se nepočítá (ještě neskončil)
        runs.append(cur)
    out_days = [x for r in runs for x in r]
    if not out_days:
        return {}, None
    out_months = {x.isoformat()[:7] for x in out_days}
    # sezónní váhy: průměrná denní hodnota pro daný kalendářní měsíc z "čistých" měsíců
    tot, cnt = {}, {}
    for k, v in merged.items():
        m = k[:7]
        if m in out_months:
            continue
        tot.setdefault(m, [0.0] * 6)
        cnt[m] = cnt.get(m, 0) + weights.get(k, 1)
        for i in range(6):
            tot[m][i] += v[i]
    cal = {}
    for m in tot:
        y, mo = int(m[:4]), int(m[5:7])
        if cnt[m] < calendar.monthrange(y, mo)[1] * 0.9:
            continue
        for i in range(6):
            cal.setdefault((mo, i), []).append(tot[m][i] / cnt[m])
    def w(day, i):
        vals = cal.get((day.month, i))
        return sum(vals) / len(vals) if vals else 1.0
    known = [sum(v[i] for v in merged.values()) for i in range(6)]
    gap = [max(0.0, life[i] - known[i]) for i in range(6)]
    est = {x.isoformat(): [0.0] * 6 for x in out_days}
    for i in (0, 2, 3, 4, 5):
        ws = [w(x, i) for x in out_days]
        sw = sum(ws) or 1.0
        for x, wx in zip(out_days, ws):
            est[x.isoformat()][i] = gap[i] * wx / sw
    for k, v in est.items():
        v[1] = max(0.0, v[0] + v[2] + v[5] - v[4] - v[3])      # spotřeba dopočtená
        for i in range(6):
            v[i] = round(v[i], 3)
    info = {"from": out_days[0].isoformat(), "to": out_days[-1].isoformat(), "days": len(out_days),
            "gap": [round(g, 1) for g in gap]}
    return est, info


def compute_stats():
    with lock:
        rows = list(state["cnt"])
    sems = load_sems()
    if not rows and not sems["d"] and not sems["m"] and not sems["y"]:
        return {"since": None, "periods": {}}
    days = {}
    for r in rows:
        key = time.strftime("%Y-%m-%d", time.localtime(r[0]))
        if key not in days:
            days[key] = [r[1:], r[1:]]
        days[key][1] = r[1:]
    local = {}
    prev = None
    for key in sorted(days):
        first, last = days[key]
        base = prev if prev else first
        local[key] = [max(0.0, round(last[i] - base[i], 3)) for i in range(6)]
        prev = last
    # SEMS má přednost tam, kde pokrývá celý den; počítadla měniče doplňují novější dny
    merged = dict(local)
    merged.update(sems["d"])
    have_months = {k[:7] for k in merged}
    weights = {}                     # kolik dní reprezentuje položka
    for k in merged:
        weights[k] = 1
    for m, v in sems["m"].items():
        if m not in have_months:
            y, mo = int(m[:4]), int(m[5:7])
            k = f"{m}-15"
            merged[k] = v
            weights[k] = calendar.monthrange(y, mo)[1]
    est_keys, outage = set(), None
    if rows:
        est, outage = estimate_outage(merged, weights, rows[-1][1:])
        for k, v in est.items():
            merged[k] = v
            weights[k] = 1
        est_keys = set(est)
    daily = sorted(merged.items())
    since_candidates = [rows[0][0]] if rows else []
    for k in list(sems["d"]) + list(sems["m"]):
        since_candidates.append(time.mktime(time.strptime(k[:7] + "-01", "%Y-%m-%d")) if len(k) == 7 else time.mktime(time.strptime(k, "%Y-%m-%d")))
    for k in sems["y"]:
        since_candidates.append(time.mktime(time.strptime(k + "-01-01", "%Y-%m-%d")))
    since = min(since_candidates) if since_candidates else None

    def group(items, n):
        agg, dd, ee = {}, {}, {}
        for key, v in items:
            m = key[:n]
            agg.setdefault(m, [0.0] * 6)
            wt = weights.get(key, 1)
            dd[m] = dd.get(m, 0) + wt
            if key in est_keys:
                ee[m] = ee.get(m, 0) + wt
            for i in range(6):
                agg[m][i] += v[i]
        return [{"l": m, "v": [round(x, 2) for x in agg[m]], "e": round(ee.get(m, 0) / dd[m], 2)} for m in sorted(agg)]

    def months(items):
        return group(items, 7)

    today = dt.date.today()
    periods = {}
    monthly_keys = {k for k in weights if weights[k] > 1}
    for name, n in (("7", 7), ("30", 30), ("365", 365)):
        if n > 31:
            # Rok = posledních 12 kalendářních měsíců včetně aktuálního
            y, mo = today.year, today.month - 11
            while mo < 1:
                y, mo = y - 1, mo + 12
            cutoff = dt.date(y, mo, 1).isoformat()
            items = [(k, v) for k, v in daily if k >= cutoff]
            want = (today - dt.date(y, mo, 1)).days + 1
        else:
            cutoff = (today - dt.timedelta(days=n - 1)).isoformat()
            items = [(k, v) for k, v in daily if k >= cutoff and k not in monthly_keys]
            want = n
        tot = [round(sum(v[i] for _, v in items), 2) for i in range(6)]
        bars = months(items) if n > 31 else [{"l": k, "v": [round(x, 2) for x in v], "e": 1.0 if k in est_keys else 0.0} for k, v in items]
        est_tot = [round(sum(v[i] for k, v in items if k in est_keys), 1) for i in range(6)]
        est_days = sum(weights.get(k, 1) for k, _ in items if k in est_keys)
        ndays = min(want, sum(weights[k] for k, _ in items))
        first = next((k for k, _ in items if k not in monthly_keys), None)
        if n > 31 and items:
            first = items[0][0] if items[0][0] not in monthly_keys else items[0][0][:7] + "-01"
        periods[name] = {"tot": tot, "bars": bars, "days": ndays, "want": want, "first": first, "est": est_tot, "estDays": est_days}
    if rows:
        life = [round(x, 2) for x in rows[-1][1:]]
    elif sems["y"]:
        life = [round(sum(v[i] for v in sems["y"].values()), 2) for i in range(6)]
    else:
        life = [round(sum(v[i] for _, v in daily), 2) for i in range(6)]
    if est_keys or not sems["y"]:
        bars_all = group(daily, 4) if (sems["y"] or est_keys) else months(daily)
    else:
        bars_all = [{"l": y, "v": [round(x, 2) for x in sems["y"][y]], "e": 0.0} for y in sorted(sems["y"])]
    periods["all"] = {"tot": life, "bars": bars_all, "days": len(daily), "want": 0,
                      "yearly": bool(sems["y"]), "est": [0] * 6, "estDays": 0}
    return {"since": since, "periods": periods, "outage": outage}


HTML = r"""<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#060709">
<script>(function(){try{var p=localStorage.getItem('theme')||'auto';var l=p==='light'||(p==='auto'&&matchMedia('(prefers-color-scheme: light)').matches);document.documentElement.dataset.theme=l?'light':'dark';}catch(e){}})();</script>
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Solár">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>Solární elektrárna</title>
<style>
:root{color-scheme:dark;
--goldgrad:linear-gradient(135deg,#f0d9a6,#d6b370 55%,#9c7a3c);
--card-bg:linear-gradient(155deg,#15171c 0%,#0c0d10 60%,#0a0b0e 100%);
--card-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 30px 60px -28px rgba(0,0,0,.85),0 0 0 1px rgba(0,0,0,.6);
--tile:linear-gradient(160deg,rgba(255,255,255,.04),rgba(255,255,255,.008));--tipbg:rgba(14,15,18,.94);--scroll:#2a2c31;
--bg:#060709;--surface:#101216;--surface2:#0a0b0e;--ink:#f3efe6;--ink2:#b9b4a8;--muted:#7d796f;
--grid:rgba(255,255,255,.07);--line:rgba(214,179,112,.28);--gold:#d6b370;--gold2:#f0d9a6;--gold3:#9c7a3c;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--good:#58c98a;--bad:#f2837c}
:root[data-theme="light"]{color-scheme:light;
--bg:#f3efe6;--surface:#ffffff;--surface2:#f6f2e9;--ink:#1d1a14;--ink2:#4d473b;--muted:#7f796c;
--grid:rgba(60,50,30,.1);--line:rgba(150,115,50,.38);--gold:#9a7424;--gold2:#6f5018;--gold3:#b8913f;
--goldgrad:linear-gradient(135deg,#f0d9a6,#d6b370 55%,#b8913f);
--card-bg:linear-gradient(155deg,#ffffff 0%,#fbf8f1 100%);
--card-shadow:0 1px 0 rgba(255,255,255,.8) inset,0 18px 40px -26px rgba(80,60,20,.35),0 0 0 1px rgba(0,0,0,.03);
--tile:linear-gradient(160deg,rgba(255,255,255,.9),rgba(250,246,238,.9));--tipbg:rgba(255,255,255,.97);--scroll:#cfc8b8;
--s4:#b37400;--good:#1f8a4c;--bad:#c2453d}
:root[data-theme="light"] .card{border-color:rgba(150,115,50,.22)}
:root[data-theme="light"] .kpi,:root[data-theme="light"] .ratio,:root[data-theme="light"] .today,:root[data-theme="light"] .prsum,:root[data-theme="light"] .cgrp{border-color:rgba(150,115,50,.2)}
:root[data-theme="light"] .seg button:not(.on),:root[data-theme="light"] .btn:not(.go),:root[data-theme="light"] .chip:not(.on){background:rgba(255,255,255,.7)}
:root[data-theme="light"] .pin{background:#fff}
:root[data-theme="light"] .tip{box-shadow:0 14px 30px rgba(60,45,15,.18)}
:root[data-theme="light"] body::before{content:"";position:fixed;top:0;left:0;right:0;height:env(safe-area-inset-top);background:#1d1a14;z-index:50}
.themeseg{display:inline-flex}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;color:var(--ink);font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif;font-weight:400;-webkit-font-smoothing:antialiased;
background:
 radial-gradient(1000px 520px at 8% -8%,rgba(214,179,112,.13),transparent 65%),
 radial-gradient(900px 500px at 100% 0%,rgba(57,135,229,.08),transparent 65%),
 radial-gradient(900px 600px at 50% 120%,rgba(214,179,112,.07),transparent 60%),
 var(--bg);background-attachment:fixed}
.wrap{max-width:1780px;margin:0 auto;padding:28px 18px 56px}
.layout{display:grid;grid-template-columns:300px minmax(0,1fr) 340px;grid-template-areas:"side main ctrl";gap:22px;align-items:start}
.layout>main{grid-area:main;min-width:0}
.side{grid-area:side}.ctrl{grid-area:ctrl}
.chip.on{background:var(--goldgrad);color:#15110a;border-color:transparent;font-weight:600}
.cgrp{padding:14px 0 6px;border-top:1px solid rgba(214,179,112,.14)}
.cgrp h3{font:400 10.5px/1.2 "Segoe UI",system-ui,sans-serif;margin:0 0 10px;letter-spacing:.22em;text-transform:uppercase;color:var(--gold)}
.cgrp label{display:flex;justify-content:space-between;font-size:12px;color:var(--ink2);margin-top:6px}
.cgrp label b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.cgrp input[type=range]{width:100%;accent-color:var(--gold);margin:4px 0}
.cgrp .btn{margin:8px 8px 0 0}
.cgrp .chips{display:flex;flex-wrap:wrap}
.btn.go{background:var(--goldgrad);color:#15110a;border-color:transparent;font-weight:600}
.cmsg{min-height:18px;font-size:12px;margin:10px 0 0;color:var(--ink2)}
.pin{width:100%;font:inherit;padding:9px 14px;margin-bottom:8px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--ink)}
.chargenote{font-size:12px;color:var(--gold2);background:rgba(214,179,112,.08);border:1px solid rgba(214,179,112,.2);border-radius:12px;padding:8px 12px;margin:8px 0 0}
.prnow .v{font:300 40px/1.1 Georgia,"Times New Roman",serif;font-variant-numeric:tabular-nums;color:var(--ink)}
.prnow .v small{font:400 13px "Segoe UI",system-ui,sans-serif;color:var(--gold);margin-left:6px;letter-spacing:.1em}
.prnow .s{font-size:12px;color:var(--muted);margin-top:4px}
.prsum{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0 12px;padding:12px 0;border-top:1px solid rgba(214,179,112,.14);border-bottom:1px solid rgba(214,179,112,.14)}
.prsum .v{font:300 17px/1.1 Georgia,"Times New Roman",serif;font-variant-numeric:tabular-nums}
.prsum .l{font-size:10px;color:var(--muted);letter-spacing:.14em;text-transform:uppercase;margin-top:3px}
.prrow{display:grid;grid-template-columns:30px 1fr 44px;align-items:center;gap:8px;font-size:12px;font-variant-numeric:tabular-nums;padding:2px 4px;color:var(--ink2);border-radius:6px}
.prrow i{display:block;height:7px;border-radius:4px;min-width:3px}
.prrow b{font-weight:400;text-align:right}
.prrow.now{background:rgba(214,179,112,.12);color:var(--gold2);font-weight:600}
.prrow.now b{font-weight:600}
header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:22px}
h1{font:400 13px/1 "Segoe UI",system-ui,sans-serif;margin:0;letter-spacing:.42em;text-transform:uppercase;color:var(--gold2)}
h1::after{content:"";display:block;width:54px;height:1px;margin-top:12px;background:linear-gradient(90deg,var(--gold),transparent)}
h1 small{display:block;margin:10px 0 0;font:300 30px/1.1 Georgia,"Times New Roman",serif;letter-spacing:.01em;text-transform:none;color:var(--ink)}
nav a{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--ink2);text-decoration:none;border:1px solid var(--line);padding:7px 16px;border-radius:999px;margin-left:8px;transition:.2s}
nav a:hover{color:#15110a;background:var(--goldgrad);border-color:transparent}
.status{font-size:12px;color:var(--ink2);display:flex;align-items:center;letter-spacing:.04em}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--muted);margin-right:8px}
.dot.ok{background:var(--good);box-shadow:0 0 10px var(--good)}.dot.err{background:var(--bad);box-shadow:0 0 10px var(--bad)}

.card{position:relative;background:var(--card-bg);border:1px solid rgba(214,179,112,.16);border-radius:22px;padding:22px 24px;margin-bottom:22px;
 box-shadow:var(--card-shadow)}
.card::before{content:"";position:absolute;left:24px;right:24px;top:0;height:1px;background:linear-gradient(90deg,transparent,rgba(240,217,166,.55),transparent)}
.card h2{font:400 11px/1.2 "Segoe UI",system-ui,sans-serif;margin:0;letter-spacing:.3em;text-transform:uppercase;color:var(--gold)}
.card .hint,.hint{font-size:12px;color:var(--ink2);letter-spacing:.03em}
.bar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.seg button,.btn,.chip{font:inherit;font-size:12px;letter-spacing:.08em;border:1px solid var(--line);background:rgba(255,255,255,.02);color:var(--ink2);padding:6px 14px;cursor:pointer;transition:.2s}
.seg button:first-child{border-radius:999px 0 0 999px}.seg button:last-child{border-radius:0 999px 999px 0}
.seg button+button{border-left:0}
.seg button:hover,.btn:hover,.chip:hover{color:var(--gold2);border-color:var(--gold)}
.seg button.on{background:var(--goldgrad);color:#15110a;border-color:transparent;font-weight:600;box-shadow:0 4px 18px -4px rgba(214,179,112,.55)}
.btn{border-radius:999px;margin-left:8px}
.chip{border-radius:999px;display:inline-flex;align-items:center;gap:7px;margin-right:6px}
.chip i{width:9px;height:9px;border-radius:3px;display:inline-block}
.chip.off{opacity:.4}
.chip.off i{background:transparent!important;border:1.5px solid var(--muted)}

/* energy flow */
#flow{width:100%;height:auto;display:block;max-height:440px}
#flow text{fill:var(--ink);font-family:"Segoe UI",system-ui,sans-serif}
#flow .t{fill:var(--ink2);font-size:11px;letter-spacing:.18em;text-transform:uppercase}
#flow .v{font-size:31px;font-weight:200;letter-spacing:-.01em}
#flow .s{fill:var(--muted);font-size:12px;letter-spacing:.06em}
#flow .node{stroke:rgba(214,179,112,.30);stroke-width:1}
.today{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-top:10px;padding-top:18px;border-top:1px solid rgba(214,179,112,.14)}
.today div{border-left:1px solid var(--gold3);padding-left:14px}
.today .v{font:300 26px/1.1 Georgia,"Times New Roman",serif;font-variant-numeric:tabular-nums;color:var(--ink)}
.today .l{font-size:10.5px;color:var(--muted);letter-spacing:.18em;text-transform:uppercase;margin-top:4px}

/* live chart */
.legend{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12px;color:var(--ink2);margin-bottom:8px;letter-spacing:.03em}
.legend span{display:flex;align-items:center;gap:8px}
.legend i{width:16px;height:2.5px;border-radius:2px;display:inline-block}
.cbox{position:relative}
svg.chart{display:block;width:100%;height:320px}
svg.chart text{fill:var(--muted);font-size:11px;letter-spacing:.04em}
.tip{position:absolute;pointer-events:none;background:var(--tipbg);border:1px solid var(--line);border-radius:12px;padding:10px 12px;font-size:12px;box-shadow:0 18px 40px rgba(0,0,0,.6);display:none;min-width:200px;z-index:3;backdrop-filter:blur(6px)}
.tip b{display:block;margin-bottom:6px;font-weight:600;color:var(--gold2);letter-spacing:.06em}
.tip div{display:flex;justify-content:space-between;gap:14px;font-variant-numeric:tabular-nums;padding:1px 0}
.tip i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:7px}

/* analysis */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px;margin-bottom:16px}
.kpi{border:1px solid rgba(214,179,112,.14);border-radius:16px;padding:16px 18px;background:var(--tile)}
.kpi .lab{font-size:10.5px;color:var(--ink2);display:flex;align-items:center;gap:9px;letter-spacing:.2em;text-transform:uppercase}
.sw{width:9px;height:9px;border-radius:50%;display:inline-block}
.kpi .val{font:300 38px/1.1 Georgia,"Times New Roman",serif;margin-top:8px;font-variant-numeric:tabular-nums;color:var(--ink)}
.kpi .val small{font:400 13px "Segoe UI",system-ui,sans-serif;color:var(--gold);margin-left:6px;letter-spacing:.14em;text-transform:uppercase}
.kpi .sub{font-size:12px;color:var(--muted);margin-top:4px}
.ratios{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px;margin-bottom:18px}
.ratio{border:1px solid rgba(214,179,112,.14);border-radius:16px;padding:16px 18px;background:var(--tile)}
.ratio h3{font:400 10.5px/1.2 "Segoe UI",system-ui,sans-serif;margin:0 0 12px;letter-spacing:.22em;text-transform:uppercase;color:var(--gold)}
.donutrow{display:flex;align-items:center;gap:16px}
.donutrow p{margin:0;font-size:12px;color:var(--ink2)}
.donut text{fill:var(--ink);font-family:Georgia,serif;font-weight:300}
.stack{display:flex;height:10px;border-radius:999px;overflow:hidden;gap:3px;margin:8px 0 8px}
.stack div{height:100%;border-radius:999px}
.stackl{display:flex;justify-content:space-between;font-size:12px;color:var(--ink2);gap:10px;flex-wrap:wrap}
.stackl b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.note{font-size:12px;color:var(--ink2);background:rgba(214,179,112,.06);border:1px solid rgba(214,179,112,.18);border-radius:12px;padding:10px 14px;margin-bottom:16px;display:none;line-height:1.5}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--gold);font-weight:400}
th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--grid)}th:first-child,td:first-child{text-align:left}
.tblbox{display:none;max-height:320px;overflow:auto;margin-top:10px}
@media (max-width:1400px){.layout{grid-template-columns:300px minmax(0,1fr);grid-template-areas:"side main" "ctrl main"}}
@media (max-width:1000px){.layout{grid-template-columns:1fr;grid-template-areas:"main" "ctrl" "side"}}
@media (max-width:700px){
 .wrap{padding:max(18px,env(safe-area-inset-top)) 12px 40px}
 .prnow .v{font-size:34px}
 .card{padding:16px 14px;border-radius:18px;margin-bottom:16px}
 h1 small{font-size:24px}
 nav a{padding:6px 12px;margin-left:4px}
 .kpi .val{font-size:32px}
 .today .v{font-size:22px}
 .seg button,.btn,.chip{padding:6px 11px}
 svg.chart{height:260px}
 #flow .t{font-size:9.5px;letter-spacing:.1em}
 .donutrow{flex-direction:column;align-items:flex-start}
}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:8px}
</style></head><body><div class="wrap">

<header>
 <h1>Solární elektrárna<small id="model">GW10K-ET</small></h1>
 <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
  <span class="seg themeseg" id="themeseg" aria-label="Vzhled"><button data-t="dark">Tmavý</button><button data-t="light">Světlý</button><button data-t="auto">Auto</button></span>
  <div class="status"><span class="dot" id="dot"></span><span id="status">připojování…</span></div>
  <nav><a href="#live">Živě</a><!--PUBLIC-STRIP-START--><a href="#ctl">Ovládání</a><!--PUBLIC-STRIP-END--><a href="#prices">Ceny</a><!--PUBLIC-STRIP-START--><a href="#analysis">Analýza</a><!--PUBLIC-STRIP-END--></nav>
 </div>
</header>

<div class="layout">
<aside class="side"><section id="prices" class="card">
 <div class="bar"><h2>Spotová cena</h2><span class="seg" id="prday"></span></div>
 <div class="prnow" id="prnow"><p class="hint">Načítám ceny…</p></div>
 <div class="prsum" id="prsum" style="display:none"></div>
 <div id="prrows"></div>
 <p class="hint" id="prfoot" style="margin:12px 0 0"></p>
</section></aside>
<main>
<section id="live" class="card">
 <div class="bar"><h2>Tok energie teď</h2><span class="hint" id="modeinfo"></span></div>
 <svg id="flow" viewBox="0 0 900 390" role="img" aria-label="Tok energie mezi solárem, sítí, domem, spotřebou a baterií"></svg>
 <div class="today" id="today"></div>
</section>

<section class="card">
 <div class="bar"><h2>Výkon v čase</h2>
  <div><span class="seg" id="range"><button data-h="1">1 h</button><button data-h="6" class="on">6 h</button><button data-h="24">24 h</button></span>
  <button class="btn" id="tblbtn">Tabulka</button></div></div>
 <div class="legend">
  <span><i style="background:var(--s1)"></i>Výroba FVE</span>
  <span><i style="background:var(--s2)"></i>Spotřeba domu</span>
  <span><i style="background:var(--s3)"></i>Síť (+ odběr, − dodávka)</span>
  <span><i style="background:var(--s4)"></i>Baterie (+ vybíjení, − nabíjení)</span></div>
 <div class="cbox"><svg class="chart" id="chart"></svg><div class="tip" id="tip"></div></div>
 <div class="tblbox" id="tbl"><table><thead><tr><th>Čas</th><th>FVE</th><th>Dům</th><th>Síť</th><th>Baterie</th><th>SOC</th></tr></thead><tbody id="tbody"></tbody></table></div>
</section>

<!--PUBLIC-STRIP-START--><section id="analysis" class="card">
 <div class="bar"><h2>Analýza energie</h2>
  <span class="seg" id="per"><button data-p="7">Týden</button><button data-p="30" class="on">Měsíc</button><button data-p="365">Rok</button><button data-p="all">Celkem</button></span></div>
 <div class="note" id="note"></div>
 <div class="kpis" id="kpis"></div>
 <div class="ratios" id="ratios"></div>
 <div class="bar" style="margin-bottom:6px"><h2 id="barstitle">Vývoj v čase</h2>
  <div><span id="chips"></span><button class="btn" id="tbl2btn">Tabulka</button></div></div>
 <div class="cbox"><svg class="chart" id="bars"></svg><div class="tip" id="btip"></div></div>
 <div class="tblbox" id="tbl2"></div>
 <p class="hint" style="margin:10px 0 0">Prodej a nákup jsou z elektroměru. „Vlastní spotřeba“ = výroba, která nešla do sítě. „Soběstačnost“ = podíl spotřeby, který nebyl nakoupen. Nabíjení baterie ze sítě se počítá do nákupu, hodnoty jsou proto orientační.</p>
</section><!--PUBLIC-STRIP-END-->
</main>
<!--PUBLIC-STRIP-START--><aside class="ctrl"><section id="ctl" class="card">
 <div class="bar"><h2>Ovládání</h2><span class="hint" id="ctlstate"></span></div>
 <div id="ctlbody"><p class="hint">Načítám…</p></div>
</section></aside><!--PUBLIC-STRIP-END-->
</div>
</div>

<script>
const $=id=>document.getElementById(id);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C=["--s1","--s2","--s3","--s4"], NAMES=["Výroba FVE","Spotřeba domu","Síť","Baterie"];
let HIST=[], NOW=null, ERR=null, hours=6, hover=null, STATS=null, per='30';
const vis=new Set([0,1]);

const fmtW=w=>Math.abs(w)>=1000?(w/1000).toFixed(2).replace('.',',')+' kW':Math.round(w)+' W';
const fmtK=(v,d)=>{if(v==null||isNaN(v))return '–';d=d==null?(Math.abs(v)>=100?0:1):d;return Number(v).toLocaleString('cs-CZ',{minimumFractionDigits:d,maximumFractionDigits:d});};
const pct=v=>v==null||!isFinite(v)?'–':Math.round(v*100)+' %';
const hhmm=t=>new Date(t*1000).toLocaleTimeString('cs-CZ',{hour:'2-digit',minute:'2-digit'});
const hhmmss=t=>new Date(t*1000).toLocaleTimeString('cs-CZ');
const MON=['led','úno','bře','dub','kvě','čvn','čvc','srp','zář','říj','lis','pro'];
const lab=l=>l.length===4?l:l.length===7?MON[+l.slice(5)-1]+' '+l.slice(2,4):(+l.slice(8))+'. '+(+l.slice(5,7))+'.';
const labFull=l=>l.length===4?l:l.length===7?MON[+l.slice(5)-1]+' '+l.slice(0,4):(+l.slice(8))+'. '+(+l.slice(5,7))+'. '+l.slice(0,4);

/* ---------- tok energie ---------- */
function icon(kind,x,y,soc){
  const col={sun:'--s1',grid:'--s3',load:'--s2',bat:'--s4'}[kind], c=css(col);
  const badge=`<circle cx="${x}" cy="${y}" r="24" fill="${c}" fill-opacity=".14"/><circle cx="${x}" cy="${y}" r="24" fill="none" stroke="${c}" stroke-opacity=".35"/>`;
  const st=`stroke="${c}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"`;
  if(kind==='sun'){let r='';for(let i=0;i<8;i++){const a=i*Math.PI/4;r+=`<line x1="${x+Math.cos(a)*11}" y1="${y+Math.sin(a)*11}" x2="${x+Math.cos(a)*15}" y2="${y+Math.sin(a)*15}"/>`;}
    return `${badge}<g ${st}><circle cx="${x}" cy="${y}" r="6.5" fill="${c}" fill-opacity=".3"/>${r}</g>`;}
  if(kind==='grid')return `${badge}<g ${st}><path d="M${x-8} ${y+14}L${x} ${y-14}L${x+8} ${y+14}M${x-5} ${y+5}H${x+5}M${x-3.5} ${y-4}H${x+3.5}M${x-13} ${y-7}H${x+13}"/></g>`;
  if(kind==='load')return `${badge}<g ${st}><path d="M${x-12} ${y}L${x} ${y-12}L${x+12} ${y}M${x-9} ${y-3}V${y+11}H${x+9}V${y-3}"/><circle cx="${x}" cy="${y+3}" r="2.6" fill="${c}"/></g>`;
  if(kind==='bat'){const h=Math.max(0,Math.min(1,soc/100))*19;
    return `${badge}<g><rect x="${x-9}" y="${y-12}" width="18" height="25" rx="3.5" fill="none" stroke="${c}" stroke-width="2.2"/><rect x="${x-4}" y="${y-16}" width="8" height="4" rx="1.5" fill="${c}"/><rect x="${x-6.2}" y="${y+10.5-h}" width="12.4" height="${h}" rx="1.8" fill="${c}"/></g>`;}
}
function node(x,y,kind,title,value,sub,soc,active){
  const col={sun:'--s1',grid:'--s3',load:'--s2',bat:'--s4'}[kind];
  return `<g filter="url(#sh)"><rect class="node" x="${x}" y="${y}" width="190" height="112" rx="18" fill="url(#ng)"/></g>
  <rect x="${x+14}" y="${y+104}" width="162" height="3" rx="1.5" fill="${css(col)}" opacity="${active?.9:.25}"/>
  ${icon(kind,x+36,y+36,soc)}
  <text class="t" x="${x+70}" y="${y+41}">${title}</text>
  <text class="v" x="${x+20}" y="${y+80}">${value}</text><text class="s" x="${x+20}" y="${y+99}">${sub}</text>`;
}
function flowline(d,color,watts,rev,id){
  const on=Math.abs(watts)>=20, w=3+Math.min(4,Math.abs(watts)/1500);
  let o=`<path d="${d}" fill="none" stroke="${css('--grid')}" stroke-width="12" stroke-linecap="round" opacity=".8"/>`;
  if(!on)return o+`<path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-dasharray="2 10" opacity=".35"/>`;
  o+=`<path id="${id}" d="${d}" fill="none" stroke="${color}" stroke-width="${w+5}" stroke-linecap="round" opacity=".25" filter="url(#glow)"/>`;
  o+=`<path d="${d}" fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round" opacity=".9"/>`;
  const dur=Math.max(1.4,3.4-Math.abs(watts)/2500).toFixed(2);
  for(let i=0;i<3;i++){
    o+=`<circle r="${4.5+w/3}" fill="#fff" stroke="${color}" stroke-width="2.5" filter="url(#glow)"><animateMotion dur="${dur}s" begin="${(-i*dur/3).toFixed(2)}s" repeatCount="indefinite" ${rev?'keyPoints="1;0" keyTimes="0;1" calcMode="linear"':''}><mpath href="#${id}"/></animateMotion></circle>`;
  }
  return o;
}
function drawFlow(){
  const n=NOW||{pv:0,load:0,grid:0,bat:0,soc:0};
  const pvw=n.pv,gw=n.grid,bw=n.bat,lw=n.load;
  const mob=window.innerWidth<700;
  const L=mob?{vb:'0 0 400 504',pv:[5,6],ld:[205,6],gr:[5,386],bt:[205,386],dx:-250,dy:67,
     p_pv:'M100 118 C100 190 100 245 135 258',p_gr:'M100 386 C100 350 105 325 135 318',p_ld:'M265 258 C300 245 300 190 300 118',p_bt:'M300 386 C300 350 295 325 265 318'}
   :{vb:'0 0 900 390',pv:[10,36],ld:[700,36],gr:[10,242],bt:[700,242],dx:0,dy:0,
     p_pv:'M200 92 C300 92 310 160 392 170',p_gr:'M200 298 C300 298 310 230 392 220',p_ld:'M508 170 C590 160 600 92 700 92',p_bt:'M700 298 C600 298 590 230 508 220'};
  $('flow').setAttribute('viewBox',L.vb);
  let s=`<defs>
   <filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="3.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
   <filter id="sh" x="-10%" y="-10%" width="120%" height="140%"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#000" flood-opacity=".14"/></filter>
   <linearGradient id="ng" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:${css('--surface')}"/><stop offset="1" style="stop-color:${css('--surface2')}"/></linearGradient>
   <linearGradient id="roof" x1="0" y1="0" x2="1" y2="1"><stop offset="0" style="stop-color:${css('--gold2')}"/><stop offset="1" style="stop-color:${css('--gold3')}"/></linearGradient>
   <radialGradient id="halo"><stop offset="0" style="stop-color:${css('--gold')};stop-opacity:.34"/><stop offset="1" style="stop-color:${css('--gold')};stop-opacity:0"/></radialGradient>
  </defs>`;
  s+=`<circle cx="${450+L.dx}" cy="${200+L.dy}" r="150" fill="url(#halo)" opacity="${pvw>=20?1:.4}"/>`;
  s+=flowline(L.p_pv,css('--s1'),pvw,false,'fl_pv');
  s+=flowline(L.p_gr,css('--s3'),gw,gw<0,'fl_gr');
  s+=flowline(L.p_ld,css('--s2'),lw,false,'fl_ld');
  s+=flowline(L.p_bt,css('--s4'),bw,bw<0,'fl_bt');
  const lit=lw>=20;
  s+=`<g transform="translate(${L.dx} ${L.dy})"><g filter="url(#sh)"><path d="M366 178 L450 98 L534 178 Z" fill="url(#roof)" stroke="${css('--surface')}" stroke-width="6" stroke-linejoin="round"/>
  <rect x="385" y="168" width="130" height="104" rx="10" fill="url(#ng)" stroke="${css('--line')}" stroke-width="1.5"/></g>
  <rect x="402" y="182" width="26" height="22" rx="4" fill="${css('--gold')}" opacity="${lit?.9:.2}"/><rect x="472" y="182" width="26" height="22" rx="4" fill="${css('--gold')}" opacity="${lit?.9:.2}"/>
  <text x="450" y="232" text-anchor="middle" style="font-size:19px;font-weight:700">Dům</text>
  <text x="450" y="251" text-anchor="middle" class="s">${NOW&&NOW.model?NOW.model:'měnič'}${NOW&&NOW.temp!=null?' · '+NOW.temp.toFixed(0)+' °C':''}</text></g>`;
  s+=node(L.pv[0],L.pv[1],'sun','Solár (FVE)',fmtW(pvw),pvw>=20?'vyrábí':'nevyrábí',0,pvw>=20);
  s+=node(L.gr[0],L.gr[1],'grid','Síť',fmtW(Math.abs(gw)),gw>=20?'odběr ze sítě':gw<=-20?'dodávka do sítě':'bez výměny',0,Math.abs(gw)>=20);
  s+=node(L.ld[0],L.ld[1],'load','Spotřeba domu',fmtW(lw),'aktuální odběr',0,lw>=20);
  s+=node(L.bt[0],L.bt[1],'bat','Baterie '+Math.round(n.soc)+' %',fmtW(Math.abs(bw)),bw>=20?'vybíjí se':bw<=-20?'nabíjí se':'nečinná',n.soc,Math.abs(bw)>=20);
  $('flow').innerHTML=s;
  $('modeinfo').textContent=NOW&&NOW.work_mode?('režim: '+NOW.work_mode+(NOW.bat_mode?' · baterie: '+NOW.bat_mode:'')):'';
}
function drawToday(){
  if(!NOW)return;
  $('today').innerHTML=[['Dnes vyrobeno',NOW.e_day],['Dnes spotřebováno',NOW.e_load_day],['Dnes nabito do baterie',NOW.bat_ch_day],['Dnes vybito z baterie',NOW.bat_dis_day]]
   .map(([l,v])=>`<div><div class="v">${fmtK(v)} <span style="font-size:13px;font-weight:500;color:var(--ink2)">kWh</span></div><div class="l">${l}</div></div>`).join('');
}

/* ---------- živý graf ---------- */
function drawChart(){
  const svg=$('chart'),W=svg.clientWidth||800,H=320,m={l:52,r:12,t:10,b:24};
  const tmax=HIST.length?HIST[HIST.length-1][0]:Date.now()/1000, tmin=tmax-hours*3600;
  const pts=HIST.filter(r=>r[0]>=tmin);
  let lo=0,hi=1000;pts.forEach(r=>{for(let k=1;k<=4;k++){lo=Math.min(lo,r[k]);hi=Math.max(hi,r[k]);}});
  const step=[500,1000,2000,2500,5000][[2000,4000,8000,12000,1e9].findIndex(x=>hi-lo<=x)];
  lo=Math.floor(lo/step)*step;hi=Math.ceil(hi/step)*step;
  const X=t=>m.l+(t-tmin)/(tmax-tmin||1)*(W-m.l-m.r), Y=v=>m.t+(hi-v)/(hi-lo)*(H-m.t-m.b);
  let out='';
  for(let v=lo;v<=hi;v+=step){out+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${css(v===0?'--line':'--grid')}" stroke-width="${v===0?1.5:1}"/><text x="${m.l-8}" y="${Y(v)+4}" text-anchor="end">${v/1000} kW</text>`;}
  const ticks=Math.max(2,Math.floor((W-m.l-m.r)/110));
  for(let i=0;i<=ticks;i++){const t=tmin+(tmax-tmin)*i/ticks;out+=`<text x="${X(t)}" y="${H-6}" text-anchor="${i==0?'start':i==ticks?'end':'middle'}">${hhmm(t)}</text>`;}
  for(let k=1;k<=4;k++){
    if(!pts.length)break;
    const d=pts.map((r,i)=>(i?'L':'M')+X(r[0]).toFixed(1)+' '+Y(r[k]).toFixed(1)).join('');
    out+=`<path d="${d}" fill="none" stroke="${css(C[k-1])}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  if(hover!=null&&pts[hover]){
    const r=pts[hover];out+=`<line x1="${X(r[0])}" x2="${X(r[0])}" y1="${m.t}" y2="${H-m.b}" stroke="${css('--muted')}" stroke-dasharray="3 3"/>`;
    for(let k=1;k<=4;k++)out+=`<circle cx="${X(r[0])}" cy="${Y(r[k])}" r="4" fill="${css(C[k-1])}" stroke="${css('--surface')}" stroke-width="2"/>`;
  }
  if(!pts.length)out+=`<text x="${W/2}" y="${H/2}" text-anchor="middle">Čekám na první data…</text>`;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML=out;
  svg._s={X,pts,W};
}
$('chart').addEventListener('mousemove',e=>{
  const svg=$('chart'),s=svg._s;if(!s||!s.pts.length)return;
  const rect=svg.getBoundingClientRect(),x=(e.clientX-rect.left)*(s.W/rect.width);
  let best=0,bd=1e9;s.pts.forEach((r,i)=>{const dd=Math.abs(s.X(r[0])-x);if(dd<bd){bd=dd;best=i;}});
  hover=best;drawChart();
  const r=s.pts[best],tip=$('tip');
  tip.innerHTML=`<b>${hhmmss(r[0])}</b>`+NAMES.map((n,k)=>`<div><span><i style="background:${css(C[k])}"></i>${n}</span><span>${fmtW(r[k+1])}</span></div>`).join('')+`<div><span>Nabití baterie</span><span>${r[5].toFixed(0)} %</span></div>`;
  tip.style.display='block';const left=(s.X(r[0])+16);tip.style.left=(left+210>s.W?left-232:left)+'px';tip.style.top='10px';
});
$('chart').addEventListener('mouseleave',()=>{hover=null;$('tip').style.display='none';drawChart();});
document.querySelectorAll('#range button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#range button').forEach(x=>x.classList.remove('on'));b.classList.add('on');hours=+b.dataset.h;hover=null;drawChart();drawTable();});
$('tblbtn').onclick=()=>{const t=$('tbl');t.style.display=t.style.display==='block'?'none':'block';};
function drawTable(){
  const tmin=(HIST.length?HIST[HIST.length-1][0]:0)-hours*3600;
  const rows=HIST.filter(r=>r[0]>=tmin).slice(-60).reverse();
  $('tbody').innerHTML=rows.map(r=>`<tr><td>${hhmmss(r[0])}</td><td>${fmtW(r[1])}</td><td>${fmtW(r[2])}</td><td>${fmtW(r[3])}</td><td>${fmtW(r[4])}</td><td>${r[5].toFixed(0)} %</td></tr>`).join('');
}

/*PUBLIC-STRIP-START*/
/* ---------- analýza ---------- */
const SER=[{k:0,n:'Výroba',c:'--s1'},{k:1,n:'Spotřeba',c:'--s2'},{k:3,n:'Prodej do sítě',c:'--s3'},{k:2,n:'Nákup ze sítě',c:'--s5'}];
function donut(frac,color,label){
  const r=46,c=2*Math.PI*r,f=frac==null||!isFinite(frac)?0:Math.max(0,Math.min(1,frac));
  return `<svg class="donut" width="116" height="116" viewBox="0 0 116 116"><circle cx="58" cy="58" r="${r}" fill="none" stroke="${css('--grid')}" stroke-width="12"/>
  <circle cx="58" cy="58" r="${r}" fill="none" stroke="${css(color)}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${(c*f).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 58 58)"/>
  <text x="58" y="64" text-anchor="middle" style="font-size:22px;font-weight:700">${label}</text></svg>`;
}
function stack(parts){
  const tot=parts.reduce((a,p)=>a+Math.max(0,p.v),0)||1;
  return `<div class="stack">${parts.map(p=>`<div style="width:${(Math.max(0,p.v)/tot*100).toFixed(1)}%;background:${css(p.c)}"></div>`).join('')}</div>
  <div class="stackl">${parts.map(p=>`<span><span class="sw" style="background:${css(p.c)}"></span> ${p.n}: <b>${fmtK(p.v)} kWh</b> (${pct(Math.max(0,p.v)/tot)})</span>`).join('')}</div>`;
}
function drawStats(){
  const P=STATS&&STATS.periods&&STATS.periods[per];
  const note=$('note');
  if(!P){note.style.display='block';note.textContent='Zatím nejsou uložená žádná počítadla energie. Počkej pár minut, než se uloží první záznam.';
    $('kpis').innerHTML='';$('ratios').innerHTML='';$('bars').innerHTML='';return;}
  const [pv,load,imp,exp,chg,dis]=P.tot;
  const msgs=[], dfmt=x=>new Date(x).toLocaleDateString('cs-CZ');
  if(per==='all')msgs.push(P.yearly?'Celkové součty jsou počítadla měniče a elektroměru. Roční sloupce jsou ze SEMS (od '+new Date(STATS.since*1000).toLocaleDateString('cs-CZ')+'), spotřeba je dopočtená.':'Celkové hodnoty jsou celoživotní počítadla měniče a elektroměru. Sloupce níže se plní od '+new Date(STATS.since*1000).toLocaleDateString('cs-CZ')+'.');
  else if(P.days<P.want)msgs.push((P.first?'Data mám od '+dfmt(P.first)+'. ':'')+'K dispozici je zatím '+P.days+' z '+P.want+' dní, součty proto zahrnují jen tuto dobu.');
  const O=STATS.outage, hasEst=P.bars.some(b=>b.e>0);
  if(O&&(P.estDays>0||(per==='all'&&hasEst)))msgs.push('Šrafované sloupce jsou ODHAD za výpadek dat ve SEMS ('+dfmt(O.from)+' až '+dfmt(O.to)+'): chybějící energie se dopočítala z rozdílu počítadel měniče a sezónního průběhu z minulých let. Výroba za výpadek ≈ '+fmtK(O.gap[0],0)+' kWh.');
  if(msgs.length){note.style.display='block';note.textContent=msgs.join(' ');}else note.style.display='none';
  const own=Math.max(0,load-imp), gridL=Math.min(imp,load);
  const selfCons=pv>0?(pv-exp)/pv:null, selfSuff=load>0?own/load:null;
  const k=(c,l,v,sub)=>`<div class="kpi"><div class="lab"><span class="sw" style="background:${css(c)}"></span>${l}</div><div class="val">${fmtK(v)}<small>kWh</small></div><div class="sub">${sub}</div></div>`;
  $('kpis').innerHTML=
    k('--s1','Vyrobeno',pv,'z toho '+pct(selfCons)+' využito doma'+(P.est&&P.est[0]>0?' · odhad '+fmtK(P.est[0],0)+' kWh':''))+
    k('--s2','Spotřebováno doma',load,'z vlastní výroby a baterie '+pct(selfSuff))+
    k('--s3','Prodáno do sítě',exp,pct(pv>0?exp/pv:null)+' výroby')+
    k('--s5','Nakoupeno ze sítě',imp,pct(load>0?imp/load:null)+' spotřeby');
  $('ratios').innerHTML=
    `<div class="ratio"><h3>Soběstačnost</h3><div class="donutrow">${donut(selfSuff,'--s1',pct(selfSuff))}<p>Podíl spotřeby domu, který pokryla vlastní výroba (přímo nebo přes baterii).</p></div></div>
     <div class="ratio"><h3>Vlastní spotřeba výroby</h3><div class="donutrow">${donut(selfCons,'--s2',pct(selfCons))}<p>Podíl vyrobené energie, který zůstal v domě a neodešel do sítě.</p></div></div>
     <div class="ratio"><h3>Odkud je spotřeba</h3>${stack([{n:'Vlastní zdroj',v:own,c:'--s1'},{n:'Ze sítě',v:gridL,c:'--s5'}])}
       <h3 style="margin-top:14px">Kam šla výroba</h3>${stack([{n:'Doma',v:Math.max(0,pv-exp),c:'--s2'},{n:'Prodáno',v:exp,c:'--s3'}])}
       <div class="stackl" style="margin-top:10px"><span>Baterie: nabito <b>${fmtK(chg)} kWh</b></span><span>vybito <b>${fmtK(dis)} kWh</b></span></div></div>`;
  $('barstitle').textContent=per==='7'||per==='30'?'Denní hodnoty (kWh)':(P.bars.length&&P.bars[0].l.length===4)?'Roční hodnoty (kWh)':'Měsíční hodnoty (kWh)';
  const anyEst=P.bars.some(b=>b.e>=0.5);
  $('chips').innerHTML=(anyEst?'<span class="hint" style="margin-right:10px">▨ odhad</span>':'')+SER.map((s,i)=>`<button class="chip ${vis.has(i)?'':'off'}" data-i="${i}"><i style="background:${css(s.c)}"></i>${s.n}</button>`).join('');
  document.querySelectorAll('#chips .chip').forEach(b=>b.onclick=()=>{const i=+b.dataset.i;if(vis.has(i)){if(vis.size>1)vis.delete(i);}else vis.add(i);drawStats();});
  drawBars(P);drawTable2(P);
}
function drawBars(P){
  const svg=$('bars'),W=svg.clientWidth||800,H=320,m={l:52,r:10,t:12,b:28},bars=P.bars,show=SER.map((s,i)=>i).filter(i=>vis.has(i));
  if(!bars.length){svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML=`<text x="${W/2}" y="${H/2}" text-anchor="middle">Historie se teprve sbírá. Sloupce se objeví po prvním celém dni.</text>`;return;}
  let hi=1;bars.forEach(b=>show.forEach(i=>hi=Math.max(hi,b.v[SER[i].k])));
  const raw=hi/4,mag=Math.pow(10,Math.floor(Math.log10(raw))),step=[1,2,2.5,5,10].map(x=>x*mag).find(x=>x>=raw);
  hi=Math.ceil(hi/step)*step;
  const Y=v=>m.t+(1-v/hi)*(H-m.t-m.b),gw=(W-m.l-m.r)/bars.length,bw=Math.min(26,gw*0.8/show.length);
  let o='<defs>'+SER.map((x,i)=>`<pattern id="hs${i}" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="7" height="7" fill="${css(x.c)}" fill-opacity=".18"/><rect width="3" height="7" fill="${css(x.c)}" fill-opacity=".75"/></pattern>`).join('')+'</defs>';
  for(let v=0;v<=hi+1e-9;v+=step)o+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${css(v===0?'--line':'--grid')}" stroke-width="${v===0?1.5:1}"/><text x="${m.l-8}" y="${Y(v)+4}" text-anchor="end">${fmtK(v,step<1?1:0)}</text>`;
  const every=Math.max(1,Math.ceil(bars.length*46/(W-m.l-m.r)));
  bars.forEach((b,gi)=>{
    const cx=m.l+gi*gw+gw/2,x0=cx-bw*show.length/2;
    show.forEach((si,j)=>{
      const v=b.v[SER[si].k],y=Y(v),h=(H-m.b)-y,x=x0+j*bw+1,w=bw-2,r=Math.min(4,h/2,w/2);
      if(h<=0.5)return;
      o+=`<path d="M${x} ${H-m.b}V${y+r}a${r} ${r} 0 0 1 ${r} ${-r}h${w-2*r}a${r} ${r} 0 0 1 ${r} ${r}V${H-m.b}Z" fill="${b.e>=0.5?`url(#hs${si})`:css(SER[si].c)}"${b.e>=0.5?` stroke="${css(SER[si].c)}" stroke-width="1.2" stroke-dasharray="3 2"`:''}/>`;
    });
    if(gi%every===0)o+=`<text x="${cx}" y="${H-8}" text-anchor="middle">${lab(b.l)}</text>`;
    o+=`<rect class="hit" data-i="${gi}" x="${m.l+gi*gw}" y="${m.t}" width="${gw}" height="${H-m.t-m.b}" fill="transparent"/>`;
  });
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML=o;svg._s={W,gw,m,bars};
}
$('bars').addEventListener('mousemove',e=>{
  const t=e.target.closest?e.target:null;if(!t||!t.classList.contains('hit'))return;
  const s=$('bars')._s,b=s.bars[+t.dataset.i],tip=$('btip');
  tip.innerHTML=`<b>${labFull(b.l)}</b>`+SER.map(x=>`<div><span><i style="background:${css(x.c)}"></i>${x.n}</span><span>${fmtK(b.v[x.k])} kWh</span></div>`).join('')+(b.e>0?`<div style="margin-top:4px;color:var(--ink2)"><span>▨ odhad</span><span>${Math.round(b.e*100)} % období</span></div>`:'');
  tip.style.display='block';const rect=$('bars').getBoundingClientRect(),px=(+t.dataset.i+.5)*s.gw+s.m.l,sc=rect.width/s.W;
  const left=px*sc+14;tip.style.left=(left+210>rect.width?left-236:left)+'px';tip.style.top='10px';
});
$('bars').addEventListener('mouseleave',()=>{$('btip').style.display='none';});
function drawTable2(P){
  const rows=P.bars.slice().reverse();
  $('tbl2').innerHTML='<table><thead><tr><th>Období</th><th>Výroba</th><th>Spotřeba</th><th>Prodej</th><th>Nákup</th><th>Nabito</th><th>Vybito</th></tr></thead><tbody>'+
   rows.map(b=>`<tr${b.e>=0.5?' style="font-style:italic;color:var(--ink2)"':''}><td>${labFull(b.l)}${b.e>0?' ~odhad':''}</td><td>${fmtK(b.v[0])}</td><td>${fmtK(b.v[1])}</td><td>${fmtK(b.v[3])}</td><td>${fmtK(b.v[2])}</td><td>${fmtK(b.v[4])}</td><td>${fmtK(b.v[5])}</td></tr>`).join('')+'</tbody></table>';
}
document.querySelectorAll('#per button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#per button').forEach(x=>x.classList.remove('on'));b.classList.add('on');per=b.dataset.p;drawStats();});
$('tbl2btn').onclick=()=>{const t=$('tbl2');t.style.display=t.style.display==='block'?'none':'block';};

/*PUBLIC-STRIP-END*/
/* ---------- spotové ceny ---------- */
let PR=null, prDay=null;
const hh=h=>String(h).padStart(2,'0');
function drawPrices(){
  const now=$('prnow'),sum=$('prsum'),rows=$('prrows'),tabs=$('prday'),foot=$('prfoot');
  const days=PR&&PR.days?Object.keys(PR.days).sort():[];
  if(!PR||!days.length){
    now.innerHTML='<p class="hint">'+(PR?'Ceny z OTE se nepodařilo načíst'+(PR.err?' ('+PR.err+')':'')+'. Zkusím to znovu za pár minut.':'Načítám ceny…')+'</p>';
    sum.style.display='none';rows.innerHTML='';tabs.innerHTML='';foot.textContent='';return;
  }
  if(!prDay||!PR.days[prDay])prDay=PR.days[PR.today]?PR.today:days[0];
  const kc=PR.kurz!=null, unit=kc?'Kč/kWh':'EUR/MWh', dec=kc?2:1;
  const conv=e=>kc?e/1000*PR.kurz+(PR.marze||0):e;
  const vals=PR.days[prDay].map(conv), n=vals.length;
  const isToday=prDay===PR.today, nowH=new Date().getHours();
  tabs.innerHTML=days.length>1?days.map(d=>`<button data-d="${d}" class="${d===prDay?'on':''}">${d===PR.today?'Dnes':'Zítra'}</button>`).join(''):'';
  tabs.querySelectorAll('button').forEach(b=>b.onclick=()=>{prDay=b.dataset.d;drawPrices();});
  const avg=vals.reduce((a,v)=>a+v,0)/n, lo=Math.min(...vals), hi=Math.max(...vals);
  const iLo=vals.indexOf(lo), iHi=vals.indexOf(hi);
  if(isToday&&nowH<n){
    now.innerHTML=`<div class="v">${fmtK(vals[nowH],dec)}<small>${unit}</small></div><div class="s">${hh(nowH)}:00–${hh(nowH+1)}:00 · ${fmtK(PR.days[prDay][nowH],1)} EUR/MWh</div>`;
  }else{
    now.innerHTML=`<div class="v">${fmtK(avg,dec)}<small>${unit}</small></div><div class="s">průměr dne</div>`;
  }
  sum.style.display='grid';
  sum.innerHTML=[['nejlevnější',lo,iLo],['průměr',avg,null],['nejdražší',hi,iHi]]
    .map(([l,v,i])=>`<div><div class="v">${fmtK(v,dec)}</div><div class="l">${l}${i!=null?' · '+hh(i)+' h':''}</div></div>`).join('');
  const s=vals.slice().sort((a,b)=>a-b), q25=s[Math.floor(n*.25)], q75=s[Math.floor(n*.75)], base=Math.min(0,lo), span=(hi-base)||1;
  rows.innerHTML=vals.map((v,h)=>{
    const col=hi===lo?'--gold3':v<=q25?'--good':v>=q75?'--s4':'--gold3', w=Math.max(1,(v-base)/span*100);
    return `<div class="prrow${isToday&&h===nowH?' now':''}"><span>${hh(h)}</span><i style="width:${w.toFixed(1)}%;background:${css(col)}"></i><b>${fmtK(v,dec)}</b></div>`;
  }).join('');
  foot.textContent='Zdroj: burza OTE'+(kc?', kurz ČNB '+fmtK(PR.kurz,3)+' Kč/EUR':'')+'. '+
    (PR.marze?'Včetně přirážky '+fmtK(PR.marze,2)+' Kč/kWh. ':'Stejná cena jako v aplikaci dodavatele. ')+
    (days.length<2?'Ceny na zítřek OTE zveřejňuje odpoledne.':'');
}
async function tickPrices(){
  try{PR=await (await fetch('/api/ceny')).json();}catch(e){}
  drawPrices();
}

/*PUBLIC-STRIP-START*/
/* ---------- ovládání měniče ---------- */
let PIN=null, UNLOCKED=false, CTL=null, CTLEN=null, CTLERR=null, ctlMsg='', ctlBusy=false, ctlAct=0;
const cv={};   // rozdělané hodnoty jezdců, ať se neztratí při překreslení
const MODE_LBL={GENERAL:'Základní',ECO:'Eco',SELF_USE:'Vlastní spotřeba',PEAK_SHAVING:'Ořezání špiček',BACKUP:'Záloha',OFF_GRID:'Ostrovní'};
function priceTxt(){
  if(!PR||!PR.days||!PR.days[PR.today]||PR.kurz==null)return '';
  const e=PR.days[PR.today][new Date().getHours()];
  return e==null?'':' Cena elektřiny teď: '+fmtK(e/1000*PR.kurz+(PR.marze||0),2)+' Kč/kWh.';
}
async function ctlLoad(){
  ctlMsg='';
  try{const j=await (await fetch('/api/ovladani')).json();CTLEN=j.enabled;CTL=j.status;CTLERR=j.err;}
  catch(e){CTLERR='dashboard neodpovídá';}
  drawCtl();
}
async function ctlPost(action,params){
  if(ctlBusy)return;
  ctlBusy=true;ctlAct=Date.now();ctlMsg='Odesílám…';$('ctlmsg')&&($('ctlmsg').textContent=ctlMsg);
  try{
    const r=await fetch('/api/ovladani',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:PIN,action,params:params||{}})});
    const j=await r.json();
    if(r.status===403){PIN=null;UNLOCKED=false;}
    if(j.ok){CTL=j.status;CTLERR=null;UNLOCKED=true;ctlMsg=action==='overit'?'':'Hotovo, změna je v měniči.';delete cv.ms;delete cv.ex;}
    else ctlMsg='Nepovedlo se: '+(j.error||r.status);
  }catch(e){ctlMsg='Nepovedlo se: '+e.message;}
  ctlBusy=false;drawCtl();
}
function ask(text,action,params){if(confirm(text))ctlPost(action,params);}
function drawCtl(){
  const el=$('ctlbody'),st=$('ctlstate');
  if(CTLEN===null){return;}
  if(!CTLEN){st.textContent='vypnuto';
    el.innerHTML='<p class="hint">Ovládání je vypnuté, protože není nastavený přístupový kód. V PowerShellu spusťte <code>setx GW_PIN "váš-kód"</code> (aspoň 6 znaků) a dashboard znovu spusťte.</p>';return;}
  if(!CTL){st.textContent='nedostupné';
    el.innerHTML='<p class="hint">Měnič teď neodpovídá'+(CTLERR?' ('+CTLERR+')':'')+'.</p><button class="btn" id="cretry">Zkusit znovu</button>';
    $('cretry').onclick=ctlLoad;return;}
  if(!UNLOCKED){st.textContent='zamčeno';
    el.innerHTML='<input class="pin" id="pin" type="password" autocomplete="off" placeholder="Přístupový kód"><button class="btn go" id="unlock">Odemknout</button><p class="cmsg" id="ctlmsg">'+ctlMsg+'</p>';
    const go=()=>{PIN=$('pin').value;if(PIN)ctlPost('overit');};
    $('unlock').onclick=go;$('pin').onkeydown=e=>{if(e.key==='Enter')go();};return;}
  st.textContent='odemčeno';
  const S=CTL,soc=NOW?Math.round(NOW.soc):null,P=S.puvodne||{};
  const ms=cv.ms??S.min_soc, ex=cv.ex??S.export_limit, cs=cv.cs??80, cp=cv.cp??100;
  const back=(g,txt)=>P[g]!=null?`<button class="btn" data-back="${g}">Vrátit na původní (${txt(P[g])})</button>`:'';
  let h=`<div class="cgrp" style="border-top:0;padding-top:0"><h3>Režim</h3><div class="chips">`+
    ['GENERAL','ECO','SELF_USE','PEAK_SHAVING','BACKUP'].map(m=>`<button class="chip ${S.work_mode===m?'on':''}" data-m="${m}">${MODE_LBL[m]}</button>`).join('')+
    `</div>`+(S.work_mode==='OFF_GRID'?'<p class="hint">Měnič je v ostrovním režimu, ten odsud nejde měnit.</p>':'')+back('mode',x=>MODE_LBL[x]||x)+`</div>`;
  h+=`<div class="cgrp"><h3>Nabít baterii teď</h3>`+
    (S.charge?`<div class="chargenote">Probíhá nabíjení do ${S.charge.target} %. Samo se ukončí po dosažení cíle a vrátí režim, jaký byl před ním.</div><button class="btn" id="cstop">Zrušit nabíjení</button>`:
    `<div class="hint">Baterie teď: ${soc==null?'–':soc+' %'}. Nabíjí se i ze sítě.</div>
    <label>Cílový stav <b id="cs_v">${cs} %</b></label><input type="range" id="cs" min="20" max="100" step="5" value="${cs}">
    <label>Výkon nabíjení <b id="cp_v">${cp} %</b></label><input type="range" id="cp" min="10" max="100" step="10" value="${cp}">
    <button class="btn go" id="cgo">Nabíjet</button>`)+`</div>`;
  h+=`<div class="cgrp"><h3>Nejnižší stav baterie</h3><div class="hint">Při vybíjení se baterie nevybije pod tuto hodnotu. Teď: ${S.min_soc} %.</div>
    <label>Nejnižší stav <b id="ms_v">${ms} %</b></label><input type="range" id="ms" min="10" max="50" step="5" value="${ms}">
    <button class="btn go" id="mgo">Nastavit</button>${back('min_soc',x=>x+' %')}</div>`;
  h+=`<div class="cgrp"><h3>Limit prodeje do sítě</h3><div class="hint">Teď: ${fmtK(S.export_limit,0)} W. Nula znamená neprodávat.</div>
    <label>Limit <b id="ex_v">${fmtK(ex,0)} W</b></label><input type="range" id="ex" min="0" max="10000" step="100" value="${ex}">
    <button class="btn go" id="ego">Nastavit</button>${back('export',x=>fmtK(x,0)+' W')}</div>`;
  if(Object.keys(P).length>1)h+=`<div class="cgrp"><button class="btn" id="call">Vrátit vše na původní</button></div>`;
  h+=`<div class="cgrp"><h3>Upozornění na tento telefon</h3><div class="hint" id="pushinfo">Zjišťuji…</div>
    <button class="btn go" id="pon" style="display:none">Zapnout</button><button class="btn" id="ptest" style="display:none">Poslat zkoušku</button><button class="btn" id="poff" style="display:none">Vypnout</button></div>`;
  h+=`<p class="cmsg" id="ctlmsg">${ctlMsg}</p>`;
  el.innerHTML=h;
  el.querySelectorAll('[data-m]').forEach(b=>b.onclick=()=>{const m=b.dataset.m;if(m!==S.work_mode)ask('Přepnout režim měniče na „'+MODE_LBL[m]+'“?','mode',{mode:m});});
  el.querySelectorAll('[data-back]').forEach(b=>b.onclick=()=>ask('Vrátit toto nastavení na původní hodnotu?','revert',{group:b.dataset.back}));
  [['cs','%'],['cp','%'],['ms','%'],['ex','W']].forEach(([id,u])=>{const r=$(id);if(!r)return;
    r.oninput=()=>{cv[id]=+r.value;$(id+'_v').textContent=(id==='ex'?fmtK(r.value,0):r.value)+' '+u;};});
  if($('cgo'))$('cgo').onclick=()=>ask('Nabíjet baterii na '+$('cs').value+' % výkonem '+$('cp').value+' %? Nabíjí se i ze sítě.'+priceTxt()+' Po dosažení cíle se režim sám vrátí zpět (nejdéle za 8 hodin).','charge',{target:+$('cs').value,power:+$('cp').value});
  if($('cstop'))$('cstop').onclick=()=>ask('Zrušit nabíjení a vrátit režim, jaký byl před ním?','revert',{group:'charge'});
  $('mgo').onclick=()=>ask('Nejnižší stav baterie při vybíjení nastavit na '+$('ms').value+' %?','min_soc',{value:+$('ms').value});
  $('ego').onclick=()=>ask('Limit prodeje do sítě nastavit na '+fmtK($('ex').value,0)+' W?','export',{value:+$('ex').value});
  if($('call'))$('call').onclick=()=>ask('Vrátit všechna změněná nastavení na původní hodnoty?','revert',{group:'all'});
  $('pon').onclick=pushOn;$('poff').onclick=pushOff;$('ptest').onclick=pushTest;
  pushState();
}
/* ---------- upozornění na telefon ---------- */
const u8=b=>Uint8Array.from(atob((b+'='.repeat((4-b.length%4)%4)).replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
const say=t=>{ctlMsg=t;if($('ctlmsg'))$('ctlmsg').textContent=t;};
async function pushReg(){
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||!window.isSecureContext)return null;
  try{return await Promise.race([navigator.serviceWorker.ready,new Promise(r=>setTimeout(()=>r(null),4000))]);}catch(e){return null;}
}
async function pushState(){
  if(!$('pushinfo'))return;
  const reg=await pushReg();
  if(!$('pushinfo'))return;
  if(!reg){$('pushinfo').textContent='Tady upozornění nejdou. Na iPhonu (iOS 16.4 a novější) otevřete dashboard z ikony na ploše přes adresu s https://, ne přes WiFi adresu s http://.';return;}
  const s=await reg.pushManager.getSubscription();
  $('pushinfo').textContent=s?'Zapnuto. Přijde: baterie plná, nabíjení skončilo, měnič nedostupný 10 minut a zase online.':'Vypnuto. Po zapnutí přijde: baterie plná, nabíjení skončilo, měnič nedostupný 10 minut a zase online.';
  $('pon').style.display=s?'none':'';$('poff').style.display=$('ptest').style.display=s?'':'none';
}
async function pushPost(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({pin:PIN},body||{}))});
  const j=await r.json().catch(()=>({ok:false,error:'neplatná odpověď'}));
  if(r.status===403){PIN=null;UNLOCKED=false;ctlMsg=j.error||'';drawCtl();}
  return j;
}
async function pushOn(){
  ctlAct=Date.now();
  try{
    const reg=await pushReg();if(!reg){say('Tady upozornění nejdou.');return;}
    const k=await (await fetch('/api/push/klic')).json();
    if(!k.enabled){say('Na počítači chybí balíček pywebpush (pip install pywebpush).');return;}
    if(await Notification.requestPermission()!=='granted'){say('Upozornění nejsou povolená v nastavení telefonu.');return;}
    let s=await reg.pushManager.getSubscription();
    if(!s)s=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:u8(k.key)});
    const j=await pushPost('/api/push/prihlasit',{subscription:s.toJSON()});
    if(j.ok)say('Upozornění zapnuta.');else if(PIN!==null)say('Nepovedlo se: '+j.error);
  }catch(e){say('Nepovedlo se: '+e.message);}
  pushState();
}
async function pushOff(){
  ctlAct=Date.now();
  try{
    const reg=await pushReg(),s=reg&&await reg.pushManager.getSubscription();
    if(s){await pushPost('/api/push/odhlasit',{endpoint:s.endpoint});await s.unsubscribe();}
    say('Upozornění vypnuta.');
  }catch(e){say('Nepovedlo se: '+e.message);}
  pushState();
}
async function pushTest(){
  ctlAct=Date.now();say('Posílám zkoušku…');
  const j=await pushPost('/api/push/test');
  if(PIN!==null)say(j.ok?'Zkouška odeslána. Za chvilku se ukáže na telefonu.':'Nepovedlo se: '+j.error);
}
setInterval(()=>{if(UNLOCKED&&Date.now()-ctlAct>600000){PIN=null;UNLOCKED=false;ctlMsg='Po 10 minutách nečinnosti se ovládání zamklo.';drawCtl();}},30000);
setInterval(()=>{const a=document.activeElement;if(!ctlBusy&&!(a&&(a.type==='range'||a.id==='pin')))ctlLoad();},60000);

/*PUBLIC-STRIP-END*/
/* ---------- smyčka ---------- */
function render(){
  drawFlow();drawToday();
  const d=$('dot'),s=$('status');
  if(ERR){d.className='dot err';s.textContent='výpadek spojení s měničem: '+ERR;}
  else if(NOW){const age=Date.now()/1000-NOW.ts;
    if(age>180){d.className='dot err';s.textContent='data jsou stará '+Math.round(age/60)+' min (naposledy '+hhmm(NOW.ts)+')';}
    else{d.className='dot ok';s.textContent='živě · '+hhmmss(NOW.ts);}}
  if(NOW)$('model').textContent=NOW.model||'';
  drawChart();drawTable();
}
async function tick(){
  try{const j=await (await fetch('/api')).json();HIST=j.hist;NOW=j.now;ERR=j.err;}
  catch(e){ERR='dashboard neběží (zkontroluj okno s příkazovou řádkou)';}
  render();
}
/*PUBLIC-STRIP-START*/async function tickStats(){
  try{STATS=await (await fetch('/api/stats')).json();}catch(e){}
  drawStats();
}/*PUBLIC-STRIP-END*/
function themePref(){try{return localStorage.getItem('theme')||'auto';}catch(e){return 'auto';}}
const mqLight=matchMedia('(prefers-color-scheme: light)');
function applyTheme(redraw){
  const p=themePref(),light=p==='light'||(p==='auto'&&mqLight.matches);
  document.documentElement.dataset.theme=light?'light':'dark';
  const m=document.querySelector('meta[name=theme-color]');if(m)m.content=light?'#f3efe6':'#060709';
  document.querySelectorAll('#themeseg button').forEach(b=>b.classList.toggle('on',b.dataset.t===p));
  if(redraw){drawFlow();drawChart();drawStats();drawPrices();}
}
document.querySelectorAll('#themeseg button').forEach(b=>b.onclick=()=>{try{localStorage.setItem('theme',b.dataset.t);}catch(e){}applyTheme(true);});
(mqLight.addEventListener?mqLight.addEventListener.bind(mqLight,'change'):mqLight.addListener.bind(mqLight))(()=>{if(themePref()==='auto')applyTheme(true);});
applyTheme(false);
addEventListener('resize',()=>{drawFlow();drawChart();drawStats();});
tick();/*PUBLIC-STRIP-START*/tickStats();/*PUBLIC-STRIP-END*/tickPrices();/*PUBLIC-STRIP-START*/ctlLoad();/*PUBLIC-STRIP-END*/setInterval(tick,5000);/*PUBLIC-STRIP-START*/setInterval(tickStats,60000);/*PUBLIC-STRIP-END*/setInterval(tickPrices,300000);setInterval(drawPrices,60000);
try{if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');}catch(e){}
</script></body></html>
"""

# ---------------------------------------------------------------------------
# Ikona aplikace (PNG se kreslí přímo v Pythonu, bez dalších knihoven)
# ---------------------------------------------------------------------------
_ICONS = {}


def make_icon(n):
    if n in _ICONS:
        return _ICONS[n]

    def inside_tri(px, py, a, b, c):
        d1 = (px - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (py - b[1])
        d2 = (px - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (py - c[1])
        d3 = (px - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (py - a[1])
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))

    cx, cy, r = 0.5 * n, 0.5 * n, 0.30 * n
    roof = ((0.5 * n, 0.30 * n), (0.29 * n, 0.51 * n), (0.71 * n, 0.51 * n))
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        for x in range(n):
            dx, dy = x + .5 - cx, y + .5 - cy
            dist = math.hypot(dx, dy)
            glow = max(0.0, 1 - dist / (0.62 * n)) ** 2
            R, G, B = 8 + 70 * glow, 9 + 56 * glow, 12 + 28 * glow          # tmavé pozadí se zlatým svitem
            edge = max(0.0, min(1.0, (r - dist) + .5))                       # okraj kruhu s vyhlazením
            if edge > 0:
                t = (y + .5 - (cy - r)) / (2 * r)
                gr, gg, gb = 240 - 80 * t, 217 - 98 * t, 166 - 106 * t       # zlatý přechod
                R, G, B = R + (gr - R) * edge, G + (gg - G) * edge, B + (gb - B) * edge
                px, py = x + .5, y + .5
                if inside_tri(px, py, *roof) or (0.35 * n <= px <= 0.65 * n and 0.51 * n <= py <= 0.665 * n):
                    window = (0.395 * n <= px <= 0.465 * n and 0.545 * n <= py <= 0.61 * n) or (0.535 * n <= px <= 0.605 * n and 0.545 * n <= py <= 0.61 * n)
                    k = 0.0 if window else 1.0
                    base = (16, 17, 21)
                    gold = (240, 217, 166)
                    col = base if k else gold
                    R, G, B = (R + (col[0] - R) * edge, G + (col[1] - G) * edge, B + (col[2] - B) * edge)
            raw += bytes((int(R), int(G), int(B)))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    _ICONS[n] = png
    return png


MANIFEST = json.dumps({
    "name": "Solární elektrárna", "short_name": "Solár", "start_url": "/", "scope": "/",
    "display": "standalone", "orientation": "portrait", "background_color": "#060709", "theme_color": "#060709",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
})
SW_JS = ("self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',()=>{});"
         "self.addEventListener('push',e=>{let d={};try{d=e.data.json();}catch(_){}"
         "e.waitUntil(self.registration.showNotification(d.title||'Solár',{body:d.body||'',icon:'/icon-192.png',tag:d.tag||undefined}));});"
         "self.addEventListener('notificationclick',e=>{e.notification.close();"
         "e.waitUntil(clients.matchAll({type:'window'}).then(l=>l.length?l[0].focus():clients.openWindow('/')));});")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/manifest.json":
            body, ctype = MANIFEST.encode(), "application/manifest+json"
        elif path == "/sw.js":
            body, ctype = SW_JS.encode(), "application/javascript"
        elif path in ("/icon-180.png", "/icon-192.png", "/icon-512.png"):
            body, ctype = make_icon(int(path[6:9])), "image/png"
        elif self.path.startswith("/api/stats"):
            body = json.dumps(compute_stats()).encode()
            ctype = "application/json"
        elif path == "/api/push/klic":
            body = json.dumps({"enabled": bool(upozorneni.public_key()), "key": upozorneni.public_key(),
                               "count": upozorneni.count()}).encode()
            ctype = "application/json"
        elif path == "/api/ovladani":
            out = {"enabled": ovladani.enabled(), "status": None, "err": None}
            if out["enabled"]:
                try:
                    out["status"] = run_cmd(lambda inv, soc: ovladani.status(inv))
                except Exception as e:  # noqa: BLE001
                    out["err"] = str(e) or type(e).__name__
            body = json.dumps(out).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/ceny"):
            body = json.dumps(prices_payload()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api"):
            with lock:
                body = json.dumps({"now": state["now"], "hist": state["hist"], "err": state["err"]}).encode()
            ctype = "application/json"
        else:
            body = HTML.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Zápisy: /api/ovladani (měnič) a /api/push/* (upozornění). Všechno vyžaduje přístupový kód."""
        path = self.path.split("?")[0]
        if path not in ("/api/ovladani", "/api/push/prihlasit", "/api/push/odhlasit", "/api/push/test"):
            return self._json(404, {"ok": False, "error": "Neznámá adresa"})
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json(415, {"ok": False, "error": "Očekává se JSON"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 8192:
                raise ValueError
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError
            action = req.get("action", "")
            params = req.get("params") or {}
            if not isinstance(action, str) or not isinstance(params, dict):
                raise ValueError
            if path == "/api/ovladani" and not action:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "Neplatný požadavek"})
        ok, msg = ovladani.check_pin(req.get("pin"))
        if not ok:
            return self._json(403, {"ok": False, "error": msg})
        if path != "/api/ovladani":
            return self._push_post(path, req)
        who = self.client_address[0]
        try:
            res = run_cmd(lambda inv, soc: ovladani.execute(inv, action, params, who, soc))
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            ovladani._log(who, "CHYBA " + action, str(e) or type(e).__name__)
            return self._json(500, {"ok": False, "error": str(e) or type(e).__name__})
        self._json(200, {"ok": True, "status": res})

    def _push_post(self, path, req):
        if not upozorneni.public_key():
            return self._json(503, {"ok": False, "error": "Upozornění jsou vypnutá (chybí balíček pywebpush)."})
        try:
            if path == "/api/push/prihlasit":
                upozorneni.subscribe(req.get("subscription"))
            elif path == "/api/push/odhlasit":
                upozorneni.unsubscribe(str(req.get("endpoint", "")))
            else:
                sent = upozorneni.notify("Zkouška upozornění", "Pokud to vidíš, upozornění fungují.", "test", wait=True)
                if not sent:
                    return self._json(502, {"ok": False, "error": "Zkouška se nepodařila odeslat. Zapněte upozornění znovu."})
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        self._json(200, {"ok": True, "count": upozorneni.count()})

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False   # Windows jinak dovolí druhému dashboardu sdílet port a oba pak zapisují do stejných CSV


if __name__ == "__main__":
    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"Dashboard už běží (port {PORT} je obsazený). Druhá kopie se nespustí, aby se nezdvojovala data.")
        raise SystemExit(1)
    load_history()
    ovladani.load_backup()
    push_ok = upozorneni.init()
    ovladani.oznam = upozorneni.notify
    sems_data = load_sems()
    n_sems = sum(len(v) for v in sems_data.values())
    print(f"Složka s dashboardem: {HERE}")
    if n_sems:
        print(f"Historie ze SEMS: načteno {n_sems} záznamů ({len(sems_data['d'])} dní, {len(sems_data['m'])} měsíců, {len(sems_data['y'])} let).")
    else:
        print("POZOR: historie ze SEMS NENÍ načtena. Soubor goodwe_sems.csv musí ležet ve stejné složce jako goodwe_dashboard.py")
        print("       (přesný název, bez čísla v závorce). Bez něj mají Týden, Měsíc a Rok jen data od dnešního spuštění.")
    threading.Thread(target=poll_thread, daemon=True).start()
    threading.Thread(target=price_thread, daemon=True).start()
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        lan_ip = sk.getsockname()[0]
        sk.close()
    except OSError:
        lan_ip = None
    print("Upozornění na telefon: " + ("zapnuta (přihlášených telefonů: %d)" % upozorneni.count() if push_ok else "vypnuta (chybí balíček pywebpush: pip install pywebpush)"))
    print("Veřejná stránka: " + verejne.start(prices_payload))
    print("Ovládání měniče: " + ("ZAPNUTO (přístupový kód nastaven)" if ovladani.enabled() else "vypnuto (chybí proměnná GW_PIN)"))
    print(f"Dashboard běží: http://localhost:{PORT}   (ukončení: Ctrl+C)")
    if lan_ip:
        print(f"Z telefonu na stejné WiFi: http://{lan_ip}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Konec.")
