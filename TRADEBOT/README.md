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

## Jupiter integration (current API)

| Purpose | Endpoint | Notes |
|---|---|---|
| Spot prices, 24h change | `GET {base}/price/v3?ids=<mints>` | Price v2 is deprecated and no longer used |
| Liquidity, volume, verification, decimals | `GET {base}/tokens/v2/search?query=<mints>` | Tokens v1 is deprecated; DexScreener is only a fallback |
| Universe scan | `GET {base}/tokens/v2/tag?query=verified` | verified, organic score ≥ 50, stables excluded |
| Quote / swap (engine `swap`) | `GET {base}/swap/v1/quote`, `POST {base}/swap/v1/swap` | `priorityLevelWithMaxLamports`, `dynamicComputeUnitLimit`, simulation errors abort before signing, expiry via `lastValidBlockHeight` |
| Order / execute (engine `ultra`) | `GET {base}/ultra/v1/order?taker=…`, `POST {base}/ultra/v1/execute` | Jupiter broadcasts; no RPC needed |
| Wallet balances | `GET {base}/ultra/v1/balances/{pubkey}` | RPC fallback |

`{base}` is `https://lite-api.jup.ag` (free tier, no key) or `https://api.jup.ag` when `[data].jupiter_api_key`
or env `JUPITER_API_KEY` is set (header `x-api-key`). Set `[execution].engine = "ultra"` to let Jupiter
handle broadcasting; the default `swap` engine sends through your `rpc_url`.

> The build sandbox had no route to any Jupiter, Solana or exchange host, so these integrations were
> verified against the documented response shapes with mocked HTTP (`tests/test_jupiter_api.py`), not
> against the live endpoints. Run `tradebot preflight` on a machine with internet access first.

## Validated strategy (read `research_lab/RESULTS.md` before changing it)

The default configuration is the outcome of an independent research pass over real Binance history
(BTC 2019-2026, ETH 2019-2025, SOL 2022-2025) with pessimistic execution, ~0.5% round-trip costs, rolling
walk-forward validation, lookahead/cost/delay checks and a random-entry control:

- **Primary: `tsmom`**: daily-evaluated 30-day time-series momentum with a 100-day EMA regime filter and a 25%
  disaster stop, on 4h candles. Out of sample it was profitable on BTC, ETH and both SOL blocks, beat buy-and-hold
  BTC on return (+1132% vs +1065%) with half the drawdown (-39% vs -77%), and survived doubled costs and one-bar-late
  fills. Win rate is about 33%; the average win is about 6x the average loss. Only ~45% of 3-month windows are
  positive, so expect long flat stretches.
- **Secondary: `momentum`**: EMA 12/26 cross above EMA 100 with a 2.5-ATR stop and no take-profit, 4h.
- **Rejected:** every "90-95% win rate" strategy we could reproduce (Freqtrade ROI-table strategies, BB/RSI scalps
  without stops, martingale/DCA). They win 60-100% of trades and still lose money after costs or hide catastrophic
  tail risk. 1h variants of all trend systems also lose after costs.

`research_lab/` holds the data loader (six cross-checked sources), the engine, 19 candidates, walk-forward, bias
checks and the report generator. It needs `pip install -e ".[research]"` (NumPy/pandas) and is not imported by the bot.

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
down or refuses, the bot continues on deterministic strategies (`fail_open=true`) and the research
loop falls back to a conservative rule (adopt a candidate only if it beats current parameters
out-of-sample by more than 2 points). Estimated API spend is tracked and subtracted in net profit.

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
