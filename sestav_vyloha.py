"""
Sestaví veřejnou stránku (jen pro čtení) z dashboardu do vyloha/public.

Spuštění:  python sestav_vyloha.py
Vzhled je stejný jako v dashboardu. Vyřízne se ovládání, analýza a upozornění (značky PUBLIC-STRIP v goodwe_dashboard.py)
a stránka čte data z /api/snapshot místo z domácího dashboardu. Po změně vzhledu spusťte znovu a nahrajte do GitHubu.
"""
import os
import re
import sys

import goodwe_dashboard as gd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "vyloha", "public")


def sestav(html):
    html = re.sub(r"<!--PUBLIC-STRIP-START-->.*?<!--PUBLIC-STRIP-END-->", "", html, flags=re.S)
    html = re.sub(r"/\*PUBLIC-STRIP-START\*/.*?/\*PUBLIC-STRIP-END\*/", "", html, flags=re.S)
    for a, b in [("fetch('/api/ceny')", "fetch('/api/snapshot?co=ceny')"),
                 ("fetch('/api')", "fetch('/api/snapshot')"),
                 ("setInterval(tick,5000)", "setInterval(tick,15000)"),
                 ("try{if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');}catch(e){}", ""),
                 ("<script>\nconst $=id=>", "<script>function drawStats(){}\nconst $=id=>"),
                 ("</style></head>", "@media (min-width:1401px){.layout{grid-template-columns:300px minmax(0,1fr)!important;"
                                     "grid-template-areas:\"side main\"!important}}\n.wrap{max-width:1300px}\n</style></head>")]:
        if html.count(a) != 1:
            raise SystemExit(f"Sestavení selhalo, nenalezeno právě jednou: {a[:60]}")
        html = html.replace(a, b)
    for zakazano in ("PUBLIC-STRIP", "/api/ovladani", "/api/push", "/api/stats", "unlock", "GW_PIN", "setx"):
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
