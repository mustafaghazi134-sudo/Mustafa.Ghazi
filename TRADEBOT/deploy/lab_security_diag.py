"""READ-ONLY diagnostic of the Opportunity Lab's security rejections. Runs the lab's own scanner and security agent
(same code and lab.toml.example thresholds as the running experiment) in a SEPARATE data folder for a few cycles,
then, for every assessed token, prints all flags, data gaps and data provenance, and re-probes the two RPC calls
behind `facts_missing` / `holders_unknown` and the Jupiter calls behind `no_route`, recording raw HTTP status.
It classifies each rejection as: real risk (a critical/high flag from observed data), data-only (rejected solely by
missing-data flags), or both. No paper entries, no wallet, no keys; does not touch the running lab.

usage: LAB_CONFIG=lab.toml LAB_DATA_DIR=/tmp/labdiag python deploy/lab_security_diag.py [cycles]
"""
from __future__ import annotations

import collections
import json
import sys
import time

import httpx

from opportunity_lab.agents.analyst import strategy_match
from opportunity_lab.agents.scanner import Scanner
from opportunity_lab.agents.security import SecurityAnalyst
from opportunity_lab.cli import _live_sources
from opportunity_lab.config import load_config
from opportunity_lab.ledger import LabLedger

DATA_FLAGS = {"facts_missing", "holders_unknown", "route_unknown", "liquidity_unknown", "age_unknown"}
MAYBE_DATA = {"no_route"}            # may be a rate-limit/HTTP error rather than a truly missing route: checked below


def raw_status(url, payload=None, params=None):
    try:
        r = httpx.post(url, json=payload, timeout=15) if payload else httpx.get(url, params=params, timeout=15)
        body = r.text[:160].replace("\n", " ")
        return f"HTTP {r.status_code} {body}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}"


def main() -> None:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    cfg = load_config()
    market, facts, routes, http = _live_sources(cfg)
    led = LabLedger(cfg.db_path)
    sc = Scanner(cfg, market, led)
    sec = SecurityAnalyst(cfg, facts, routes, lambda m: list(sc.history.get(m, [])))
    print(f"=== LAB SECURITY DIAGNOSTIC (read-only) {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  rpc={facts.rpc_url.split('?')[0]}"
          f"  thresholds: min_liq ${cfg.security.min_liquidity_usd:,.0f}, min_age {cfg.security.min_age_min:.0f} min, "
          f"top10<= {cfg.security.max_top10_holder_pct}%, rt_loss<= {cfg.security.max_round_trip_loss_bps} bps, "
          f"impact<= {cfg.security.max_price_impact_bps} bps, size ${cfg.risk.position_usd}")
    seen, rows = set(), []
    for c in range(cycles):
        opps = sc.cycle()
        for o in opps:
            if o.mint in seen or not o.eligible or not any(strategy_match(n, o.signals, o.snapshot) for n in cfg.analyst.strategies):
                continue
            seen.add(o.mint)
            a = sec.assess(o, cfg.risk.position_usd)
            codes = [f["code"] for f in a.flags if f["severity"] in ("critical", "high")]
            ds = getattr(a.facts, "data_status", None) or {}
            row = {"symbol": o.snapshot.symbol, "decision_basis": getattr(a, "decision_basis", ""),
                   "data_status": {k: {x: v.get(x) for x in ("state", "outcome", "http", "attempts")} for k, v in ds.items()},
                   "route_status": {k: {x: v.get(x) for x in ("outcome", "http", "confirmed_no_route")} for k, v in (getattr(a.route, "status", None) or {}).items()}, "mint": o.mint, "liq": o.snapshot.liquidity_usd, "age_min": o.snapshot.age_min(time.time()),
                   "signals": o.signals, "approved": a.approved, "blocking": codes, "flags": a.flags, "gaps": a.data_gaps,
                   "provenance": getattr(a.facts, "provenance", None), "http_last_error": dict(http.last_error)}
            if "facts_missing" in codes or "holders_unknown" in codes:
                row["probe_getAccountInfo"] = raw_status(facts.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                                                          "params": [o.mint, {"encoding": "jsonParsed"}]})
                time.sleep(1.0)
                row["probe_getTokenLargestAccounts"] = raw_status(facts.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                                                                                   "params": [o.mint]})
            if "no_route" in codes:
                row["probe_jupiter_buy"] = raw_status(f"{facts.jup_base}/swap/v2/order", params={
                    "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "outputMint": o.mint, "amount": str(int(cfg.risk.position_usd * 1e6))})
            rows.append(row)
            time.sleep(1.0)
        print(f"cycle {c + 1}: scanner stats {sc.stats}  http_last_error={http.last_error}  limiter={http.limiter.stats()}")
        time.sleep(cfg.scan.poll_sec)
    # classification
    cls = collections.Counter()
    flag_counts = collections.Counter()
    for r in rows:
        b = set(r["blocking"])
        flag_counts.update(b)
        real = b - DATA_FLAGS - MAYBE_DATA
        data = b & (DATA_FLAGS | MAYBE_DATA)
        r["class"] = "APPROVED" if r["approved"] else ("real_risk_only" if real and not data else "data_only" if data and not real else "real_and_data")
        cls[r["class"]] += 1
    n = max(1, len(rows))
    basis = collections.Counter(r.get("decision_basis") or "n/a" for r in rows)
    hold = collections.Counter((r.get("data_status", {}).get("holders") or {}).get("state", "n/a") for r in rows)
    auth = collections.Counter((r.get("data_status", {}).get("authorities") or {}).get("state", "n/a") for r in rows)
    print(f"\n=== {len(rows)} tokens assessed")
    print("decision basis (code):", dict(basis), " holders lookup:", dict(hold), " authorities lookup:", dict(auth))
    print("classification:", {k: f"{v} ({v / n * 100:.0f}%)" for k, v in cls.items()})
    print("blocking flags (tokens, %):", {k: f"{v} ({v / n * 100:.0f}%)" for k, v in flag_counts.most_common()})
    for r in rows:
        print("\n" + json.dumps({k: v for k, v in r.items() if k != "flags"}, default=str)[:1500])
        print("   flags:", "; ".join(f"{f['code']}[{f['severity']}] {f['detail']}" for f in r["flags"])[:700])
    led.close()
    print("\nread-only: no paper entries, no wallet, nothing signed; the running lab experiment was not touched.")


if __name__ == "__main__":
    main()
