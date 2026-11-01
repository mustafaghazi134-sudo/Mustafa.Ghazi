#!/usr/bin/env bash
# Linux/macOS equivalent of windows/update.bat. Run from the TRADEBOT directory.
set -euo pipefail
cd "$(dirname "$0")"
stamp=$(date +%Y%m%d-%H%M%S); mkdir -p backups
[ -f config.toml ] && cp config.toml "backups/config.toml.$stamp" && echo "backed up config.toml -> backups/config.toml.$stamp"
[ -f .env ] && cp .env "backups/.env.$stamp" && echo "backed up .env -> backups/.env.$stamp"
git fetch origin claude/inspiring-edison-6xsm35 && git checkout claude/inspiring-edison-6xsm35 && git pull --ff-only origin claude/inspiring-edison-6xsm35
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -e ".[dev]"
[ -f config.toml ] || tradebot init
tradebot apply-profile
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export TRADEBOT_FORCE_PAPER=1; unset TRADEBOT_LIVE_TRADING SOLANA_PRIVATE_KEY
echo "=== tests ===";            python -m pytest -q
echo "=== preflight ===";        tradebot preflight || true
echo "=== asset verification ==="; tradebot verify-assets || true
echo "=== 3 paper cycles on real market data ==="; tradebot run --paper --cycles 3 || true
tradebot status; tradebot report --decisions 10
