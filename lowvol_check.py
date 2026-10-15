"""Selection-bias test: run the underdog rule on 2026 markets with FINAL volume
5k-25k (excluded from the main set). If upsets inflate volume, these markets
should show the underdog winning LESS than its price implies."""
import json, os, random, sys
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import fetch, backtest as bt

LOW = os.path.join(bt.DATA, "lowvol")
os.makedirs(os.path.join(LOW, "hist"), exist_ok=True)
lst = os.path.join(LOW, "markets.jsonl")

if not os.path.exists(lst):
    out, cursor = [], None
    while True:
        p = {"closed": "true", "limit": 100, "volume_num_min": 5000, "volume_num_max": 25000,
             "end_date_min": "2026-01-01T00:00:00Z"}
        if cursor: p["after_cursor"] = cursor
        d = fetch.get(fetch.GAMMA, p)
        if not d: break
        out += [r for r in map(fetch.parse, d.get("markets", [])) if r]
        cursor = d.get("next_cursor")
        if not cursor or not d.get("markets") or len(out) > 60000: break
    random.seed(3)
    out = random.sample(out, min(8000, len(out)))
    with open(lst, "w") as f:
        for m in out: f.write(json.dumps(m) + "\n")
ms = [json.loads(l) for l in open(lst)]
print(len(ms), "low-volume markets sampled; max vol", max(m["vol"] for m in ms), flush=True)

fetch.HIST = os.path.join(LOW, "hist")
with ThreadPoolExecutor(8) as ex: list(ex.map(fetch.pull_hist, ms))

bt.DATA = LOW
mk = bt.load()
for k in [1, 3, 7]:
    r, w, imp = [], [], []
    for m in mk:
        p = bt.price_at(m, k)
        if p is None or not .70 <= max(p, 1 - p) < .85: continue
        t = bt.trade(m, 0 if p >= .5 else 1, p, k)
        r.append(t["roi"]); w.append(t["won"]); imp.append(min(p, 1 - p))
    print(f"k={k} vol 5-25k: n={len(r)} underdog win {np.mean(w):.3f} vs implied {np.mean(imp):.3f} roi {np.mean(r):+.3f}")
