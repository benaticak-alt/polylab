"""Pull resolved binary Polymarket markets + their daily price history.

Writes data/markets.jsonl and data/hist/<market_id>.json. Safe to rerun:
markets are re-listed, histories already on disk are skipped.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import requests

GAMMA = "https://gamma-api.polymarket.com/markets/keyset"
CLOB = "https://clob.polymarket.com/prices-history"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "hist")
MIN_VOLUME = 25_000
END_MIN = "2023-01-01T00:00:00Z"
MIN_DAYS = 3  # skip the 5m/15m/hourly crypto markets - daily bars can't see them

S = requests.Session()


def ts(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    return datetime.fromisoformat(s).timestamp()


def get(url, params, tries=5):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(1 + 2 * i)
    return None


def parse(m):
    try:
        outs = json.loads(m.get("outcomes") or "[]")
        px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        toks = json.loads(m.get("clobTokenIds") or "[]")
    except (ValueError, TypeError):
        return None
    if len(outs) != 2 or len(px) != 2 or len(toks) != 2:
        return None
    if px[0] > 0.99 and px[1] < 0.01:
        winner = 1  # outcome 0 ("YES") won
    elif px[1] > 0.99 and px[0] < 0.01:
        winner = 0
    else:
        return None  # 50/50 refunds, unresolved, weird
    start, end = ts(m.get("startDate")), ts(m.get("closedTime") or m.get("endDate"))
    if not start or not end or (end - start) < MIN_DAYS * 86400:
        return None
    ev = (m.get("events") or [{}])[0]
    return {
        "id": m["id"], "q": m.get("question"), "cat": m.get("category") or ev.get("category"),
        "event": str(ev.get("id") or m["id"]), "start": start, "end": end,
        "sched_end": ts(m.get("endDate")), "yes": winner, "token": toks[0],
        "outcome0": outs[0], "vol": float(m.get("volumeNum") or 0),
        "fees": bool(m.get("feesEnabled")),
    }


def list_markets():
    out, cursor, pages = [], None, 0
    while True:
        p = {"closed": "true", "limit": 100, "volume_num_min": MIN_VOLUME, "end_date_min": END_MIN}
        if cursor:
            p["after_cursor"] = cursor
        d = get(GAMMA, p)
        if not d:
            print("listing failed, stopping at", len(out)); break
        for m in d.get("markets", []):
            r = parse(m)
            if r:
                out.append(r)
        pages += 1
        if pages % 20 == 0:
            print(f"  {pages} pages, {len(out)} usable markets", flush=True)
        cursor = d.get("next_cursor")
        if not cursor or not d.get("markets"):
            break
    return out


def pull_hist(m):
    path = os.path.join(HIST, f"{m['id']}.json")
    if os.path.exists(path):
        return 0
    d = get(CLOB, {"market": m["token"], "interval": "max", "fidelity": 1440})
    h = (d or {}).get("history")
    if h is None:
        return 0
    with open(path, "w") as f:
        json.dump(h, f)
    return 1


if __name__ == "__main__":
    os.makedirs(HIST, exist_ok=True)
    if "--skip-list" not in sys.argv:
        ms = list_markets()
        with open(os.path.join(DATA, "markets.jsonl"), "w") as f:
            for m in ms:
                f.write(json.dumps(m) + "\n")
    ms = [json.loads(l) for l in open(os.path.join(DATA, "markets.jsonl"))]
    print(f"{len(ms)} markets; fetching histories", flush=True)
    done = 0
    with ThreadPoolExecutor(8) as ex:
        for i, n in enumerate(ex.map(pull_hist, ms)):
            done += n
            if i % 1000 == 0:
                print(f"  {i}/{len(ms)} ({done} new)", flush=True)
    print("done")
