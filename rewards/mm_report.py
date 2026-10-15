"""Read-only report for paper_mm.py: rewards vs adverse selection, per offset and market kind."""
import json, os, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
st = json.load(open(os.path.join(HERE, "mm_state.json")))
evs = [json.loads(l) for l in open(os.path.join(HERE, "mm_log.jsonl"))]

settled = {(e["cid"]): (1.0 if e["yes_won"] else 0.0) for e in evs if e["ev"] == "settle"}
mark = {cid: settled.get(cid, m.get("mark")) for cid, m in st["markets"].items()}
kind = {cid: m["kind"] for cid, m in st["markets"].items()}

hours = st["minutes"] / 60
start = min(e["ts"] for e in evs)
print(f"{st['minutes']} sampled minutes (~{hours:.1f}h) since {time.strftime('%Y-%m-%d %H:%M', time.localtime(start))}; "
      f"{len(settled)} markets settled\n")

for o, bk in st["books"].items():
    fills = [e for e in evs if e["ev"] == "fill" and e["off"] == int(o)]
    by = defaultdict(lambda: [0, 0.0, 0.0])  # fills, pnl vs current mark, edge captured at fill (vs mid)
    for f in fills:
        m = mark.get(f["cid"])
        if m is None: continue
        sgn = 1 if f["side"] == "buy" else -1
        b = by[f["kind"]]
        b[0] += 1
        b[1] += sgn * f["sh"] * (m - f["px"])
        b[2] += sgn * f["sh"] * (f["mid"] - f["px"])
    trading = sum(v[1] for v in by.values())
    capital = 200 * len([m for m in st["markets"].values()])
    print(f"=== offset {o}c ===  rewards ${bk['rewards']:.2f}  trading ${trading:+.2f}  "
          f"NET ${bk['rewards'] + trading:+.2f}  ({len(fills)} fills)")
    if hours > 0:
        print(f"    per day: rewards ${bk['rewards'] / hours * 24:.0f}, trading ${trading / hours * 24:+.0f}")
    for k, (n, pnl, spread) in sorted(by.items(), key=lambda x: x[1][1]):
        print(f"    {k:8} fills {n:4}  spread earned ${spread:+8.2f}  marked P&L ${pnl:+8.2f}  "
              f"-> adverse selection ${pnl - spread:+8.2f}")
print("\nTrading P&L uses the latest midpoint for open markets and 0/1 for settled ones;\n"
      "it only becomes trustworthy once most filled markets have settled.")
