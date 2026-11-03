# Intraday edge-ceiling test: pre-registration

Written and committed on 2026-11-03 **before** any result was computed. The script is
`research_lab/intraday_edge_ceiling.py`, and its output goes to `research_lab/results/intraday_edge_ceiling.json`.

## Question

At horizons of 5 minutes to 4 hours, can any of a small fixed set of public-data features predict BTC, ETH or SOL
moves **larger than the cost of a perpetual-futures round trip**, long or short?

This is a **ceiling** test:

- It conditions on the most extreme 10% of each feature, which is the most favourable case.
- It assumes perfect, instantaneous taker execution at the next 1-minute open.

If even that does not clear costs on held-out data, no strategy built on that feature can, and the feature is dropped.
If it does clear costs, that is **not** a strategy yet. A forward paper test with real order-book fills would still
be needed.

## Data

- Binance spot 1-minute bars with taker-buy volume:
  - BTC and SOL: 2023-01 to 2026-10;
  - ETH: 2024-01 to 2026-10;
  - verified against `data.binance.vision`.
- Hyperliquid hourly funding from 2023-05 (`research_lab/data_ext/`).
- **Train:** to 2024-12-31. Decile thresholds and the sign (momentum or reversal) are chosen here only.
- **Holdout:** 2025-01-01 to 2026-10-24. These features and horizons have never been tested in this project. Earlier
  studies did see these prices, so the holdout is new for these signals, not untouched data.

## Features (fixed; computed at the close of minute *t* from data ≤ *t*)

| ID | Feature |
|---|---|
| F1 | Order-flow imbalance over 5 min: (2·taker-buy − volume) / volume |
| F2 | Order-flow imbalance over 60 min |
| F3 | Return over the last 5 min |
| F4 | Return over the last 60 min |
| F5 | Volatility burst: 5-min return divided by the 24 h standard deviation of 5-min returns (signed) |
| F6 | Hyperliquid funding rate of the last completed hour (crowding) |
| F7 | 60-min return divided by its 30-day standard deviation (signed z-score) |

## Horizons

The forward return is measured from the open of minute *t*+1 over **5, 15, 60 and 240 minutes**.

## Procedure

1. On train, per asset and feature, find the 10th and 90th percentiles.
2. Set the sign to +1 (momentum) if the mean forward return of the top decile minus the bottom decile is positive,
   otherwise −1 (reversal).
3. On holdout, take events where the feature is beyond the train thresholds. Keep **non-overlapping** events: after
   an event, skip until the horizon has passed. The trade direction is sign × side. The signed gross return is
   measured per event.

## Costs (round trip)

| Scenario | Cost | Notes |
|---|---|---|
| **Taker** (base) | **11 bps** | Hyperliquid base taker 4.5 bps × 2 (official fee page), plus 2 bps slippage |
| Optimistic maker | 3 bps | Fill at mid; ignores adverse selection. An upper bound only |

Funding over the holding period is ignored: it is at most about 1.5 bps per hour at 2024–26 levels.

## Statistics and acceptance

- Pooled over the three assets. Block bootstrap by day for confidence intervals.
- Tests: 7 features × 4 horizons = **28**. Bonferroni α = 0.05/28 = 0.0018 (one-sided).

A feature and horizon **passes the ceiling** when all of these hold on holdout:

1. Mean net return after 11 bps is > 0, with the one-sided bootstrap p below 0.0018.
2. It is positive in at least 2 of 3 assets.
3. It is positive in both halves of the holdout: 2025, and 2026 to October.
4. There are at least 200 non-overlapping events.

Passing features would become candidates for a separate, pre-registered forward paper test on order-book data.
**If none passes**, the conclusion is that public 1-minute-bar signals do not support intraday taker trading on these
assets at retail costs. Remaining routes would then need different data (order book, events) or maker execution,
each with its own forward test.

Run once. No features, horizons or thresholds are added after the results are seen.
