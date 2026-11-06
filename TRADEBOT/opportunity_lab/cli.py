"""Command line for the Opportunity Lab (paper only). `python -m opportunity_lab --help`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import LabConfig, load_config


def _live_sources(cfg: LabConfig):
    from .sources.base import Http
    from .sources.dexscreener import DexScreenerSource
    from .sources.geckoterminal import GeckoTerminalSource
    from .sources.onchain import OnchainFacts
    http = Http(timeout=cfg.sources.request_timeout_sec, user_agent=cfg.sources.user_agent)
    market = []
    if cfg.sources.dexscreener:
        market.append(DexScreenerSource(http))
    if cfg.sources.geckoterminal:
        market.append(GeckoTerminalSource(http))
    facts = OnchainFacts(http, cfg.sources.rpc_url, use_jupiter=cfg.sources.jupiter_tokens)
    return market, facts, facts, http


def _engine(cfg: LabConfig, source: str, args):
    from .ai.scorers import build_scorers
    from .engine import LabEngine
    from .ledger import LabLedger
    if source == "live":
        market, facts, routes, _ = _live_sources(cfg)
        clock, advance, truth = time.time, None, None
    else:
        from .sources.synthetic import SimClock, SyntheticMarket
        start = args.start or 1_800_000_000.0
        clock = SimClock(start)
        if source == "synthetic":
            m = SyntheticMarket(clock, seed=args.seed, n_tokens=args.tokens, hours=args.hours, null_edge=args.null)
            truth = m.ground_truth
        else:
            from .sources.replay import ReplayMarket
            m = ReplayMarket.from_jsonl(clock, args.replay_jsonl) if args.replay_jsonl else ReplayMarket.from_ledger(clock, args.replay_db)
            clock.t = m.start
            truth = None
        market, facts, routes, advance = [m], m, m, clock.advance
    scorers = build_scorers(cfg.ai, LabLedger(cfg.db_path), clock)
    return LabEngine(cfg, market, facts, routes, clock=clock, ai_scorers=scorers), advance, truth


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = args.data_dir
        cfg.data_path.mkdir(parents=True, exist_ok=True)
    eng, advance, truth = _engine(cfg, args.source, args)
    try:
        minutes = args.minutes if args.minutes is not None else (args.hours * 60 if args.source != "live" else None)
        res = eng.run(cycles=args.cycles, minutes=minutes, advance=advance)
        if args.source != "live":
            eng.finish()
        from .report import build_report, format_report
        print(json.dumps(res, indent=1, default=str))
        print(format_report(build_report(cfg, eng.ledger, truth)))
    except KeyboardInterrupt:
        print("stopped; state is saved and recovered on the next start")
    finally:
        eng.close()
    return 0


def cmd_scan(args) -> int:
    """Read-only discovery + security assessment + proposals, no paper entries."""
    cfg = load_config(args.config)
    from .agents.analyst import OpportunityAnalyst, strategy_match
    from .agents.scanner import Scanner
    from .agents.security import SecurityAnalyst
    from .ledger import LabLedger
    market, facts, routes, http = _live_sources(cfg)
    led = LabLedger(cfg.db_path)
    sc = Scanner(cfg, market, led)
    sec = SecurityAnalyst(cfg, facts, routes, lambda m: list(sc.history.get(m, [])))
    an = OpportunityAnalyst(cfg, led)
    end = time.time() + args.minutes * 60
    while True:
        opps = sc.cycle()
        for o in opps:
            if o.eligible and any(strategy_match(n, o.signals, o.snapshot) for n in cfg.analyst.strategies):
                a = sec.assess(o, cfg.risk.position_usd)
                led.add_assessment(a, o.id)
                p = an.propose(o, a) if a.approved else None
                if p:
                    led.add_proposal(p)
                print(f"{time.strftime('%H:%M:%S')} {o.snapshot.symbol:<10} {o.mint[:8]} signals={o.signals} "
                      f"security={'OK' if a.approved else 'REJECT ' + '; '.join(a.reasons[:2])}"
                      + (f" proposal={'ACCEPT' if p.accept else 'reject'} cost={p.est_cost_bps}bps" if p else ""))
        print(f"{time.strftime('%H:%M:%S')} tracked={len(sc.history)} stats={sc.stats} http={http.last_error} "
              f"limits={http.limiter.stats()}")
        if time.time() >= end:
            break
        time.sleep(cfg.scan.poll_sec)
    led.close()
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    from .ledger import LabLedger
    from .report import build_report, format_report
    rep = build_report(cfg, LabLedger(cfg.db_path))
    print(json.dumps(rep, indent=1, default=str) if args.json else format_report(rep))
    return 0


def cmd_backtest(args) -> int:
    cfg = load_config(args.config)
    from .backtest import synthetic_monte_carlo, walk_forward_replay
    if args.replay_db:
        res = walk_forward_replay(cfg, args.replay_db, holdout_log=cfg.data_path / "holdout_looks.jsonl")
    else:
        seeds = list(range(args.seed, args.seed + args.seeds))
        res = {f"cost_x{m:g}": synthetic_monte_carlo(cfg, seeds, args.null, hours=args.hours, n_tokens=args.tokens, cost_mult=m,
                                                    unsellable_rate=args.unsellable) for m in args.stress}
        for v in res.values():
            v.pop("per_seed", None)
    text = json.dumps(res, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


def cmd_halt(args) -> int:
    cfg = load_config(args.config)
    cfg.halt_file.write_text(json.dumps({"ts": time.time(), "reason": args.reason, "flatten": args.flatten, "source": "cli"}))
    print(f"lab halted ({cfg.halt_file}): no new paper entries" + ("; open positions exit at the next cycle" if args.flatten else ""))
    return 0


def cmd_resume(args) -> int:
    cfg = load_config(args.config)
    if cfg.halt_file.exists():
        cfg.halt_file.unlink()
    print("lab halt removed (the main bot's data/KILL, if present, still stops new entries)")
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    from .ledger import LabLedger
    led = LabLedger(cfg.db_path)
    print(f"mode: paper (only)   data: {cfg.data_path}")
    print(f"lab halt: {cfg.halt_file.read_text() if cfg.halt_file.exists() else 'clear'}   main kill: {Path(cfg.main_kill_file).exists()}")
    print(f"cash: ${led.get('cash', cfg.risk.paper_capital_usd):,.2f}   open positions: {len(led.open_positions())}   "
          f"pending intents: {len(led.pending_intents())}")
    for k in ("snapshots", "opportunities", "assessments", "proposals", "trades"):
        print(f"{k}: {led.count(k)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m opportunity_lab", description="TRADEBOT Opportunity Lab (PAPER ONLY)")
    ap.add_argument("--config", default=None, help="lab.toml (default: ./lab.toml or env LAB_CONFIG)")
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("run", help="full paper loop: scan, assess, analyse, simulate entries and exits, record")
    p.add_argument("--source", choices=["live", "synthetic", "replay"], default="live")
    p.add_argument("--minutes", type=float, default=None); p.add_argument("--cycles", type=int, default=None)
    p.add_argument("--hours", type=float, default=24.0, help="synthetic/replay length"); p.add_argument("--seed", type=int, default=7)
    p.add_argument("--tokens", type=int, default=80); p.add_argument("--null", action="store_true", help="synthetic market with no edge")
    p.add_argument("--replay-db", default=None); p.add_argument("--replay-jsonl", nargs="+", default=None)
    p.add_argument("--start", type=float, default=None); p.add_argument("--data-dir", default=None)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("scan", help="READ-ONLY live discovery + security + proposals (no paper entries)")
    p.add_argument("--minutes", type=float, default=0.0); p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("report", help="performance report from the lab ledger"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("backtest", help="walk-forward on recorded data (--replay-db) or synthetic Monte Carlo")
    p.add_argument("--replay-db", default=None); p.add_argument("--seeds", type=int, default=10); p.add_argument("--seed", type=int, default=100)
    p.add_argument("--hours", type=float, default=12.0); p.add_argument("--tokens", type=int, default=60)
    p.add_argument("--null", action="store_true"); p.add_argument("--stress", type=float, nargs="+", default=[1.0, 2.0, 3.0])
    p.add_argument("--unsellable", type=float, default=0.0); p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("halt", help="EMERGENCY: stop new paper entries (data/lab/LAB_HALT)")
    p.add_argument("--reason", default="manual"); p.add_argument("--flatten", action="store_true", help="also exit open positions")
    p.set_defaults(fn=cmd_halt)
    p = sub.add_parser("resume", help="remove the lab halt"); p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("status", help="lab state"); p.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
