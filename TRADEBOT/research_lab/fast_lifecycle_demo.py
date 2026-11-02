"""BUY -> MANAGE -> SELL on real historical prices with the ledger verified after every step.

Runs the production FastEngine (gateway, risk engine, durable intents, atomic booking, self-checks) over real Binance
1-minute closes via ReplayFeed. What is simulated, stated plainly: there is no Jupiter venue offline, so quotes are
ReplayFeed's parametric model (15 bps per leg, min-out at the 50 bps slippage tolerance, $0.01 network fee per
transaction) and the Binance-vs-Jupiter basis signal cannot fire. Nothing is fabricated: the engine finds its own
entries and exits on the real price path, and every rejection is counted.

After every poll in which the fast book holds a position or a fill happened, three independent views of the books are
compared and printed: the in-memory book, the values stored in the ledger, and cash/positions recomputed from the fill
history alone. Any disagreement aborts the run.

usage (from TRADEBOT/):
  PYTHONPATH=$PWD python -m research_lab.fast_lifecycle_demo --symbol SOL --start 2026-08-21 --end 2026-08-24 "<1m csv glob>"...
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import tempfile
from pathlib import Path

from tradebot.config import Settings
from tradebot.fast.engine import FastEngine
from tradebot.fast.recovery import expected_balances, verify_balances
from tradebot.fast.replay import ReplayFeed, load_bars
from tradebot.portfolio.ledger import Ledger
from tradebot.risk.kill_switch import KillSwitch
from tradebot.scanner.store import ScannerStore


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--symbol", default="SOL")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--data-dir", default=None)
    a = ap.parse_args()
    start = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.UTC).timestamp()
    end = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.UTC).timestamp()
    bars = [b for b in load_bars(a.files) if start - 3 * 3600 <= b[0] < end]      # 3 h warm-up for 1m signals
    data_dir = Path(a.data_dir or tempfile.mkdtemp())
    s = Settings(data_dir=str(data_dir))
    s.universe.symbols = [a.symbol]
    s.scanner.extra_mints = []
    s.scanner.poll_sec = 60.0
    feed = ReplayFeed({a.symbol: bars}, {a.symbol: s.universe.mints[a.symbol]}, s.universe.decimals, start_ts=bars[0][0])
    led = Ledger(data_dir / "fast.db")
    eng = FastEngine(s, ScannerStore(data_dir / "scanner.db"), feed, led, KillSwitch(data_dir / "KILL"), clock=feed.now)
    initial = eng.book.cash
    print(f"replay {a.symbol} {_iso(bars[0][0])} .. {_iso(bars[-1][0])} UTC, {len(bars)} real 1m bars; paper capital ${initial:.2f}; "
          f"gateway: max position ${s.fast_risk.max_position_usd:g}, max loss/trade ${s.fast_risk.max_loss_per_trade_usd:g}, "
          f"daily loss ${s.fast_risk.daily_loss_limit_usd:g}")
    print(f"{'time (UTC)':<17} {'step':<8} {'price':>9} {'qty':>10} {'cash: book':>12} {'ledger':>12} {'fills':>12} {'equity':>11} {'unreal.':>8}  note")
    steps = {"BUY": 0, "MANAGE": 0, "SELL": 0}
    n_fills = 0
    while not feed.finished():
        now = feed.now()
        eng.poll()
        fills = led._exec("SELECT * FROM fills ORDER BY rowid").fetchall()
        new = fills[n_fills:]
        n_fills = len(fills)
        if not new and not eng.book.positions:
            feed.advance()
            continue
        problems = verify_balances(led, eng.book)
        exp_cash, exp_pos = expected_balances(led, initial)
        if problems:
            raise SystemExit(f"LEDGER MISMATCH at {_iso(now)}: {problems}")
        px = eng.px[a.symbol].price if a.symbol in eng.px else float("nan")
        qty = eng.book.positions[a.symbol].qty if a.symbol in eng.book.positions else 0.0
        unreal = eng.book.unrealized({a.symbol: px})
        if new:
            for f in new:
                step = "BUY" if f["side"] == "buy" else "SELL"
                steps[step] += 1
                note = f"fill {f['qty']:.6g} @ {f['price']:.6g}, notional ${f['notional_usd']:.4f}, fee ${f['fee_usd']:.4f}"
                if step == "SELL":
                    tr = led.trades()[-1]
                    note += f"; {tr.exit_reason}; net {tr.pnl_usd:+.4f} USD ({tr.return_pct:+.2f}%)"
                print(f"{_iso(now):<17} {step:<8} {px:>9.4f} {qty:>10.6f} {eng.book.cash:>12.6f} {float(led.get('cash')):>12.6f} "
                      f"{exp_cash:>12.6f} {eng.book.equity({a.symbol: px}):>11.4f} {unreal:>8.4f}  {note}")
        else:
            steps["MANAGE"] += 1
            pos = eng.book.positions[a.symbol]
            print(f"{_iso(now):<17} {'MANAGE':<8} {px:>9.4f} {qty:>10.6f} {eng.book.cash:>12.6f} {float(led.get('cash')):>12.6f} "
                  f"{exp_cash:>12.6f} {eng.book.equity({a.symbol: px}):>11.4f} {unreal:>8.4f}  stop {pos.stop_loss:.4f} / tp {pos.take_profit:.4f}")
        feed.advance()
    rej: dict[str, int] = {}
    for d in led.decisions(kind="fast_reject", limit=100000):
        for r in json.loads(d["payload"])["reasons"]:
            k = r.split("(")[0]
            rej[k] = rej.get(k, 0) + 1
    states = {r["state"]: r["n"] for r in led._exec("SELECT state, COUNT(*) n FROM intents GROUP BY state")}
    print(f"\nsteps: {steps}; closed trades {len(led.trades())}; final cash ${eng.book.cash:.6f} (from fills ${expected_balances(led, initial)[0]:.6f}); "
          f"net {eng.book.cash - initial:+.6f} USD; intents {states}; ledger self-check {'OK' if not verify_balances(led, eng.book) else 'MISMATCH'}")
    print(f"rejections: {dict(sorted(rej.items(), key=lambda kv: -kv[1]))}")
    print(f"JSON events: {data_dir / 'fast_events.jsonl'}")


if __name__ == "__main__":
    main()
