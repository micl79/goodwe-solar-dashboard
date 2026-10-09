"""
Import historie ze SEMS Portal (exporty "Station Statistical Report ... .xlsx").

Použití:
  1) pip install openpyxl           (jen jednou)
  2) Exporty ze SEMS nech ve Stažených souborech (Downloads) nebo je dej na plochu k dashboardu.
  3) python sems_import.py

Skript najde všechny soubory "Station*Statistical*Report*.xlsx" ve složce Downloads
a ve složce, kde leží tento skript, a uloží je do goodwe_sems.csv.
Dashboard si ten soubor načte sám (stačí ho restartovat).
Export lze kdykoli spustit znovu, novější hodnoty přepíší starší.
"""
import csv
import glob
import os
import re
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "goodwe_sems.csv")

ROWS = {  # začátek názvu řádku v exportu -> pořadí ve výstupu
    "Energy Generation": 0,
    "Import Energy": 1,
    "Grid Export Energy": 2,
    "Charged Energy": 3,
    "Discharge Energy": 4,
}


def num(x):
    if x is None:
        return None
    s = str(x).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def classify(h):
    """Vrátí (druh, klíč) podle hlavičky sloupce, nebo None."""
    h = str(h).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", h)
    if m:
        return "d", f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", h)
    if m:
        return "m", f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", h)
    if m:
        return "m", f"{m.group(1)}-{int(m.group(2)):02d}"
    if re.fullmatch(r"\d{4}", h):
        return "y", h
    return None


def parse(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = next((r for r in rows if r and r[0] == "Station Information"), None)
    if not head:
        print("  přeskočeno (nepoznaná struktura):", os.path.basename(path))
        return {}
    cols = {}
    for i, h in enumerate(head):
        c = classify(h) if h is not None else None
        if c:
            cols[i] = c
    out = {}
    for r in rows:
        label = str(r[1]) if len(r) > 1 and r[1] else ""
        for name, idx in ROWS.items():
            if label.startswith(name):
                for i, (kind, key) in cols.items():
                    v = num(r[i]) if i < len(r) else None
                    out.setdefault((kind, key), [0.0] * 5)[idx] = v or 0.0
    return out


def main():
    dirs = {HERE, os.path.join(os.path.expanduser("~"), "Downloads")}
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, "*Station*Statistical*Report*.xlsx"))
    if not files:
        print("Nenašel jsem žádné exporty ze SEMS (Station Statistical Report ... .xlsx).")
        return 1
    data = {}
    for f in sorted(files, key=os.path.getmtime):   # novější přepíšou starší
        got = parse(f)
        print(f"{os.path.basename(f)}: {len(got)} záznamů")
        data.update(got)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for (kind, key) in sorted(data):
            w.writerow([kind, key] + [round(x, 3) for x in data[(kind, key)]])
    print(f"Uloženo {len(data)} záznamů do {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
