# Autonomous Solana Trading Bot (Claude + Phantom-compatible wallet) — Design

Date: 2026-11-01. Status: implemented in `TRADEBOT/`. Paper trading is the default and only
mode that works without explicit, multi-step arming by the owner.

## Goal

A 24/7 bot that scans Solana markets, researches and backtests strategies, generates signals,
executes buys/sells, manages open positions, and tracks net profitability, with an independent
risk engine, hard loss limits, and an emergency kill switch. Once live trading is explicitly
armed, individual trades need no manual approval.

## Decisions and assumptions (made autonomously, flagged for review)

1. **Chain and venue.** Phantom is a Solana wallet, so the bot trades Solana SPL tokens via the
   Jupiter aggregator (best-route swaps against USDC). No CEX account is required.
2. **How "Phantom" fits.** Phantom is a browser extension that signs only when a human clicks.
   A fully autonomous bot must hold its own signing key. The intended setup is a *dedicated bot
   account* created in Phantom, funded only with risk capital, whose private key is exported
   from Phantom and given to the bot via environment variable. The owner watches balances and
   trades in Phantom. Live execution signs Jupiter swap transactions locally with that key.
3. **Language.** Python 3.11+ (3.13 in this environment). Dependencies: `anthropic`, `httpx`,
   `pydantic`, `numpy`, `pandas`, `solders`, `base58`, `pyyaml`.
4. **Market data.** Live: Jupiter price API (spot), Binance public klines (OHLCV history for
   majors), DexScreener (liquidity/volume per mint), CoinGecko (fallback OHLC). Offline:
   deterministic synthetic provider and CSV replay, used by tests and for paper runs in
   sandboxes without market access.
5. **Claude's role is advisory and reduce-only.** Claude (model `claude-opus-5-5`, adaptive
   thinking, structured outputs) proposes strategy/parameter candidates and token picks for the
   research loop, reviews backtest tables, optionally vets each signal (may veto or shrink size,
   never enlarge, never create orders), and writes daily narratives. Claude cannot modify risk
   limits, the kill switch, or the live gate. If the API is unavailable, trading continues on
   deterministic strategies alone.
6. **Risk engine independence.** `RiskEngine` is a separate module with its own state. Every
   broker (paper and live) refuses an order without an HMAC approval token minted by the risk
   engine for that exact order. Exits that reduce risk are always allowed unless the kill switch
   is in "halt" mode.
7. **Kill switch.** Tripped by: a file (`data/KILL`), env `TRADEBOT_KILL=1`, CLI `tradebot kill`,
   or automatically on daily-loss breach, max-drawdown breach, stale data, repeated execution
   failures, or ledger/wallet mismatch. Default behaviour on trip: flatten all positions to USDC
   and stop. Reset is manual only (`tradebot resume`).
8. **Live gate.** Live execution requires all of: `mode = "live"` in config, env
   `TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK`, and an armed file written by
   `tradebot arm-live --confirm "..."`, which records a hash of the risk limits (changing limits
   disarms) and refuses unless paper history meets minimum trade/day thresholds (or `--force`).
9. **Persistence.** SQLite ledger: orders, fills, positions, closed trades, equity curve,
   decisions (strategy/Claude/risk), events, API cost. Net profitability = realized + unrealized
   P&L − fees − estimated Claude API spend.

## Architecture

```
market data ──> universe scan ──> strategies ──> signals
                                   │                │
            research loop (backtest + Claude) ──────┘ (params/universe updates)
                                                    │
                                 Claude vetting (veto / size-down only)
                                                    │
                                 RiskEngine.evaluate → approval token
                                                    │
                                 Broker (Paper | JupiterLive) → fills
                                                    │
                      Ledger (SQLite) ← PositionManager (stops, TP, trailing, time)
                                                    │
                                 KillSwitch monitors equity, data, errors
```

Modules: `tradebot/config.py`, `models.py`, `data/*`, `strategies/*`, `research/*`, `risk/*`,
`execution/*`, `portfolio/*`, `engine/*`, `cli.py`.

## Strategies shipped

- Momentum: EMA fast/slow crossover with trend filter, ATR stop.
- Mean reversion: RSI + Bollinger band re-entry.
- Breakout: Donchian channel breakout with volume confirmation.

All parameterised; the optimizer grid-searches with walk-forward (train/test) validation and
rejects candidates whose out-of-sample result is negative.

## Error handling

Data errors → skip cycle, mark stale, trip kill switch after `stale_data_sec`. Execution errors →
retry bounded, then count toward `max_consecutive_errors` → kill. Claude errors → fail-open to
deterministic path, logged. Any exception in the main loop is logged and the loop continues;
the kill switch state is re-read every cycle.

## Testing

Pytest suite covers risk limits and token verification, kill-switch trip/reset and auto-trip,
paper broker fills/fees/slippage, backtester correctness on synthetic data, accounting P&L,
live gate refusal paths, and strategy signal generation. A full offline paper session runs in CI.

## Deployment

`python -m tradebot run` (paper by default). Dockerfile and systemd unit in `TRADEBOT/deploy/`.
Live arming is a deliberate, owner-only action documented in `TRADEBOT/README.md`.
