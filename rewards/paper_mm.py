"""Paper market-maker for Polymarket liquidity rewards. No wallet, no orders.

Every minute, for a rotating set of reward markets:
  - read the live book, compute the reward midpoint (only orders >= min size count)
  - place VIRTUAL quotes at mid +/- OFFSET for several offsets side by side
  - accrue rewards with the published quadratic rule: our share of the min
    two-sided score x the market's daily pool / 1440
  - count a fill whenever a real trade prints THROUGH a quote, or the book
    crosses it (pessimistic: assumes the whole quote fills, which maximises
    adverse selection)
  - hold inventory to resolution, marked to market in the meantime

The question it answers: do rewards outpay the fills we get picked off on?
State survives restarts (mm_state.json); events go to mm_log.jsonl.
"""
import json, math, os, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "mm_state.json")
LOG = os.path.join(HERE, "mm_log.jsonl")
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com/trades"

CAP = 200.0             # $ per market: half quoted each side; also the inventory cap per side
OFFSETS = [1, 2, 3]     # cents from the reward midpoint
N_MARKETS = 50
MIN_RATE = 10.0         # $/day pool
MAX_SPREAD = 0.10       # beyond this the book has no real midpoint -> don't quote
MIN_HOURS_LEFT = 2.0
REFRESH_MIN = 30
SC = 3.0                # single-sided scaling factor from the docs

S = requests.Session()


def log(ev):
    ev["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(ev) + "\n")


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 2 * i); continue
            r.raise_for_status(); return r.json()
        except Exception:
            time.sleep(1 + i)
    return None


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"markets": {}, "books": {str(o): {"pos": {}, "cash": 0.0, "rewards": 0.0, "fills": 0}
                                     for o in OFFSETS}, "last_refresh": 0, "minutes": 0}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w")); os.replace(tmp, STATE)


# ---------- market selection ----------

def kind(q):
    q = (q or "").lower()
    if "temperature" in q or "°" in q: return "weather"
    if "up or down" in q or "opens" in q or "price of" in q or "above" in q or "below" in q: return "price"
    if " vs" in q or "win on" in q or "o/u" in q or "spread" in q: return "sports"
    return "other"


def refresh_universe(st):
    pages, cur = [], ""
    while True:
        d = get(f"{CLOB}/rewards/markets/current", {"next_cursor": cur} if cur else None)
        if not d: break
        pages += d.get("data", []); cur = d.get("next_cursor")
        if not cur or cur == "LTE=" or not d.get("data"): break
    cands = sorted((m for m in pages if (m.get("total_daily_rate") or 0) >= MIN_RATE),
                   key=lambda m: -m["total_daily_rate"])[:400]

    def info(m):
        i = get(f"{CLOB}/markets/{m['condition_id']}")
        if not i or i.get("closed") or not i.get("accepting_orders"): return None
        end = i.get("end_date_iso")
        try:
            hrs = (time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) - time.mktime(time.gmtime())) / 3600
        except Exception:
            hrs = 999
        if hrs < MIN_HOURS_LEFT: return None
        return {"cid": m["condition_id"], "token": i["tokens"][0]["token_id"], "q": i.get("question", ""),
                "kind": kind(i.get("question")), "rate": m["total_daily_rate"],
                "v": m["rewards_max_spread"], "minsize": m["rewards_min_size"],
                "tick": float(i.get("minimum_tick_size") or 0.01), "active": True}
    with ThreadPoolExecutor(8) as ex:
        good = [x for x in ex.map(info, cands) if x]
    # check the books once and keep markets that have a real midpoint
    books = fetch_books([g["token"] for g in good])
    chosen = []
    for g in good:
        b = books.get(g["token"])
        if b and reward_mid(b, g["minsize"]) is not None:
            chosen.append(g)
        if len(chosen) >= N_MARKETS: break
    for m in st["markets"].values():
        m["active"] = False
    for g in chosen:
        old = st["markets"].get(g["cid"], {})
        g["last_trade_ts"] = old.get("last_trade_ts", int(time.time()))
        st["markets"][g["cid"]] = g
    st["last_refresh"] = time.time()
    log({"ev": "refresh", "n": len(chosen), "kinds": {k: sum(c["kind"] == k for c in chosen) for k in
                                                      ("weather", "price", "sports", "other")}})
    print(time.strftime("%H:%M"), f"universe: {len(chosen)} markets", flush=True)


# ---------- book / reward math ----------

def fetch_books(tokens):
    out = {}
    for i in range(0, len(tokens), 100):
        try:
            r = S.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens[i:i + 100]], timeout=30)
            for b in r.json():
                out[b["asset_id"]] = {"bids": [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])],
                                      "asks": [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])],
                                      "last": float(b.get("last_trade_price") or 0)}
        except Exception:
            pass
    return out


def reward_mid(b, minsize):
    bids = [p for p, s in b["bids"] if s >= minsize]
    asks = [p for p, s in b["asks"] if s >= minsize]
    if not bids or not asks: return None
    bb, ba = max(bids), min(asks)
    if ba - bb > MAX_SPREAD or ba <= bb: return None
    return (bb + ba) / 2


def side_score(levels, mid, v, minsize, side):
    t = 0.0
    for p, s in levels:
        d = (mid - p if side == "bid" else p - mid) * 100
        if 0 <= d < v and s >= minsize:
            t += ((v - d) / v) ** 2 * s
    return t


def qmin(q1, q2, mid):
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1 / SC, q2 / SC))
    return min(q1, q2)


def quotes(mid, off, tick):
    bid = math.floor((mid - off / 100) / tick + 1e-9) * tick
    ask = math.ceil((mid + off / 100) / tick - 1e-9) * tick
    return round(bid, 4), round(ask, 4)


# ---------- main loop ----------

def new_trades(m):
    d = get(DATA_API, {"market": m["cid"], "limit": 500})
    if not d: return []
    out = [t for t in d if int(t["timestamp"]) > m["last_trade_ts"]]
    if out:
        m["last_trade_ts"] = max(int(t["timestamp"]) for t in out)
    # express every trade as a YES price
    return [(float(t["price"]) if int(t.get("outcomeIndex", 0)) == 0 else 1 - float(t["price"])) for t in out]


def settle(st):
    held = {cid for bk in st["books"].values() for cid, p in bk["pos"].items() if abs(p["sh"]) > 1e-9}
    for cid in held:
        i = get(f"{CLOB}/markets/{cid}")
        if not i or not i.get("closed"): continue
        win = [t.get("winner") for t in i.get("tokens", [])]
        if True not in win: continue
        yes_won = bool(win[0])
        for o, bk in st["books"].items():
            p = bk["pos"].pop(cid, None)
            if p:
                bk["cash"] += p["sh"] * (1.0 if yes_won else 0.0)
                log({"ev": "settle", "off": o, "cid": cid, "sh": p["sh"], "yes_won": yes_won})
        if cid in st["markets"]:
            st["markets"][cid]["active"] = False


def cycle(st):
    act = [m for m in st["markets"].values() if m["active"]]
    held = {cid for bk in st["books"].values() for cid in bk["pos"]}
    watch = [m for m in st["markets"].values() if m["active"] or m["cid"] in held]
    books = fetch_books([m["token"] for m in watch])
    with ThreadPoolExecutor(8) as ex:
        trades = dict(zip([m["cid"] for m in act], ex.map(new_trades, act)))
    for m in watch:
        b = books.get(m["token"])
        if not b: continue
        mid = reward_mid(b, m["minsize"])
        if mid is not None:
            m["mark"] = mid
        elif b["last"]:
            m["mark"] = b["last"]
        if not m["active"] or mid is None:
            continue
        best_bid = max((p for p, _ in b["bids"]), default=0)
        best_ask = min((p for p, _ in b["asks"]), default=1)
        px = trades.get(m["cid"], [])
        q1 = side_score(b["bids"], mid, m["v"], m["minsize"], "bid")
        q2 = side_score(b["asks"], mid, m["v"], m["minsize"], "ask")
        for o in OFFSETS:
            bk = st["books"][str(o)]
            bid, ask = quotes(mid, o, m["tick"])
            if not (0 < bid < best_ask and best_bid < ask < 1): continue
            pos = bk["pos"].setdefault(m["cid"], {"sh": 0.0})
            # inventory cap: stop adding to a side already holding CAP of exposure
            q_bid = (CAP / 2) / bid if pos["sh"] * bid < CAP else 0.0
            q_ask = (CAP / 2) / (1 - ask) if -pos["sh"] * (1 - ask) < CAP else 0.0
            if q_bid and q_bid < m["minsize"]: q_bid = 0.0
            if q_ask and q_ask < m["minsize"]: q_ask = 0.0
            # rewards for the minute our quotes rested
            if (o * 1.0) < m["v"]:
                w = ((m["v"] - o) / m["v"]) ** 2
                ours = qmin(w * q_bid, w * q_ask, mid)
                theirs = qmin(q1, q2, mid)
                if ours > 0:
                    bk["rewards"] += ours / (ours + theirs) * m["rate"] / 1440
            # fills since last minute: trades printed through us
            if q_bid and any(p < bid for p in px):
                pos["sh"] += q_bid; bk["cash"] -= q_bid * bid; bk["fills"] += 1
                log({"ev": "fill", "off": o, "cid": m["cid"], "side": "buy", "px": bid, "sh": q_bid,
                     "mid": mid, "kind": m["kind"]})
            if q_ask and any(p > ask for p in px):
                pos["sh"] -= q_ask; bk["cash"] += q_ask * ask; bk["fills"] += 1
                log({"ev": "fill", "off": o, "cid": m["cid"], "side": "sell", "px": ask, "sh": q_ask,
                     "mid": mid, "kind": m["kind"]})
    st["minutes"] += 1


def summary(st):
    marks = {cid: m.get("mark") for cid, m in st["markets"].items()}
    rows = []
    for o, bk in st["books"].items():
        inv = sum(p["sh"] * (marks.get(cid) or 0) for cid, p in bk["pos"].items())
        trade_pnl = bk["cash"] + inv
        rows.append(f"{o}c: rewards ${bk['rewards']:.2f}  trading ${trade_pnl:+.2f}  net ${bk['rewards'] + trade_pnl:+.2f}  fills {bk['fills']}")
    return " | ".join(rows)


if __name__ == "__main__":
    st = load_state()
    print(f"paper MM: {N_MARKETS} markets, ${CAP:.0f}/market, offsets {OFFSETS}c - Ctrl+C to stop", flush=True)
    while True:
        t0 = time.time()
        try:
            if t0 - st["last_refresh"] > REFRESH_MIN * 60:
                settle(st)
                refresh_universe(st)
            cycle(st)
            save_state(st)
            if st["minutes"] % 10 == 0:
                s = summary(st)
                log({"ev": "summary", "minutes": st["minutes"], "text": s})
                print(time.strftime("%H:%M"), f"[{st['minutes']} min]", s, flush=True)
        except KeyboardInterrupt:
            save_state(st); break
        except Exception:
            traceback.print_exc()
        if "--once" in sys.argv:
            print(summary(st)); break
        time.sleep(max(1, 60 - (time.time() - t0)))
