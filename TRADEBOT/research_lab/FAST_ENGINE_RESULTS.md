# Fast engine: first end-to-end results (replay on real 1-minute data, paper fills)

Date 2026-11-02. Engine: `tradebot fast` (`tradebot/fast/engine.py`), paper only. Pipeline per 5-second poll:
SCAN → DETECT → COST (two fresh size-specific quotes, worst-case round trip) → RISK (independent `RiskEngine`) →
BUY (re-quote immediately before the order, reject if the edge vanished) → MANAGE (stop, target, trailing, max hold,
kill switch) → SELL (reduce-only through the same risk engine) → RECORD (own ledger `data/fast.db`).

## What was run

The user's PC was not connected and this container has no route to Jupiter or Binance, so the engine was exercised two
ways: (1) the test suite with a deterministic fake feed (automatic BUY → take-profit SELL, stop-loss, max-hold,
edge-vanished rejection, risk-engine rejections, kill-switch flatten, low-edge candidates not forced, WebSocket
parser); (2) a replay over **real Binance 1-minute closes** for SOL and BTC, 2026-07-01 → 2026-09-30 (132,480 polls,
one per bar), with the engine's clock following the bars.

What is simulated in the replay, stated plainly: there is no second venue offline, so the `basis_reversion` signal
cannot fire and the "venue" price is the Binance close; token facts are fixed to verified and liquid; quotes are
parametric (15 bps per leg, minimum-out at the configured 50 bps slippage tolerance), the same assumption the paper
broker uses without a Jupiter key. Fills, fees ($0.30 on the one $50 round trip) and timestamps are therefore a
simulation on real prices, not evidence about live execution.

## Automatic events produced (nothing forced, nothing hand-picked)

| Asset | BUY (UTC) | Entry | SELL (UTC) | Exit | Reason | Net after fees | Fees | Held | Signal→order |
|---|---|---|---|---|---|---|---|---|---|
| SOL | 2026-08-22 05:11:00 | 90.5451 (0.550555 SOL, $50.00) | 2026-08-22 05:13:00 | 93.6238 | take_profit | **+$1.39 (+2.79%)** | $0.30 | 120 s | 1 ms (compute only; no network) |
| BTC | – | – | – | – | – | no trades | – | – | – |

Everything else was rejected by the deterministic gate, and the reasons are on record:

| Asset | Candidates | Executed | Rejected | Rejection reason | Rejected candidates that would have cleared cost at the horizon ("missed") |
|---|---|---|---|---|---|
| SOL | 3,643 (2,478 dip, 1,165 breakout) | 1 | 3,642 | `net_edge<20bps` for all 3,642 | 4 (all breakout_5m, three of them within 45 minutes of the executed trade on 2026-08-22, marks +21 to +44 bps) |
| BTC | 3,257 (2,651 dip, 606 breakout) | 0 | 3,257 | `net_edge<20bps` for all 3,257 | 0 |

Average expected move of a candidate: 8.8 bps. Average worst-case round trip at the 50 bps slippage tolerance:
133 bps. A candidate therefore needs an expected move above roughly 165 bps (cost + 10 bps adverse buffer + 20 bps
minimum edge) to be bought; on SOL that happened once in three months, during the 22 August sell-off, and the trade
was closed by take-profit two minutes later. This is the same conclusion as `REALTIME_SCANNER_EVAL.md` reached from
the walk-forward: at retail taker costs, short-horizon signals almost never carry enough edge, and the engine's job
is to prove that trade by trade rather than to generate volume.

## Measured engine properties (from the replay and the tests)

| Property | Value |
|---|---|
| Compute latency detect → order | p50 1 ms, max 1 ms (replay; live adds two quote round trips and the broker quote, unmeasured here) |
| Paper order → fill | 2 ms |
| Poll rate sustained in replay | 132,480 polls per asset, no errors |
| Risk engine involvement | every BUY and SELL carries a `risk` decision row; the exit on a tripped kill switch is reduce-only through the same engine |
| Live execution reachable | no: `FastEngine` raises `PermissionError` on `mode=live`; only `PaperBroker` is constructed; the ledger is separate from the 4h engine's and never feeds the live gate |

## Knobs that decide how often it trades (and why they are where they are)

- `execution.live_slippage_bps` (50): the quote's `otherAmountThreshold` is what the engine assumes it will actually
  get on both legs. Lower tolerance → lower worst-case cost → more trades, but more failed swaps when live.
- `scanner.min_net_edge_bps` (20) and `scanner.adverse_buffer_bps` (10): the margin demanded after worst-case cost.
- `fast.stop_pct` (1.0), `fast.take_profit_frac` (1.0 × expected move, floored at cost + 10 bps), `fast.max_hold_mult` (2).

Loosening these produces more trades; it does not produce more edge. Any change should be judged on the forward
paper record the engine writes, not on this replay.

## What the live run on the PC adds

`windows\run_fast.bat` (or `tradebot fast --ws`) replaces every simulated element: Jupiter prices with `blockId`
staleness, Binance reference (WebSocket, with measured exchange→local latency, or REST), real size-specific quotes and
fills from the paper broker's Jupiter quote path, and the `basis_reversion` signal, which is the only one whose
expected move is a measured dislocation rather than a forecast. `tradebot fast-report --hours 24` prints the trades
with timestamps and net results, win rate, fees, drawdown, frequency, latency percentiles and rejection counts.
