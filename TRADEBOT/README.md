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
| Strategies | `tradebot/strategies/` | Momentum (EMA cross + trend filter + ATR stop), mean reversion (RSI + Bollinger re-entry), breakout (Donchian + volume) |
| Research | `tradebot/research/` | Event-driven backtester (next-open fills, intrabar stops, fees, slippage), metrics, walk-forward grid optimizer, Claude analyst |
| Risk | `tradebot/risk/engine.py` | Position/exposure/order caps, daily-loss halt, drawdown kill, trade-rate cap, loss streak cooldown, liquidity/volume/slippage floors, allow/deny lists, stale-data and error-streak kills, HMAC approval tokens |
| Kill switch | `tradebot/risk/kill_switch.py` | `data/KILL` file, `TRADEBOT_KILL=1`, `tradebot kill`, or auto; flattens to USDC; manual reset only |
| Execution | `tradebot/execution/` | `PaperBroker`; `JupiterLiveBroker` (quote → swap → local sign with `solders` → send → confirm); `LiveGate`; wallet loading |
| Portfolio | `tradebot/portfolio/` | SQLite ledger (orders, fills, positions, trades, equity, decisions, events, API cost), accounting, reports |
| Engine | `tradebot/engine/bot.py` | 24/7 loop, exits, entries, research cadence, daily reports, flatten-on-kill |
| CLI | `tradebot/cli.py` | `init run backtest research report status kill resume arm-live disarm-live fetch-data` |

## Quick start (paper trading)

```bash
cd TRADEBOT
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
tradebot init                      # writes config.toml and .env.example
cp .env.example .env               # add ANTHROPIC_API_KEY
set -a; source .env; set +a
tradebot run                       # paper trading on live market data, forever
```

Read-only diagnostics (nothing is signed or sent):

```bash
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
