# Candidate strategy repositories: sceptical review

Scope: three shallow, read-only clones under `scratchpad/refs/`. Nothing was executed; code and notebook JSON were read, CSVs inspected with shell tools only. Every performance figure quoted below from a README / METRICS file / notebook output is an **unverified claim by the repository author** and is marked as such. Review date 2026-11-01.

| Repo | Head commit | Author | Language |
|---|---|---|---|
| statistical-arbitrage-crypto | 780a6c4 (2026-04-08) | Sujith Kamme | Jupyter notebook (Python) |
| ultimate-crypto-trading-bot | bd459b3 (2025-07-18) | Devendra Parmar | TradingView Pine v5 |
| HMM_TR_Alg | 4d646e8 (2026-07-06) | Krishhiv Mehra | Python |

**Headline caveat for HMM_TR_Alg**: every `*.csv` in the clone (raw data, processed features, `trades_btc_long.csv`, both `equity_curve.csv`, `opt_results.csv`) is a **Git LFS pointer file** (3 lines: `version https://git-lfs.github.com/spec/v1`, `oid sha256:...`, `size ...`), per `.gitattributes` (`*.csv filter=lfs`). No trade-level or equity-level record is actually present, so none of the claimed metrics could be recomputed (section 3.4).

---

## 1. statistical-arbitrage-crypto

### 1.1 What it is / data
Research notebook (`labs/research.ipynb`, 74 cells, Python 3) implementing daily-bar cointegration stat-arb on crypto baskets: Johansen rank-1 baskets of 2-3 assets, OU mean-reversion "alpha" on the log-price spread, a two-tier regime filter, hysteresis/bucketed position sizing, inverse-vol risk parity, walk-forward basket selection on an 80% train split, then one run on the 20% hold-out. No `.py` package, no tests, no saved results beyond notebook outputs and `artifacts/crypto_pipeline_detailed.svg` (a diagram whose labels restate the parameters).

Data (cell 9-16): `binance.client.Client(tld='US')` (Binance.US), `get_historical_klines(symbol, "1d", "1 Mar 2023", "1 Mar 2026")`, close prices only. 23 symbols requested (cell 10); assets with >5% NaN dropped (cell 12) leaving 18: BTC, ETH, SOL, BNB, ADA, AVAX, DOT, ATOM, LINK, LDO, OP, FIL, LTC, DOGE, FET, NEAR, APT, CRV (all `/USDT`). 1097 daily rows 2023-03-01 to 2026-03-01. Split (cell 14) `split_idx = int(len(prices)*0.8)`: train 877 rows 2023-03-01..2025-07-24, test 220 rows 2025-07-25..2026-03-01 (cells 15-16 outputs).

Claimed results (readme.md "Results", UNVERIFIED): OOS "Sharpe Ratio 1.953 gross / **1.764** net (20bps)", "Cumulative Return +6.50% / +5.61%", "Max Drawdown -1.38%", "Win Rate 72.2%", "Annual Turnover 7.05x", "Entry Events 9 (~15 annualised)". Walk-forward on train (cell 52 output): "WF Sharpe 1.440, Cumulative return 40.08%, Max drawdown -16.61%, Win rate (active days) 53.6%", fold Sharpes -2.672, 4.761, 4.371, 7.471, 1.497, -0.772, -1.399, 0.773. The readme itself says the OOS Sharpe "is based on only 9 round-trip trades ... not yet statistically meaningful".

### 1.2 Rules (quoted from notebook cells)
Constants (cell 6): `MIN_HALF_LIFE=2`, `MAX_HALF_LIFE=15` (days), `HL_WINDOW=120`, `REGIME_WINDOW=30`, `ALPHA_CLIP=3.0`, `ALPHA_DEADBAND=0.10`, `HARD_VOL_RATIO=4.0`, `HARD_ADF_PVAL=0.40`, `REGIME_W_KAPPA=0.35`, `REGIME_W_ADF=0.35`, `REGIME_W_VOL=0.30`, `ENTRY_THRESHOLD=0.08`, `EXIT_THRESHOLD=0.02`, `MAX_POS_SIZE=1.0`, `MIN_POS_SIZE=0.10`, `VOL_WINDOW=30`, `MIN_FOLDS=2`, `MIN_MEAN_SR=0.0`, `MIN_FLOOR_SR=0.0`, `N_SELECT=8`, `BUCKET_THRESHOLDS=[0.08,0.10]`, `BUCKET_SIZES=[0.0,0.5,1.0]`, `TCOST_BPS=20.0`.

**Basket discovery (cell 19, `find_cointegrated_baskets`)**: for every combination of 2..3 symbols with >=200 rows: `coint_johansen(log_prices, det_order=0, k_ar_diff=1)`; `rank = sum(result.lr1 > result.cvt[:,1])` (95% trace); keep only `rank == 1`; `weights = result.evec[:,0]; weights /= sum(|weights|)`; sign-flip so `weights[0] > 0`; `spread = log_prices.dot(weights)`.

**Quality filters (cells 22-28, applied in cell 51 lines 125-183)**: ADF `adfuller(s, maxlag=1, autolag=None)[1] < 0.05`; variance ratio `np.var(s.diff(k)) / (k*np.var(s.diff()))` with `k=5` must be `< 0.9`; half-life from OLS `Δs_t = c + κ s_{t-1}`, `half_life = -ln2/κ`, require `2 <= HL <= 15`; `halflife_stability` (rolling 200-bar windows stepped 30, CV of HL) must be `<= 0.5`; `spread_predictability`: regress `s.diff(h).shift(-h)` on full-sample z-score with `h=int(HL)`, require `beta < 0 and |t| > 1.5`.

**Hedge ratio / dynamic spread (cells 30-31)**: `rolling_coint_weights(prices, assets, window=180, step=30)` re-runs Johansen on 180-bar windows every 30 bars, keeps the first eigenvector normalised as above; `build_dynamic_spread` forward-fills those weights (`rolling_w.reindex(idx).ffill()`) and returns `(lp_aligned * w_aligned).sum(axis=1)` plus `weights_latest = rolling_w.iloc[-1]`. In the walk-forward (cell 51 lines 205-216) and in the OOS run (cell 68 lines 111-123) the signal spread actually used is **static**: `beta = b.get("weights_latest", b["weights"])`, `spread_all = log(prices[assets]) @ beta` over train+test, and `basket_ret_all = (pct_change(prices[assets]) * beta).sum(axis=1)`.

**Signal = OU alpha (cell 34 `compute_ou_alpha_point`)**, evaluated point-in-time on `s = spread.iloc[:i+1]`:
```
hl_est = estimate_half_life(s.iloc[-hl_window:]); hl, kappa
if hl == inf or not (2 <= hl <= 15): alpha = 0
bb_window = max(20, int(3*hl)); mu = s[-bb_window:].mean(); sigma = s[-bb_window:].std()
raw_alpha = abs(kappa) * (mu - s.iloc[-1]) / sigma
alpha = clip(raw_alpha, -3.0, 3.0); if |alpha| < 0.10: alpha = 0
```
(cell 35 loops `i` from `hl_window+20` to end). Alpha > 0 means spread below its mean -> long the spread (long positive-weight legs, short negative-weight legs).

**Regime filter (cells 37-38)**: hard kill -> score 0 if `std(Δs[-30:]) / std(Δs[-90:]) > 4.0` or rolling ADF p-value on last 90 bars `> 0.40`; soft score `0.35*clip(|κ_local|/(ln2/15),0,1) + 0.35*clip(1 - p_adf/0.15,0,1) + 0.30*clip(1 - vol_ratio/2.5,0,1)` with κ_local from OLS over `max(20, 3*hl)` bars.

**Final alpha (cell 39)**: `gated = raw_alpha * regime_score`; `smoothed = gated.ewm(span=5, min_periods=5).mean()`; adaptive floor: zero out bars where `|smoothed| < rolling(100, min_periods=20).quantile(0.25)` (fallback `expanding().mean()*0.5`).

**Entry / exit / sizing**
- Walk-forward scoring (cell 42 `compute_hysteresis_positions`): flat -> enter when `|alpha| > 0.08` with `size = clip(|alpha|/0.08, 0.10, 1.0) * sign(alpha)`; in position -> exit when `|alpha| < 0.02` OR `sign(alpha)` flips; otherwise resize to `clip(|alpha|/0.08, 0.10, 1.0)` every bar.
- OOS deployment (cells 65-66 `compute_bucketed_positions`): target = 0 if `|alpha| < 0.08`, 0.5 if `< 0.10`, else 1.0, signed; flat -> enter when target != 0; in position -> exit when `|alpha| < 0.02` or target sign flips; resize only when bucket changes.
- No stop loss, no take profit, no time exit; exits are purely signal (alpha decay / sign flip) or regime hard-kill driving alpha to 0 (`|alpha| < 0.02`).
- Capital allocation (cell 44): `w_i = (1/vol_i) / Σ(1/vol_j)` over baskets with non-zero position at t, `vol_i = std(basket_ret[:t].iloc[-30:])`, floored at 1e-6. Weights sum to 1 across active baskets, i.e. 100% of capital is always deployed across whatever baskets are active (concentrated when one basket is active).
- PnL (cell 46 lines 78-87, cell 68 lines 145-156): `daily_pnl_t = Σ_k pos_k[t-1] * w_k[t-1] * basket_ret_k[t]`.
- Costs (cell 68 lines 169-183): `tcost = 20e-4`; `signal_changes = pos_df.diff()` (ignored if `|Δ| <= 0.01`); `daily_cost = Σ_k |Δpos_k| * w_k[t-1] * tcost`; `net = gross - daily_cost`. Note cell 6 comment calls 20bps "round-trip", cell 68 docstring calls it "One-way"; the code charges 20bps per unit of position change, i.e. 40bps for an enter+exit of a full position.

**Basket selection (cells 55-62)**: per-basket fold Sharpe = `mean/std*sqrt(365)` of the basket's decomposed daily PnL; `composite = mean_SR * 1/(1+std_SR) * log1p(n_folds)`; filter `n_folds >= 2, mean_SR >= 0, min_SR >= 0` (155 -> 10); greedy diversification, max 2 baskets per asset, `n_select = 8` (10 -> 4). Selected (cell 62 output): `OPUSDT_FILUSDT_APTUSDT`, `FILUSDT_NEARUSDT_APTUSDT`, `ADAUSDT_ATOMUSDT_CRVUSDT`, `DOTUSDT_OPUSDT_LTCUSDT`, each appearing in only 2 of 8 folds. OOS uses each basket's weights and `hl_window` from the most recent fold containing it (71 for the first two, 99 for the others; cell 69 output), with no re-estimation during the 220-day test.

Walk-forward (cell 51/52): `train_size=365`, `test_size=60`, step 60, on `prices_train` only; `calibrate_params_on_train` sets `hl_window = clip(10*median_HL, 40, 180)` per fold (76, 95, 99, 71, 123, 88, 100, 63).

### 1.3 Dependencies of the result
- **Shorting**: intrinsic. Cointegrating vectors have mixed signs (normalised by `sum(|w|)`), so every basket position is long some legs / short others; `basket_ret = Σ β_i r_i`. Not reproducible long-only.
- **Leverage**: `MAX_POS_SIZE=1.0` per basket times risk-parity weights summing to 1, so gross notional is at most 1x capital; but 100% of capital sits in a single basket when only one is active.
- **Averaging / pyramiding**: hysteresis version resizes every bar with |alpha|; bucketed version resizes 0.5 <-> 1.0 (cell 69 output: "Bucket resizes: 0" in OOS).
- **Fills**: position decided from the close at t, PnL accrues from close t to close t+1 (`pos[t-1]*ret[t]`): same-bar close fill at the signal bar's close. Costs only 20bps per unit turnover; no spread/slippage model on Binance.US alt pairs (CRV, APT, OP, FIL, NEAR, LTC, DOT, ADA, ATOM) where 20bps one-way is optimistic.
- **Lookahead**: signal functions are point-in-time (`s.iloc[:i+1]`), regime, EWM and adaptive floor are rolling/expanding, risk-parity vol uses `.loc[:t]`, and the test block is only touched in cell 68 -- no in-sample normalisation of the OOS alpha was found. Caveats: (a) `spread_predictability` uses full-train-window mean/std (fine, train only); (b) the static `weights_latest` is applied to the whole train+test history when building `spread_all`, so the warm-up portion of each fold's alpha uses weights estimated at the end of the train window (affects only in-fold scoring, not OOS); (c) all 25 constants in cell 6 and the selection thresholds are fixed by hand and there is a single OOS run -- whether they were iterated against the OOS window cannot be determined from the repo.
- **Survivorship / selection**: the 23-asset universe was hand-picked in 2026 from assets still listed on Binance.US (no delisted/collapsed tokens), so the basket search is survivorship-biased. Basket selection keeps baskets with positive Sharpe in just 2 folds out of 155 candidates -- a multiple-comparison problem the notebook does not address.
- **Statistical weight**: 9 entries in 220 days; `Alpha (daily) 0.000267 (t=1.44)` in the author's own regression (cell 72 output).

### 1.4 Trade records
None. No trade log or equity CSV is saved; the only records are notebook outputs (cell 52, 60-62, 69, 72 quoted above) and a figure (`<Figure size 1400x1100 with 4 Axes>`, image not inspected). Nothing to recompute.

### 1.5 Jupiter / Solana long-only spot
Not runnable as designed: it needs simultaneous shorts, daily rebalancing of 9 alt legs, and none of OP, FIL, APT, NEAR, ADA, ATOM, CRV, DOT, LTC is Solana-native (bridged versions have negligible Jupiter liquidity). A long-only adaptation restricted to cbBTC/WETH/SOL would be a different strategy: e.g., hold only the positive-weight leg(s) when the OU alpha says the spread is cheap, or a relative-value rotation (overweight whichever of the three sits below its cointegration-implied value). That drops the hedge, turns it into directional alt exposure, and the 2-15 day half-life / 20bps assumptions no longer apply; with ~0.5% round-trip on Jupiter the modelled cost drag (annual turnover 7.05x -> about 1.8%/yr at 0.5% RT vs 0.9%/yr modelled) would consume a third of the claimed 5.6% net. Fresh testing required; the original edge is unproven anyway (9 trades).

### 1.6 Missing evidence
Raw data snapshot (Binance.US klines not saved); any trade log; code as importable modules; multiple OOS seeds/windows; cost sensitivity; borrow/funding costs for the short legs; universe without survivorship; results on >2 folds per basket; robustness to the ~25 hand-set constants.

---

## 2. ultimate-crypto-trading-bot (Pine Script)

### 2.1 What it is / data
A single TradingView strategy script `ultimate_crypto_bot.pine` (225 lines, `//@version=5`) plus a README and an empty CI workflow. README references `docs/strategy_explanation.md`, `docs/configuration.md`, `tests/backtest_results.md`, `src/ultimate_crypto_bot.pine` -- none of those paths exist in the repo. No data files; data is whatever TradingView chart it is applied to. README "Backtesting Results" (UNVERIFIED, no artefact): "Total Trades 150+, Win Rate 65%, Max Drawdown 15%, Profit Factor 1.8, Sharpe Ratio 1.2 -- Results based on BTC/USD 1-hour timeframe over 6 months" (exchange, dates, settings not stated).

### 2.2 Exact transcription (file: `ultimate_crypto_bot.pine`)
`strategy()` line 4: `overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10, commission_type=strategy.commission.percent, commission_value=0.1`. Not set, so TradingView defaults apply: `initial_capital=100000`, `slippage=0`, `pyramiding=0` (one entry per direction), `calc_on_every_tick=false`, `process_orders_on_close=false` (market entries fill at the next bar's open), `calc_on_order_fills=false`, `margin_long/margin_short` default.

Inputs (lines 8-39, defaults): `higher_tf="1D"`, `use_higher_tf=true`, `ma_fast=9`, `ma_slow=21`, `rsi_length=14`, `rsi_overbought=70`, `rsi_oversold=30`, `macd_fast=12`, `macd_slow=26`, `macd_signal=9`, `bb_length=20`, `bb_mult=2.0`, `volume_ma_length=20`, `volume_threshold=1.5`, `stop_loss_pct=3.0`, `take_profit_pct=6.0`, `risk_reward_ratio=2.0` (unused), `trend_filter=true`, `atr_length=14`, `volatility_threshold=2.0`.

Indicators (lines 43-71): `ma_fast_val=ta.sma(close,9)`, `ma_slow_val=ta.sma(close,21)`, `ma_200=ta.sma(close,200)`, `rsi=ta.rsi(close,14)`, `[macd_line,signal_line,histogram]=ta.macd(close,12,26,9)`, `[bb_upper,bb_middle,bb_lower]=ta.bb(close,20,2.0)` (plotted only, never used in logic), `volume_ma=ta.sma(volume,20)`, `volume_spike = volume > volume_ma*1.5`, `atr=ta.atr(14)`, `volatility = atr/close*100`, `k=ta.stoch(close,high,low,14)`, `d=ta.sma(k,3)`, `htf_close=request.security(syminfo.tickerid,"1D",close)`, `htf_ma=request.security(syminfo.tickerid,"1D",ta.sma(close,50))`, `htf_trend = htf_close > htf_ma`.

Structure (lines 75-81): `higher_highs = high > ta.highest(high[1],10)`, `higher_lows = low > ta.lowest(low[1],10)`, `lower_highs = high < ta.highest(high[1],10)`, `lower_lows = low < ta.lowest(low[1],10)`, `uptrend_structure = higher_highs and higher_lows`, `downtrend_structure = lower_highs and lower_lows`. Pivots (84-85) and engulfing patterns (88-89) are computed but unused.

Conditions (lines 93-114):
```
bullish_trend  = ma_fast_val > ma_slow_val and close > ma_200
bearish_trend  = ma_fast_val < ma_slow_val and close < ma_200
rsi_bullish    = rsi > 40 and rsi < 80
rsi_bearish    = rsi < 60 and rsi > 20
macd_bullish   = macd_line > signal_line and histogram > histogram[1]
macd_bearish   = macd_line < signal_line and histogram < histogram[1]
stoch_bullish  = k > d and k > 20 and k < 80
stoch_bearish  = k < d and k < 80 and k > 20
volume_confirm = volume_spike or volume > volume_ma*1.2     // == volume > 1.2*SMA20(volume)
low_volatility  = volatility < 2.0        // unused
high_volatility = volatility > 2.0*1.5    // ATR14/close > 3%
```
Entry (lines 118-133):
```
long_signal  = bullish_trend and rsi_bullish and macd_bullish and stoch_bullish
               and (uptrend_structure and not downtrend_structure)
               and volume_confirm
               and (not trend_filter or (use_higher_tf and htf_trend) or not use_higher_tf)   // defaults => htf_trend required
               and not high_volatility
short_signal = bearish_trend and rsi_bearish and macd_bearish and stoch_bearish
               and (downtrend_structure and not uptrend_structure)
               and volume_confirm
               and (not trend_filter or (use_higher_tf and not htf_trend) or not use_higher_tf) // defaults => not htf_trend
               and not high_volatility
```
Execution (lines 149-165): `if long_signal and strategy.position_size == 0: strategy.entry("Long", strategy.long); strategy.exit("Long Exit","Long", stop=close*(1-0.03), limit=close*(1+0.06))`; symmetric for shorts with `stop=close*1.03, limit=close*0.94`. Stop/limit levels are computed from the **signal bar's close**, not the fill price (next bar open). Additional exits: `exit_long = bearish_trend and rsi > 70` -> `strategy.close("Long")`; `exit_short = bullish_trend and rsi < 30` -> `strategy.close("Short")`. These require RSI>70 while price is below the 200-SMA with 9<21 SMA (or the mirror), a near-impossible combination, so the "trend reversal" exit is effectively dead; the README's "RSI extreme levels (>70 for longs)" misdescribes it. No time exit, no regime exit, no trailing stop.

Position sizing: 10% of equity per trade via `default_qty_value=10`. Lines 137-138 compute `risk_amount = equity*0.03; position_size = risk_amount/(close*0.03)` (= equity/close, i.e. 100% of equity) but the variable is never passed to `strategy.entry`. No reversal while in a position (entry gated by `position_size == 0`), no pyramiding.

### 2.3 Dependencies of the result
- **Shorting**: half the strategy (symmetric short side). Long-only is a trivial removal of lines 153-155 and 164-165.
- **Leverage**: none (10% equity).
- **Pyramiding**: none.
- **Fills**: commission 0.1% per side is modelled; **slippage 0**; stops/limits are filled at the stop/limit price by TradingView's broker emulator using bar OHLC (optimistic intrabar assumption when both stop and limit fall inside one bar); market entries fill at next-bar open (realistic).
- **Lookahead / repainting**: `request.security(... "1D", close)` and `request.security(... "1D", ta.sma(close,50))` are called with no `[1]` offset and no explicit `lookahead` argument on a sub-daily chart. This is the pattern TradingView's own documentation flags as repainting: the daily value is only confirmed at the daily close, so historical bars see a value that a live run would not have at that hour. The HTF filter in any backtest of this script is therefore not causal/reproducible; a reimplementation must use the previous completed daily bar. Also `ta.pivothigh(high,5,5)` is inherently 5 bars late (plot only).
- **Parameter provenance**: all thresholds hard-coded; no optimisation evidence either way.
- **Survivorship**: single-asset BTC/USD, not applicable.

### 2.4 Trade records
None in the repo. README table only (quoted in 2.1). Nothing to recompute.

### 2.5 Jupiter / Solana long-only spot
Long side is mechanically long-only compatible: 1h bars, fixed 10% of equity, -3% stop / +6% limit, filters on SMA 9/21/200, RSI, MACD, stochastic, 10-bar breakout, volume > 1.2x SMA20, ATR/close < 3%, daily close > daily SMA50 (previous completed day). Issues: Jupiter has no native stop/limit orders (needs an off-chain executor polling 1h bars; stop fills at the next poll, not at the stop price); volume must come from a DEX/aggregator feed and behaves differently from CEX volume; with 0.5% RT cost on a 10%-of-equity position each trade costs 0.05% of equity, i.e. ~15%/yr of equity at the README's ~25 trades/month pace vs a 0.6% equity gain on a full +6% winner. Since no backtest artefact exists and the HTF filter repaints, performance is unknown and must be tested from scratch on cbBTC/WETH/SOL 1h data.

### 2.6 Missing evidence
Backtest artefact (TradingView report/CSV), exchange/date range/settings behind the README numbers, the four referenced doc/test files, any out-of-sample test, slippage, causal HTF handling.

---

## 3. HMM_TR_Alg

### 3.1 What it is / data
Python project: a daily 3-state Gaussian HMM (hmmlearn) labels regimes; an hourly long-only breakout/thrust system ("TR3") trades only in the middle-return state; trade returns are scaled by an equity-curve throttle (ECT) and a volatility target; BTC and ETH sleeves are blended 50/50. Headline claims (README.md and `outputs/portfolio_multi/METRICS.txt`, UNVERIFIED): "Backtest period 2021-12-31 -> 2026-07-06 (4.52 years, walk-forward)", portfolio "Total Return 140.83%, CAGR 21.51%, Sharpe 1.461, Sortino 1.893, Calmar 2.523, Max Drawdown -8.52%, Annualized Volatility 13.96%"; BTC sleeve "CAGR 19.90%, Sharpe 1.182, MaxDD -9.83%"; ETH sleeve "CAGR 21.81%, Sharpe 1.134, MaxDD -12.94%"; strategy-return correlation 0.25; per-year 2022 -2.7%, 2023 +40.3%, 2024 +25.3%, 2025 +44.1%, 2026 YTD -2.3%; robustness "Sharpe range 1.29-1.49 over 80 configs", "bootstrap 5th pct 0.79 / median 1.47", halves "Sharpe 1.55 and 1.39". An older `docs/strategy_v2_standard/config_spec.md` claims a different configuration (ETH features incl. `CumRet_30d`, 70/30 dynamic allocation) with "CAGR 22.95%, Max Drawdown -13.35%, Sharpe 1.05" for 2022-2025 -- the docs are internally inconsistent about what was run.

Data: `src/data/coinbase_downloader.py` pulls `https://api.exchange.coinbase.com/products/{product}/candles` at granularity 86400 (1d) and 3600 (1h) for `BTC-USD`, `ETH-USD` (`download_daily(start="2015-01-01")`, `download_hourly_monthly(start="2015-08-09")`). Files `data/raw/btc_1d.csv` (LFS size 293,818 B), `btc_1h.csv` (6,894,834 B), `eth_usd_1d_coinbase.csv` (236,499 B), `eth_usd_1h_coinbase.csv` (5,564,915 B); processed `btc_1d_features.csv`, `btc_1h_features_tr.csv`, `eth_1d_features.csv`, `eth_1h_features_tr.csv` -- all LFS pointers, contents unavailable. Claimed coverage "through 2026-07" (METRICS.txt). HMM timeframe 1D, trading timeframe 1H. `config.py` lists an `ELITE10_SYMBOLS` universe incl. SOL-USD but no SOL data or results exist; `config.py` is imported by nothing (verified by grep) -- scripts carry their own constants.

### 3.2 Rules with file:line references

**HMM** (`src/hmm/model.py`; the same code is duplicated in `scripts/run_holdout_ect.py` 72-140 and used by the runners):
- Library `hmmlearn.hmm.GaussianHMM` (requirements `hmmlearn>=0.3.0`, `scikit-learn`), `n_components=3`, `covariance_type="diag"` (patched to "full" after fit, model.py 138-140), `n_iter=1500`, `tol=1e-4`, `min_covar=1e-5`, Dirichlet priors `compute_priors(k, self_bias=1.8)` with concentration 50 (model.py 67-88, 119). Warm-start refits use `n_iter=500, tol=1e-3` (model.py 145-196). Multi-sequence fit with `lengths_by_year` (one sequence per calendar year).
- Features `HMM_FEATURES` (model.py 32-40): `Log_Returns`, `GKVol_20` (Garman-Klass 20d), `VolZ_20` (volume z-score 20d), `EMA_20_slope` (`EMA20.diff()`), `BodyPct` (`(C-O)/O`), `ClosePosInRange` (`(C-L)/(H-L) - 0.5`), `CloseOverEMA20` (`C/EMA20 - 1`); computed in `src/features/daily_features.py` 32-74 and model.py 43-64, all rolling/causal. `StandardScaler` fit on the training window only.
- State labelling: `align_states(model, X, feature_idx_for_sort=0)` (model.py 199-249) permutes states by ascending mean of scaled `Log_Returns`: 0 = lowest return, 1 = middle, 2 = highest. TR3 trades `allowed_regime = 1` (the middle state; README: "sustainable grind-up"). Note `config.py` 62-66 and model.py 385-389 carry contradictory labels ("1: Trending", "2: Bearish").
- **Walk-forward used for the headline** (`scripts/run_btc_strategy.py` 35-232, called by `run_portfolio.py`, `run_portfolio_multi.py`, `overfitting_test.py`): simulation starts at `val_end="2021-12-31"`; at each step the HMM is refit on `[current_date - 730 days, current_date)` (line 83-84), scaler refit on that window (94), first fit `fit_hmm(seed=42)` then `fit_hmm_warm_start` from the previous model (98-105), re-aligned by mean return each refit; the model is held fixed for the next `rebalance_days=30`.
- **Lookahead question, answered precisely**: for each day T in the 30-day chunk the state is `model.predict(Xs_combined[w_end-60 : w_end])[-1]` where `w_end` indexes day T, so the Viterbi window is the 60 observations ending at **T-1 and excluding T** (run_btc_strategy.py 167-176; same in `run_holdout.py` 112-119 and `optimize_tr3.py` with window 90). The returned state is the last element of the MAP path over past observations only -- causal. No forward-backward posterior smoothing (`score_samples`/gamma) is used on the test path; `enforce_min_dwell_causal` (model.py 331-363) only delays switches until `min_run` consecutive days. The daily state for day T is mapped onto all hourly bars of T via `day_idx.map(states_series)` (run_btc_strategy.py 195-200) and the TR3 engine additionally shifts the hourly signal by one bar. So **no lookahead in the walk-forward regime path**. Non-causal code does exist and is labelled as such: `BTCRegimeDetector.predict` (full-sequence Viterbi + gamma-based `enforce_min_dwell`, model.py 431-450, docstring "WARNING: This has subtle lookahead") and `train_and_save` writes `btc_1d_with_states.csv` from it; `hourly_features.merge_daily_states` (hourly_features.py 191-220) derives `D1_State_lag1d` from that full-sample file. The runners overwrite that column with the online states, but `scripts/optimize_tr3_params.py` (277-279) consumes the file's column as-is for its 2020-2021 validation.
- Other HMM variants in repo (not used for the headline): `src/hmm/eth_model.py` (features `CumRet_10d`, `CumRet_30d`, `TrendScore`; priors self_bias 0.8, concentration 15; relabel by realised mean return), `scripts/run_eth_strategy.py`, `scripts/run_portfolio_4h.py` (4h-bar HMM), `run_holdout_ect.py --walk-forward` branch uses `k=4, seed=101`. The headline ETH sleeve runs **the BTC 7-feature HMM** on `eth_1d_features.csv` via `run_btc_walkforward` (run_portfolio_multi.py 57), contradicting `config_spec.md`.

**TR3 entry** (`src/strategy/tr3_engine.py` 150-163; identical in `scripts/run_holdout_ect.py` 268-281 which is what the runners import), parameters `TR3Config` (tr3_engine.py 16-61) = `TR3` dict (run_holdout_ect.py 39-66):
```
thrust   = (Close - Close.shift(1)) >= 0.75 * ATR14
breakout = Close > MOM_donchian20_up          # 20-bar rolling max of High, shifted 1 bar (hourly_features.py 144-148)
cond_now = valid_bar & (D1_State_lag1d == 1) & (MOM_ema50 > EMA200) & (Close > MOM_ema50)
           & (breakout | thrust) & (CLV >= 0.55) & (ADX14 >= 14) & (R2_ema50 >= 0.10)
entry_sig = cond_now.shift(1)                  # signal on bar j, buy at Open of bar j+1 (lines 179-189)
```
`CLV = (C-L)/(H-L)` (105-106); `ADX14` Wilder (hourly_features.py 74-94); `R2_ema50` = rolling 72-bar OLS R-squared of EMA50 against time (hourly_features.py 181); `EMA200 = ewm(span=200)`; `ATR14` = simple 14-bar mean of true range if absent (tr3_engine.py 69-75, 101-102). Hourly bars; one position at a time; `valid_bar` excludes bars with non-positive prices, High<Low, or |open-to-open return| > 40% (108-115).

**TR3 exits** (evaluated on the close of bar j, executed at Open of bar j+1, tr3_engine.py 190-298). `advance_atr = (high_since_entry - entry_price)/ATR14_j` (209-213), with `high_since_entry` seeded from the signal bar's High (186-187):
1. Fail-to-hold (219-225), only while `advance_atr < 0.5`: exit if `CLV_j <= 0.35` ("fth_clv") or `Close_j < entry_bar_high - 0.25*ATR` ("fth_level").
2. Breakeven (227-249): arm when `0.75 <= advance_atr < 1.0`; disarm if `>= 1.0`; active one bar after arming (`be_one_bar_delay`); exit if `Close_j < entry_price - (-0.02)*ATR` i.e. below entry + 0.02 ATR ("breakeven").
3. Ratcheting ATR trail (251-262): `mult = 2.5` if `advance_atr < 1`, `3.5` if `>= 1`, `4.5` if `>= 2`; exit if `Close_j < high_since_entry - mult*ATR14_j` ("trail_atr").
4. EMA50 break (264-271): if `advance_atr >= 1.0`: exit if `Close_j < EMA50 - 0.25*ATR` ("ema50_relaxed"), else exit if `Close_j < EMA50` ("ema50_break").
5. No-progress (273-278): if held >= 12h and `high_since_entry - entry_price < 0.5*ATR` -> exit.
6. Time stop (280-284): held >= 168h (7 days) -> exit.
7. End of data -> close at last Open ("eod_close", 319-339).
Trade return `gross_ret = exit_open/entry_open - 1`; `fee = 0.0` (`fee_bps=0.0`, tr3_engine.py 59, 169; hard-coded `fee = 0.0` in run_holdout_ect.py 286); `net_ret *= leverage (1.0)`; `net_ret = max(net_ret, -0.08)` (`max_loss_cap`, 297-298). Hourly strategy return stream `strat_rets = pos.shift(1) * ret_oo` (open-to-open, 345), resampled to daily by compounding (run_btc_strategy.py 213).

**Position sizing / overlays**: base position = 100% of sleeve capital (`pos = 1.0`). ECT (`run_holdout_ect.py` 439-486, called with `window=40, risk_high=1.3, risk_low=0.5` from run_portfolio.py 172-174 and run_portfolio_multi.py 36): for each trade in order, `sma = mean(last 40 trade-equity points)`; during the first 40 trades `risk = 1.3`; afterwards `risk = 1.3 if equity >= 0.995*sma else 0.5`; `net_ret_scaled = net_ret*risk`, equity compounds on scaled returns. The daily return series is multiplied by the trade's `risk_factor` over `entry.normalize():exit.normalize()` (run_portfolio_multi.py 39-46). Vol target (run_portfolio.py 95-121): `scale = clip(target / (std(20d close returns)*sqrt(365)).shift(1), 0.1, max_scale=1.0)`, BTC target 0.40, ETH 0.50. ETH additionally `min_dwell=2` causal smoothing. Portfolio `0.5*btc + 0.5*eth` on the common calendar with 0 fill (run_portfolio_multi.py 104-123). Metrics use calendar-daily returns incl. flat days, `sqrt(365)` (run_portfolio.py 128-147).

**Parameters elsewhere**: `grid_sharpe.json` (6 params: adx_min 18/22/25/28, r2_min 0.10-0.25, clv_min 0.55-0.65, thrust 0.5-1.0, be_activate 0.5-1.0, trail_base 2.0-3.0) is the grid for `scripts/optimize_tr3.py`, whose docstring reads "Optimize TR^3 parameters on the holdout test window (unseen vs train+val)" -- i.e. the grid is scored **on the 2022+ test period** (optimize_tr3.py 534-540, 600, 616-640, ranked by `Sharpe_Daily`). Result `outputs/optimize/opt_best.json`: `adx_min 25, r2_min 0.25, clv_min 0.6, thrust_atr_mult 0.5, be_activate_atr 1.0, trail_mult_base 3.0`, "Sharpe_Daily 0.648, Sharpe 2.94 (hourly), CAGR 9.5%, WinRate 26.3%, ProfitFactor 1.457, NumTrades 167". `opt_results.csv` is an LFS pointer (336,446 B). The production defaults (adx 14, r2 0.10, clv 0.55, thrust 0.75, be 0.75, trail 2.5) differ from opt_best, are attributed to "TR_MODEL_2.py" (not in repo), so their provenance is unknown. `scripts/optimize_tr3_params.py` tunes a different grid on 2020-2021 by Calmar; `run_eth_strategy.py` 34-41 carries ETH params "found via optimize_tr3_params.py on 2020-2021" (`adx_min 18, r2_min 0.08, fth_disable 0.4, clv_fail_max 0.4, be_activate 1.0`) that the headline run does not use.

**Holdout split definitions**: `run_holdout.py` 39-69: `train_end 2019-12-31`, `val_end 2021-12-31`, HMM fit on train+val (`fit_hmm(seed=101)`), test = `> val_end`, sliding Viterbi window 90, `min_dwell 3`, TR3 defaults, no ECT. `run_holdout_ect.py` 594-600 same dates, `--walk-forward` or `--use-saved-model`, ECT window 20 default. `run_btc_strategy.run_btc_walkforward`: test = `> 2021-12-31`, trailing 730d retrain every 30d (no fixed train/val split other than the start date). `overfitting_test.py` (docstring 5-13): "The overlays (vol-target, min_dwell, weight) were chosen on full-period hindsight, so this script stress-tests THOSE choices" -- Test 1 sweeps 4x4x5 = 80 (BTC vol target x ETH vol target x weight) combos on the **same** 2022-2026 sample; Test 2 20-day block bootstrap of the chosen portfolio; Test 3 splits at `port.index[len//2]`. ECT (window 40, 1.3/0.5) and TR3 parameters are not swept.

### 3.3 Dependencies of the result
- **Shorting**: none in the headline (long-only). An optional bear-regime short overlay exists (`run_portfolio.py` 50-88, `--short-size`, default 0).
- **Leverage**: **yes, 1.3x**. ECT `risk_high=1.3` scales trade and daily returns by 1.3 whenever equity is at/above 0.995x its 40-trade SMA and unconditionally for the first 40 trades. README ("De-risk, don't lever ... max scale 1.0") describes only the vol-target overlay; the combined scale is up to 1.3x. On spot without borrowing this is unattainable.
- **Pyramiding / averaging**: none (single position, no adds).
- **Fills**: entries and exits at the next hourly bar's open (realistic timing); **zero fees, zero slippage** everywhere in the executed path (`fee = 0.0` hard-coded), despite METRICS.txt "Net of a per-bar cost model". Trade log losses are floored at -8% (`max_loss_cap`), flattering trade-level stats; |open-to-open| > 40% bars are zeroed.
- **Lookahead**: regime path causal (3.2). Daily features rolling; scaler per window; vol-target lag-1; ECT uses equity before the trade. Caveats: hourly feature file generated once over the full history is fine (rolling), but the hold-out TR3 parameter grid (`optimize_tr3.py`) was scored on the test window, and `opt_results.csv`/`opt_best.json` sit in `outputs/`; overlay parameters admitted to be chosen with full-period hindsight; sleeve weights 50/50 and vol targets 0.40/0.50 picked on the reported sample; the robustness "plateau" is in-sample over those same choices. The repo's processed `D1_State_lag1d` column (from a full-sample Viterbi) is in-sample by construction and is used by `optimize_tr3_params.py`.
- **Survivorship**: BTC and ETH only; the "unified engine generalises across assets" claim rests on n=2, both chosen after the fact.
- **Statistical**: "~300+ trades total" (METRICS.txt) over 4.5 years; low win rate (~26% in opt_best) means a handful of large winners carry the result.

### 3.4 Trade records (what exists, what could be computed)
Paths and LFS pointer metadata (contents not in clone):
- `outputs/portfolio_eth/trades_btc_long.csv` -- size 20,891 B, sha256 `4a10d073...`; written by `run_portfolio.py` 223 (file name is hard-coded "btc_long" even for the ETH run; columns per `simulate_tr3` + `apply_ect`: `entry_time, entry_price, exit_time, exit_price, gross_ret, net_ret, holding_hours, regime, strategy, exit_reason, net_ret_scaled, risk_factor, equity, equity_sma`). At ~150 B/row this is roughly 130-150 trades.
- `outputs/portfolio_eth/equity_curve.csv` -- 116,294 B (columns `equity_long_only, equity_combined, daily_ret_long, daily_ret_short, daily_ret_combined`, run_portfolio.py 308-314; ~1,650 daily rows).
- `outputs/portfolio_multi/equity_curve.csv` -- 166,811 B (columns `btc, eth, portfolio, ret_btc, ret_eth, ret_portfolio`, run_portfolio_multi.py 137-143).
- `outputs/optimize/opt_results.csv` -- 336,446 B (one row per grid point).
- `outputs/portfolio_multi/METRICS.txt`, `STRATEGY.md`, `portfolio_chart.png`, `outputs/portfolio_eth/equity_chart.png` (PNG not LFS-tracked but not inspected), `outputs/optimize/opt_best.json` (quoted in 3.2).

Because the CSVs are pointers, **win rate, average win/loss, profit factor, max drawdown and longest losing streak could not be computed**, and no claim in METRICS.txt/README could be checked. The intended computation (to run once `git lfs pull` has fetched the files) is:
```
t = read_csv(trades_btc_long.csv); r = t['net_ret']            # or net_ret_scaled for ECT view
win_rate = (r > 0).mean(); avg_win = r[r>0].mean(); avg_loss = r[r<0].mean()
profit_factor = r[r>0].sum() / -r[r<0].sum()
streak = longest run of consecutive (r <= 0) in exit_time order
e = read_csv(equity_curve.csv, index_col=0)['portfolio' or 'equity_long_only']
max_dd = (e / e.cummax() - 1).min(); daily = e.pct_change(); sharpe = daily.mean()/daily.std()*sqrt(365)
```
Note `net_ret` in that file is already floored at -8% and fee-free, so even the recomputed numbers would inherit those assumptions.

### 3.5 Jupiter / Solana long-only spot
Closest fit of the three: long-only, spot BTC/ETH, hourly entries and exits at the next bar open, 7-day max hold. Adaptation: run the daily HMM and hourly TR3 on cbBTC/WETH/SOL 1h data (Coinbase BTC-USD/ETH-USD can proxy the signal, with basis risk; SOL has no model or data in the repo and the HMM would need its own walk-forward); cap ECT `risk_high` at 1.0 (no leverage); keep vol-target <= 1.0; execute with an hourly poller (no exchange stop orders needed since all exits are evaluated on bar close and filled at next open). Why fresh testing is mandatory: the backtest has zero costs, while this strategy's profile (fail-to-hold, breakeven and no-progress exits produce many near-zero trades; ~70 trades/yr/sleeve, ~26% win rate) is exactly the one most damaged by a 0.5% round-trip -- roughly 3.5%/yr per sleeve in fees alone, and each breakeven exit becomes a -0.5% loss; the 1.3x ECT boost disappears; the TR3 defaults have unknown provenance and a test-window optimisation script exists; SOL's higher volatility shifts every ATR-based threshold. Set `fee_bps=25` per side in `TR3Config` (tr3_engine.py 59) and re-run before believing anything.

### 3.6 Missing evidence
The LFS data and output files; raw Coinbase snapshot; the trade log for BTC (only the ETH-run folder is kept; `outputs/portfolio_voltarget40` referenced in README is absent); provenance of TR3 defaults ("TR_MODEL_2.py" missing); any cost/slippage run; out-of-sample test of the overlays (all tuned on the reported window); results for assets beyond BTC/ETH; tests; pinned library versions (hmmlearn 0.3.x covariance patch suggests fragility); reconciliation of the three contradictory state-label schemes and of `config_spec.md` vs the shipped scripts.

---

## 4. Overlap with previously tested candidates

| Previously tested | Overlap found | Where |
|---|---|---|
| buy_hold | none (benchmark only) | -- |
| random_entry_atr_exits | partial: ATR-multiple trailing stop and ATR-based breakeven/fail levels | HMM_TR_Alg TR3 exits (tr3_engine.py 219-284); Pine uses % stops, not ATR |
| ema_cross_trend | strong: EMA50 > EMA200 filter + EMA50-break exit; SMA9 > SMA21 and close > SMA200 filter | TR3 entry/exit 4; Pine `bullish_trend` |
| donchian_turtle | strong: Donchian-20 (prior-bar highs) breakout entry with ATR trail; no Donchian-low exit, adds ADX/R2/CLV/regime gates | TR3 `breakout` |
| supertrend_consensus | none | -- |
| macd_trend | partial: MACD(12,26,9) line > signal and rising histogram as an entry filter, not a standalone signal | Pine `macd_bullish` |
| tsmom_30d / tsmom_vote / tsmom_trail / tsmom_ema_filter | partial: 30-day cumulative return is an HMM feature in the unused ETH model (`CumRet_30d`); TR3 "thrust" is 1-bar momentum; tsmom_trail/ema_filter share the trail + EMA-filter mechanics | eth_model.py 39-43; TR3 |
| bb_rsi_meanrev_nostop / _stop | conceptual only: notebook is z-score-style mean reversion with entry/exit thresholds and no stop, but on a cointegrated multi-asset spread, not a single-asset BB/RSI; Pine computes BB but never uses it | notebook cells 34, 42, 66 |
| connors_rsi2 | none | -- |
| ft_sample_strategy | weak: RSI + BB + volume-style filters mixed into one entry | Pine |
| ft_strategy001 / ft_strategy003 | weak: EMA-stack trend confirmation | Pine, TR3 |
| ft_bbandrsi | weak (same as bb_rsi row) | Pine (BB unused) |
| ft_adx_smas | moderate: ADX threshold + SMA/EMA cross filter | TR3 `ADX14 >= 14` + EMA50/200; Pine SMA 9/21 (no ADX) |
| ft_adx_momentum | moderate: ADX + momentum/RSI/MACD confirmation stack | TR3; Pine (RSI/MACD/stoch, no ADX) |

Genuinely new relative to the list: (1) HMM regime gating with walk-forward refits and causal sliding-window decoding; (2) cointegration-basket OU mean reversion with regime score, bucketed sizing and inverse-vol allocation (requires shorting; not Jupiter-compatible); (3) the TR3 exit stack (CLV fail-to-hold, delayed breakeven arm, ratcheting 2.5/3.5/4.5-ATR trail, EMA50 break with profit-dependent cushion, 12h no-progress, 7-day time stop); (4) equity-curve throttle and vol-target overlays as position-size modulators; (5) Pine's stochastic %K/%D band, 10-bar structure, volume > 1.2x SMA20 and ATR/close < 3% filters, and a daily-close > daily-SMA50 higher-timeframe gate (must be made causal). None of the three repos supplies verifiable trade records: the notebook has 9 OOS trades in outputs only, the Pine repo has a README table, and HMM_TR_Alg's logs are un-fetched LFS pointers.
