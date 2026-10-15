"""Is the daily-bar edge real? Re-price a sample of holdout trades from HOURLY
bars stamped strictly before the entry cut, and compare ROI to the daily-bar ROI.

usage: python leakcheck.py [k] [lo] [hi] [sample]
"""
import json, os, random, sys, time
import numpy as np
import requests
import backtest as bt

K = int(sys.argv[1]) if len(sys.argv) > 1 else 3
LO = float(sys.argv[2]) if len(sys.argv) > 2 else 0.70
HI = float(sys.argv[3]) if len(sys.argv) > 3 else 0.85
N = int(sys.argv[4]) if len(sys.argv) > 4 else 600
S = requests.Session()


def hourly(token, a, b):
    for i in range(4):
        try:
            r = S.get("https://clob.polymarket.com/prices-history",
                      params={"market": token, "startTs": int(a), "endTs": int(b), "fidelity": 60}, timeout=30)
            if r.status_code == 429:
                time.sleep(3 + 3 * i); continue
            return r.json().get("history") or []
        except Exception:
            time.sleep(2)
    return []


ms = [m for m in bt.load() if m["end"] >= bt.SPLIT]
picks = []
for m in ms:
    p = bt.price_at(m, K)
    if p is None: continue
    f = max(p, 1 - p)
    if LO <= f < HI:
        picks.append((m, p))
random.seed(7)
picks = random.sample(picks, min(N, len(picks)))
print(f"k={K} band {LO}-{HI}: checking {len(picks)} holdout trades", flush=True)

rows = []
for i, (m, p_daily) in enumerate(picks):
    cut = m["end"] - K * bt.DAY
    h = hourly(m["token"], cut - 2 * bt.DAY, m["end"])
    pre = [x for x in h if x["t"] <= cut - 3600]  # at least 1h before the cut
    if not pre:
        continue
    p_hr = pre[-1]["p"]
    dog = 0 if p_daily >= 0.5 else 1
    # daily bar stamp vs the hourly path around it
    i_d = np.searchsorted(m["t"], cut, side="right") - 1
    t_bar = m["t"][i_d]
    post = [x["p"] for x in h if t_bar <= x["t"] <= t_bar + bt.DAY]
    rows.append({
        "daily": bt.trade(m, dog, p_daily, K), "hourly": bt.trade(m, dog, p_hr, K),
        "gap": p_daily - p_hr, "q": m["q"],
        # does the daily bar's price match the hour it's stamped at, or later in that day?
        "bar_vs_stamp": p_daily - (min(h, key=lambda x: abs(x["t"] - t_bar))["p"] if h else np.nan),
        "bar_vs_dayend": p_daily - (post[-1] if post else np.nan),
    })
    if i % 100 == 0:
        print(f"  {i}", flush=True)


def roi(key):
    r = [x[key]["roi"] for x in rows if x[key]]
    return np.mean(r), len(r)


print(f"\n{len(rows)} trades with hourly data")
print("daily-bar entry  roi=%+.4f n=%d" % roi("daily"))
print("hourly-pre entry roi=%+.4f n=%d" % roi("hourly"))
g = np.array([x["gap"] for x in rows])
print(f"daily - hourly price: mean {g.mean():+.4f}  mean|gap| {np.abs(g).mean():.4f}")
for k in ["bar_vs_stamp", "bar_vs_dayend"]:
    v = np.array([x[k] for x in rows], float); v = v[~np.isnan(v)]
    print(f"{k}: mean|diff| {np.abs(v).mean():.4f}  share<0.005: {(np.abs(v) < 0.005).mean():.2f}")
json.dump([{k: v for k, v in x.items()} for x in rows], open(os.path.join(bt.DATA, f"leak_k{K}.json"), "w"), default=str)
