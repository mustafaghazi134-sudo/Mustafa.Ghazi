# Strategy research: independently reproduced results

Produced by `research_lab/` in this repository (run: `python -m research_lab.run_all`), 2026-11-01.
Every number here was computed by our own engine on the data described below. No performance claim
from any strategy's author was accepted; where a claim exists in the source it is marked UNVERIFIED.

## Short version

1. **The "90–95% win rate" strategies are real but worthless.** Every Freqtrade community strategy that
   advertises a high win rate gets it from a tiny take-profit (1–5%) against a large stop (10–25%) or no
   stop at all. Reproduced on real data they post 60–97% win rates **and lose money after costs** on
   every asset and timeframe, because the average loss is 4–17× the average win. A martingale/DCA
   construction reaches a 100% win rate while underperforming buy-and-hold with 50–87% drawdowns and a
   large open loss. These are rejected by policy (martingale, unlimited averaging down, hidden tail risk).
2. **Timeframe matters more than the indicator.** On 1-hour bars at ~0.5% round-trip cost, every
   crossover/breakout/mean-reversion system churns itself to death (hundreds of trades, profit factor
   0.3–1.0). The same trend rules become profitable out of sample on 4-hour and daily bars.
3. **What survived walk-forward validation:** daily-evaluated time-series momentum (TSMOM). Its
   regime-filtered variant on 4-hour bars is the only candidate that is profitable out of sample on
   BTC, ETH and both SOL blocks, beats buy-and-hold BTC on return and on drawdown, keeps a profit factor
   above 1.6 on ETH, survives doubled costs and one-bar-late fills, and has the most stable parameters.
   Its win rate is ~33%: it wins by letting winners run (average win ≈ 6× average loss), not by winning often.
4. **It is not "consistently profitable" in the calendar sense.** Only 12 of 27 three-month
   out-of-sample windows on BTC were positive. Trend following makes its money in a minority of
   periods and bleeds small losses in the rest. Anyone promising smooth monthly returns from spot
   crypto with these tools is selling something.

## Selected for the bot (paper trading first)

| Setting | Value | Why |
|---|---|---|
| Strategy | `tsmom` with `lookback_days=30`, `ema_filter_days=100`, `stop_pct=25` | best robustness across assets/timeframes (table below) |
| Secondary | `momentum` (EMA 12/26 cross above EMA 100, 2.5-ATR stop, no take-profit) | positive OOS on BTC/ETH/SOL-block-1 at 4h; diversifies entry timing; weaker robustness (late-fill sensitive) |
| Timeframe | 4h candles, 1000 bars history | where the edge exists after costs; fewer API calls |
| Rejected | `mean_reversion`, `breakout` (1h), all Freqtrade ROI-table strategies, Connors RSI2, MACD cross, martingale/DCA | lose money OOS after costs or hide tail risk |

Risk controls are unchanged (independent risk engine, daily-loss halt, drawdown kill, kill switch).
Live trading stays disabled until paper trading on live prices confirms behaviour and the owner arms it.

## Method

- **Data**: Binance 1h klines from six independent GitHub mirrors of the official dumps; BTC 2019-01 → 2026-09
  (67,861 bars), ETH 2019-09 → 2025-04 (49,621), SOL 2022-03 → 2023-11 and 2024-03 → 2025-04 (two blocks;
  SOL 2021 and 2023-12..2024-02 were not obtainable offline). Five sources agree to the hour exactly (0.0000%
  close deviation on 178 overlapping months); the sixth (only SOL source for 2022–23) deviates 0.04% on average,
  consistent with a perpetual-futures feed, so SOL block 1 carries that caveat. OHLC sanity: 0 rows dropped.
- **Execution model** (deliberately pessimistic): signal on bar close → fill at next bar open; stop checked on
  the bar low before take-profit; gap-through fills at the open; 10 bps fee + 15 bps slippage per side
  (~0.5% round trip, realistic for liquid Jupiter routes); fixed-fraction sizing; one position per asset; no
  pyramiding, no averaging down.
- **Walk-forward**: 12-month train / 3-month test rolling (18/6 on daily bars). Parameters chosen on the training
  window only by a shrunk-Sharpe/drawdown score; the chosen set is run once on the unseen window; OOS segments
  are compounded. "IS" figures use default parameters over the whole history and are shown only to expose
  overfitting (big IS/OOS gaps).
- **Bias checks** on every candidate: lookahead test (signals recomputed on truncated history must match — all
  passed), cost sensitivity (0×/1×/2× costs), delayed-entry test (every fill one bar late), random-entry control
  with the same exits, parameter-stability across windows.
- **Cross-validation of the engine**: the bot's own, independently written backtester reproduces the lab on
  identical data: EMA-cross 4h BTC 441.6% vs 442.3% (187 trades both), Donchian daily BTC 443.5% vs 443.1%
  (24 trades both), TSMOM 1h BTC 2387.5% vs 2385.6% (110 trades both), TSMOM 1h ETH 2106.7% vs 2107.4% (72 both).

## Comparison: trend candidates, walk-forward out-of-sample (BTC, ETH; SOL block 1 for reference)

Buy-and-hold over the same OOS spans: BTC 2020-01→2026-09 **+1065%, max DD −77%**; ETH 2020-09→2025-04 **+314%, −81%**.

| TF | Strategy | Asset | OOS trades | Win % | Net return | PF | Max DD | Sharpe | Avg win / loss | 2× costs | 1-bar-late retained | Param stability | Positive windows |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4h | **tsmom_ema_filter** | BTC | 81 | 33.3 | **+1132%** | **2.89** | **−39%** | **1.17** | +21.1% / −3.6% | +1429% | 1.12 | 0.78 | 12/27 |
| 4h | tsmom_ema_filter | ETH | 58 | 32.8 | +134% | 1.65 | −54% | 0.62 | +16.4% / −4.5% | +1711% | 1.09 | 0.53 | 9/19 |
| 4h | tsmom_ema_filter | SOL b1 | 8 | 25.0 | +145% | 12.2 | −30% | 2.37 | +87.7% / −2.4% | +139% | 0.76 | 0.67 | 1/3 |
| 4h | tsmom_30d | BTC | 117 | 34.2 | +463% | 2.08 | −65% | 0.83 | +13.7% / −3.7% | +778% | 1.10 | 0.37 | 13/27 |
| 4h | tsmom_30d | ETH | 77 | 35.1 | +524% | 2.12 | −60% | 0.99 | +20.6% / −4.9% | +2009% | 1.30 | 0.68 | 11/19 |
| 4h | tsmom_trail | BTC | 114 | 34.2 | +98% | 1.55 | −67% | 0.45 | +13.6% / −4.9% | +520% | 1.39 | 0.30 | 11/27 |
| 4h | tsmom_trail | ETH | 94 | 45.7 | +885% | 1.82 | −43% | 1.16 | +14.1% / −5.6% | +842% | 0.78 | 0.63 | 12/19 |
| 4h | ema_cross_trend | BTC | 122 | 33.6 | +539% | 2.07 | −52% | 1.02 | +12.1% / −3.0% | +113% | 0.65 | 0.22 | 12/27 |
| 4h | ema_cross_trend | ETH | 89 | 30.3 | +97% | 1.45 | −57% | 0.56 | +13.5% / −4.0% | +393% | 0.70 | 0.32 | 9/19 |
| 4h | donchian_turtle | BTC | 135 | 33.3 | +331% | 1.76 | −44% | 0.84 | +10.7% / −3.1% | +597% | 1.05 | 0.41 | 14/27 |
| 4h | donchian_turtle | ETH | 113 | 30.1 | −25% | 0.97 | −74% | 0.07 | +10.2% / −4.2% | +136% | 1.18 | 0.42 | 8/19 |
| 4h | supertrend_consensus | BTC | 237 | 35.9 | −5% | 1.08 | −67% | 0.17 | +7.0% / −3.6% | +102% | 0.47 | 0.44 | 11/27 |
| 1h | tsmom_30d | BTC | 130 | 30.0 | +494% | 1.95 | −64% | 0.84 | +15.4% / −3.6% | +1334% | 0.80 | 0.44 | 14/27 |
| 1h | tsmom_30d | ETH | 70 | 37.1 | +241% | 1.76 | −59% | 0.75 | +19.0% / −5.7% | +1440% | 1.10 | 0.58 | 7/19 |
| 1h | tsmom_trail | BTC | 112 | 36.6 | +446% | 1.92 | −52% | 0.82 | +14.4% / −4.6% | +941% | 0.78 | 0.56 | 13/27 |
| 1h | tsmom_trail | ETH | 92 | 40.2 | +389% | 1.62 | −45% | 0.90 | +14.7% / −5.4% | +594% | 0.93 | 0.58 | 8/19 |
| 1h | ema_cross_trend | BTC | 432 | 25.2 | −33% | 0.99 | −82% | −0.03 | +5.3% / −1.8% | −100% | 0.94 | 0.19 | 12/27 |
| 1h | donchian_turtle | BTC | 539 | 28.4 | −77% | 0.85 | −88% | −0.49 | +4.1% / −1.9% | −98% | 0.97 | 0.37 | 7/27 |
| 1D | tsmom_ema_filter | BTC | 67 | 34.3 | +1193% | 2.91 | −35% | 1.29 | +23.6% / −3.8% | +1748% | 1.00 | 0.38 | 9/13 |
| 1D | tsmom_ema_filter | ETH | 48 | 29.2 | +64% | 1.82 | −56% | 0.49 | +17.4% / −4.5% | +974% | 0.83 | 0.44 | 3/9 |
| 1D | donchian_turtle | BTC | 38 | 39.5 | +708% | 3.20 | −49% | 1.11 | +31.0% / −6.3% | +382% | 1.77 | 1.00 | 9/13 |
| 1D | donchian_turtle | ETH | 23 | 39.1 | +131% | 2.43 | −42% | 0.69 | +25.7% / −7.8% | +807% | 2.02 | 1.00 | 4/9 |
| 1D | supertrend_consensus | BTC | 47 | 36.2 | +932% | 3.18 | −42% | 1.26 | +29.0% / −5.0% | +545% | 0.95 | 1.00 | 8/13 |
| 1D | supertrend_consensus | ETH | 29 | 44.8 | +331% | 3.23 | −32% | 1.07 | +20.7% / −5.4% | +123% | 12.3 | 0.89 | 6/9 |

Daily-bar systems look excellent but rest on 20–50 trades and 9–13 windows; they are noted, not selected.
"1-bar-late retained" is OOS-return(late fills)/OOS-return(modelled fills); values near or above 1 mean the
result does not depend on precise fill timing. SOL block 2 (2024-03→2025-04) produced too few trades for any
strategy to be judged (≤ 10) and is omitted here; it is in the per-timeframe tables.

## Comparison: "high win rate" and mean-reversion candidates (1h, walk-forward OOS)

| Strategy | Source | Asset | Trades | Win % | Net return | PF | Max DD | Avg win / loss | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| bb_rsi_meanrev_nostop | Freqtrade BBRSI/Cluc family | BTC | 183 | **90.2** | −86% | 0.59 | −89% | +1.1% / −17.3% | REJECT: no stop, tail risk |
| bb_rsi_meanrev_nostop | | ETH | 175 | **96.6** | −44% | 0.84 | −78% | +0.8% / −26.0% | REJECT |
| ft_adx_momentum (ROI 1% / SL 25%) | berlinguyinca/ADXMomentum | BTC | 1681 | 77.6 | −99.9% | 0.49 | −99.9% | +0.5% / −3.5% | REJECT |
| ft_sample_strategy | freqtrade sample | BTC | 259 | 75.3 | −86% | 0.57 | −88% | +1.2% / −6.5% | REJECT |
| ft_strategy001 | Strategy001 | BTC | 350 | 73.4 | −44% | 0.90 | −73% | +1.4% / −4.4% | REJECT |
| ft_bbandrsi | berlinguyinca/BbandRsi | BTC | 230 | 60.4 | −83% | 0.76 | −88% | +3.1% / −6.1% | REJECT |
| ft_strategy003 (simplified) | Strategy003 | BTC | 57 | 61.4 | −58% | 0.33 | −61% | +1.1% / −5.5% | REJECT |
| connors_rsi2 | Connors RSI(2) | BTC | 621 | 29.5 | −95% | 0.28 | −95% | +0.6% / −0.9% | REJECT |
| bb_rsi_meanrev_stop (bot's old `mean_reversion`) | tradebot | BTC | 149 | 38.9 | −77% | 0.36 | −78% | +1.4% / −2.5% | REJECT |
| random_entry_atr_exits (control) | — | BTC | 859 | 35.9 | −96% | 0.78 | −98% | +3.0% / −2.2% | control |
| DCA martingale demo (double down every −3%, +1% TP, no stop) | classic "95% win rate" | BTC | 2047 | **100.0** | +115% vs B&H +2306% | — | −69% | open position −28% | REJECT by policy |
| DCA martingale demo | | ETH | 1506 | **100.0** | +41% vs B&H +934% | — | −51% | open position −44% | REJECT by policy |
| DCA martingale demo | | SOL b1 | 61 | **100.0** | −47% | — | −87% | open position −53% | REJECT by policy |

UNVERIFIED CLAIMS in sources (quoted in `refs/STRATEGY_CATALOG.md` during research, not reproduced here):
Freqtrade community files carry comments such as hyperopt results and win/draw/loss tallies; the catalog shows
these tallies are ROI-table artefacts. None were reproducible as profitable after costs.

## Full per-timeframe tables

Machine-generated tables for every candidate, asset and block: `results/results_1h.json`,
`results/results_4h.json`, `results/results_1d.json` (render with `python -m research_lab.report <file>`).
Each JSON holds per-window chosen parameters, IS and OOS metrics, cost sensitivity, delayed-entry and
lookahead results.

## What would change this conclusion

- A liquid venue with ≤ 0.1% round-trip cost would revive some 1h systems; Jupiter spot is not that venue.
- More SOL history (2020–2021, 2023-12→2024-02) is needed before SOL-specific parameters are trusted.
- Forward paper trading on live prices for several months is the next test; a strategy must stay positive
  after costs there before live arming is considered.
