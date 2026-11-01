# Fair comparison: adapted candidates vs the deployed strategies

Date 2026-11-01. Code: `research_lab/candidates.py`, run with `research_lab/run_all.py` at the **effective deployment
configuration** of `tradebot apply-profile`: 4h candles, 10% of equity per position, one position per asset, long-only
spot, entries at next bar open, stop checked on bar low before take-profit, 10 bps fee + 15 bps slippage per side
(**assumption**: no observed Jupiter quote costs exist yet; `verify-assets` will measure them), cost sensitivity at 2x.
Development = rolling walk-forward (12-month train / 3-month test, parameters chosen on train only) on data **before
2025-01-01**; **final holdout = 2025-01-01 onward with parameters frozen from the last development window**, never used
for selection. Drawdown is computed on mark-to-market equity, so open-position losses are included. Data provenance:
`DATA_SOURCES.md`. Repository review and rule transcription: `CANDIDATE_REPOS.md`.

Holdout spans: BTC 2025-01-01 → 2026-09-30 (21 months); ETH and SOL only 2025-01-01 → 2025-04-30 (4 months,
**insufficient**). SOL development evidence is **insufficient** (< 25 trades for every strategy). Buy-and-hold over the
BTC holdout at the same 10% sizing: −1.1% (a sideways period).

## Candidates and their origin

| Candidate | Origin | Adaptation | Already among the earlier 19? |
|---|---|---|---|
| pine_ultimate_long | dextergocode/ultimate-crypto-trading-bot (Pine v5, long+short, 1h) | long side only; repainting daily filter replaced by previous completed day; dead "reversal exit" dropped | partially (SMA cross, MACD, ADX-style filters) |
| tr3_long | Krishhiv/HMM_TR_Alg TR3 engine | full 7-layer exit stack reproduced; 1.3x equity-curve throttle (leverage) removed; costs added (original: zero) | strong overlap with donchian_turtle / ema_cross_trend entries |
| hmm_tr3_long | Krishhiv/HMM_TR_Alg | as above plus 3-state Gaussian HMM daily regime gate, walk-forward refit (730 d, every 30 d), causal decoding | new |
| pairs_longleg | sujith-kamme/statistical-arbitrage-crypto | **new strategy**: long the cheap leg of a 2-asset spread (BTC/ETH/SOL) when z < −2; original is long/short cointegration baskets on 18 alts | conceptual overlap with mean reversion only |

## Author claims (UNVERIFIED, quoted from the repositories; none reproduced)

| Repository | Claim | Why it cannot be carried over |
|---|---|---|
| ultimate-crypto-trading-bot | "Total Trades 150+, Win Rate 65%, Max Drawdown 15%, Profit Factor 1.8, Sharpe 1.2" BTC/USD 1h, 6 months | no artefact; daily filter repaints; slippage 0; includes shorts |
| HMM_TR_Alg | portfolio "Total Return 140.83%, CAGR 21.51%, Sharpe 1.461, Max Drawdown −8.52%" 2021-12-31 → 2026-07-06 | zero fees and slippage; 1.3x equity-curve leverage; −8% per-trade loss floor; TR3 parameters optimised on the test window; trade logs are un-fetched Git LFS pointers |
| statistical-arbitrage-crypto | OOS "Sharpe 1.764 net, +5.61%, Max DD −1.38%, Win Rate 72.2%" | 9 trades; requires shorting 9 alt legs not on Solana; survivorship-biased universe |

## Reproduced results (4h, deployment sizing). Dev = walk-forward out-of-sample before 2025; Hold = frozen-parameter 2025+

| Strategy | Assets | Test period | Trades | Win rate | Avg win / avg loss | Profit factor | Net return | Max drawdown | Longest losing streak | Jupiter compatibility |
|---|---|---|---|---|---|---|---|---|---|---|
| **tsmom_ema_filter (deployed)** | BTC | Dev 2020-01→2024-12 | 62 | 37.1% | 6.0 | **3.55** | **+45.9%** | **−4.4%** | 7 | yes (cbBTC) |
| | BTC | Hold 2025-01→2026-09 | 23 | 26.1% | 2.9 | 1.03 | +0.1% | −4.0% | 9 | |
| | ETH | Dev 2020-09→2024-12 | 56 | 33.9% | 3.6 | 1.84 | +14.8% | −10.7% | 6 | yes (WETH) |
| | ETH | Hold (4 mo) | 0 | – | – | – | 0.0% | 0.0% | 0 | insufficient |
| | SOL b1 | Dev 2023-03→2023-11 | 8 | 25.0% | 36.7 | 12.2 | +15.9% | −5.4% | 5 | insufficient sample |
| tsmom_30d | BTC | Dev | 81 | 38.3% | 3.9 | 2.39 | +33.3% | −9.6% | 7 | yes |
| | BTC | Hold | 43 | 27.9% | 2.4 | 0.92 | −0.6% | −4.3% | 12 | |
| | ETH | Dev | 76 | 35.5% | 4.1 | 2.28 | +34.3% | −12.0% | 5 | yes |
| momentum / ema_cross_trend (deployed, secondary) | BTC | Dev | 101 | 31.7% | 4.4 | 2.04 | +24.8% | −8.6% | 9 | yes |
| | BTC | Hold | 31 | 29.0% | 2.6 | 1.07 | +0.3% | −2.6% | 7 | |
| | ETH | Dev | 85 | 31.8% | 3.4 | 1.58 | +12.0% | −6.7% | 15 | yes |
| tr3_long (adapted) | BTC | Dev | 141 | 30.5% | 4.2 | 1.86 | +19.5% | −2.9% | 9 | yes (no stop orders needed; bar-close exits) |
| | BTC | Hold | 29 | 24.1% | 2.7 | 0.86 | −0.5% | −2.4% | 11 | |
| | ETH | Dev | 142 | 19.0% | 5.0 | 1.18 | +5.2% | −5.7% | 20 | yes |
| hmm_tr3_long (adapted) | BTC | Dev | 71 | 26.8% | 4.2 | 1.55 | +6.4% | −3.6% | 9 | yes, but needs NumPy/hmmlearn (blocked on the Windows host) |
| | BTC | Hold | 31 | 25.8% | 3.4 | 1.18 | +0.6% | −2.7% | 11 | |
| | ETH | Dev | 46 | 21.7% | 5.6 | 1.55 | +4.8% | −3.4% | 14 | |
| pairs_longleg (new) | BTC | Dev | 49 | 40.8% | 2.0 | 1.39 | +6.8% | −8.6% | 7 | yes |
| | BTC | Hold | 0 | – | – | – | 0.0% | – | – | no signal in 21 months |
| | ETH | Dev | 46 | **60.9%** | 1.8 | 2.74 | +20.4% | −4.4% | 3 | yes |
| | ETH | Hold (4 mo) | 3 | 0.0% | – | 0.0 | −1.8% | −2.2% | 3 | insufficient |
| pine_ultimate_long (adapted) | BTC | Dev | 10 | 50.0% | 1.6 | 1.57 | +1.0% | −0.8% | 2 | partial (needs an off-chain stop/limit poller) |
| | BTC | Hold | 6 | 50.0% | 1.6 | 1.56 | +0.6% | −1.0% | 2 | |
| | ETH | Dev | 5 | 20.0% | 1.6 | 0.39 | −0.9% | −1.4% | 4 | insufficient |
| random entries, ATR exits (control) | BTC | Dev | 135 | 43.0% | 1.5 | 1.13 | +3.8% | −7.6% | 6 | – |
| | BTC | Hold | 64 | 34.4% | 1.8 | 0.95 | −0.5% | −3.5% | 5 | – |
| buy and hold at 10% sizing | BTC | Dev | 1 | – | – | – | +228% | −50.8% | – | – |
| | BTC | Hold | 1 | – | – | – | −1.1% | −6.9% | – | – |

Cost sensitivity at 2x (dev, BTC): tsmom_ema_filter stays strongly positive; tr3_long and hmm_tr3_long remain positive but
shrink; pairs_longleg was not cost-stressed (partner strategy; flagged). Parameter stability across development windows:
tsmom_ema_filter 0.90 (BTC), pine 1.0 (fixed), tr3 0.45, hmm_tr3 0.50, pairs 0.75.

The Pine strategy on its native 1h bars (`comparison_1h_holdout2025.json`): BTC dev 36 trades, PF 0.92, −0.6%; ETH PF 0.76;
holdout PF 0.69. TR3 on 1h: PF 0.79 (BTC), −8.7%, 26-trade losing streak. Neither author claim was reproduced.

## Reading

- **No candidate improves on the deployed strategy** in net profit after costs at comparable drawdown. On the development
  period the baseline leads by a wide margin (BTC +45.9%, PF 3.55, DD −4.4%); the best adapted candidate, tr3_long, has
  less than half the return at a similar drawdown and a worse holdout (PF 0.86).
- **Higher win rates did not mean more money.** pairs_longleg wins 61% on ETH but produced no BTC signal at all in 21
  holdout months and only 3 trades on ETH; pine_ultimate_long wins 50% on BTC but makes +1% over five years (10 trades).
  The baseline's 37% win rate with a 6:1 average-win-to-loss ratio is what produces the return.
- **The HMM gate does what it claims and not more.** It removes half of TR3's trades, cuts ETH drawdown from −5.7% to
  −3.4%, lifts the BTC holdout profit factor from 0.86 to 1.18, and loses two thirds of the development return. It is the
  only candidate with a holdout profit factor above the baseline's, on 31 trades, which is suggestive, not conclusive.
- **The 2025–2026 holdout is a sideways market** (buy-and-hold −1.1%): every trend system is flat there. The baseline
  neither made nor lost money, which is the expected behaviour and not evidence of decay.
- **Risk-limit coherence.** At the actual 10% sizing the baseline's drawdown is 4–11% in development and 4% in the
  holdout, so the existing 10% drawdown kill and 3% daily halt are consistent with it. Earlier research figures (−39%)
  were at 100% allocation and do not apply to the deployed configuration. No limit was loosened.

## Decision

Keep `tsmom_ema_filter` (plus the EMA-cross secondary) as the deployed, forward-tested strategy. Nothing here earns a
replacement on evidence. If a second forward test is wanted, `hmm_tr3_long` is the only candidate with a rationale
(lowest drawdown, best holdout profit factor), but it needs NumPy/hmmlearn, which the Windows host's Application Control
blocks, so it would have to run paper-only on a Linux host, and its development return is a fraction of the baseline's.
`pairs_longleg` is not ready: no cost stress, no BTC holdout signal, and its one strong series (ETH) has 4 months of holdout.
