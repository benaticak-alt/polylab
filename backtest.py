"""Strategy tournament over resolved Polymarket markets.

Rules fixed before looking at results:
  - one trade per market per config, decided only from prices up to entry day
  - entry = daily price + COST (spread/slippage haircut), held to resolution, $1 stake
  - TRAIN = markets closing before SPLIT, HOLDOUT = after; holdout is only read
    for configs that survive train with a Bonferroni-corrected t-stat
  - t-stats clustered by close month AND by event; the smaller one counts
"""
import json, math, os, sys
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SPLIT = datetime.fromisoformat(os.environ.get("SPLIT", "2025-01-01")).replace(tzinfo=timezone.utc).timestamp()
COST = float(os.environ.get("COST", "0.01"))
DAY = 86400.0


# ---------- data ----------

def load():
    ms = []
    for line in open(os.path.join(DATA, "markets.jsonl")):
        m = json.loads(line)
        p = os.path.join(DATA, "hist", f"{m['id']}.json")
        if not os.path.exists(p):
            continue
        h = json.load(open(p))
        # keep only bars from while the market was live
        h = [(x["t"], x["p"]) for x in h if m["start"] - DAY <= x["t"] < m["end"]]
        if len(h) < 3:
            continue
        m["t"] = np.array([x[0] for x in h], float)
        m["p"] = np.array([x[1] for x in h], float)
        ms.append(m)
    return ms


def price_at(m, days_before):
    """YES price at `days_before` days before close, None if no fresh bar."""
    cut = m["end"] - days_before * DAY
    i = np.searchsorted(m["t"], cut, side="right") - 1
    if i < 0 or cut - m["t"][i] > 1.5 * DAY:
        return None
    return float(m["p"][i])


def trade(m, side, p_yes, k):
    """side 1 = buy YES at p_yes, 0 = buy NO at 1-p_yes. Returns ROI on $1."""
    q = (p_yes if side else 1 - p_yes) + COST
    if q >= 0.995 or q <= 0.005:
        return None
    won = (m["yes"] == 1) == (side == 1)
    return {"roi": (1 - q) / q if won else -1.0, "won": won, "q": q, "hold": k,
            "end": m["end"], "event": m["event"]}


# ---------- strategies: (name, fn(m) -> trade|None) ----------

BANDS = [(0.55, 0.70), (0.70, 0.85), (0.85, 0.93), (0.93, 0.985)]
HORIZONS = [1, 3, 7, 30]


def make_strategies(calib=None):
    S = []
    for k in HORIZONS:
        for lo, hi in BANDS:
            def fav(m, k=k, lo=lo, hi=hi):
                p = price_at(m, k)
                if p is None: return None
                f = max(p, 1 - p)
                if lo <= f < hi: return trade(m, 1 if p >= 0.5 else 0, p, k)
            def dog(m, k=k, lo=lo, hi=hi):
                p = price_at(m, k)
                if p is None: return None
                f = max(p, 1 - p)
                if lo <= f < hi: return trade(m, 0 if p >= 0.5 else 1, p, k)
            S.append((f"favorite  k={k:<2} fav{lo:.2f}-{hi:.2f}", fav))
            S.append((f"underdog  k={k:<2} fav{lo:.2f}-{hi:.2f}", dog))
    # "nothing ever happens": buy NO on Yes/No questions
    for k in [7, 30, 90]:
        for lo, hi in [(0.03, 0.15), (0.15, 0.30), (0.30, 0.50)]:
            def sq(m, k=k, lo=lo, hi=hi):
                if str(m["outcome0"]).lower() != "yes": return None
                p = price_at(m, k)
                if p is not None and lo <= p < hi: return trade(m, 0, p, k)
            S.append((f"buy-NO    k={k:<2} yes{lo:.2f}-{hi:.2f}", sq))
    # 7-day momentum / reversal
    for k in [3, 7, 30]:
        for th in [0.05, 0.10, 0.20]:
            def mom(m, k=k, th=th, sign=1):
                p, p0 = price_at(m, k), price_at(m, k + 7)
                if p is None or p0 is None or abs(p - p0) < th: return None
                up = p > p0
                return trade(m, int(up if sign > 0 else not up), p, k)
            S.append((f"momentum  k={k:<2} d7>{th:.2f}", mom))
            S.append((f"reversal  k={k:<2} d7>{th:.2f}", lambda m, f=mom: f(m, sign=-1)))
    # learned calibration table (fit on TRAIN only, see fit_calibration)
    if calib:
        for k, (edges, freq) in calib.items():
            for margin in [0.03, 0.06]:
                def cal(m, k=k, edges=edges, freq=freq, margin=margin):
                    p = price_at(m, k)
                    if p is None: return None
                    b = min(np.searchsorted(edges, p, side="right") - 1, len(freq) - 1)
                    f = freq[b]
                    if np.isnan(f): return None
                    if f > p + COST + margin: return trade(m, 1, p, k)
                    if (1 - f) > (1 - p) + COST + margin: return trade(m, 0, p, k)
                S.append((f"calibrate k={k:<2} margin{margin:.2f}", cal))
    def coin(m):
        p = price_at(m, 7)
        if p is not None: return trade(m, int(m["id"][-1]) % 2, p, 7)
    S.append(("random    k=7  (control)", coin))
    return S


def fit_calibration(train):
    """Empirical YES frequency per 5c price bucket, per horizon, from TRAIN."""
    edges = np.linspace(0, 1, 21)
    out = {}
    for k in [1, 7, 30]:
        hit, n = np.zeros(20), np.zeros(20)
        for m in train:
            p = price_at(m, k)
            if p is None: continue
            b = min(np.searchsorted(edges, p, side="right") - 1, 19)
            hit[b] += m["yes"]; n[b] += 1
        freq = np.where(n >= 30, hit / np.maximum(n, 1), np.nan)
        out[k] = (edges, freq)
    return out


# ---------- stats ----------

def clustered_t(rois, keys):
    rois = np.asarray(rois); n = len(rois)
    if n < 10: return 0.0
    mu = rois.mean()
    g = defaultdict(float)
    for r, k in zip(rois, keys): g[k] += r - mu
    var = sum(v * v for v in g.values()) / n ** 2
    G = len(g)
    if G < 2 or var <= 0: return 0.0
    var *= G / (G - 1)
    return mu / math.sqrt(var)


def month(ts):
    d = datetime.fromtimestamp(ts, timezone.utc); return d.year * 12 + d.month


def summarize(trades):
    if not trades: return None
    r = np.array([t["roi"] for t in trades])
    t_m = clustered_t(r, [month(t["end"]) for t in trades])
    t_e = clustered_t(r, [t["event"] for t in trades])
    return {"n": len(r), "events": len({t["event"] for t in trades}),
            "win": np.mean([t["won"] for t in trades]), "roi": r.mean(),
            "t": min(t_m, t_e, key=abs),  # the more conservative clustering
            "t_month": t_m, "t_event": t_e,
            "apr": r.mean() / np.mean([t["hold"] for t in trades]) * 365}


def run(strats, markets):
    return {name: summarize([x for x in (fn(m) for m in markets) if x]) for name, fn in strats}


def fmt(s):
    if not s: return "  (no trades)"
    return (f"n={s['n']:>6} ev={s['events']:>5} win={s['win']:.3f} roi={s['roi']:+.4f} "
            f"t={s['t']:+6.2f} (mon {s['t_month']:+.1f} ev {s['t_event']:+.1f})")


if __name__ == "__main__":
    ms = load()
    train = [m for m in ms if m["end"] < SPLIT]
    hold = [m for m in ms if m["end"] >= SPLIT]
    print(f"{len(ms)} markets  train={len(train)}  holdout={len(hold)}  cost={COST}")
    strats = make_strategies(fit_calibration(train))
    N = len(strats)
    # two-sided Bonferroni z for alpha=0.05 over N configs
    from statistics import NormalDist
    z = NormalDist().inv_cdf(1 - 0.025 / N)
    print(f"{N} configs -> survival bar |t| > {z:.2f} on train\n")

    tr = run(strats, train)
    rows = sorted(((n, s) for n, s in tr.items() if s), key=lambda x: -x[1]["t"])
    print("=== TRAIN (sorted by t) ===")
    for n, s in rows:
        print(f"{n:<34} {fmt(s)}")

    survivors = [n for n, s in rows if s["t"] > z and s["roi"] > 0]
    print(f"\n=== {len(survivors)} survivors -> HOLDOUT (read once) ===")
    ho = run([(n, f) for n, f in strats if n in survivors], hold)
    for n in survivors:
        print(f"{n:<34} {fmt(ho[n])}")
    if "--all-holdout" in sys.argv:
        print("\n=== HOLDOUT, all configs (diagnostic only) ===")
        for n, s in sorted(run(strats, hold).items(), key=lambda x: -(x[1] or {"t": -99})["t"]):
            print(f"{n:<34} {fmt(s)}")
