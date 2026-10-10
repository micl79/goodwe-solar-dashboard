"""
Sestaví veřejnou stránku (jen pro čtení) z dashboardu do vyloha/public.

Spuštění:  python sestav_vyloha.py
Vzhled je stejný jako v dashboardu. Vyřízí se všechno, co něco odesílá (značky PUBLIC-STRIP v goodwe_dashboard.py). Analýza energie zůstává a ovládání
je jen ukázka v prohlížeči (DEMO níže). Stránka čte data z /api/snapshot místo z domácího dashboardu. Po změně vzhledu spusťte znovu a nahrajte do GitHubu.
"""
import os
import re
import sys

import goodwe_dashboard as gd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "vyloha", "public")


DEMO = r"""/* ---------- ukázka ovládání: běží jen v prohlížeči návštěvníka, na server se NIC neodesílá ---------- */
CTLEN=true;UNLOCKED=true;
CTL={work_mode:'ECO',min_soc:15,export_limit:4000,puvodne:{},charge:null};
async function ctlLoad(){drawCtl();}
async function ctlPost(action,p){
  p=p||{};const P=CTL.puvodne;
  const back=g=>{if(g==='charge')CTL.charge=null;
    else if(g==='mode'&&P.mode!=null){CTL.work_mode=P.mode;delete P.mode;}
    else if(g==='min_soc'&&P.min_soc!=null){CTL.min_soc=P.min_soc;delete P.min_soc;}
    else if(g==='export'&&P.export!=null){CTL.export_limit=P.export;delete P.export;}};
  if(action==='mode'){if(P.mode==null)P.mode=CTL.work_mode;CTL.work_mode=p.mode;CTL.charge=null;}
  else if(action==='charge'){CTL.charge={target:p.target,power:p.power};}
  else if(action==='min_soc'){if(P.min_soc==null)P.min_soc=CTL.min_soc;CTL.min_soc=p.value;}
  else if(action==='export'){if(P.export==null)P.export=CTL.export_limit;CTL.export_limit=p.value;}
  else if(action==='revert'){if(p.group==='all'){['charge','mode','min_soc','export'].forEach(back);}else back(p.group);}
  ctlMsg='Ukázka: nic se neodeslalo a v elektrárně se nic nezměnilo.';
  drawCtl();
}
var _drawCtl=drawCtl;
drawCtl=function(){_drawCtl();const b=$('ctlbody');
  if(b){b.insertAdjacentHTML('afterbegin','<div class="chargenote" style="margin:0 0 12px">Ukázka ovládání. Tlačítka vypadají jako skutečná, ale pracují jen v tomto okně prohlížeče. Nic se neodesílá a elektrárna se nemění.</div>');
    $('ctlstate').textContent='ukázka';}};
function pushState(){if($('pushinfo'))$('pushinfo').textContent='V ukázce se upozornění nezapínají.';}
function pushOn(){}function pushOff(){}function pushTest(){}
"""


def sestav(html):
    html = re.sub(r"<!--PUBLIC-STRIP-START-->.*?<!--PUBLIC-STRIP-END-->", "", html, flags=re.S)
    html = re.sub(r"/\*PUBLIC-STRIP-START\*/.*?/\*PUBLIC-STRIP-END\*/", "", html, flags=re.S)
    for a, b in [("fetch('/api/ceny')", "fetch('/api/snapshot?co=ceny')"),
                 ("fetch('/api/stats')", "fetch('/api/snapshot?co=stats')"),
                 ("fetch('/api')", "fetch('/api/snapshot')"),
                 ("setInterval(tick,5000)", "setInterval(tick,15000)"),
                 ("try{if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');}catch(e){}", ""),
                 ("/* ---------- smyčka ---------- */", DEMO + "/* ---------- smyčka ---------- */")]:
        if html.count(a) != 1:
            raise SystemExit(f"Sestavení selhalo, nenalezeno právě jednou: {a[:60]}")
        html = html.replace(a, b)
    # pojistka: veřejná stránka nesmí umět nic odeslat ani znát skutečné adresy ovládání
    for zakazano in ("PUBLIC-STRIP", "/api/ovladani", "/api/push", "'/api/stats'", "method:'POST'", "pushPost",
                     "serviceWorker", "requestPermission"):
        if zakazano in html:
            raise SystemExit(f"Sestavení selhalo: ve veřejné stránce zůstalo '{zakazano}'")
    return html


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    html = sestav(gd.HTML)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    for n in (180, 192, 512):
        with open(os.path.join(OUT, f"icon-{n}.png"), "wb") as f:
            f.write(gd.make_icon(n))
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(gd.MANIFEST)
    print(f"Hotovo: {OUT} ({len(html) // 1024} kB)")
    sys.exit(0)
