"""Command-line interface. `python -m tradebot --help`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .config import EXAMPLE_CONFIG, Settings, load_settings
from .execution.live_gate import LiveGate
from .portfolio.ledger import Ledger
from .portfolio.reporting import format_report, performance_report
from .risk.kill_switch import KillSwitch

ENV_EXAMPLE = """# Copy to .env and `set -a; source .env; set +a` (or use your process manager's env file).
ANTHROPIC_API_KEY=sk-ant-...
# --- live trading only (leave unset for paper) ---
# Dedicated Phantom bot account private key (Phantom -> Settings -> Manage Accounts -> Show Private Key)
# SOLANA_PRIVATE_KEY=
# Second independent gate; must be exactly this value for any live order to be sent.
# TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK
# Emergency stop from the environment (also: `tradebot kill`, or create data/KILL)
# TRADEBOT_KILL=1
"""


def _setup_logging(level: str, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(data_dir / "tradebot.log")]
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _prices_for_report(settings: Settings, ledger: Ledger) -> dict[str, float]:
    """Live prices when the feed is reachable; otherwise the last prices the bot recorded (always for replay)."""
    stored = ledger.get("last_prices", {}) or {}
    if settings.data.provider != "live":
        return stored
    try:
        from .engine.bot import build_provider
        p = build_provider(settings)
        live = {s: t.price for s, t in p.get_tickers(settings.universe.symbols).items()}
        return {**stored, **live} if live else stored
    except Exception:  # noqa: BLE001
        return stored


def cmd_init(args) -> int:
    cfg = Path(args.config)
    if cfg.exists() and not args.force:
        print(f"{cfg} exists; use --force to overwrite")
    else:
        cfg.write_text(EXAMPLE_CONFIG)
        print(f"wrote {cfg}")
    env = Path(".env.example")
    env.write_text(ENV_EXAMPLE)
    print(f"wrote {env}")
    print("\nNext: edit config.toml, export ANTHROPIC_API_KEY, then `python -m tradebot run` (paper trading).")
    return 0


def cmd_run(args) -> int:
    settings = load_settings(args.config)
    if args.provider:
        settings.data.provider = args.provider
    if args.paper:
        settings.mode = "paper"
    _setup_logging(settings.log_level, settings.data_path)
    from .engine.bot import build_bot
    bot = build_bot(settings)
    if settings.is_live:
        logging.getLogger(__name__).warning("*** LIVE MODE: real funds will be traded without per-trade approval ***")
    bot.run_forever(max_cycles=args.cycles)
    try:
        prices = {s: t.price for s, t in bot.provider.get_tickers(bot.symbols).items()}
    except Exception as e:  # noqa: BLE001 - report with last known prices if the feed is down
        logging.getLogger(__name__).error("market data unavailable for final report: %s", e)
        prices = {}
    print(format_report(performance_report(bot.ledger, prices)))
    errors = [e for e in bot.ledger.events(20) if e["kind"] == "error"]
    if errors:
        print(f"\n{len(errors)} recent errors; last: {errors[0]['message']}  (run `tradebot preflight`)")
    return 0


def cmd_backtest(args) -> int:
    settings = load_settings(args.config)
    if args.provider:
        settings.data.provider = args.provider
    from .engine.bot import build_provider
    from .research.backtester import backtest
    from .research.optimizer import optimize
    from .strategies.registry import STRATEGY_CLASSES, build_strategy
    provider = build_provider(settings)
    symbols = args.symbols.split(",") if args.symbols else settings.universe.symbols
    names = [args.strategy] if args.strategy else list(STRATEGY_CLASSES)
    candles = {}
    for s in symbols:
        try:
            c = provider.full_series(s) if hasattr(provider, "full_series") else provider.get_candles(s, settings.data.candle_interval, args.bars)
            if c:
                candles[s] = c[-args.bars:]
        except Exception as e:  # noqa: BLE001
            print(f"skip {s}: {e}")
    cfg_params = {c.name: c.params for c in settings.strategies}
    for name in names:
        print(f"\n=== {name} ===")
        if args.optimize:
            cands = optimize(name, candles, fee_bps=settings.execution.paper_fee_bps, slippage_bps=settings.execution.paper_slippage_bps,
                             base_params=cfg_params.get(name, {}))
            for c in cands[:args.top]:
                r = c.row()
                flag = "" if c.score > -1e8 else "  (rejected: not profitable OOS)"
                print(f"score {r['score']:>9}  IS {r['is_return_pct']:>7}%  OOS {r['oos_return_pct']:>7}%  OOS sharpe {r['oos_sharpe']:>6}  "
                      f"OOS dd {r['oos_max_dd_pct']:>6}%  trades {r['oos_trades']:>3}  {r['params']}{flag}")
        else:
            strat = build_strategy(name, cfg_params.get(name, {}))
            for s, c in candles.items():
                res = backtest(strat, c, s, fee_bps=settings.execution.paper_fee_bps, slippage_bps=settings.execution.paper_slippage_bps)
                m = res.metrics
                print(f"{s:<6} return {m['total_return_pct']:>8.2f}%  dd {m['max_drawdown_pct']:>6.2f}%  sharpe {m['sharpe']:>6.2f}  "
                      f"trades {m['trades']:>3}  win {m['win_rate_pct']:>5.1f}%  pf {m['profit_factor']:>6.2f}")
    return 0


def cmd_research(args) -> int:
    settings = load_settings(args.config)
    if args.provider:
        settings.data.provider = args.provider
    settings.mode = "paper"
    _setup_logging(settings.log_level, settings.data_path)
    from .engine.bot import build_bot
    bot = build_bot(settings)
    out = bot.research_once()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_report(args) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode=args.mode or settings.mode)
    since = time.time() - args.days * 86400 if args.days else 0.0
    print(format_report(performance_report(ledger, _prices_for_report(settings, ledger), since_ts=since)))
    if args.decisions:
        print("\nRecent decisions:")
        for d in ledger.decisions(limit=args.decisions):
            print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(d['ts']))} {d['kind']:<10} {d['symbol']:<6} {d['strategy']:<15} {d['payload'][:160]}")
    return 0


def cmd_status(args) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode=args.mode or settings.mode)
    kill = KillSwitch(settings.kill_file)
    gate = LiveGate(settings, ledger)
    st = gate.status()
    trades, days = gate.paper_record()
    print(f"mode:          {settings.mode}" + (f"  (showing ledger: {ledger.mode})" if ledger.mode != settings.mode else ""))
    print(f"kill switch:   {'TRIPPED - ' + str(kill.reason()) if kill.is_tripped() else 'clear'}")
    print(f"live gate:     {'ARMED' if st['armed'] else 'not armed'}  (env_ok={st['env_ok']} armed_file={st['armed_file']} limits_match={st['limits_match']})")
    print(f"paper record:  {trades} closed trades over {days:.1f} days "
          f"(need {settings.engine.min_paper_trades_before_live} / {settings.engine.min_paper_days_before_live} to arm)")
    print(f"cash:          ${ledger.get('cash', settings.initial_capital_usd):,.2f}")
    pos = ledger.load_positions()
    print(f"positions:     {len(pos)}" + (" -> " + ", ".join(f"{p.symbol} {p.qty:.6g}@{p.avg_cost:.6g}" for p in pos.values()) if pos else ""))
    for e in ledger.events(5):
        print(f"  event {time.strftime('%Y-%m-%d %H:%M', time.gmtime(e['ts']))} {e['kind']}: {e['message']}")
    return 0


def cmd_kill(args) -> int:
    settings = load_settings(args.config)
    kill = KillSwitch(settings.kill_file, flatten_on_trip=settings.engine.flatten_on_kill)
    kill.trip(args.reason, source="cli")
    Ledger(settings.db_path, mode=settings.mode).log_event("kill_switch", f"cli: {args.reason}")
    print(f"kill switch tripped: {settings.kill_file}. The running bot will stop opening positions"
          f"{' and flatten' if settings.engine.flatten_on_kill else ''} on its next cycle.")
    return 0


def cmd_resume(args) -> int:
    settings = load_settings(args.config)
    kill = KillSwitch(settings.kill_file)
    if not kill.is_tripped():
        print("kill switch is not tripped")
        return 0
    kill.reset()
    Ledger(settings.db_path, mode=settings.mode).log_event("kill_reset", "cli resume")
    print("kill switch reset; the bot resumes trading on its next cycle")
    return 0


def cmd_arm_live(args) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode="paper")
    gate = LiveGate(settings, ledger)
    try:
        gate.arm(args.confirm, force=args.force)
    except PermissionError as e:
        print(f"REFUSED: {e}")
        return 2
    print("Armed. Live orders will be sent only when ALL of these hold:")
    print('  1. config.toml has mode = "live"')
    print("  2. env TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK")
    print(f"  3. {settings.armed_file} exists with the current risk-limits hash (editing [risk] disarms)")
    print("  4. SOLANA_PRIVATE_KEY is set for the dedicated bot wallet")
    return 0


def cmd_disarm_live(args) -> int:
    settings = load_settings(args.config)
    LiveGate(settings).disarm()
    print("disarmed")
    return 0


def cmd_preflight(args) -> int:
    from .preflight import preflight
    settings = load_settings(args.config)
    rows = preflight(settings)
    worst = 0
    for check, status, detail in rows:
        print(f"{status:<5} {check:<22} {detail}")
        worst = max(worst, {"OK": 0, "WARN": 1, "FAIL": 2}[status])
    print("\nresult:", ["all checks passed", "warnings (see above)", "FAILURES: live market data unavailable"][worst])
    return worst


def cmd_quote(args) -> int:
    from .preflight import format_quote, jupiter_quote
    settings = load_settings(args.config)
    try:
        rep = jupiter_quote(settings, args.input, args.output, args.amount, args.slippage_bps, wallet=args.wallet)
    except Exception as e:  # noqa: BLE001
        print(f"quote failed: {type(e).__name__}: {e}")
        return 2
    print(format_quote(rep))
    return 0


def cmd_wallet(args) -> int:
    from .preflight import format_wallet, wallet_balances
    settings = load_settings(args.config)
    try:
        bal = wallet_balances(settings, args.address)
    except Exception as e:  # noqa: BLE001
        print(f"balance lookup failed: {type(e).__name__}: {e}")
        return 2
    print(format_wallet(bal, args.sol_price))
    return 0


def cmd_fetch_data(args) -> int:
    settings = load_settings(args.config)
    from .data.csv_replay import write_candles_csv
    from .data.live import LiveProvider
    p = LiveProvider(settings.data, settings.universe, settings.execution)
    out_dir = settings.data_path / "csv"
    for s in (args.symbols.split(",") if args.symbols else settings.universe.symbols):
        try:
            c = p.get_candles(s, args.interval, args.bars)
            write_candles_csv(out_dir / f"{s}_{args.interval}.csv", c)
            print(f"{s}: {len(c)} bars -> {out_dir / f'{s}_{args.interval}.csv'}")
        except Exception as e:  # noqa: BLE001
            print(f"{s}: failed ({e})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradebot", description="Autonomous Solana trading bot (paper by default)")
    ap.add_argument("--config", default=os.environ.get("TRADEBOT_CONFIG", "config.toml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write config.toml and .env.example"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("run", help="run the bot loop (paper unless armed live)")
    p.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: forever)")
    p.add_argument("--provider", choices=["live", "synthetic", "csv"], default=None)
    p.add_argument("--paper", action="store_true", help="force paper mode regardless of config")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("backtest", help="backtest / optimise strategies")
    p.add_argument("--strategy", default=None); p.add_argument("--symbols", default=None); p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--provider", choices=["live", "synthetic", "csv"], default=None)
    p.add_argument("--optimize", action="store_true", help="walk-forward grid search"); p.add_argument("--top", type=int, default=8)
    p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("research", help="run one research cycle (backtests + Claude proposal) and apply bounded updates")
    p.add_argument("--provider", choices=["live", "synthetic", "csv"], default=None); p.set_defaults(fn=cmd_research)
    p = sub.add_parser("report", help="net profitability report"); p.add_argument("--days", type=float, default=0)
    p.add_argument("--mode", choices=["paper", "live", "replay"], default=None); p.add_argument("--decisions", type=int, default=0); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("status", help="mode, kill switch, live gate, positions")
    p.add_argument("--mode", choices=["paper", "live", "replay"], default=None); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("kill", help="EMERGENCY STOP: halt and flatten"); p.add_argument("--reason", default="manual kill"); p.set_defaults(fn=cmd_kill)
    p = sub.add_parser("resume", help="reset the kill switch after review"); p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("arm-live", help="arm real-money trading (deliberate, owner-only)")
    p.add_argument("--confirm", required=True, help="exact confirmation phrase from [engine].live_confirm_phrase")
    p.add_argument("--force", action="store_true", help="skip the minimum paper-record requirement"); p.set_defaults(fn=cmd_arm_live)
    p = sub.add_parser("disarm-live", help="remove the armed file"); p.set_defaults(fn=cmd_disarm_live)
    p = sub.add_parser("preflight", help="read-only checks: network reachability, API key, kill switch, live gate"); p.set_defaults(fn=cmd_preflight)
    p = sub.add_parser("quote", help="read-only Jupiter quote with fee/rent/slippage breakdown (signs nothing)")
    p.add_argument("--input", default="SOL"); p.add_argument("--output", default="USDC"); p.add_argument("--amount", type=float, required=True)
    p.add_argument("--slippage-bps", type=int, default=None); p.add_argument("--wallet", default=None, help="public key to check affordability")
    p.set_defaults(fn=cmd_quote)
    p = sub.add_parser("wallet", help="read-only balances for a public key"); p.add_argument("--address", required=True)
    p.add_argument("--sol-price", type=float, default=None); p.set_defaults(fn=cmd_wallet)
    p = sub.add_parser("fetch-data", help="download candles to CSV for offline backtests/replay")
    p.add_argument("--symbols", default=None); p.add_argument("--interval", default="1h"); p.add_argument("--bars", type=int, default=1000)
    p.set_defaults(fn=cmd_fetch_data)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
