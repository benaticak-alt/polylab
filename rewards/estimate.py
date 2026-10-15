"""What would a small maker earn from Polymarket liquidity rewards?

For a sample of reward markets, score the existing book with the published
quadratic rule  S = ((v - s) / v)^2 * size  (v = max spread, s = distance from
mid, both in cents; only orders >= min size count), add our hypothetical quote
($CAP split across both sides, OFFSET cents from mid), and take our share of the
two-sided (min) score times the market's daily pool.
"""
import json, os, random, sys
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import requests

CAP = float(os.environ.get("CAP", "200"))
OFFSET = float(os.environ.get("OFFSET", "1.0"))
HERE = os.path.dirname(os.path.abspath(__file__))
S = requests.Session()
mk = json.load(open(os.path.join(HERE, "current_rewards.json")))


def score(levels, mid, v, minsize, side):
    tot = 0.0
    for lv in levels:
        p, sz = float(lv["price"]), float(lv["size"])
        s = (mid - p if side == "bid" else p - mid) * 100
        if 0 <= s < v and sz >= minsize:
            tot += ((v - s) / v) ** 2 * sz
    return tot


def one(m):
    try:
        info = S.get(f"https://clob.polymarket.com/markets/{m['condition_id']}", timeout=20).json()
        tok = info["tokens"][0]["token_id"]
        b = S.get("https://clob.polymarket.com/book", params={"token_id": tok}, timeout=20).json()
        bids, asks = b.get("bids") or [], b.get("asks") or []
        if not bids or not asks: return None
        bb, ba = max(float(x["price"]) for x in bids), min(float(x["price"]) for x in asks)
        mid = (bb + ba) / 2
        if not 0.10 <= mid <= 0.90: return None  # keep to the single-sided-allowed band
        v, ms, rate = m["rewards_max_spread"], m["rewards_min_size"], m["total_daily_rate"]
        qb, qa = score(bids, mid, v, ms, "bid"), score(asks, mid, v, ms, "ask")
        # ours: half the capital buys YES at mid-OFFSET, half buys NO at (1-mid)-OFFSET (= YES ask)
        pb, pa = mid - OFFSET / 100, mid + OFFSET / 100
        nb, na = (CAP / 2) / pb, (CAP / 2) / (1 - pa)
        if nb < ms or na < ms: return None
        w = ((v - OFFSET) / v) ** 2
        ob, oa = w * nb, w * na
        ours, theirs = min(ob, oa), min(qb, qa)
        share = ours / (ours + theirs) if ours + theirs else 0
        return {"rate": rate, "share": share, "usd_day": share * rate, "spread": ba - bb,
                "theirs_bid": qb, "theirs_ask": qa, "q": info.get("question", "")[:70]}
    except Exception:
        return None


random.seed(11)
buckets = [(5, 20), (20, 50), (50, 100), (100, 300), (300, 1e9)]
sample = []
for lo, hi in buckets:
    pool = [m for m in mk if lo <= (m.get("total_daily_rate") or 0) < hi]
    sample += random.sample(pool, min(60, len(pool)))
with ThreadPoolExecutor(8) as ex:
    rows = [r for r in ex.map(one, sample) if r]

print(f"CAP=${CAP:.0f}/market, quoting {OFFSET}c from mid; {len(rows)} markets scored\n")
for lo, hi in buckets:
    r = [x for x in rows if lo <= x["rate"] < hi]
    if not r: continue
    u = np.array([x["usd_day"] for x in r]); sh = np.array([x["share"] for x in r])
    print(f"pool ${lo}-{hi if hi < 1e8 else 'inf'}/day: n={len(r):3d}  our share median {np.median(sh):.1%}  "
          f"$/day median {np.median(u):.2f} mean {u.mean():.2f}  -> {u.mean() / CAP * 365:.0%} APR on capital")
best = sorted(rows, key=lambda x: -x["usd_day"])[:10]
print("\nbest opportunities in sample:")
for x in best:
    print(f"  ${x['usd_day']:6.2f}/day  share {x['share']:.0%}  pool ${x['rate']:.0f}  spread {x['spread']:.3f}  {x['q']}")
json.dump(rows, open(os.path.join(HERE, f"estimate_cap{int(CAP)}.json"), "w"))
