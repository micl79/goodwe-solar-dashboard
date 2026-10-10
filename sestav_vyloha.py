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
CTLEN=true;UNLOCKED=true;window.DEMO_MODE=true;
PLAN={settings:{enabled:false,target:100,hodin:3,kapacita:10},plan:null,started:null};
function demoPlanCalc(s){
  if(!s.enabled)return {stav:'vypnuto'};
  if(!PR||!PR.days||PR.kurz==null)return {stav:'ceka'};
  const soc=NOW?NOW.soc:50,need=Math.max(0,s.target-soc)/100*s.kapacita;
  if(need<0.3)return {stav:'netreba',soc:Math.round(soc)};
  const now=new Date();let st=new Date(now.getFullYear(),now.getMonth(),now.getDate(),now.getHours());
  if(now-st>18e5)st=new Date(st.getTime()+36e5);
  const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  const price=t=>{const a=PR.days[iso(t)];return a&&a.length===24?a[t.getHours()]/1000*PR.kurz+(PR.marze||0):null;};
  const by=new Map();for(let i=0;i<24;i++){const t=new Date(st.getTime()+i*36e5),p=price(t);if(p!=null)by.set(t.getTime(),p);}
  const n=s.hodin;let best=null;
  for(const k of [...by.keys()].sort((x,y)=>x-y)){let tot=0,ok=true;for(let i=0;i<n;i++){const p=by.get(k+i*36e5);if(p==null){ok=false;break;}tot+=p;}if(ok&&(best==null||tot<best.tot-1e-9))best={tot,k};}
  if(!best)return {stav:'ceka'};
  const f=ms=>{const d=new Date(ms);return iso(d)+'T'+String(d.getHours()).padStart(2,'0')+':00';};
  const avg=best.tot/n;
  return {stav:'naplanovano',hodin:n,cena:+avg.toFixed(2),bloky:[{id:f(best.k),start:f(best.k),end:f(best.k+n*36e5),cena:+avg.toFixed(2),hotovo:false}],kwh:+Math.min(need,n*4.5).toFixed(1),castecne:need>n*4.5};
}

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
  else if(action==='plan'){PLAN.settings=Object.assign({},PLAN.settings,p);PLAN.plan=demoPlanCalc(PLAN.settings);}
  else if(action==='revert'){if(p.group==='all'){['charge','mode','min_soc','export'].forEach(back);}else back(p.group);}
  ctlMsg=action==='plan'?'Plán nabíjení uložen.':'Ukázka: nic se neodeslalo a v elektrárně se nic nezměnilo.';
  drawCtl();
}
var _drawCtl=drawCtl;
drawCtl=function(){if(PLAN.settings.enabled&&!PLAN.plan)PLAN.plan=demoPlanCalc(PLAN.settings);_drawCtl();const b=$('ctlbody');
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
                 ("fetch('/api')", "fetch('/api/snapshot'+(new URLSearchParams(location.search).get('demo')?'?demo=1':''))"),
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
