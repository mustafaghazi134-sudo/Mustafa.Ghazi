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
# The trading process NEVER holds the wallet's private key. The key lives only in the separate signer service
# (python -m tradebot_signer, run as its own OS user); see docs/LIVE_EXECUTION_ADAPTER.md.
# Client secret copied from the signer home (hex), if not using [live] signer_secret_file:
# TRADEBOT_SIGNER_SECRET=
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
    res = gate.evaluate_readiness()
    print(f"mode:          {settings.mode}" + (f"  (showing ledger: {ledger.mode})" if ledger.mode != settings.mode else ""))
    print(f"kill switch:   {'TRIPPED - ' + str(kill.reason()) if kill.is_tripped() else 'clear'}")
    print(f"live gate:     {'ARMED' if st['armed'] else 'not armed'}  (env_ok={st['env_ok']} armed_file={st['armed_file']} record_valid={st['record_valid']})")
    if st["armed_file"]:
        print("  activation:  " + ("valid, " + f"{(st['seconds_remaining'] or 0) / 3600:.1f} h remaining, authenticated={st['authenticated']}"
                                   if st["record_valid"] else "INVALID - " + "; ".join(st["problems"])))
    e = settings.engine
    print(f"paper record:  {res.trades} closed trades over {res.days:.1f} days, covered {res.coverage:.0%} of that time by gap-free snapshots "
          f"(need {e.min_paper_trades_before_live} / {e.min_paper_days_before_live:g} days / {e.min_paper_coverage_before_live:.0%} to arm); "
          f"{res.operating.outage_count} outage(s), longest {res.operating.longest_gap_sec / 3600:.1f} h")
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


def _interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _print_readiness(res) -> None:
    op = res.operating
    print(f"  evidence source:   {res.source}")
    print(f"  closed trades:     {res.trades}")
    print(f"  span:              {res.days:.1f} days   covered by gap-free snapshots: {res.coverage:.0%}")
    print(f"  outages:           {op.outage_count} (longest {op.longest_gap_sec / 3600:.1f} h), snapshots rejected: {op.snapshots_rejected}")
    sr = res.strategy
    print("  strategy evidence (REPORT ONLY, nothing here is a profitability test):")
    print(f"    net P&L ${sr['net_pnl_usd']:+.2f}, mean/trade {sr['mean_pnl_usd']}, 95% bootstrap CI {sr['ci95_mean_pnl_usd']}, "
          f"max drawdown {sr['max_drawdown_pct']}%")
    print(f"    out-of-sample: {sr['out_of_sample']}")


def cmd_arm_live(args) -> int:
    """Interactive only: a terminal is required, readiness is shown, and a one-time challenge must be typed back.
    There is no override flag."""
    if not _interactive():
        print("REFUSED: arm-live needs an interactive terminal (stdin and stdout must be a TTY). It cannot be scripted or piped.")
        return 2
    settings = load_settings(args.config)
    gate = LiveGate(settings, Ledger(settings.db_path, mode="paper"))
    verdict = None
    try:
        if args.evidence:
            from .execution.evidence_import import verify_bundle
            verdict = verify_bundle(json.loads(Path(args.evidence).read_text()), settings, expected_sha256=args.evidence_sha256)
        ch = gate.begin_arm(args.ttl_hours, verdict)
        print("Readiness (existing thresholds, gap-based):")
        _print_readiness(gate.evaluate_readiness(ch.evidence))
        print(f"\nThis will arm LIVE trading for {ch.ttl_hours:g} h, bound to config digest {ch.digest[:12]}.")
        print("Real submission also needs the build switch and the separate signer; this does not enable them.")
        typed = input(f"To confirm, type exactly:  {ch.code}\n> ")
        gate.complete_arm(ch, typed)
    except (PermissionError, ValueError, OSError, EOFError) as e:
        print(f"REFUSED: {e}")
        return 2
    st = gate.status()
    print(f"Armed until {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(st['expires_at']))}. Live orders are sent only when ALL hold:")
    print('  1. config.toml has mode = "live"')
    print("  2. env TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK")
    print(f"  3. {settings.armed_file} is valid, unexpired and matches the current configuration (editing it disarms)")
    print("  4. the signer service is running, activated by the owner, and the build allows submission")
    print(f"  authenticated by an independent attestor: {st['authenticated']}"
          + ("" if st["authenticated"] else "  (no independent verifier is configured: a process with write access to this file could forge it)"))
    print("  Operational readiness is NOT evidence of profitability; unattended LIVE use also needs a separately approved strategy-evidence gate.")
    a = gate.audit.anchor()
    print(f"audit anchor: seq {a['seq']} head {a['hash']}\n  (keep this somewhere you alone control; `audit-verify --anchor-seq N --anchor-hash H` then detects a rewritten log)")
    return 0


def cmd_disarm_live(args) -> int:
    settings = load_settings(args.config)
    gate = LiveGate(settings)
    gate.disarm("cli disarm-live")
    a = gate.audit.anchor()
    print(f"disarmed\naudit anchor: seq {a['seq']} head {a['hash']}")
    return 0


def cmd_readiness(args) -> int:
    """Read-only: the operational check with the existing thresholds, plus the report-only strategy statistics."""
    settings = load_settings(args.config)
    gate = LiveGate(settings, Ledger(settings.db_path, mode="paper"))
    res = gate.evaluate_readiness()
    print("Operational readiness (existing thresholds, gap-based):", "MET" if res.ok else "NOT MET")
    for r in res.reasons:
        print("  -", r)
    _print_readiness(res)
    return 0 if res.ok else 1


def cmd_export_evidence(args) -> int:
    """Read-only on the ledger. Writes a bundle for off-machine review; prints the hash to record out-of-band."""
    from .execution.evidence_import import export_bundle
    settings = load_settings(args.config)
    prov = dict(kv.split("=", 1) for kv in args.provenance)
    bundle = export_bundle(Ledger(settings.db_path, mode="paper"), settings, prov)
    Path(args.out).write_text(json.dumps(bundle, sort_keys=True))
    print(f"wrote {args.out}\ncontent_sha256 {bundle['content_sha256']}")
    return 0


def cmd_verify_evidence(args) -> int:
    from .execution.evidence_import import verify_bundle
    settings = load_settings(args.config)
    v = verify_bundle(json.loads(Path(args.file).read_text()), settings, expected_sha256=args.expected_sha256)
    for name, ok, detail in v.checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name:<30} {detail if not ok else ''}")
    print("accepted for arming:", v.accepted, "| provenance authenticated:", v.authenticated)
    if v.evidence is not None:
        from .execution.readiness import evaluate
        res = evaluate(settings, v.evidence)
        print("(informational) operational readiness of this evidence:", "MET" if res.ok else "NOT MET")
        _print_readiness(res)
    return 0 if v.accepted else 1


def cmd_audit_verify(args) -> int:
    from .execution.audit_log import HashChainedAuditLog
    settings = load_settings(args.config)
    log = HashChainedAuditLog(settings.audit_log_file)
    anchor = {"seq": args.anchor_seq, "hash": args.anchor_hash} if args.anchor_hash is not None else None
    res = log.verify(anchor)
    print(f"entries {res['entries']}  head {res['head']}  chain {'OK' if res['ok'] else 'BROKEN: ' + str(res['error'])}")
    print("anchored to an externally preserved head:", res["anchored"],
          "" if res["anchored"] else "(without an anchor a whole-file rewrite is undetectable; record `seq`/`head` somewhere you alone control)")
    return 0 if res["ok"] else 1


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
        eng.ledger.log_event("start", f"replay of real 1m bars, quote-model fills: {','.join(syms)}", ts=feed.now())
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
        ws = BinanceTradeWS(pairs)                     # pings every 20 s, reconnects with backoff, stall timeout 30 s
        if ws.start():
            feed = WSReferenceFeed(feed, ws, max_age_sec=settings.fast_risk.max_reference_age_sec)
            settings.fast.ws_reference = True
    from .fast.ratelimit import RateLimiter
    eng = FastEngine(settings, store, feed, ledger, kill, limiter=RateLimiter())   # per-API budgets + backoff
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


def cmd_fast_halt(args) -> int:
    """Emergency halt of the FAST engine only (file data/FAST_HALT): it opens nothing and flattens what it holds."""
    from .fast.telemetry import FastHalt
    settings = load_settings(args.config)
    h = FastHalt(Path(settings.data_dir) / "FAST_HALT")
    h.trip(args.reason, source="cli")
    print(f"fast engine halted: {h.path}. It flattens open fast positions on its next poll. The 4h engine is not affected "
          f"(use `tradebot kill` to stop both).")
    return 0


def cmd_fast_resume(args) -> int:
    from .fast.telemetry import FastHalt
    settings = load_settings(args.config)
    h = FastHalt(Path(settings.data_dir) / "FAST_HALT")
    if not h.own_tripped():
        print("fast engine is not halted"); return 0
    h.reset()
    print("fast halt cleared; the fast engine resumes on its next poll (the shared kill switch, if tripped, still applies)")
    return 0


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



# ---------------------------------------------------------------------- live adapter (read-only / dry-run / halt)
def _live_parts(settings: Settings, with_controls: bool = True):
    from .data.live import LiveProvider
    from .execution.jupiter_adapter import JupiterExecutionAdapter
    from .execution.live_controls import LiveControls
    from .execution.signer_protocol import SignerClient, load_client_secret
    from .risk.engine import RiskEngine
    provider = LiveProvider(settings.data, settings.universe, settings.execution)
    ledger = Ledger(settings.db_path, mode="live")
    kill = KillSwitch(settings.kill_file)
    controls = LiveControls(settings.live, ledger, kill, settings.live_halt_file) if with_controls else None
    signer = None
    try:
        signer = SignerClient(settings.live.signer_url, load_client_secret(settings.live.signer_secret_file))
    except Exception:  # noqa: BLE001 - preflight reports it
        signer = None
    adapter = JupiterExecutionAdapter(RiskEngine(settings.risk, kill), settings, provider, controls=controls, signer=None, dry_run=True)
    return adapter, signer, ledger


def cmd_live_preflight(args) -> int:
    from .execution.live_preflight import live_preflight
    settings = load_settings(args.config)
    adapter = signer = None
    try:
        adapter, signer, _ = _live_parts(settings)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  adapter                {type(e).__name__}: {e}")
    worst = 0
    for check, status, detail in live_preflight(settings, adapter, signer):
        print(f"{status:<5} {check:<26} {detail}")
        worst = max(worst, {"OK": 0, "WARN": 1, "FAIL": 2}[status])
    print("\nread-only: nothing was signed or submitted. result:", ["all checks passed", "warnings", "FAILURES"][worst])
    return 0 if worst < 2 else 1


def cmd_live_dry_run(args) -> int:
    from .models import Side
    settings = load_settings(args.config)
    if args.wallet:
        settings.live.wallet_pubkey = args.wallet
    adapter, _, _ = _live_parts(settings)
    rep = adapter.dry_run_order(args.symbol, Side(args.side), args.amount)
    print(json.dumps(rep, indent=1, default=str))
    print("\nDRY RUN " + ("PASSED: built, verified and simulated" if rep.get("ok") else "REFUSED (see problems)")
          + ". No signature was requested and nothing was submitted.")
    return 0 if rep.get("ok") else 1


def cmd_live_status(args) -> int:
    from .execution.submission import submission_enabled
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode="live")
    st = LiveGate(settings).status()
    halt = settings.live_halt_file
    print(f"build submission switch: {'ENABLED' if submission_enabled() else 'disabled'}")
    print(f"live gate:  armed={st['armed']} mode={settings.mode} env_ok={st['env_ok']} armed_file={st['armed_file']} limits_match={st['limits_match']}")
    print(f"live halt:  {halt.read_text() if halt.exists() else 'clear'}")
    print(f"kill:       {'TRIPPED' if settings.kill_file.exists() else 'clear'}")
    pend = ledger.pending_intents()
    print(f"unresolved live intents: {len(pend)}")
    for r in pend:
        print(f"  {r['id']} {r['symbol']} {r['side']} {r['state']} sig={(r['tx_signature'] or '-')} age={time.time() - r['ts']:.0f}s")
    for r in ledger.intents(10):
        print(f"  intent {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts']))} {r['symbol']} {r['side']} {r['state']} sig={(r['tx_signature'] or '')[:16]}")
    return 0


def cmd_live_halt(args) -> int:
    from .execution.live_controls import LiveControls
    settings = load_settings(args.config)
    LiveControls(settings.live, Ledger(settings.db_path, mode="live"), KillSwitch(settings.kill_file), settings.live_halt_file).halt(args.reason, source="cli")
    print(f"LIVE HALT written ({settings.live_halt_file}): every live order is refused. Also halt the signer: "
          "python -m tradebot_signer halt --home <signer home>")
    return 0


def cmd_live_resume(args) -> int:
    from .execution.live_controls import LiveControls
    settings = load_settings(args.config)
    ledger = Ledger(settings.db_path, mode="live")
    if ledger.pending_intents():
        print("REFUSED: unresolved live intents exist; reconcile them first (tradebot live-status)")
        return 2
    LiveControls(settings.live, ledger, KillSwitch(settings.kill_file), settings.live_halt_file).resume()
    print("live halt removed (the build switch, live gate, signer activation and limits still apply)")
    return 0

def cmd_approve_exec_test(args) -> int:
    """OWNER ONLY. Single-use approval for one supervised execution test (one buy, one sell back) with exact limits."""
    from .execution.exec_test import approval_phrase, write_approval
    settings = load_settings(args.config)
    try:
        a = write_approval(settings, args.symbol, args.usd, args.max_loss, args.max_cost_bps, args.hours, args.confirm)
    except PermissionError as e:
        print(f"REFUSED: {e}")
        print("expected confirmation: " + approval_phrase(args.symbol, args.usd, args.max_loss))
        return 2
    print(f"approved ONE {a['symbol']} round trip of ${a['usd']:.2f}, max loss ${a['max_loss_usd']:.2f}, max cost {a['max_cost_bps']} bps, "
          f"wallet {a['wallet']}, valid until {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(a['expires_ts']))}.")
    print("This does not arm the trading engine. Run: tradebot exec-test")
    return 0


def cmd_exec_test(args) -> int:
    """One supervised execution test with real funds: buy, sell straight back, reconcile with the chain, report."""
    from .data.live import LiveProvider
    from .engine.bot import TradingBot
    from .execution.exec_test import ExecTestGate, exec_test_risk_limits, load_approval, run_exec_test
    from .execution.jupiter_adapter import JupiterExecutionAdapter
    from .execution.live_controls import LiveControls
    from .execution.signer_protocol import SignerClient, load_client_secret
    from .portfolio.accounting import PortfolioBook
    from .research.claude_analyst import ClaudeAnalyst
    from .risk.engine import RiskEngine
    settings = load_settings(args.config)
    settings.claude.enabled = False
    settings.claude.vet_signals = False
    appr = load_approval(settings)
    gate = ExecTestGate(settings)
    if gate.problems():
        print("REFUSED: " + "; ".join(gate.problems()))
        return 2
    settings.live.max_total_cost_bps = min(settings.live.max_total_cost_bps, float(appr["max_cost_bps"]))
    provider = LiveProvider(settings.data, settings.universe, settings.execution)
    ledger = Ledger(settings.data_path / "exec_test.sqlite", mode="live")
    kill = KillSwitch(settings.kill_file)
    controls = LiveControls(settings.live, ledger, kill, settings.live_halt_file)
    signer = SignerClient(settings.live.signer_url, load_client_secret(settings.live.signer_secret_file))
    probe = JupiterExecutionAdapter(RiskEngine(settings.risk, kill), settings, provider, controls=None, signer=None, dry_run=True)
    usdc = probe.get_balances().get(settings.universe.quote_symbol, 0.0)
    risk = RiskEngine(exec_test_risk_limits(settings, float(appr["usd"]), usdc), kill)
    adapter = JupiterExecutionAdapter(risk, settings, provider, gate=gate, controls=controls, signer=signer, dry_run=False)
    book = PortfolioBook(ledger, max(usdc, 0.01), time.time)
    bot = TradingBot(settings, provider, ledger, risk, kill, adapter, book, [], ClaudeAnalyst(settings.claude), time.time)
    rep = run_exec_test(settings, adapter, bot, controls, exit_only=args.exit_only)
    print(json.dumps(rep, indent=1, default=str))
    print(f"\nEXECUTION TEST: {rep['result']}. Report: {rep.get('report_file')}")
    print("This was an execution test, not evidence of a profitable strategy. The engine was not armed.")
    print("Now: deactivate the signer (python -m tradebot_signer deactivate --home <signer home>) and restore the build switch.")
    return 0 if rep["result"] in ("COMPLETED",) else 1


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
    p = sub.add_parser("arm-live", help="arm real-money trading (interactive terminal only; one-time challenge; expires)")
    p.add_argument("--ttl-hours", type=float, default=None, help="activation life; default [engine].live_arm_ttl_hours (12), hard maximum 24")
    p.add_argument("--evidence", help="verified paper-evidence bundle (see verify-evidence); ignored unless imported evidence is enabled and authenticated")
    p.add_argument("--evidence-sha256", help="bundle hash obtained out-of-band"); p.set_defaults(fn=cmd_arm_live)
    p = sub.add_parser("readiness", help="read-only: paper-record readiness (gap-based) and report-only strategy statistics"); p.set_defaults(fn=cmd_readiness)
    p = sub.add_parser("export-evidence", help="read-only: export the paper ledger as a hashed evidence bundle")
    p.add_argument("--out", required=True); p.add_argument("--provenance", nargs="*", default=[], metavar="KEY=VALUE"); p.set_defaults(fn=cmd_export_evidence)
    p = sub.add_parser("verify-evidence", help="read-only: check an evidence bundle's structure, integrity, correspondence and provenance")
    p.add_argument("file"); p.add_argument("--expected-sha256", required=True, help="hash obtained out-of-band"); p.set_defaults(fn=cmd_verify_evidence)
    p = sub.add_parser("audit-verify", help="verify the live-gate audit chain, optionally against an externally preserved anchor")
    p.add_argument("--anchor-seq", type=int, default=0); p.add_argument("--anchor-hash"); p.set_defaults(fn=cmd_audit_verify)
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
    p = sub.add_parser("fast-halt", help="emergency halt of the fast engine only (flattens, opens nothing)")
    p.add_argument("--reason", default="manual halt"); p.set_defaults(fn=cmd_fast_halt)
    p = sub.add_parser("fast-resume", help="clear the fast engine's emergency halt (data/FAST_HALT)")
    p.set_defaults(fn=cmd_fast_resume)
    p = sub.add_parser("fast-report", help="fast engine: trades with timestamps and net results, win rate, fees, drawdown, latency, rejections")
    p.add_argument("--hours", type=float, default=24.0); p.set_defaults(fn=cmd_fast_report)
    p = sub.add_parser("scan-report", help="summarise the scanner database: sources, refresh, costs, candidates, false positives, missed")
    p.add_argument("--hours", type=float, default=24.0); p.set_defaults(fn=cmd_scan_report)
    p = sub.add_parser("apply-profile", help="update an existing config.toml to the validated settings, preserving everything else (backup first)")
    p.add_argument("--no-backup", action="store_true"); p.set_defaults(fn=cmd_apply_profile)
    p = sub.add_parser("resolve-intent", help="manually settle an UNKNOWN order intent after verifying it yourself")
    p.add_argument("intent_id"); p.add_argument("state", choices=["expired", "failed", "confirmed"]); p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_resolve_intent)
    p = sub.add_parser("live-preflight", help="READ-ONLY live-adapter checks: build switch, gate, Jupiter quotes, RPC, wallet, signer health")
    p.set_defaults(fn=cmd_live_preflight)
    p = sub.add_parser("live-dry-run", help="build + verify + simulate one live order WITHOUT signing or submitting")
    p.add_argument("--symbol", default="SOL"); p.add_argument("--side", choices=["buy", "sell"], default="buy")
    p.add_argument("--amount", type=float, required=True, help="USD for buys, token quantity for sells")
    p.add_argument("--wallet", default=None, help="public key to build for (default [live] wallet_pubkey)"); p.set_defaults(fn=cmd_live_dry_run)
    p = sub.add_parser("approve-exec-test", help="OWNER ONLY: single-use approval for one supervised execution test (exact limits)")
    p.add_argument("--symbol", default="SOL"); p.add_argument("--usd", type=float, required=True)
    p.add_argument("--max-loss", type=float, required=True, help="USD"); p.add_argument("--max-cost-bps", type=float, default=60.0)
    p.add_argument("--hours", type=float, default=1.0); p.add_argument("--confirm", required=True); p.set_defaults(fn=cmd_approve_exec_test)
    p = sub.add_parser("exec-test", help="one supervised real-funds execution test: buy, sell straight back, reconcile, report")
    p.add_argument("--exit-only", action="store_true", help="only sell back a position a previous run left held"); p.set_defaults(fn=cmd_exec_test)
    p = sub.add_parser("live-status", help="live build switch, gate, halt, unresolved intents"); p.set_defaults(fn=cmd_live_status)
    p = sub.add_parser("live-halt", help="EMERGENCY: refuse every live order (data/LIVE_HALT)"); p.add_argument("--reason", default="manual")
    p.set_defaults(fn=cmd_live_halt)
    p = sub.add_parser("live-resume", help="remove data/LIVE_HALT (refused while intents are unresolved)"); p.set_defaults(fn=cmd_live_resume)
    p = sub.add_parser("fetch-data", help="download candles to CSV for offline backtests/replay")
    p.add_argument("--symbols", default=None); p.add_argument("--interval", default="1h"); p.add_argument("--bars", type=int, default=1000)
    p.set_defaults(fn=cmd_fetch_data)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
