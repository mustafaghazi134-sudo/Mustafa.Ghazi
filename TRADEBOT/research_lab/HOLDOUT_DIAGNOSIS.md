# Why the 2025-01 → 2026-09 BTC holdout returned ~0%

Script: `research_lab/holdout_diagnosis.py` (output `results/holdout_diagnosis_BTCUSDT.json`). Data: Binance BTCUSDT 1h
klines from the six cross-checked mirrors (`DATA_SOURCES.md`), resampled to 4h. Costs 10 bps fee + 15 bps slippage per
side, 10% of equity per position, next-open fills, drawdown on mark-to-market equity. Every evaluation on the holdout is
logged in `results/holdout_looks.jsonl` (see `SPLITS.md`).

## 1. The +0.1% figure was not the deployed configuration

The comparison table reported the walk-forward's **final** parameters (30-day momentum, **50-day** EMA), because that is
what a frozen-parameter holdout of the research procedure means. The bot deployed by `apply-profile` runs 30-day momentum
with a **100-day** EMA, fixed in `config.toml` before the holdout was opened. Both were evaluated once:

| Configuration | Trades | Win rate | Profit factor | Net return (10% sizing) | Max DD | Avg hold | Trades held ≤ 7 days | Net of those short trades |
|---|---|---|---|---|---|---|---|---|
| Walk-forward final 30/50 (table figure) | 23 | 26.1% | 1.03 | +0.13% | −3.95% | 10.8 d | 17 of 23 | **−$450** on $10,000 |
| **Deployed 30/100** | 11 | 36.4% | 2.18 | **+2.41%** | −2.22% | 17.9 d | 6 of 11 | −$204 |
| Buy and hold at 10% sizing | 1 | – | – | −1.1% | −6.9% | – | – | – |

The deployed configuration did not lose money and beat buy-and-hold, but +2.4% in 21 months is still far below the
development record (+45.9% over 5 years, roughly +9% a year at the same sizing).

## 2. Where the money went: whipsaws, and costs eating the gross

Gross vs cost decomposition on $10,000 equity:

| Configuration | Gross P&L | Fees + slippage | Net | Costs as share of gross |
|---|---|---|---|---|
| 30/50 | +$128 | $115 | +$13 | **89.5%** |
| 30/100 | +$298 | $56 | +$241 | 18.9% |

For 30/50, 22 of 23 trades were closed by the signal flipping, and 17 lasted a week or less; those 17 lost $450 while
the two long trends (Apr–Jun 2025, Aug–Sep 2026) made +$395. The pattern is textbook trend-follower whipsaw: momentum
crosses zero, the bot buys at the next open, momentum crosses back within days, the bot sells, paying 50 bps round trip
each time. The 100-day EMA filter cut the number of whipsaws roughly in half, which is the entire difference between
the two configurations.

## 3. Did the market change, or did the signal stop working? Both are measured

**Path statistics** (daily closes; development years vs holdout):

| Period | Net change | 30-day momentum sign flips / year | Realised vol (ann.) | >15% swings / year | Best 30 d | Worst 30 d |
|---|---|---|---|---|---|---|
| 2019 | +96% | 30.1 | 74% | 8.0 | +69% | −30% |
| 2020 | +302% | 16.0 | 71% | 4.0 | +61% | −50% |
| 2021 | +61% | 23.1 | 81% | 15.1 | +83% | −45% |
| 2022 | −65% | 34.1 | 64% | 8.0 | +27% | −39% |
| 2023 | +155% | 18.1 | 44% | 6.0 | +48% | −14% |
| 2024 | +119% | 27.0 | 52% | 11.0 | +58% | −22% |
| Development, all | +2265% | 26.8 | 66% | 9.2 | +124% | −50% |
| **Holdout 2025-01 → 2026-09** | **−11%** | **36.7** | **43%** | 4.0 | +37% | −31% |

The holdout had more momentum sign flips per year than any development year (36.7 vs a previous worst of 34.1 in 2022)
combined with the lowest realised volatility in the sample. More flips means more entries and exits; lower volatility
means each trend that does develop pays less. Both hurt a 30-day trend follower and neither is an interpretation.

**Signal quality** (forward return of BTC after bars where the strategy is in-signal vs out-of-signal, non-overlapping
samples, deployed 30/100):

| Horizon | Period | In-signal mean | Out-of-signal mean | Difference | t-stat |
|---|---|---|---|---|---|
| 5 days | Development | +1.87% (n=214) | +0.36% (n=224) | +1.51% | 2.06 |
| 5 days | Holdout | +0.12% (n=44) | −0.17% (n=83) | +0.29% | 0.35 |
| 20 days | Development | +8.02% (n=53) | +1.43% (n=56) | +6.59% | 2.26 |
| 20 days | Holdout | −1.50% (n=11) | +0.74% (n=21) | **−2.24%** | −0.61 |

In development, being in-signal was worth about 6.6 percentage points of 20-day forward return over being out. In the
holdout the difference was −2.2 points. The holdout estimate is noisy (standard error about 3.7 points), but it sits
roughly 2.4 standard errors below the development estimate. That is evidence that the development figures overstate
the forward edge, whatever the cause. It is **not** evidence that the edge is negative: the holdout is consistent with
zero. With 11 in-signal samples it cannot distinguish "the regime was unfavourable" from "the signal has decayed".

**Historical context.** The deployed configuration was run over every 21-month window of the development history
(33 windows, starting 2020-07 to 2023-04):

| Statistic of 21-month returns (10% sizing) | Value |
|---|---|
| Minimum | −0.2% |
| 10th percentile | +0.6% |
| Median | +7.1% |
| 90th percentile | +13.0% |
| Maximum | +27.8% |
| Windows below +1% | 12% (4 of 33) |
| Percentile of the holdout result (+2.4%) | 15th |

A 21-month return near zero has happened before in the development history (the windows beginning in early 2021, when
buy-and-hold lost 34–72%). The holdout outcome is in the bottom sixth of the historical distribution, not outside it.

## 4. Conclusion

1. The deployed configuration returned +2.4% (profit factor 2.18, max drawdown −2.2%) on the holdout, not +0.1%. The
   table figure belongs to the 50-day-EMA variant the walk-forward last selected.
2. The holdout was weak because 2025–2026 produced the most 30-day momentum reversals per year in the data set at the
   lowest volatility, so the strategy paid repeated round-trip costs for trends that did not follow through. Costs
   consumed 19% (deployed) to 90% (30/50) of gross profit.
3. The signal's forward-return edge in the holdout was about 2.4 standard errors below its development value. Expect
   forward returns well below the development record. The right planning number is the historical median 21-month
   return (+7%) at best, with a real chance of windows near zero, not the +9% a year the development total implies.
4. "Market conditions" is supported by measurements (section 3), but the same measurements say the development
   estimate was optimistic. Both statements are true; neither alone is the explanation.

## 5. Improvement tested, and why it is not adopted

The obvious fix for whipsaw is a dead-band: enter only when 30-day momentum exceeds +b, exit only below −b.
Because this hypothesis came from reading the holdout, it was evaluated on **development data only**
(`run_all.py --dev-only`, walk-forward, b ∈ {2%, 5%}); the holdout was not touched (`SPLITS.md`).

| Strategy (dev walk-forward OOS, 4h, 10% sizing) | Asset | Trades | Win rate | PF | Net return | Max DD | Longest losing streak |
|---|---|---|---|---|---|---|---|
| tsmom_ema_filter (deployed) | BTC | 62 | 37.1% | 3.55 | **+45.9%** | −4.4% | 7 |
| tsmom_ema_band (b = 2%) | BTC | 21 | 47.6% | 2.36 | +9.6% | −4.3% | 4 |
| tsmom_ema_filter (deployed) | ETH | 56 | 33.9% | 1.84 | +14.8% | −10.7% | 6 |
| tsmom_ema_band (b = 2%) | ETH | 26 | 34.6% | 2.10 | +10.5% | −11.8% | 11 |

The dead-band raises the win rate and removes whipsaws, but it also enters late on every real trend and gives up
most of the return at the same drawdown. It fails the stated test (higher net profit or lower risk) on independent
development data, so the deployed strategy stays as it is. The forward paper record, with frozen parameters, is the
next evidence; the forward period must not be used to tune anything presented later as validation.
