# Fast engine: production-style reliability (paper only)

Date 2026-11-02. Scope: `tradebot fast` (BTC, ETH, SOL; Binance WebSocket reference; Jupiter prices and quotes;
5-second poll). The 4h engine, its ledger (`data/ledger.sqlite`) and its history are unchanged. Execution stays
PAPER-only: the fast engine refuses `mode = "live"` and only constructs a paper broker.

## 1. Were the scanner's problems present in the fast engine? (measured, not assumed)

Fault injection against the engine as it was (`research_lab/results/fast_reliability_diagnostics.txt`):

| Reported symptom | In the fast engine? | Evidence |
|---|---|---|
| Jupiter HTTP 429 | **Yes, and worse.** No backoff, no Retry-After, one 50/min budget shared across Jupiter, Binance and RPC. A feed failure counted as an *execution* error, so 5 polls (25 s) of 429s on prices tripped the **shared** kill file `data/KILL`, which also stops the 4h engine. | D1: KILL written after 5 polls |
| Bursty load | **Yes.** Cost probes fired 24 `/order` calls within 10 s every 10 min (3 symbols x 4 sizes x 2 legs). | D2: peak 24 per 10 s; 37 Jupiter calls per 60 s |
| Stale-price rejections | **Partly an artefact.** Staleness was Price v3 `blockId` versus a slot *extrapolated* at 0.4 s/slot from a `processed` `getSlot` every 30 s. With real slot times of 0.42-0.50 s the extrapolation alone adds 7-17 slots, so a price only 10-20 slots old was rejected (threshold 25). | D3 table |
| Quote latency | **Structural.** Every entry made three sequential quote calls; the broker's pre-execution quote bypassed every budget. Live latency could not be measured from the build environment (no route to Jupiter). | code |
| No completed forward trades | **Not a fault.** Replays on real data show the cost gate rejects nearly everything (section 5); additionally, any 429 burst would have halted the engine via the shared kill switch. | replays |

After the changes (same injected faults): no kill file and no fast halt after 60 s of 429s; 5 of 12 price
requests actually sent during the storm (backoff); no probe bursts (peak `/order` 0 per 10 s in a quiet market); peak
13 Jupiter calls per minute; staleness measured directly as seconds since the price or `blockId` last changed.

## 2. What was implemented

| # | Requirement | Implementation | File |
|---|---|---|---|
| 1 | API-specific rate limiting, exponential backoff, jitter | Token bucket per API (`jupiter_swap` 30/10 s vs documented 50/10 s; `jupiter_price` 30/min; `jupiter_tokens` 6/min; `binance`; `solana_rpc`). 429/5xx/timeouts back off `min(max, base·2^(n-1))·U[0.5,1.5)`, `Retry-After` is a floor, success resets. Calls are suppressed locally during backoff (status `-1`). | `tradebot/fast/ratelimit.py` |
| 2 | WebSocket heartbeat, reconnection, freshness | Protocol pings every 20 s (10 s pong timeout); no message for 30 s forces a reconnect (silent half-open sockets); reconnect backoff with jitter, reset on first message; `fresh_price(pair, max_age)`; REST fallback when stale. | `tradebot/fast/ws.py` |
| 3 | All-in cost estimator, every entry and exit | Round trip (entry) and sale-vs-mark (exit): spread+impact, Jupiter fee (inside outAmount), min-out slippage allowance, signature + priority fees and token-account rent from the quote's lamport fields (else $0.01/tx), adverse-selection buffer. Paper fills use the same quotes at their minimum-out amount and charge the network fee in cash. | `tradebot/fast/costs.py`, `tradebot/fast/broker.py` |
| 4 | Separate deterministic pre-trade gateway | Pure function of a ledger snapshot: capital $300, position $100, exposure $200 (incl. in-flight), 2 open positions, $2 max loss per trade (size × (stop + worst cost)), $15 daily loss (realized + unrealized), $40 drawdown, 3-loss streak pause, 4 trades/hour, 20/day, 60 s spacing, 300 s symbol cooldown, 150 bps max entry cost, price ≤ 15 s, quote ≤ 3 s, reference ≤ 10 s. Runs before the shared RiskEngine; both must approve. Any exception → `risk_service_unavailable` → reject + alert. | `tradebot/fast/gateway.py`, `[fast_risk]` in config |
| 5 | Persistent intent transitions, unique order ids | Ledger enforces `created → signed → submitted → confirmed/failed/expired/abandoned`; terminal states are final; every change is logged in `intent_events`; one intent per order id (unique index + check) → `DuplicateOrderError`. Applies to both engines; all existing 4h flows already conform. | `tradebot/portfolio/ledger.py` |
| 6 | Timeouts and unknown outcomes without duplicates | Unknown outcome → intent `submitted` with its signature; the symbol is blocked and its cash/slot reserved; evidence decides (confirmed → booked once; not found and expired → expired); never resubmitted. Quote timeouts → no order. | `tradebot/fast/engine.py`, `tradebot/fast/recovery.py` |
| 7 | Restart recovery and position reconciliation | On start and every poll: evidence-based intent reconciliation (replaces the old blanket "abandon"); cash and positions recomputed from the fill history and compared with the book and the stored ledger state at startup and after **every** fill; mismatch → critical alert + `FAST_HALT`. | `tradebot/fast/recovery.py` |
| 8 | Structured JSON logs, latency, alerts, emergency halt | `data/fast_events.jsonl` (startup, entry, fill, exit, rejections, alerts, poll summaries, balances on every fill); per-stage latency (quote, gateway, risk, requote, order→fill, signal→order, exit, poll, feed); deduplicated alerts in the log, ledger and event file with an optional notifier; `data/FAST_HALT` via `tradebot fast-halt` / `fast-resume`. Automatic halts write `FAST_HALT`, never the shared `data/KILL`; the fast engine still obeys `data/KILL`. | `tradebot/fast/telemetry.py`, `tradebot/cli.py` |
| 9 | Fail closed | No fresh venue price, no token facts, no quote, stale quote, gateway or risk failure → no entry. Exits act only on fresh prices; an unpriced position raises a critical alert; exits are never blocked by the gateway. | `tradebot/fast/engine.py` |
| 10 | Failure-injection tests | 44 tests: `tests/test_fast_reliability.py` | |

No Claude/LLM call exists anywhere in `tradebot/fast` or `tradebot/scanner` (asserted by a test).

## 3. Test evidence

| Check | Result |
|---|---|
| Full suite | **172 passed** (was 128; 44 new) |
| Execution selftest | **OPERATIONAL 10/10** |
| WebSocket against a real local server | stall detected, dropped connection detected, reconnected with backoff, newer prices received |

## 4. BUY → MANAGE → SELL on real prices, ledger verified after every step

`research_lab/fast_lifecycle_demo.py` on real Binance SOL 1-minute closes, 2026-08-21 → 2026-08-24
(`research_lab/results/fast_lifecycle_demo_SOL_2026-08-22.txt`). Quotes are ReplayFeed's model (15 bps per leg,
min-out at 50 bps slippage, $0.01 per transaction), because there is no Jupiter venue offline. The engine found the
entry and exit itself.

| Time (UTC) | Step | Price | Qty | Cash: book | Cash: ledger | Cash: from fills | Note |
|---|---|---|---|---|---|---|---|
| 2026-08-22 05:11 | BUY | 90.49 | 0.548960 | 949.990000 | 949.990000 | 949.990000 | fill @ 91.0813 (min-out), $50.01 incl. $0.01 fee |
| 2026-08-22 05:12 | MANAGE | 93.18 | 0.548960 | 949.990000 | 949.990000 | 949.990000 | stop 90.1705, take-profit 93.5888 |
| 2026-08-22 05:13 | SELL | 93.73 | 0 | 1001.099938 | 1001.099938 | 1001.099938 | take-profit, **net +$1.0999** (+2.20%) |

Both intents ended `confirmed`; the self-check found no difference at any step. 170 other candidates in those three
days were rejected for insufficient net edge.

## 5. Three-month replays (real prices, same model)

| Asset (2026-07-01 → 09-30) | Polls | Trades | Net | Rejections | Self-check | Alerts |
|---|---|---|---|---|---|---|
| SOL | 132,480 | 1 | +$1.10 | 3,642 (all net edge < 20 bps) | OK | 0 |
| BTC | 132,480 | 0 | $0.00 | 3,257 (all net edge < 20 bps) | OK | 0 |

The engine is reliable; the strategy rarely has an edge large enough to pay a Jupiter taker's costs. This is the same
conclusion as `research_lab/REALTIME_SCANNER_EVAL.md`, now on a hardened engine. One trade is not a track record.

## 6. Still unmeasured

Live Jupiter latency, real 429 frequency at these budgets, WebSocket behaviour on the PC's network, and the Price v3
`blockId` lag distribution. Run on the PC:

```bash
tradebot fast --ws --minutes 60
```

```bash
tradebot fast-report --hours 1
```

The report prints per-stage latency, per-API 429s and local throttles, WebSocket reconnects and stalls, alerts,
intent states, the ledger self-check and every rejection reason.
