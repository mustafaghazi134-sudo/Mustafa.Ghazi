# Real-time opportunity scanner: benchmark, external-system review, design, economics

Date 2026-11-02. Scope: evaluate whether TRADEBOT can evolve toward hundreds or thousands of evaluated events per day
with seconds-level execution, without replacing or interrupting the 4h paper engine. Everything measurable here was
measured offline (this container has no route to Jupiter, Binance, Solana RPC or any documentation site other than
GitHub); everything that needs the live network is implemented, tested with mocked feeds, and marked **UNMEASURED**.

## 1. Benchmark of the existing engine (`research_lab/benchmark_engine.py`, `results/benchmark_engine.json`)

Real `TradingBot` on the synthetic provider, 300 cycles, 3 symbols, 1000 bars each, under cProfile:

| Measure | Value |
|---|---|
| Cycle compute time (median / p95 / max) | 6.8 ms / 10.5 ms / 18.1 ms |
| Share of compute: strategies / SQLite / accounting / provider | 45% / ~25% (inside "other") / 0.5% / 0.6% |
| Strategy evaluation, 1000 bars, per symbol | tsmom 0.31 ms, momentum 0.30 ms |
| Ledger write (one decision row) | 0.14 ms |
| HTTP requests per live cycle | 3 typical (Price v3, Binance ticker, Tokens v2 cached; klines cached 25% of a bar) |
| Poll interval | 60 s |
| Strategy decision cadence | tsmom once per UTC day; momentum once per completed 4h bar |
| Order path worst case (timeouts x retries) | 105 s; typical seconds, **UNMEASURED** here |

**Real bottlenecks, in order.** (1) Design cadence: a 4h, daily-evaluated trend system produces about one decision a
day per symbol; nothing in the code path matters next to that. (2) The 60 s poll adds up to 60 s of reaction delay
after a bar closes. (3) Network round trips: each live cycle is three sequential HTTP calls (hundreds of ms each,
unmeasured); the quote → sign → execute path is already seconds-level by construction (Jupiter submits and confirms
the transaction itself). (4) Pure-Python indicators recompute over 1000 bars every cycle; at 0.3 ms per symbol this
would only matter past ~1,000 symbols. CPU, SQLite and the risk engine are not bottlenecks. The engine can
technically act within seconds of seeing a signal; it simply does not look for signals more than once a day.

## 2. What the scanner is (`tradebot/scanner/`, `tradebot scan`, `tradebot scan-report`)

A second process with its own SQLite file, poll loop and request budget. Per poll (default 5 s): one Price v3 request
for the whole universe (≤ 50 mints), one Binance `ticker/price` for the reference pairs, `getSlot` every 30 s, Tokens v2
facts every 10 min. Prices are aggregated into 1-minute bars in memory. Deterministic signals produce candidates with
a mechanical expected move; each candidate is then priced with two size-specific read-only `/swap/v2/order` quotes
(USDC → token → USDC) and passed through the rejection rules in `filters.py`. Survivors become hypothetical positions
at the **worst-case** buy price (minimum-out), settled at the horizon with a real sell quote; rejected candidates are
marked at the horizon so "missed" is a number. Cost probes at $5 / $50 / $100 / $1,000 run every 10 minutes.

Safety: no wallet, no signing import, no `taker`, no `/execute` (asserted by `tests/test_scanner.py` on the module's
code tokens). Claude is not in the loop. The request budget (default 50/min) protects the free-tier key and the 4h
engine, which shares the same key.

Signals (all long-only, all cost-aware, none tuned): `basis_reversion` (Jupiter below Binance by > 30 bps, expect half
the gap to close within 5 min), `breakout_5m` (close above the prior 30-minute high with 5-minute momentum, expect
half the average 5-minute range), `dip_reversion_1m` (1-minute return < −2.5 sd, expect half the drop back).

Measured in tests (mocked feeds): candidate opened only when net edge ≥ threshold after worst-case cost; a 2% basis
gap that converges settles as a win; one that widens is counted as a false positive; rejected candidates whose
price later moves by more than the measured cost are counted as missed; 429 responses are recorded as rate-limited
with the error rate; a request budget of 3/min forces `request_budget` rejections; probes record all four sizes and
the $5 probe is the most expensive because of the fixed network fee.

## 3. Live measurements still required (on the user's machine)

```bash
tradebot scan --minutes 60
tradebot scan-report --hours 1
```

That run yields, for the first time, real numbers for: Price v3 latency and how often its `blockId` advances between
5-second polls (true refresh rate), slot drift of the price, Binance reference latency, quote latency, 429 frequency
at this request rate, and worst-case round-trip cost at four sizes for SOL, cbBTC and WETH. Until then every cost
figure below is an assumption.

## 4. Economics: is 20 / 50 / 100 profitable trades a day plausible? (`research_lab/economics.py`)

Arithmetic, not a forecast. Cost floor per round trip = variable cost (spread + impact + Jupiter fee, both legs)
+ fixed network fees ($0.01 × 2, divided by the position size) + 10 bps adverse selection. Variable cost is ASSUMED
at 20 bps (≤ $100) and 30 bps ($1,000) until probes replace it. Typical moves come from the last 12 months of 1h data
scaled by √time (approximation).

| Capital | Trades/day | Fixed fee | Variable | Cost floor / trade | Daily cost as % of capital | Gross edge per trade for +1%/day |
|---|---|---|---|---|---|---|
| $5 | 20 | 40 bps | 20 bps | 70 bps | 14% | 75 bps |
| $5 | 100 | 40 bps | 20 bps | 70 bps | 70% | 71 bps |
| $50 | 20 | 4 bps | 20 bps | 34 bps | 6.8% | 39 bps |
| $50 | 100 | 4 bps | 20 bps | 34 bps | 34% | 35 bps |
| $100 | 20 | 2 bps | 20 bps | 32 bps | 6.4% | 37 bps |
| $100 | 100 | 2 bps | 20 bps | 32 bps | 32% | 33 bps |
| $1,000 | 20 | 0.2 bps | 30 bps | 40 bps | 8% | 45 bps |
| $1,000 | 100 | 0.2 bps | 30 bps | 40 bps | 40% | 41 bps |

Typical moves (1 sd): SOL ≈ 13 bps per minute, 28 bps per 5 minutes; BTC ≈ 6 bps per minute, 14 bps per 5 minutes.

Reading: at $5 the two network fees alone are 40 bps, so each trade must earn more than 70 bps net of losers from
1-minute moves whose standard deviation is 13 bps. That is not plausible at any trade count. At $50 to $1,000 the
floor is 32 to 40 bps per trade; 100 trades a day burns 32 to 40% of capital a day in costs, so the strategy's average
net capture per trade must exceed a 5-minute standard deviation of SOL on every trade just to break even. For 20
trades a day the daily cost is 6 to 8% of capital, still an order of magnitude above what the deployed 4h strategy
pays in a month. No public evidence reviewed here, and nothing in the development data, shows a retail taker signal
with that capture. The scanner exists to measure whether any such candidate survives real costs, and its design makes
a negative result as informative as a positive one. Probability of 100 profitable trades a day at retail sizes on
Jupiter as a taker: economically implausible until the scanner shows otherwise; 20 a day at ≥ $100 is the only cell
worth testing, and only for `basis_reversion`, whose expected move is a measured dislocation rather than a forecast.

## 5. 1-minute / 5-minute candidates vs the 4h baseline (`research_lab/intraday.py`)

Harness: Binance 1m CSVs → 1m, 5m and 4h frames from the same bars; four candidates (`breakout_5m`, `dip_reversion_1m`,
`ema_cross_5m`, `vwap_pullback_5m`) with rolling 30-day-train / 7-day-test walk-forward, fees 10 + slippage 15 bps per
side plus the fixed network fee scaled by capital; truncation lookahead check (signals at bar t identical with or
without later bars); 4h baseline with deployed parameters on the same period when it is long enough.

Smoke test on a synthetic random walk (not evidence about markets, only that the harness is honest): every candidate
loses roughly its cost load (profit factors 0.0 to 0.4, 1.4 to 5 trades a day), lookahead checks pass.

PLACEHOLDER_1M_RESULTS

## 6. External systems review

PLACEHOLDER_EXTERNAL

## 7. Verdict and next step

PLACEHOLDER_VERDICT
