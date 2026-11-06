# Long/short strategy study: pre-registration

Written and committed on 2026-11-03, **before** any result of this study was computed. The study script is
`research_lab/longshort_study.py`. Its output goes to `research_lab/results/longshort_study.json` and is
summarised in `docs/FEASIBILITY_LONG_SHORT.md`.

The six candidates below are run once with fixed parameters. If none qualifies, the conclusion is that no
qualifying strategy was found. No candidates are added, no parameters are changed and no costs are relaxed after
the results are seen.

## 1. Question

Does a small set of standard rules that go **long in rising markets and short in falling markets**, executed on a
perpetual-futures venue, show an edge after realistic costs and funding? Does it beat the current long-or-flat spot
design? Does a higher-frequency version earn its extra trading costs?

## 2. Data, and what is not untouched

| Item | Detail |
|---|---|
| Prices | Binance spot BTCUSDT, ETHUSDT, SOLUSDT hourly bars:<br>• 2023–2026: 1-minute files verified against `data.binance.vision` (`research_lab/results/data_audit.json`)<br>• earlier: the hourly mirrors in `research_lab/DATA_SOURCES.md`<br>4h and daily bars are resampled from these. Spot prices stand in for perpetual prices; the perp basis is ignored and is usually a few bps. |
| Funding | Binance USD-M funding history (`data.binance.vision`), fetched by `.github/workflows/research-data-fetch.yml`. If it is missing for a period, an assumption of 0.01% per 8 h is used: longs pay, shorts receive. |
| Evaluation window **E** | 2024-01-01 to 2026-10-31. All three assets are complete, from verified 1-minute data. |
| Earlier window **P** | Start of the data to 2023-12-31:<br>• BTC from 2019-01<br>• ETH from 2019-09<br>• SOL from 2022-03; the 2022 SOL mirror is probably a perp feed |

**No window is untouched.** The earlier long-only audit (`STRATEGY_AUDIT_GO_NOGO.md`) used 2023–2026 data, and the
author has seen these markets. The controls are therefore:

- parameters fixed in advance from standard published values and the two deployed rules, with **nothing fitted**
  on any of this data;
- a small, closed candidate list;
- a multiple-testing correction;
- a direction-randomised null;
- a lookahead truncation test.

**The only clean test is forward paper trading.**

## 3. Candidates (closed list, fixed parameters)

| ID | Bar | Long when | Short when | Otherwise |
|---|---|---|---|---|
| C1 `tsmom_ls_1d` | 1d | 30-day return > 0 **and** close > EMA(100 days) | 30-day return < 0 **and** close < EMA(100 days) | Flat |
| C2 `ema_ls_4h` | 4h | EMA12 > EMA26 **and** close > EMA100 | EMA12 < EMA26 **and** close < EMA100 | Flat |
| C3 `donchian_ls_1d` | 1d | Close > highest high of the prior 20 days. Hold until close < lowest low of the prior 10 days | Close < lowest low of the prior 20 days. Hold until close > highest high of the prior 10 days | Flat |
| C4 `ewmac_ls_1d` | 1d | Sign of the average of three trend forecasts, EMA 8/32, 16/64 and 32/128, each divided by 25-day return volatility and clipped to ±2. Position ±1 when \|average\| ≥ 0.5; flat below 0.25 (hysteresis) | Same rule, negative sign | Flat |
| C5 `ema_ls_1h` (the "more frequent" test) | 1h | EMA24 > EMA96 **and** close > EMA480 | EMA24 < EMA96 **and** close < EMA480 | Flat |
| C6 `meanrev_ls_1h` (counter-trend, frequent) | 1h | 1h return z-score (60-bar) < −2.5; exit after 6 bars | 1h return z-score > +2.5; exit after 6 bars | Flat |

C1 and C2 are the deployed tsmom and momentum rules with a mirrored short side. Each candidate is also reported
long-only at Jupiter spot cost, as the comparison with the current architecture.

**Position size:** each asset gets one third of capital at 1x notional. There is no leverage beyond 1x gross per asset.

## 4. Execution and cost model

- A signal is computed at the close of bar *t*. The position change fills at the **open of bar *t*+1**: returns are
  open-to-open.
- Sensitivity check: one extra bar of delay.
- Costs per side, as a fraction of notional traded:

| Scenario | Perpetual (long/short) | Spot on Jupiter (long-only comparison) |
|---|---|---|
| Base | 8 bps: taker 5 + slippage 3 | 26 bps (as `STRATEGY_AUDIT_GO_NOGO.md`) |
| Stress | 16 bps | 51 bps |

- Funding (perpetual only):
  - **Base:** actual funding at each funding time; longs pay a positive rate and shorts receive it.
  - **Stress:** both sides pay \|rate\|.

## 5. Statistics and controls

| Control | What is computed |
|---|---|
| Portfolio | Equal weight across the three assets. Statistics use **daily** portfolio returns. |
| Significance | One-sided stationary block bootstrap (mean block 10 days, 5,000 draws) of mean daily net return > 0. Bonferroni over **12** configurations (6 long/short + 6 long-only): α = 0.05 / 12 = 0.0042. |
| Direction-randomised null | Per asset, keep the candidate's exposure timing and holding periods, and flip each holding episode's direction at random (1,000 draws). Report the percentile of the actual result. |
| Lookahead | For 50 random cut points per candidate and asset: signals from data truncated at *t* must equal the full-data signal at *t*. **Any mismatch invalidates the candidate.** |
| Legs | Long-leg and short-leg net P&L reported separately |
| Other | Per-asset results; turnover; cost and funding drag; trades per day; max drawdown |

## 6. Acceptance (decided in advance)

### Tier A: may be reviewed for real money after forward paper confirmation

All of the following, in window E, at base costs, with next-open execution:

1. annualised net return > 0, and bootstrap p < 0.0042;
2. net return > 0 at stress costs and stress funding;
3. net return > 0 with one extra bar of delay;
4. net return > 0 in window P;
5. at least 2 of 3 assets net positive;
6. actual result above the 95th percentile of the direction-randomised null;
7. at least 30 round trips in E across the three assets;
8. the short leg is net positive after costs and funding, otherwise shorting adds nothing;
9. the lookahead test passes.

### Tier B: worth a forward paper test only, never real money on this evidence

Criteria 2–9, and bootstrap p < 0.05 (uncorrected).

### Otherwise: rejected

**Note on how strict Tier A is.** E has about 2.8 years. Criterion 1 then needs an annualised Sharpe of about
1.6 or more. That is rare for trend rules on three correlated assets, and the bar is deliberately strict. The
expectation, written before running, is that most or all candidates fail Tier A and that, at best, a daily trend
rule reaches Tier B.

## 7. What is reported however the results turn out

- every candidate's numbers, including failures;
- trades per day;
- the validation time a forward test would need to reach 30 round trips;
- the capital fraction that keeps the historical maximum drawdown within the 10% account kill limit.
