# tradebot — autonomous Solana trading bot (Claude research · independent risk engine · paper-first)

`tradebot` scans Solana markets around the clock, backtests and tunes strategies, generates
signals, executes swaps through Jupiter, manages open positions, and reports net profitability.
Claude (`claude-opus-5-5`) acts as a bounded research analyst. A deterministic risk engine, hard
loss limits, and an emergency kill switch sit between every signal and every order.

**Paper trading is the default.** Real-money execution stays off until the owner deliberately
arms it through three independent gates (see *Going live*). Once armed, individual trades need
no manual approval.

```
market data ─► universe ─► strategies ─► signals ─► Claude vet (veto / shrink only)
                                                        │
                       research loop (walk-forward backtests + Claude) updates params
                                                        ▼
                                           RiskEngine.evaluate ─► HMAC approval token
                                                        ▼
                                     PaperBroker | JupiterLiveBroker ─► fills
                                                        ▼
                               SQLite ledger ◄─ PositionManager (stops · TP · trailing · time)
                                                        ▲
                                KillSwitch (file · env · CLI · auto-trip) flattens & halts
```

## What is inside

| Area | Module | Notes |
|---|---|---|
| Config | `tradebot/config.py` | TOML + env; `mode="paper"` default; env `TRADEBOT_FORCE_PAPER=1` can force paper, nothing can force live |
| Market data | `tradebot/data/live.py` | Jupiter prices + verified token list, Binance klines (OHLCV), DexScreener liquidity/volume, CoinGecko fallback |
| Offline data | `tradebot/data/synthetic.py`, `csv_replay.py` | Deterministic regime-switching series and CSV replay for tests and sandboxes |
| Strategies | `tradebot/strategies/` | `tsmom` (validated, default), `momentum` (EMA cross, default), `mean_reversion` and `breakout` (rejected in research, disabled) |
| Research | `tradebot/research/` | Event-driven backtester (next-open fills, intrabar stops, fees, slippage), metrics, walk-forward grid optimizer, Claude analyst |
| Risk | `tradebot/risk/engine.py` | Position/exposure/order caps, daily-loss halt, drawdown kill, trade-rate cap, loss streak cooldown, liquidity/volume/slippage floors, allow/deny lists, stale-data and error-streak kills, HMAC approval tokens |
| Kill switch | `tradebot/risk/kill_switch.py` | `data/KILL` file, `TRADEBOT_KILL=1`, `tradebot kill`, or auto; flattens to USDC; manual reset only |
| Execution | `tradebot/execution/` | `PaperBroker`; `JupiterLiveBroker` (quote → swap → local sign with `solders` → send → confirm); `LiveGate`; wallet loading |
| Portfolio | `tradebot/portfolio/` | SQLite ledger (orders, fills, positions, trades, equity, decisions, events, API cost), accounting, reports |
| Engine | `tradebot/engine/bot.py` | 24/7 loop, exits, entries, research cadence, daily reports, flatten-on-kill |
| CLI | `tradebot/cli.py` | `init run backtest research report status kill resume arm-live disarm-live fetch-data` |

## Windows one-click setup

Download [`windows/setup_windows.bat`](windows/setup_windows.bat) into your trading folder and double-click
it. It checks Git and Python (asking before installing either via winget), clones or updates this branch
into `Mustafa.Ghazi\`, creates `.venv`, installs dependencies, runs the test suite and `selftest`, writes
`config.toml`, and runs `preflight` against the live market hosts. Then:

- `windows\update.bat` (or `scripts/update.sh` on Linux/macOS) updates an existing install: backs up `config.toml`
  and `.env`, pulls this branch, updates dependencies, runs `tradebot apply-profile` (edits only the validated keys in
  your config, preserving API keys and limits, with a backup), then tests, preflight, asset verification and 3 paper
  cycles. Trade history in `data/` is untouched.
- `windows\run_paper.bat` starts continuous paper trading with real prices and auto-restarts. It sets
  `TRADEBOT_FORCE_PAPER=1` and blanks the live-trading variables, so it can never send a real order.
- `windows\status.bat` shows state and the 7-day report. `windows\kill_switch.bat` is the emergency stop.

## Quick start (paper trading)

```bash
cd TRADEBOT
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"             # the bot is pure Python + httpx/pydantic/solders (no NumPy: Windows Application Control blocks numpy's _pcg64)
tradebot init                      # writes config.toml and .env.example
cp .env.example .env               # add ANTHROPIC_API_KEY
set -a; source .env; set +a
tradebot run                       # paper trading on live market data, forever
```

Read-only diagnostics (nothing is signed or sent):

```bash
tradebot selftest                                    # offline: paper fills, risk approvals, limits, kill switch
tradebot preflight                                   # network reachability per host, API key, kill switch, gate
tradebot quote --amount 0.001 --wallet <PUBKEY>      # real Jupiter SOL->USDC quote + fee/rent/slippage breakdown
tradebot wallet --address <PUBKEY>                   # SOL and SPL balances by public key
```

Other useful commands:

```bash
tradebot status                    # mode, kill switch, live gate, paper record, positions
tradebot report --days 7 --decisions 20
tradebot backtest --optimize       # walk-forward grid search on live history
tradebot research                  # one research cycle: backtests + Claude proposal, applied with bounds
tradebot kill --reason "news"      # EMERGENCY STOP: no new entries, flatten, halt
tradebot resume                    # after review
```

No market access (CI, sandbox)? `tradebot run --provider synthetic --cycles 1000` runs the whole
system on deterministic synthetic data; results land in the separate `replay` ledger
(`tradebot report --mode replay`) and never count toward the live gate.

## Jupiter integration (aligned with the official `jup-ag/agent-skills`)

Reviewed against Jupiter's official agent skills `integrating-jupiter` (v1.2.0) and `jupiter-swap-migration` (v1.1.0).

| Purpose | Endpoint | Notes |
|---|---|---|
| Spot prices, 24h change | `GET https://api.jup.ag/price/v3?ids=<mints>` | a mint missing from the response is treated as unpriced (fail closed) |
| Liquidity, volume, verification, decimals | `GET /tokens/v2/search?query=<mints>` | tokens flagged `audit.isSus` are excluded |
| Universe scan (optional) | `GET /tokens/v2/tag?query=verified` | verified, organic score >= 50, not `isSus`, stables excluded |
| Read-only quote | `GET /swap/v2/order` **without** `taker` | `transaction` is null; `/swap/v1/quote` is deprecated |
| Live execution | `GET /swap/v2/order?taker=…` -> sign locally -> `POST /swap/v2/execute` | Jupiter submits and confirms; actual amounts from `inputAmountResult`/`outputAmountResult`; retryable codes re-quote |
| Wallet balances | Solana RPC | the Ultra balances endpoint is deprecated |

An `x-api-key` from https://developers.jup.ag (free tier) is required for every Jupiter call; set `JUPITER_API_KEY`
or `[data].jupiter_api_key`. Quotes use a 5 s timeout, executes 30 s, HTTP 429 gets exponential backoff with jitter.

> The build sandbox had no route to any Jupiter, Solana or exchange host, so these integrations were verified
> against the skills' documented request/response shapes with mocked HTTP (`tests/test_jupiter_api.py`), not
> against the live endpoints. Run `tradebot preflight` and `tradebot verify-assets` on a connected machine first.

## Assets: how BTC/ETH exposure is traded on Solana

The validated strategies were tested on BTC and ETH spot prices (Binance history). On Solana the bot takes that
exposure through wrapped assets routed by Jupiter: **BTC = cbBTC** (Coinbase Wrapped BTC,
`cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij`) and **ETH = WETH** (Wormhole,
`7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs`), plus native SOL. Signals come from Binance candles
(`[universe].candle_symbols`), fills from the wrapped token's Jupiter price, so the small basis between the two
is visible in the paper ledger. `auto_scan` is off by default so no untested token is added.

```bash
tradebot verify-assets          # read-only: mint, decimals, Jupiter price, liquidity vs limits, Binance candles,
                                # buy+sell quotes at the bot's real order size, round-trip cost, Jupiter/Binance basis
```

## How Claude is used, and how it is bounded

Claude is called through the Anthropic SDK with structured JSON outputs, adaptive thinking, and
server-side refusal fallbacks enabled. Three jobs, all advisory:

1. **Research** (every `research_interval_hours`): the bot walk-forward backtests every strategy's
   parameter grid, discards anything unprofitable out-of-sample, and shows Claude the ranked
   candidates plus live performance and market stats. Claude may pick a *tested* candidate per
   strategy, disable a strategy, or suggest symbols to drop. It cannot invent parameters.
2. **Signal vetting** (optional, `vet_signals=true`): Claude sees the signal, liquidity, 24h move,
   recent strategy stats and open exposure, and returns approve/veto plus a size multiplier that is
   clamped to ≤ 1.0. It can only reduce.
3. **Daily narrative**: a plain-language summary appended to `data/reports/YYYY-MM-DD.txt`.

Claude never sees or touches the risk limits, kill switch, live gate, or wallet. If the API is
down or refuses, the bot continues on deterministic strategies (`fail_open=true`). Estimated API
spend is tracked and subtracted in net profit.

**Default during the forward test: no Claude calls at all.** `tradebot apply-profile` (and the
generated `config.toml`) set `[claude] enabled=false`, `research_enabled=false`, `vet_signals=false`,
`daily_report=false`, `auto_apply_overrides=false`. Monitoring, signals, risk and execution are
deterministic software. When research is switched on later, the default cadence is weekly
(`research_interval_hours=168`), and without Claude the research pass only **logs proposals**: it
never changes the running parameters unless `auto_apply_overrides=true`. A forward test whose
parameters are silently re-fitted every six hours is not a forward test (see
`research_lab/SPLITS.md`).

## Forward-test integrity and the tracking check

- **Frozen parameters.** The paper record is evidence only if the strategy that produced it is the
  one that was validated. Parameter changes require an explicit config edit or Claude's bounded
  choice from tested candidates; nothing re-optimises on its own.
- **Signal venue vs execution venue.** Strategies are computed on Binance candles (`BTCUSDT`,
  `ETHUSDT`, `SOLUSDT`); orders are quoted on Jupiter (cbBTC, Wormhole WETH, SOL). Every cycle the
  bot fetches Binance spot for the signal symbols and compares it with the Jupiter price. The gap
  is logged (`last_basis_bps`), and a symbol whose gap exceeds `[data] max_basis_bps` (100) is
  blocked from **new entries** that cycle (`basis_block` event, `entry_blocked` decision). Exits are
  never blocked. `tradebot verify-assets` reports the same basis at the same instant.
- **Costs on record.** `tradebot verify-assets` quotes a full USDC → asset → USDC round trip at the
  minimum, intended and maximum order sizes, reports the expected cost (quoted `outAmount`) and the
  worst case (`otherAmountThreshold`, the minimum the swap would accept, on both legs) plus two
  network fees, and appends each run to `data/verify_assets.jsonl`. Research assumed 25 bps per
  side (50 bps round trip); the first real run on your machine replaces that assumption.

## Real-time opportunity scanner (read-only, paper, separate process)

`tradebot scan` is a second, independent process for evaluating short-horizon opportunities without touching the
4h engine: its own database (`data/scanner.db`), its own poll loop (default every 5 s), no wallet, no signing
module, and no `taker` on any Jupiter request (a test asserts this from the module's code tokens). It:

- measures every data source each poll: latency, p95, error and 429 rate, achieved refresh (how often the Jupiter
  price actually changed and how often its `blockId` advanced), plus Solana slot drift via `getSlot`;
- builds 1-minute bars from polled Jupiter prices and evaluates deterministic signals (`basis_reversion` against the
  Binance reference, `breakout_5m`, `dip_reversion_1m`);
- prices every candidate with two size-specific read-only `/swap/v2/order` quotes and computes the round-trip cost
  both as quoted and worst-case (minimum-out on both legs, two network fees);
- rejects unverified or `audit.isSus` tokens, active mint/freeze authorities, thin liquidity, stablecoins, stale
  prices (blockId far behind the slot), excessive impact, and anything below `min_net_edge_bps` after worst-case
  cost and an adverse-selection buffer;
- opens hypothetical positions at the worst-case buy price and settles them at the horizon with a real sell quote,
  so every candidate ends as a win, a false positive, or a rejected candidate that is marked at the horizon to
  count **missed** opportunities;
- probes round-trip cost at $5 / $50 / $100 / $1,000 every ten minutes, which feeds `research_lab/economics.py`.

`tradebot scan-report` summarises all of it. Claude is nowhere in this loop. Design notes, benchmark of the
existing engine, external-system review and the trades-per-day economics are in `research_lab/REALTIME_SCANNER_EVAL.md`.

## Fast engine (paper): scan → detect → cost → risk → buy → manage → sell → record

`tradebot fast` (Windows: `windows\run_fast.bat`) turns the scanner into an autonomous paper trading loop that runs
unattended every 5 seconds and never asks for approval. It is a separate process from the 4h engine with its own
ledger (`data/fast.db`) and it shares the kill switch file (`data/KILL`), so one switch stops both engines.

1. **Scan**: Jupiter Price v3 (with `blockId`), Binance reference price, slot, Tokens v2 facts.
2. **Detect**: 1-minute bars → `basis_reversion`, `breakout_5m`, `dip_reversion_1m` (deterministic, no tuning).
3. **Cost**: two fresh size-specific read-only quotes → worst-case round trip (min-out both legs + network fees) →
   net edge = expected move − worst-case cost − adverse-selection buffer. Below `min_net_edge_bps`: rejected.
4. **Risk**: the same independent `RiskEngine` as the 4h engine (position and exposure limits, daily-loss halt,
   drawdown kill, loss streaks, data age, slippage, trade cap). Rejected reasons are logged per candidate.
5. **Buy**: the broker re-quotes immediately before the order; if the edge is gone at the executable price the
   trade is rejected (`edge_vanished`). Fills come from the `PaperBroker`: a fresh Jupiter quote when a key is
   present, otherwise the parametric model. Intents are persisted before and after the fill.
6. **Manage**: every poll: stop-loss (`fast.stop_pct` below the worst-case entry), take-profit (expected move,
   never below cost plus `min_take_profit_bps`), optional trailing stop, max hold (signal horizon × 2).
7. **Sell**: reduce-only exits through the same risk engine and broker; the kill switch and daily halt flatten.
8. **Record**: fills, trades net of fees, equity curve, per-stage latency (detect → quote → risk → order → fill),
   every rejection with its reason. `tradebot fast-report` prints trades with timestamps and net results, win rate,
   fees, drawdown, trading frequency, latency percentiles and rejection counts.

Event-driven data: `--ws` (or `fast.ws_reference=true`, needs `pip install "tradebot[fast]"`) streams Binance trades
over WebSocket for the reference leg and measures exchange-to-local latency; the Jupiter leg has no public price
stream and stays polled. Claude is not in this loop; research stays on the weekly cadence described above.

Offline replay over real historical 1-minute bars (parametric fills, no second venue, clearly a simulation):

```bash
tradebot fast --replay "data/1m/SOLUSDT_1m_2026-*.csv" --symbols SOL --start 2026-07-01
```

Live money is not reachable from this engine: it refuses to construct with `mode = "live"` and only ever builds a
`PaperBroker`. Its trades do not count toward the 4h engine's live gate.

## Risk controls (defaults in `config.toml`)

| Control | Default | Effect |
|---|---|---|
| `max_position_pct` | 10% of equity | per-position cap; orders are sized down, not just rejected |
| `max_total_exposure_pct` | 50% | sum of positions |
| `max_open_positions` | 5 | |
| `max_order_notional_usd` | $500 | absolute cap per order |
| `max_daily_loss_pct` | 3% | **flatten and no new entries until next UTC day** (`daily_loss_action="kill"` to hard-stop instead) |
| `max_drawdown_pct` | 10% from peak | **kill switch** |
| `max_trades_per_day` | 30 | |
| `loss_cooldown_sec` | 30 min | no new entries after any loss |
| `max_consecutive_losses` / `loss_streak_pause_sec` | 5 / 4 h | after a losing streak, pause entries for 4 h from the last loss |
| `min_liquidity_usd` / `min_volume_24h_usd` | $250k / $500k | memecoin / rug filter; also ≤1% of pool liquidity per order |
| `max_slippage_bps` | 100 | quotes above this are refused |
| `stale_data_sec` | 300 | **kill switch** if data stops |
| `max_consecutive_errors` | 5 | **kill switch** on execution failure streaks |
| `allowlist` / `denylist` | — | symbol gates |

Every order needs an HMAC approval token minted by `RiskEngine.evaluate` for that exact order,
quantity and time (30 s TTL). Brokers verify it before quoting. Risk-reducing exits are always
allowed so the kill switch can flatten.

## Crash protection (execution infrastructure)

Every order is recorded as a durable **intent** in the ledger before anything is signed; the Jupiter `requestId`,
the transaction signature (known locally before broadcast) and the exact signed payload are persisted before the
first submission. A network timeout after broadcast is reported as *outcome unknown*, never retried as a new order
(the same payload is resent, which Jupiter treats idempotently for ~2 minutes). On start and on every cycle the bot
settles pending intents by signature: confirmed on-chain gets booked exactly once from the persisted quote, failed
is closed, not-found after `intent_expiry_sec` (150 s, longer than blockhash validity) is expired. While an intent is
pending, new orders for that symbol are blocked. In live mode the ledger is reconciled against on-chain balances at
startup (`reconcile_on_start`); a shortfall beyond `balance_tolerance_pct` trips the kill switch. Failure-injection
tests: `tests/test_crash_protection.py`.

## Going live (owner action, deliberately multi-step)

The bot cannot click Phantom. It needs its own signing key:

1. In Phantom, **create a new, dedicated account** for the bot. Fund it with USDC you can afford to
   lose plus ~0.05 SOL for fees. You watch its activity in Phantom like any other account.
2. Export that account's private key (Settings → Manage Accounts → account → Show Private Key) and
   put it in `.env` as `SOLANA_PRIVATE_KEY=...`. Never share this key or commit `.env`.
3. Paper trade first. `arm-live` refuses until the paper ledger shows at least
   `min_paper_trades_before_live` closed trades over `min_paper_days_before_live` days
   (defaults 30 trades / 14 days) unless you pass `--force`.
4. Arm with the exact phrase from `[engine].live_confirm_phrase`:
   ```bash
   tradebot arm-live --confirm "I UNDERSTAND THIS TRADES REAL MONEY WITHOUT APPROVAL"
   ```
   This writes `data/LIVE_ARMED` containing a hash of your `[risk]` section. Editing risk limits
   afterwards disarms automatically.
5. Set `mode = "live"` in `config.toml` and `TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK` in the env.
6. `tradebot run`. The live broker re-checks the gate on every single order.

Stop at any time: `tradebot kill`, or `touch data/KILL`, or `TRADEBOT_KILL=1`, or `tradebot disarm-live`.

## Deployment

- `deploy/Dockerfile` + `deploy/docker-compose.yml` (state in `./data`, env in `.env`)
- `deploy/tradebot.service` (systemd, hardened, auto-restart)

Run one instance per ledger. The kill file is shared through the mounted `data/` directory, so
`tradebot kill` from the host stops a containerised bot.

## Testing

```bash
pytest -q
```

Covers the risk engine (every limit, token binding/expiry/forgery, kill-switch exemptions,
daily halt vs. kill), kill switch sources, paper fills, accounting and persistence, backtester
correctness (uptrend profit, bounded stop losses, walk-forward filtering), live-gate refusal
paths, Claude boundary handling with a mocked API, and a full offline paper session.

## Honest caveats

- Synthetic data has no real edge; backtest numbers there only prove plumbing. Use
  `tradebot backtest --optimize` on real history before trusting any parameter set.
- Live fills are recorded from the Jupiter quote (Jupiter enforces `live_slippage_bps` on-chain).
  Reconcile against `JupiterLiveBroker.get_balances()` periodically.
- Public endpoints (Jupiter lite API, Binance, DexScreener, CoinGecko) are rate-limited; a paid
  Jupiter key (`[data].jupiter_api_key`) and a private RPC are recommended for live use.
- This is software that can lose money quickly. Nothing here is financial advice.
