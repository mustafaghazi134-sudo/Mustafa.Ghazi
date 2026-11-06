#!/usr/bin/env bash
# Paper-run checkpoint (used by .github/workflows/paper-trading.yml). Waits, then writes a timestamped evidence file:
# bot alive?, kill/halt state, positions and intents, recent decisions (every signal and the risk decision), orders,
# fills, trades, events and the log tail. Never fails the job. Usage: paper_checkpoint.sh <label> <wait_seconds>
LABEL="$1"; WAIT="${2:-0}"
D="${TRADEBOT_DATA_DIR:-data_paper}"
PID="$(cat "$D/bot.pid" 2>/dev/null)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then sleep "$WAIT"; fi
mkdir -p "$D/checkpoints"
OUT="$D/checkpoints/${LABEL}.txt"
{
  echo "=== PAPER TRADING CHECKPOINT ${LABEL}  $(date -u +%FT%TZ)  (SIMULATED FUNDS) ==="
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "engine: RUNNING (pid $PID)"; else echo "engine: NOT RUNNING"; fi
  echo; python -m tradebot status --mode paper 2>&1
  echo; python -m tradebot report --mode paper --decisions 30 2>&1
  echo; python - <<'EOF' 2>&1
import os, sqlite3, time
d = os.environ.get("TRADEBOT_DATA_DIR", "data_paper")
con = sqlite3.connect(f"{d}/ledger.sqlite"); con.row_factory = sqlite3.Row
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else "-"
def q(title, sql):
    print(f"--- {title}")
    try:
        rows = con.execute(sql).fetchall()
    except sqlite3.Error as e:
        print("  (n/a)", e); return
    for r in rows:
        print("  " + " | ".join(f"{k}={fmt(r[k]) if k in ('ts', 'entry_ts', 'exit_ts') else r[k]}" for k in r.keys()))
    if not rows:
        print("  (none)")
q("orders (last 20)", "SELECT ts, mode, symbol, side, notional_usd, status, detail FROM orders ORDER BY ts DESC LIMIT 20")
q("fills (last 20)", "SELECT ts, mode, symbol, side, qty, price, notional_usd, fee_usd, slippage_bps FROM fills ORDER BY ts DESC LIMIT 20")
q("closed trades (last 20)", "SELECT exit_ts, symbol, strategy, pnl_usd, exit_reason FROM trades ORDER BY exit_ts DESC LIMIT 20")
q("events (last 25)", "SELECT ts, kind, message FROM events ORDER BY ts DESC LIMIT 25")
q("decision counts by kind", "SELECT kind, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts FROM decisions GROUP BY kind")
EOF
  echo; echo "--- log tail"; tail -n 60 "$D/tradebot.log" 2>&1
} > "$OUT" 2>&1
cat "$OUT" | head -n 400
exit 0
