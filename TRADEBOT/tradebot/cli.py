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
    from .instance_lock import AlreadyRunningError, lock_for
    try:
        lock = lock_for(settings.db_path).acquire()          # one engine per ledger: no duplicate orders from a 2nd process
    except AlreadyRunningError as e:
        print(f"refusing to start: {e}")
        return 3
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
    lock.release()
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
    pend = ledger.pending_intents()
    print(f"pending intents: {len(pend)}" + ("  <- unresolved orders; the bot reconciles these before trading" if pend else ""))
    for i in ledger.intents(5):
        print(f"  intent {time.strftime('%Y-%m-%d %H:%M', time.gmtime(i['ts']))} {i['symbol']} {i['side']} {i['state']} sig={(i['tx_signature'] or '')[:12]}")
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


def cmd_selftest(args) -> int:
    from .selftest import format_checks, run_selftest
    logging.disable(logging.CRITICAL)
    checks = run_selftest(cycles=args.cycles)
    print(format_checks(checks))
    return 0 if all(c.ok for c in checks) else 1


def cmd_verify_assets(args) -> int:
    from .preflight import format_assets, verify_assets
    settings = load_settings(args.config)
    rows = verify_assets(settings, args.symbols.split(",") if args.symbols else None, args.equity)
    print(format_assets(rows))
    return 0 if all(r["status"] == "OK" for r in rows) else (1 if any(r["status"] == "FAIL" for r in rows) else 0)


def cmd_scan(args) -> int:
    """Read-only real-time opportunity scanner. Own process, own database (data/scanner.db); never trades."""
    from .scanner.scanner import LiveFeed, OpportunityScanner
    from .scanner.store import ScannerStore
    settings = load_settings(args.config)
    _setup_logging(settings.log_level, Path(settings.data_dir))
    if args.poll:
        settings.scanner.poll_sec = args.poll
    if args.size:
        settings.scanner.size_usd = args.size
    if args.discover:
        settings.scanner.discover_category = args.discover
    try:
        feed = LiveFeed(settings)
    except RuntimeError as e:
        print(e); return 2
    store = ScannerStore(Path(settings.data_dir) / "scanner.db")
    OpportunityScanner(settings, store, feed).run(minutes=args.minutes)
    print(scan_report_text(settings, store, args.minutes / 60 if args.minutes else 24.0))
    return 0


def scan_report_text(settings: Settings, store, hours: float) -> str:
    from .scanner.scanner import scan_report
    return scan_report(store, since_hours=hours, min_edge_bps=settings.scanner.min_net_edge_bps)


def cmd_scan_report(args) -> int:
    from .scanner.store import ScannerStore
    settings = load_settings(args.config)
    path = Path(settings.data_dir) / "scanner.db"
    if not path.exists():
        print(f"no scanner database at {path}; run `tradebot scan` first"); return 1
    print(scan_report_text(settings, ScannerStore(path), args.hours))
    return 0


def _fast_components(settings: Settings):
    from .fast.engine import FastEngine
    from .portfolio.ledger import Ledger
    from .risk.kill_switch import KillSwitch
    from .scanner.store import ScannerStore
    data = Path(settings.data_dir); data.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(data / "fast.db", mode="paper")
    store = ScannerStore(data / "scanner.db")
    kill = KillSwitch(data / "KILL")                 # shared with the 4h engine: one switch stops both
    return FastEngine, ledger, store, kill


def cmd_fast(args) -> int:
    """Fast engine: scanner detection -> risk engine -> PAPER broker -> position management, own ledger data/fast.db."""
    settings = load_settings(args.config)
    _setup_logging(settings.log_level, Path(settings.data_dir))
    if settings.mode == "live" or os.environ.get("TRADEBOT_LIVE_TRADING"):
        print("fast engine is paper-only; refusing to start with live settings in place"); return 2
    from .instance_lock import AlreadyRunningError, lock_for
    try:
        _fast_lock = lock_for(Path(settings.data_dir) / "fast.db").acquire()   # held for the life of this process
    except AlreadyRunningError as e:
        print(f"refusing to start: {e}")
        return 3
    FastEngine, ledger, store, kill = _fast_components(settings)
    if args.poll:
        settings.scanner.poll_sec = args.poll
    if args.size:
        settings.fast.size_usd = args.size
    if args.min_edge is not None:
        settings.scanner.min_net_edge_bps = args.min_edge
    if args.replay:
        from .fast.replay import ReplayFeed, load_bars
        syms = (args.symbols or ",".join(settings.universe.symbols)).split(",")
        series = {}
        for sym, pattern in zip(syms, args.replay):
            bars = load_bars([pattern])
            if not bars:
                print(f"no bars for {sym} from {pattern}"); return 1
            series[sym] = bars
        start_ts = time.mktime(time.strptime(args.start, "%Y-%m-%d")) - time.timezone if args.start else None
        feed = ReplayFeed(series, {s: settings.universe.mints[s] for s in syms}, settings.universe.decimals, start_ts=start_ts)
        settings.scanner.poll_sec = 60.0; settings.scanner.probe_interval_sec = 1e12; settings.scanner.check_slot = False
        settings.universe.symbols = syms
        eng = FastEngine(settings, store, feed, ledger, kill, clock=feed.now)
        eng.ledger.log_event("start", f"replay of real 1m bars, parametric fills: {','.join(syms)}", ts=feed.now())
        n = 0
        while not feed.finished() and (args.max_bars is None or n < args.max_bars):
            eng.poll(); feed.advance(); n += 1
        eng.ledger.log_event("stop", f"replay: {n} bars", ts=feed.now())
        print(fast_report_text(settings, ledger, store, 0))
        return 0
    from .scanner.scanner import LiveFeed
    try:
        feed = LiveFeed(settings)
    except RuntimeError as e:
        print(e); return 2
    if settings.fast.ws_reference or args.ws:
        from .fast.ws import BinanceTradeWS, WSReferenceFeed
        pairs = [settings.universe.candle_symbols[s] for s in settings.universe.symbols if s in settings.universe.candle_symbols]
        ws = BinanceTradeWS(pairs)
        if ws.start():
            feed = WSReferenceFeed(feed, ws)
            settings.fast.ws_reference = True
    eng = FastEngine(settings, store, feed, ledger, kill)
    try:
        eng.run(minutes=args.minutes)
    finally:
        if settings.fast.ws_reference and hasattr(feed, "ws"):
            print("websocket:", feed.ws.stats()); feed.ws.stop()
    print(fast_report_text(settings, ledger, store, args.minutes / 60 if args.minutes else 24.0))
    return 0


def fast_report_text(settings: Settings, ledger, store, hours: float) -> str:
    from .fast.engine import fast_report
    return fast_report(ledger, store, since_hours=hours, min_edge_bps=settings.scanner.min_net_edge_bps)


def cmd_fast_report(args) -> int:
    settings = load_settings(args.config)
    data = Path(settings.data_dir)
    if not (data / "fast.db").exists():
        print(f"no fast ledger at {data / 'fast.db'}; run `tradebot fast` first"); return 1
    from .portfolio.ledger import Ledger
    from .scanner.store import ScannerStore
    print(fast_report_text(settings, Ledger(data / "fast.db", mode="paper"), ScannerStore(data / "scanner.db"), args.hours))
    return 0


def cmd_apply_profile(args) -> int:
    """Edit the EXISTING config.toml in place (backup first): Claude off, BTC/ETH/SOL, no auto-scan, 4h, tsmom+momentum."""
    from .profile import apply_validated_profile
    from pathlib import Path
    try:
        changes = apply_validated_profile(Path(args.config), backup=not args.no_backup)
    except FileNotFoundError as e:
        print(e)
        return 2
    print(f"applied validated profile to {args.config}" + ("" if args.no_backup else " (backup written alongside)"))
    for c in changes:
        print("  -", c)
    if not changes:
        print("  (already up to date)")
    s = load_settings(args.config)
    print(f"now: mode={s.mode} claude={s.claude.enabled} symbols={s.universe.symbols} auto_scan={s.universe.auto_scan} "
          f"interval={s.data.candle_interval} strategies={[c.name for c in s.strategies if c.enabled]}")
    return 0


def cmd_resolve_intent(args) -> int:
    """Manually settle an UNKNOWN intent after verifying the transaction yourself (explorer / wallet).
    'confirmed' books the fill from the persisted quote; 'expired'/'failed' closes it without a fill."""
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode=settings.mode)
    try:
        row = ledger.resolve_intent_manually(args.intent_id, args.state, args.note)
    except (KeyError, ValueError) as e:
        print(f"refused: {e}")
        return 2
    if args.state == "confirmed":
        from .models import Fill, Side
        from .portfolio.accounting import PortfolioBook
        book = PortfolioBook(ledger, settings.initial_capital_usd, time.time)
        qty, notional = float(row["qty"]), float(row["notional_usd"])
        fill = Fill(order_id=row["order_id"], symbol=row["symbol"], side=Side(row["side"]), qty=qty, price=notional / qty if qty else 0.0,
                    notional_usd=notional, fee_usd=0.0, tx_signature=row["tx_signature"])
        book.apply_fill(fill, exit_reason="manual_reconcile" if row["side"] == "sell" else "")
        print(f"booked {row['side']} {row['symbol']} qty {qty:.6g} from the persisted quote")
    print(f"intent {args.intent_id} -> {args.state} (logged)")
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
    p = sub.add_parser("selftest", help="offline execution self-test: risk approvals, fills, limits, kill switch")
    p.add_argument("--cycles", type=int, default=700); p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("verify-assets", help="read-only: can each universe symbol be executed via Jupiter at the bot's order size within limits?")
    p.add_argument("--symbols", default=None); p.add_argument("--equity", type=float, default=None); p.set_defaults(fn=cmd_verify_assets)
    p = sub.add_parser("scan", help="READ-ONLY real-time opportunity scanner (paper): latency, 1m signals, quoted round-trip costs, hypothetical outcomes")
    p.add_argument("--minutes", type=float, default=None, help="stop after N minutes (default: run until Ctrl+C)")
    p.add_argument("--poll", type=float, default=None); p.add_argument("--size", type=float, default=None, help="hypothetical size in USD")
    p.add_argument("--discover", default=None, help="widen universe from Jupiter Tokens v2, e.g. toptraded")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("fast", help="PAPER fast engine: scan -> detect -> cost -> risk -> buy -> manage -> sell -> record (own ledger data/fast.db)")
    p.add_argument("--minutes", type=float, default=None); p.add_argument("--poll", type=float, default=None)
    p.add_argument("--size", type=float, default=None); p.add_argument("--min-edge", type=float, default=None, help="min net edge bps after worst-case cost")
    p.add_argument("--ws", action="store_true", help="Binance WebSocket reference prices (needs websockets package)")
    p.add_argument("--replay", nargs="+", default=None, help="offline: one 1m CSV/zip glob per symbol (real historical bars, parametric fills)")
    p.add_argument("--symbols", default=None, help="with --replay: symbols matching the globs, e.g. SOL,BTC")
    p.add_argument("--start", default=None); p.add_argument("--max-bars", type=int, default=None)
    p.set_defaults(fn=cmd_fast)
    p = sub.add_parser("fast-report", help="fast engine: trades with timestamps and net results, win rate, fees, drawdown, latency, rejections")
    p.add_argument("--hours", type=float, default=24.0); p.set_defaults(fn=cmd_fast_report)
    p = sub.add_parser("scan-report", help="summarise the scanner database: sources, refresh, costs, candidates, false positives, missed")
    p.add_argument("--hours", type=float, default=24.0); p.set_defaults(fn=cmd_scan_report)
    p = sub.add_parser("apply-profile", help="update an existing config.toml to the validated settings, preserving everything else (backup first)")
    p.add_argument("--no-backup", action="store_true"); p.set_defaults(fn=cmd_apply_profile)
    p = sub.add_parser("resolve-intent", help="manually settle an UNKNOWN order intent after verifying it yourself")
    p.add_argument("intent_id"); p.add_argument("state", choices=["expired", "failed", "confirmed"]); p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_resolve_intent)
    p = sub.add_parser("fetch-data", help="download candles to CSV for offline backtests/replay")
    p.add_argument("--symbols", default=None); p.add_argument("--interval", default="1h"); p.add_argument("--bars", type=int, default=1000)
    p.set_defaults(fn=cmd_fetch_data)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
