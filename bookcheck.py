"""Can you actually BUY the underdog near the history price? Look at live markets
closing in 1-4 days whose favorite sits at 70-85c, and compare the price-history
value with the real best ask on the underdog token."""
import json, time
import numpy as np
import requests
from fetch import ts

S = requests.Session()
now = time.time()
rows, cursor = [], None
for _ in range(40):
    p = {"closed": "false", "limit": 100, "volume_num_min": 5000,
         "end_date_min": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400)),
         "end_date_max": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 4 * 86400))}
    if cursor: p["after_cursor"] = cursor
    d = S.get("https://gamma-api.polymarket.com/markets/keyset", params=p, timeout=30).json()
    for m in d.get("markets", []):
        try:
            toks = json.loads(m["clobTokenIds"]); outs = json.loads(m["outcomes"])
        except Exception:
            continue
        if len(toks) != 2: continue
        rows.append((m["question"], toks, outs, m.get("volumeNum")))
    cursor = d.get("next_cursor")
    if not cursor or not d.get("markets"): break
print(len(rows), "live markets closing in 1-4 days")

out = []
for q, toks, outs, vol in rows:
    h = S.get("https://clob.polymarket.com/prices-history",
              params={"market": toks[0], "interval": "1d", "fidelity": 60}, timeout=30).json().get("history") or []
    if not h: continue
    p = h[-1]["p"]
    if not (0.70 <= max(p, 1 - p) < 0.85): continue
    dog_tok = toks[1] if p >= 0.5 else toks[0]
    dog_hist = 1 - p if p >= 0.5 else p
    b = S.get("https://clob.polymarket.com/book", params={"token_id": dog_tok}, timeout=30).json()
    asks = sorted(float(a["price"]) for a in b.get("asks", []))
    bids = sorted((float(a["price"]) for a in b.get("bids", [])), reverse=True)
    if not asks or not bids: continue
    depth = sum(float(a["size"]) * float(a["price"]) for a in b["asks"] if float(a["price"]) <= asks[0] + 0.01)
    out.append({"q": q, "hist": dog_hist, "ask": asks[0], "bid": bids[0], "depth$": depth, "vol": vol})
    if len(out) >= 150: break

a = np.array([[x["hist"], x["ask"], x["bid"], x["depth$"]] for x in out])
print(f"{len(out)} underdog books checked")
print(f"ask - history price: mean {np.mean(a[:,1]-a[:,0]):+.4f}  median {np.median(a[:,1]-a[:,0]):+.4f}")
print(f"spread (ask-bid):    mean {np.mean(a[:,1]-a[:,2]):.4f}  median {np.median(a[:,1]-a[:,2]):.4f}")
print(f"$ depth within 1c of ask: median {np.median(a[:,3]):.0f}")
print(f"history price == mid? mean|hist-mid| {np.mean(np.abs(a[:,0]-(a[:,1]+a[:,2])/2)):.4f}")
for x in out[:12]:
    print(f"  hist {x['hist']:.3f} bid {x['bid']:.3f} ask {x['ask']:.3f} depth ${x['depth$']:.0f}  {x['q'][:70]}")
