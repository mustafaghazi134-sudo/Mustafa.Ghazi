# Independent strategy audit: GO / NO-GO

Date 2026-11-02. Scope: the FAST engine's strategy and every alternative horizon, on identical data and identical
cost assumptions. Nothing here changes the 4h engine, its configuration, its ledger, the risk limits or PAPER mode.

## Verdict

| Question | Decision | Why (one line) |
|---|---|---|
| Live trading of the FAST strategy (1-minute dip mean reversion), on Jupiter | **NO-GO** | Gross edge about +1 bp per trade against a cost of 22–52 bps per round trip. Historical analogs lose 27 bps per trade at realistic fills (95% CI −44 to −10). |
| Live trading of any 1m / 5m / 15m / 1h strategy tested, on Jupiter **or** as an exchange taker | **NO-GO** | All 12 combinations lose money out of sample at base cost. At a 12 bps/side exchange-taker cost, 1m–15m still lose 11–32 bps per trade. At 1h, breakout and trend_ema are within ±7 bps of zero, and dip reversion loses 18 bps. |
| Live trading of anything, today | **NO-GO** | There is **no** forward (live-paper) evidence for any strategy. All evidence below is historical. |
| 4h / 1d trend following (the deployed `tsmom_ema_filter`, and a new `trend_ema` 4h candidate) | **CONTINUE IN PAPER, not proven** | Positive out of sample after costs and robust to delay and cost, but the result is not significant once corrected for 20 tests. It depends on a few very large winners and has weakened every year since 2023. |
| Venue | **Keep Jupiter for the 4h engine. Do not open an exchange account for FAST trading.** | At 4h the strategy breaks even at 146–356 bps/side, so venue cost is secondary. Below 4h no venue's taker cost is low enough. |

The single next action is to let the existing 4h engine accumulate **forward paper** trades with frozen parameters on the
user's PC. Measure real Jupiter round-trip costs at the same time, using `tradebot preflight` and `verify-assets`. Section 10 lists what remains unknown.

## Evidence classes (kept apart throughout)

| Class | What exists | Where |
|---|---|---|
| Observed live trading | **None.** No funded wallet, no real orders. | — |
| Forward paper (real-time prices, simulated fills) | **None** for the FAST engine. The cloud build has no route to Jupiter or Binance. The 4h engine's paper record on the PC is not visible from here. | `research_lab/forward_evidence.py` will classify it when run on the PC |
| Historical replay / walk-forward backtest | Out-of-sample (OOS) trades on real Binance prices, with modelled costs | `results/strategy_audit.json`, `results/robustness.json`, `results/analogs.json` (part B) |
| Scenario simulation | Bootstrap and Monte Carlo resamples of historical paths and trades. These are **not** predictions and **not** results. | `results/analogs.json` (part C), Monte Carlo columns of `strategy_audit.json` |

## Pre-registered acceptance (written into `strategy_audit.py` before the run)

Pooled over BTC, ETH and SOL, at **jup_base** cost (25 bps/side + 1 bp network fee) with **one extra bar of execution
delay**:

- at least 30 OOS trades;
- 95% bootstrap CI of expectancy above 0;
- profit factor at least 1.2;
- positive expectancy on at least 2 of 3 assets;
- expectancy still above 0 at **jup_stress** (50 + 1 bps/side).

Robustness checks added after the run, because one of 20 passed: a Bonferroni CI for 20 tests, removing the best trades, results by year, and a buy-and-hold benchmark. These were not
pre-registered, and they can only make the verdict stricter, never looser.

## 1. Data integrity (`research_lab/data_audit.py` → `results/data_audit.json`)

| Check | Result |
|---|---|
| Primary data vs official `data.binance.vision` files | **0 mismatches** in open/high/low/close/volume over 2,363,040 overlapping minutes. SOL 2024-01 → 2026-06 is 1,313,280 minutes; SOL, BTC and ETH 2026-01 → 08 are 349,920 minutes each. |
| Duplicates, out-of-order rows, non-positive prices, OHLC violations | 0 in every dataset |
| Gaps | The Binance outage on 2023-03-24 (80 min, BTC and SOL), and 1 missing minute on SOL 2023-02-14 |
| Extreme minutes | The spike-and-revert events that fall inside the official files (5 of SOL's 6) are identical there, so they are real market events (for example 2024-03-05 −6.75% then +5.65%). SOL 2023-01-02 +19.5% has no official copy to compare against. It falls in the first walk-forward training window and in no test window. |

**Biases found:**

1. **Survivorship.** BTC, ETH and SOL were chosen in 2026, knowing they survived and that SOL rose about 4×. Every result inherits this.
2. **Lookahead in the old replay.** `ReplayFeed` shows a 1-minute bar's **close** at the bar's **open** time, so prices are labelled 60 s early. It also samples once per minute, so the engine never sees the intrabar low. This produced the Aug-22 profit (section 2). The walk-forward harness avoids both problems: signals use closed bars only, fills happen at the next open plus delay, and stops are checked against the intrabar low before take-profit. `tests/test_strategy_audit.py` verifies that no signal changes when the future is truncated, and a planted one-bar lookahead makes that test fail.
3. **Holdout contamination.** All data up to 2026-10-24 has now been looked at. The 2025+ holdout look is logged in `results/holdout_looks.jsonl`. `trend_ema` was defined during this audit, so its 2025–26 numbers are **not** independent validation.

## 2. The SOL trade on 2026-08-22 (+$1.39 in the old replay, +$1.10 in the new one)

The crash is real. It is in the official Binance file with 45,188 trades in the 05:10 minute.

| UTC | Open | High | Low | Close |
|---|---|---|---|---|
| 05:10 | 95.52 | 95.89 | 89.83 | 90.27 (−5.51%) |
| 05:11 | 90.16 | 90.69 | **87.72** | 90.49 |
| 05:12 | 90.50 | 93.31 | 89.97 | 93.18 |
| 05:13 | 93.18 | 93.77 | 93.06 | 93.73 |

- A live engine knows the 05:10 close at 05:11:00 and would buy near **90.16**. The 05:11 low of 87.72 then triggers
  the 1% stop at 89.26: **−$0.77 on $50 (−153 bps)**.
- The replay bought at 90.49, the 05:11 close, which in reality is only known at 05:12:00. That is one minute after
  the low, and a once-per-minute sampler cannot see the low anyway. A buy at 90.49 makes +$1.1 to +$1.6 under any exit
  assumption and any cost up to 100 bps/leg (`results/analogs.json`, part A).
- **Conclusion:** the profit came from entry timing that the replay's sampling made possible, not from an edge. The
  same rule applied to all 186 comparable dips loses money (section 4).

## 3. Horizons and strategies on identical data and costs (`research_lab/strategy_audit.py`)

**Method:**

- Data: BTC, SOL 2023-01 → 2026-10; ETH 2024-01 → 2026-10.
- Walk-forward: 9-month train, 3-month test, parameters chosen on train only. The OOS period is 2023-10 → 2026-10 (ETH from 2024-10).
- Fills: next bar's open plus 1 bar of delay. Stops are checked before take-profit.
- Each trade uses 100% of capital, so per-trade figures do not depend on position size.

Gross means zero cost and no extra delay. Base is 26 bps/side.

| Horizon | Family | OOS trades | Trades/day | Gross bps | Net bps (base) | 95% CI | PF | Accepted |
|---|---|---|---|---|---|---|---|---|
| 1m | breakout | 75,228 | 75.5 | −0.0 | −52.1 | [−52.4, −51.9] | 0.07 | no |
| 1m | dip_reversion (FAST family) | 10,388 | 10.4 | +1.5 | −51.6 | [−52.4, −50.7] | 0.05 | no |
| 1m | trend_ema | 45,541 | 45.7 | +1.0 | −51.1 | [−51.5, −50.7] | 0.09 | no |
| 5m | breakout | 6,144 | 6.2 | −0.1 | −51.2 | [−54.6, −47.5] | 0.37 | no |
| 5m | dip_reversion | 2,648 | 2.7 | +1.3 | −53.1 | [−56.7, −49.5] | 0.18 | no |
| 5m | trend_ema | 5,308 | 5.3 | +6.9 | −45.1 | [−48.9, −41.1] | 0.41 | no |
| 15m | breakout | 1,952 | 2.0 | +9.5 | −38.7 | [−50.7, −26.3] | 0.65 | no |
| 15m | dip_reversion | 1,097 | 1.1 | −11.2 | −59.6 | [−67.6, −51.2] | 0.29 | no |
| 15m | trend_ema | 1,744 | 1.7 | +12.9 | −42.7 | [−55.0, −30.0] | 0.59 | no |
| 1h | breakout | 518 | 0.52 | +34.4 | −28.7 | [−75.0, +22.3] | 0.85 | no |
| 1h | dip_reversion | 342 | 0.34 | −9.2 | −46.0 | [−70.1, −19.2] | 0.57 | no |
| 1h | trend_ema | 535 | 0.54 | +32.3 | −21.5 | [−60.9, +23.3] | 0.86 | no |
| 4h | breakout | 258 | 0.26 | +97.6 | +32.3 | [−66.0, +141.4] | 1.13 | no |
| 4h | dip_reversion | 252 | 0.25 | +24.6 | −38.8 | [−93.1, +15.1] | 0.79 | no |
| **4h** | **trend_ema** | 170 | 0.17 | +292.3 | **+238.3** | **[+49.0, +489.3]** | 2.13 | **YES** |
| 4h | tsmom_ema_deployed | 95 | 0.10 | +726.8 | +655.7 | [−15.6, +1646.4] | 3.02 | no (CI) |
| 1d | breakout | 52 | 0.05 | +823.6 | +649.5 | [−182.2, +1983.2] | 2.40 | no (CI) |
| 1d | dip_reversion | 29 | 0.03 | −200.1 | −508.2 | [−830.9, −169.4] | 0.24 | no |
| 1d | trend_ema | 25 | 0.03 | +743.7 | +1958.9 | [−82.2, +5107.5] | 4.50 | no (n, CI) |
| 1d | tsmom_ema_deployed | 97 | 0.10 | +691.4 | +588.0 | [−3.5, +1466.2] | 2.83 | no (CI) |

**Cost and delay sensitivity, net bps per trade** (full table: `python -m research_lab.audit_tables`):

| Horizon / family | Exchange taker 12/side | Jupiter optimistic 11/side | Jupiter base, no delay | Jupiter base, +1 bar | Jupiter stress 51/side |
|---|---|---|---|---|---|
| 1m dip_reversion | −23.7 | −21.7 | −50.4 | −51.6 | −101.2 |
| 5m trend_ema (best below 15m) | −17.2 | −15.2 | −45.0 | −45.1 | −94.8 |
| 1h trend_ema (best below 4h) | +6.5 | +8.5 | −19.8 | −21.5 | −71.3 |
| 4h trend_ema | +267.0 | +269.0 | +238.9 | +238.3 | +187.2 |
| 4h tsmom_ema_deployed | +685.6 | +687.7 | +671.2 | +655.7 | +602.6 |
| 1d tsmom_ema_deployed | +617.7 | +619.9 | +636.0 | +588.0 | +535.2 |

**Reading:**

- Below 4h, the gross edge is −11 to +34 bps per round trip, so no venue or speed improvement makes these strategies
  profitable. Execution delay barely matters there, because there is nothing to lose.
- At 4h and 1d, extra delay of 2–3 bars (8–12 hours on 4h) costs only 20–90 bps per trade. Doubling the cost leaves them positive, and break-even is 5–14 times the base cost. **Latency is not the problem at any horizon.**
- `1d trend_ema` shows net above gross (+1959 vs +744). That is because the extra bar of delay changes which 25 trades happen. It is a symptom of a sample far too small to read, not an edge.

## 4. Historical analogs and scenario simulation (`research_lab/analogs.py`)

**B. Historical analogs.** This is backtested, not observed. Every minute from 2023 to 2026 on BTC, ETH and SOL where the FAST rule would
have fired is traded with the engine's own exits: 1% stop checked first, take-profit, 6-bar hold.

| Cost per leg | Entry | n | Win % | Mean bps | 95% CI | PF |
|---|---|---|---|---|---|---|
| 15 bps | next open (realistic best) | 357 | 56.3 | −16.1 | [−26.8, −5.6] | 0.70 |
| 25 bps | next open | 186 | 52.7 | **−27.3** | [−44.4, −10.1] | 0.59 |
| 25 bps | +1 minute | 186 | 40.9 | −40.2 | [−56.6, −22.9] | 0.44 |
| 50 bps | next open | 63 | 49.2 | −54.2 | [−91.9, −16.9] | 0.45 |

- Every year from 2023 to 2026 and every asset is negative at 25 bps.
- The "same-bar" rows in the JSON (delay 0) assume entry at the open of the bar that produced the signal. That is
  lookahead, so they are excluded.
- A win rate above 50% with negative expectancy shows why win rate is the wrong target.

**C. Scenario simulation.** This is a Time-Machine-style simulation, **not a forecast**. 10,000 continuations of the Aug-22 setup were built
by resampling the minutes that followed the 43 historical dips deeper than −3%.

| Outcome | Share |
|---|---|
| Take-profit first | 36.9% |
| Stop first | 60.2% |
| Neither | 2.9% |
| Share needed to break even | 22.7% |

The setup looks favourable in isolation. The simulation breaks the dependence between consecutive minutes and rests on only 43 events. The real-path analogs in part B, which keep that dependence, are negative. Part B is the evidence. Part C is illustration only.

## 5. Robustness of the strategies that looked profitable (`research_lab/robustness.py` → `results/robustness.json`)

Base cost, 1 bar of delay. The Bonferroni interval is the 99.75% two-sided CI that corrects for 20 tested combinations.

| | 4h trend_ema | 4h tsmom (deployed) | 1d tsmom (deployed) | 4h breakout |
|---|---|---|---|---|
| Expectancy, bps per trade | +238 | +656 | +588 | +32 |
| 95% CI | [+44, +482] | [−19, +1626] | [−12, +1447] | [−68, +142] |
| **Bonferroni CI (20 tests)** | **[−27, +655]** | [−187, +2361] | [−184, +2073] | [−108, +208] |
| Without the best 5 trades | +59 [−54, +181] | −47 | −46 | −48 |
| Without the best 10% of trades | **−117** | −271 | −250 | −204 |
| Expectancy by entry year 2023 / 2024 / 2025 / 2026 | +1320 / +283 / +104 / **+28** | +13881 (n=3) / +481 / +32 / +95 | +12290 (n=3) / +403 / +75 / +101 | +869 / +115 / −90 / −69 |
| Delay of 1 / 2 / 3 bars | +238 / +221 / +213 | +656 / +608 / +612 | +588 / +500 / +534 | +32 / +31 / +9 |
| Break-even cost per side | 146 bps | 356 bps | 322 bps | 42 bps |
| Equal-weight portfolio 2024-10 → 2026-10: return / max DD | +35.6% / −25.3% | +21.0% / −37.4% | +28.6% / −34.4% | −38.2% / −57.3% |
| Buy-and-hold of the same three assets, same window | −6.5% / −63.3% | −6.5% / −63.3% | +0.6% / −62.8% | −6.5% / −63.3% |
| Per asset, full OOS, strategy vs hold (BTC / ETH / SOL, %) | +121 vs +198 / +45 vs −11 / +405 vs +377 | +154 vs +198 / +25 vs −11 / +422 vs +377 | +160 vs +190 / +53 vs −3 / +256 vs +331 | +3 vs +198 / −49 vs −11 / +93 vs +377 |
| Time in market | 29–33% | 34–49% | 38–52% | 36–39% |

**What this says:**

- Trend following at 4h–1d did what trend following does in this sample. It sat out most of the 2025–26 decline and kept drawdowns at about 25–37% against 63% for holding. It also lagged buy-and-hold on BTC and earned its profit from a handful of large moves.
- That is a real and well-documented pattern. In this sample it is **not statistically established**: corrected for 20 tests, every interval includes zero, and removing the best 10% of trades makes every one negative.
- The edge per trade has shrunk every year. 2026 shows +28 bps for `trend_ema` and +95 bps for tsmom. Both are still above zero after costs but within noise.
- **4h breakout is rejected.** It is positive only because of 2023–24, and negative in 2025 and 2026.

## 6. Forward paper evidence, latency and frequency

- **Forward evidence: none.** The collector `research_lab/forward_evidence.py` reads `data/fast.db` and `data/scanner.db`. It labels the run LIVE PAPER or REPLAY from the engine's own start events and never mixes them. It reports every trade with timestamps and net P&L, rejections by reason, missed opportunities, latency per stage, rate limits, WebSocket health and alerts, and computes an expectancy CI only once there are at least 5 trades.
- **Example run on the 2026 Q3 SOL replay ledger:** it correctly reports "REPLAY (historical bars; not forward evidence)", with 1 trade, 3,642 rejections for insufficient net edge, 27 missed opportunities, and no CI because there were fewer than 5 trades.
- **Latency:** in replay, signal-to-order takes 1.1 ms at the median (compute only) and the quote stage 0.36 ms. Live network latency to Jupiter is unmeasured, because there is no route from this environment. The delay tests above show latency does not decide profitability at any horizon tested.
- **Trade frequency:** the FAST rule at realistic costs fires about 0.13 times a day across three assets (186 analogs at 25 bps per leg, 0.26 a day at 15 bps), not tens of times a day. The 4h candidates trade about 0.1–0.17 times a day across three assets. Thirty forward trades will take roughly **6–12 months**.

## 7. Rejected strategies

| Strategy | Reason |
|---|---|
| FAST 1m dip mean-reversion (the engine's rule) | Gross edge +1.5 bps per round trip. Negative at every cost, including an exchange taker. Analogs −27 bps (CI below 0). The Aug-22 profit was a sampling artefact. |
| All 1m, 5m and 15m families | Gross −11 to +13 bps per trade, negative at every cost scenario |
| All 1h families | Negative at Jupiter costs. At exchange-taker cost, breakout and trend_ema are within ±7 bps of zero (CIs about ±40 bps) and dip reversion loses 18 bps. |
| 4h and 1d breakout, 4h and 1d dip_reversion | Fail the CI, negative in 2025–26, or both |
| 1d trend_ema | 25 trades, too few to evaluate |
| Any variant that is profitable only at zero cost, with same-bar fills, or once-per-minute replay fills | Optimistic assumptions (task 9) |

## 8. Venue recommendation

- **Keep Jupiter for the 4h engine.** Its cost (assumed 11–26 bps/side, still unmeasured live) is a small fraction of the
  146–356 bps/side break-even. A cheaper venue would add perhaps 20–30 bps per trade on a strategy that trades 30–60 times a year. That is not worth new custody, KYC and API-key risk at this stage.
- **Do not open an exchange account to rescue the FAST strategy.** At 12 bps/side taker cost, every horizon below 1h
  still loses, and 1h is a coin flip.
- Market making (earning the spread as a maker) is the only intraday route the data does not rule out. It is a different business, with inventory risk, adverse selection, an exchange account and an always-on quoting system. It would be a new research project, not a fix. It is not recommended now.

## 9. GO / NO-GO summary

- **NO-GO:** live trading of the FAST engine's strategy, on any venue.
- **NO-GO:** live trading of anything until forward paper evidence exists.
- **GO (paper only):** keep the 4h engine's forward paper test running with frozen parameters. Use the FAST engine's
  live-quote telemetry, still paper only, to measure real Jupiter costs and latency, not to look for intraday edge.
- **Candidate, not adopted:** `trend_ema` 4h (EMA 10/30 crossover above EMA 200, 3×ATR stop, exit when EMA 10 < EMA 30).
  If the user wants it tested, add it as a **separate paper-only shadow** with parameters frozen now, and judge it only on
  forward data. Its 2025–26 backtest numbers cannot count as validation.
- **Review point, pre-registered:** after at least 30 forward paper trades or 6 months, whichever is later, compare the forward
  expectancy and realized Jupiter costs with this report. Live trading stays off unless the forward expectancy CI excludes zero
  **and** realized costs are within the base assumption.

## 10. Remaining uncertainties

1. **No forward data at all.** Every number above is historical or simulated.
2. **Real Jupiter costs are unmeasured.** The 11 / 26 / 51 bps/side scenarios are assumptions. Run `tradebot preflight` and
   `tradebot verify-assets` on the PC.
3. **Signal–venue basis.** Signals come from Binance spot, while fills happen on Solana tokens through Jupiter (SOL, and the wrapped BTC and ETH tokens). The basis between them is unmeasured from here.
4. **Short, single-regime sample.** About 3 years OOS, roughly one bull and one bear phase, with right-tail-dependent returns. Three
   2023 tsmom trades contributed most of its total.
5. **Multiple testing is larger than 20.** Earlier research rounds also tested families and parameters on overlapping data, so the
   Bonferroni correction here is a lower bound.
6. **Survivorship.** The asset universe was chosen with hindsight.
7. **Stop fills.** Stops fill at the stop price. In fast markets real fills are worse, so results are optimistic for stopped trades.
8. **Sizing.** Per-trade figures assume 100% of capital per trade. The portfolio figures assume one third per asset. The real engine
   sizes through the risk engine, so its returns and drawdowns will differ.
9. **Operational risks not modelled:** Jupiter or RPC outages, failed or partial transactions, MEV (bounded by the min-out
   slippage tolerance), wallet custody, and taxes.

## Reproduce (from `TRADEBOT/`, data under `<root>`)

```bash
PYTHONPATH=$PWD python -m research_lab.data_audit --root <root> --out research_lab/results/data_audit.json
```

```bash
PYTHONPATH=$PWD python -m research_lab.analogs --root <root> --out research_lab/results/analogs.json
```

```bash
PYTHONPATH=$PWD python -m research_lab.strategy_audit --root <root> --out research_lab/results/strategy_audit.json
```

```bash
PYTHONPATH=$PWD python -m research_lab.robustness --root <root> --out research_lab/results/robustness.json
```

```bash
PYTHONPATH=$PWD python -m research_lab.audit_tables
```

```bash
PYTHONPATH=$PWD python -m research_lab.forward_evidence --data-dir data --out data/forward_evidence.json
```

The primary data is Binance spot 1-minute klines (the parshant231234-eng mirrors). It was verified against official `data.binance.vision` zips from
the JienWeng and metatim89 repositories, as described in `results/data_audit.json`. Random seeds are fixed in each script. The full audit takes about 10
minutes, most of it on the 1-minute horizon.
