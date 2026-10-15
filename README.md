# PolyLab

A pre-registered strategy tournament over **112,636 resolved Polymarket markets** (2023 – Oct 2026), built to answer one question honestly: *does any simple, systematic betting rule beat Polymarket prices?*

**Answer: no.** One rule looked like it did. It returned **+18% per trade on a 75,000-market holdout at t = 8**, and passed a look-ahead check and an order-book check. Then it failed a selection-bias test. This repo is mostly about how that result got caught.

## The headline result that wasn't

| Check | Question | Outcome |
|---|---|---|
| Train/holdout split | Does it survive a Bonferroni bar over 66 configs and then replicate out of sample? | ✅ train t = +4.0, holdout t = +8.4 |
| Look-ahead (`leakcheck.py`) | Does the daily price bar leak information from later in the day? | ✅ daily bar = price at its own timestamp; re-entering from hourly bars ≥1h earlier gives the same ROI (+17.9% vs +19.0%) |
| Executability (`bookcheck.py`) | Is the history price something you could actually buy at? | ✅ mostly: it's the midpoint, the live ask sits ~1–2c above it, and the edge survives to ~4c of cost (but depth is only ~$100) |
| **Selection (`lowvol_check.py`)** | Did choosing *which markets to study* leak the outcome? | ❌ **yes** |

The rule was: *1–3 days before close, buy the underdog when the favorite trades at 70–85c.* It was strongest on sports matches.

The flaw: the dataset only includes markets whose **final** volume was at least $25k. Upsets attract a burst of late trading, so markets where the underdog won are more likely to clear that cutoff. Filtering on a number you only know after resolution is look-ahead, even though no price is ever read early.

Running the same rule on a sample of the markets the filter dropped:

| Universe (2026 markets, k = 3 days) | Underdog win rate | Implied by price | ROI per trade |
|---|---|---|---|
| Final volume ≥ $25k (studied) | 28.5% | 23.2% | **+18%** |
| Final volume $5k–25k (excluded) | 20.9% | 23.2% | **−14%** |

Combine the two groups and the edge mostly disappears, before even counting markets under $5k. The edge is spread evenly across volume buckets, all the way up to $1M+ markets, so the bias isn't just near the cutoff. That's why a simple "check near the threshold" wouldn't have caught it. Only testing the excluded markets did.

**General lesson:** any filter on a market's *final* statistics (volume, liquidity, comment count, number of traders) can carry outcome information. Select markets on what was knowable at entry time, or don't filter at all.

## Method

- **Data** (`fetch.py`): every closed two-outcome market with ≥3 days of life and ≥$25k volume, from the Gamma keyset API. Daily price history comes from the CLOB `prices-history` endpoint (finer bars aren't retained for old markets). Markets that refunded 50/50 or never resolved are dropped.
- **Strategies** (`backtest.py`): 66 configurations, fixed before any results were seen:
  - buy the favorite or the underdog, by price band and days to close
  - "nothing ever happens" (buy NO)
  - 7-day momentum and reversal
  - a calibration table fit on the training period only
  - a random control
- **Trades:** one per market per config, decided only from prices on or before the entry day, held to resolution, with a fixed cost added to the entry price (default 1c).
- **Statistics:**
  - t-stats are clustered **by close month and by event**, and the more conservative of the two counts.
  - A config must clear a two-sided Bonferroni bar (|t| > 3.37 for 66 configs) on the training period before the holdout is read at all.
- **Split:** markets closing before 2026-01-01 are training (36.8k) and the rest are holdout (75.8k). About 2/3 of all markets closed after October 2025, so an earlier split leaves too little training data. The split was chosen from market counts, not from results.

## Everything else

At 1c cost, none of the other 65 configs produced a usable edge:

- **Longshots are overpriced.** Buying underdogs at 93c+ favorites loses 40–50% per trade, and the random control loses 22%.
- **Favorites aren't underpriced enough** to beat a 1c spread at any horizon.
- **Momentum and reversal** are flat to negative.
- **The calibration model** passed in training, but it was fit on the same volume-filtered universe, so it inherits the same bias.

## Liquidity rewards (`rewards/`)

Polymarket pays makers to quote near the midpoint. The pool is about $209k/day across about 18.7k markets as of 2026-10-14 (from the public `rewards/markets/current` endpoint). `rewards/estimate.py` scores live books with the published quadratic formula and estimates a small maker's share. The snapshot looks extraordinary: $200 quoted 1c from mid would collect most of many daily pools.

**Treat that as an upper bound, not a forecast.** The unclaimed pools sit mostly in same-day weather and index-open markets, where resting quotes get filled by traders who already know the forecast or the futures price. The snapshot ignores that adverse selection, competition that changes minute to minute, and the fact that the pools are configured caps. The honest next step is a paper-maker simulation that logs books every minute and marks fills to market, not live capital.

## Files

| File | Purpose |
|---|---|
| `fetch.py` | Pull markets and daily price histories (resumable) |
| `backtest.py` | The 66-config tournament (`SPLIT`, `COST` env vars) |
| `leakcheck.py` | Re-price trades from hourly bars strictly before entry |
| `bookcheck.py` | Compare history prices to live order books |
| `lowvol_check.py` | Run the rule on markets excluded by the volume filter |
| `rewards/estimate.py` | Liquidity-reward share estimate from live books |
| `results/` | Raw output of every run quoted above |

```bash
python fetch.py                      # ~116k histories; ~20 min
SPLIT=2026-01-01 python backtest.py
```

Uses only free, public, unauthenticated endpoints. No keys and no trading.
