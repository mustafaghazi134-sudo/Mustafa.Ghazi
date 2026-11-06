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

### Real data: Binance 1m klines, 2025-01-01 → 2026-10-24 (unseen by every candidate; 953,280 bars per symbol)

Data: `parshant231234-eng/*-Historical-Data` monthly 1m CSVs, verified bit-exact against official
data.binance.vision zips (and their committed checksums) held in two other public repos; zero gaps in this period.
Candidates were walk-forward tested (30-day train, 7-day test, parameters chosen on train only) with 10 bps fee +
15 bps slippage per side plus the $0.01 network fee per transaction on $100 capital, whole capital per trade.

| Strategy (OOS, with costs, $100) | Asset | Trades | Trades/day | Win rate | Profit factor | Net return | Max DD | Expectancy/trade |
|---|---|---|---|---|---|---|---|---|
| breakout_5m | SOL | 1,852 | 2.9 | 21.0% | 0.24 | −100% | −100% | −52 bps |
| dip_reversion_1m | SOL | 636 | 1.0 | 13.4% | 0.12 | −96% | −96% | −50 bps |
| ema_cross_5m | SOL | 3,289 | 5.2 | 14.8% | 0.29 | −100% | −100% | −52 bps |
| vwap_pullback_5m | SOL | 1,175 | 1.9 | 20.0% | 0.12 | −100% | −100% | −49 bps |
| breakout_5m | BTC | 1,122 | 1.8 | 12.8% | 0.13 | −100% | −100% | – |
| dip_reversion_1m | BTC | 702 | 1.1 | 2.3% | 0.01 | −98% | −98% | – |
| ema_cross_5m | BTC | 3,351 | 5.3 | 9.9% | 0.15 | −100% | −100% | – |
| vwap_pullback_5m | BTC | 1,154 | 1.8 | 8.2% | 0.05 | −100% | −100% | – |
| (ETH: same picture, profit factors 0.07 to 0.26) | | | | | | | | |
| **baseline tsmom_ema_filter 4h, deployed params, 100% sizing** | BTC | 11 | 0.02 | – | – | **+21.8%** | −20.3% | – |
| | ETH | 18 | 0.03 | – | – | +16.4% | −46.7% | – |
| | SOL | 13 | 0.02 | – | – | −15.3% | −41.0% | – |
| buy and hold | BTC / ETH / SOL | | | | | −13.6% / −29.3% / −46.0% | | |

Every intraday candidate lost essentially everything; every one is cost-dominated (expectancy ≈ −50 bps per trade,
which is the 52 bps round trip). Lookahead (truncation) checks passed for all four.

### Why: the gross edge is one to two basis points per trade

The same candidates with **zero** fees and slippage (network fee negligible on $100,000), same period, same
walk-forward:

| Strategy (OOS, zero cost) | Asset | Trades | Win rate | PF | Net return | Max DD | **Gross expectancy/trade** | Avg win / avg loss |
|---|---|---|---|---|---|---|---|---|
| dip_reversion_1m | SOL | 5,097 | 58.7% | 1.09 | +64.7% | −14.0% | **+1.1 bps** | 22 / −28 bps |
| vwap_pullback_5m | SOL | 1,591 | 34.1% | 1.09 | +30.4% | −11.0% | **+1.8 bps** | 64 / −30 bps |
| breakout_5m | SOL | 2,798 | 41.7% | 0.99 | −18.2% | −36.8% | −0.3 bps | 71 / −51 bps |
| ema_cross_5m | SOL | 4,258 | 26.1% | 1.00 | −12.5% | −50.3% | +0.2 bps | 105 / −37 bps |
| dip_reversion_1m | BTC | 6,123 | 60.9% | 1.07 | +26.4% | −13.3% | **+0.4 bps** | 10 / −15 bps |
| vwap_pullback_5m | BTC | 1,532 | 28.6% | 1.03 | +4.0% | −12.7% | +0.3 bps | 43 / −17 bps |
| breakout_5m / ema_cross_5m | BTC | 1,979 / 4,425 | 39% / 27% | 0.88 / 0.96 | −38% / −31% | | −2.3 / −0.7 bps | |

This is the measured answer to "how much edge is there to pay costs with": the best short-horizon candidates earn
**+0.3 to +1.8 bps gross per trade** before any cost. The cheapest realistic round trip on Jupiter for a retail
taker is 30 to 40 bps (section 4), 20 to 100 times larger. A 1-minute mean-reversion signal with a 59 to 61% win
rate is real, and it is worthless to a taker paying the spread twice; it is the kind of edge market makers earn by
being paid the spread instead. Freqtrade's own documentation makes the same point about fees applied twice per
trade. The baseline, by trading 11 to 18 times in 22 months, pays about 1% in costs over the whole period and keeps
its edge; it also shows SOL is the one asset where the deployed strategy lost in this period (−15% at 100% sizing,
−9% even without costs), which is consistent with SOL being flagged unvalidated in every earlier report.

## 6. External systems review

Full review with file-level citations: `research_lab/EXTERNAL_SYSTEMS_REVIEW.md` (every fact from a read-only
GitHub clone; no documentation site or API was reachable, so pricing and rate-limit figures are repo-stated and
marked UNVERIFIED where the vendor's own files disagree).

| System | What it gives | Latency model | Cost (repo-stated) | Effort | Scanner verdict | Execution verdict |
|---|---|---|---|---|---|---|
| Helius standard WebSockets | `accountSubscribe`, `logsSubscribe`, `programSubscribe`, `signatureSubscribe`, `slotSubscribe` | push | all plans, credit-metered; free tier exists but the vendor's files disagree on the entry plan | S | useful later for balance and signature events; not needed for Jupiter-vs-Binance signals | `signatureSubscribe` can replace polling for confirmation; Jupiter still lands the transaction |
| Helius Enhanced WS / LaserStream (Geyser gRPC) | parsed transaction streams, shred-level accounts/slots | push | paid; Developer $49 vs Business $499 minimum **UNVERIFIED (conflicting files)**; no Python SDK for LaserStream | M to L | overkill for 1 to 10 s polling of a dozen majors | no value while `/swap/v2/execute` submits |
| "Helius Core AI" | `helius-labs/core-ai`: CLI, MCP server and Claude skills for Helius APIs | n/a | same plans | n/a | developer tooling, not a data or signal product | not an execution path |
| Hummingbot 2.17 client | 1 s clock with per-component exception isolation, order-book tracker (REST snapshot + WS diffs), PMM / XEMM / amm_arb, Strategy V2 controllers and triple-barrier executors, paper trading against the live book with a 5 s fill delay | 1 s tick | open source; Cython, pandas, numpy (numpy is blocked on the user's Windows host) | L to adopt, S to borrow | patterns borrowed: fee-inclusive go/no-go per cycle, proposal pipeline, executor state machine, paper fill delay | its Jupiter path (Gateway, Node) still calls deprecated `/swap/v1`, polls quotes at 1 s and signatures at 2 s: slower than TRADEBOT's direct v2 calls |
| Freqtrade | backtest assumptions (open fills, worst-case fees), `lookahead-analysis`, `recursive-analysis`, hyperopt overfitting guards, FreqAI sliding window, trades/day, rejected-signal counting; edge module removed 2025.6 | offline | open source | S to M | adopted: truncation lookahead test in `intraday.py`; trades/day; worst-case fees. To do: startup-length variance test for EMA/tsmom | n/a |
| Birdeye | price, OHLCV to 1 s, `token_security`, top traders, Smart Money token list, WebSocket price/trade feeds | REST (1 rps on the $0 tier) + WS (paid tiers) | Standard $0 / Lite $39 / Starter $99 (needed for security and Smart Money) / WS from $250 to $499, **all from third-party doc mirrors, UNVERIFIED** | S | only if the universe grows beyond cbBTC, WETH and SOL; Jupiter Tokens v2 already gives the audit fields for free | data only |
| Jupiter Swap v2 / Price v3 / Tokens v2 | routers `metis`, `jupiterz`, `dflow`, `okx`; `mode` ultra/manual; `feeBps` 0 to 50 by pair class; idempotent `requestId` for ~2 min; Price v3 `blockId`/`liquidity`; Tokens v2 audit fields | request/response | key required; **Swap API 50 requests per 10 s at $0 executed volume**, scaling with volume; "Pro plan does NOT increase Swap API limits"; other APIs managed at portal level (unquantified) | already integrated | yes: the scanner's 50 req/min budget sits inside the Swap limit; Price v3 serves 50 mints per call | yes, current path |
| Jupiter Trigger / Recurring | custodial limit, OCO, DCA orders | server-side | min $10 / $100, 0.1% fee | M | n/a | conflicts with the independent risk engine and local custody; not recommended |
| X / Grok, Reddit, YouTube, RSS | text | minutes-scale polling or LLM calls | mostly UNVERIFIED; RSS free | S to M | outside the tick: hours-scale research cadence only | no |

What was adopted from the review: the fee-inclusive per-cycle go/no-go (amm_arb pattern) is the scanner's
`net_edge_bps`; Freqtrade's trades/day metric and a truncation lookahead test are in `intraday.py`; Jupiter's
Swap API limit sets the scanner's request budget. What was rejected: Hummingbot wholesale (Cython and numpy are
non-starters on the Windows host), Gateway (deprecated Jupiter v1 path), Trigger/Recurring custody, paid Helius
and Birdeye tiers until a free-tier measurement shows the data would change a decision.

## 7. Verdict and next step

**Measured.** The existing engine's limit is cadence, not code (7 ms of compute per cycle; one decision per day by
design). Intraday candidates on 22 months of unseen 1-minute data have a gross edge of at most +1.8 bps per trade
against a cost floor of 32 to 70 bps per trade; with costs they lose everything, at every capital level, on all
three assets. The deployed 4h strategy made +21.8% (BTC) and +16.4% (ETH) at 100% sizing over the same period and
lost on SOL.

**20 / 50 / 100 profitable trades per day.** At $5: implausible under any assumption (the two network fees alone
are 40 bps per trade). At $50 to $1,000: 100 trades a day costs 32 to 40% of capital per day; 20 a day costs 6 to 8%;
the measured gross edge of the best candidates is about 1 bps per trade. Nothing reviewed (Hummingbot, Freqtrade,
Helius, Birdeye, Jupiter routing) changes that arithmetic, because none of them reduces a taker's spread, impact,
Jupiter fee or network fee. The only mechanism that could is being a maker (quoting, as Hummingbot's PMM does),
which Jupiter's swap API does not offer; it would need a limit-order venue and a different risk model. Verdict:
not economically plausible for this bot as a Jupiter taker; the claim should be considered closed unless the live
scanner contradicts the cost assumptions by a factor of ten.

**What the scanner is for, then.** It is cheap, read-only and measures the one thing still unmeasured: real
Jupiter round-trip cost at $5 to $1,000 and the real frequency and size of Jupiter-vs-Binance dislocations
(`basis_reversion`), the single candidate whose expected move is a measured gap rather than a forecast. If a 60-minute
run shows basis gaps of 50 to 100 bps occurring several times an hour on SOL at a worst-case round trip under 20 bps,
that would be worth a longer paper run. If not, short-horizon trading is finished as a line of inquiry and effort
goes back to the 4h strategy's forward test.

**Next step (user's machine, nothing else pending).** After `windows\update.bat` has pulled this commit:

```bash
tradebot scan --minutes 60
```

```bash
tradebot scan-report --hours 1
```

Paste the report block. Live trading permissions, the production strategy, the 4h paper engine and the risk
engine were not changed by any of this work.
