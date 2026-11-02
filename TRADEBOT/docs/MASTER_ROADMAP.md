# TRADEBOT master roadmap

Date 2026-11-02. Branch `claude/inspiring-edison-6xsm35`, latest commit `590e799`.

## Two alignment points before anything else

1. **Claude does not make real-time trading decisions in this design, by choice.** Execution is deterministic Python:
   strategies, the risk engine, the ledger and the execution adapter. Claude does research, engineering, auditing and
   reporting outside the loop. The reasons are below in section 1.4, and I recommend keeping it that way.
2. **No profitable edge is proven yet.** The independent strategy audit (`docs/STRATEGY_AUDIT_GO_NOGO.md`) is NO-GO
   for live trading. "Winning" has to mean a statistically supported, net-of-cost, risk-adjusted result in forward
   trading. Better features alone cannot guarantee it, and nothing in this roadmap promises profit.

## 1. Project overview and architecture

### 1.1 Market and instruments

| Item | Current scope |
|---|---|
| Asset class | Crypto spot only. No equities, forex, futures, leverage or shorting. |
| Venue | Solana, through the Jupiter aggregator (Swap API v2: `/order` + `/execute`). |
| Instruments | SOL, BTC as cbBTC, ETH as WETH, all against USDC. Long or flat. |
| Wallet | A dedicated wallet; its key will live only in the separate signer service. Phantom can be used to watch it. |
| Signal data | Binance spot klines, verified minute for minute against official Binance files. |
| Execution prices | Jupiter Price v3, Tokens v2 safety data, and Swap v2 quotes. |

### 1.2 Strategies

| Engine | Strategy | Status |
|---|---|---|
| 4h engine (`tradebot run`) | **tsmom**: 30-day momentum > 0 and price above the 100-day EMA, evaluated daily, 25% stop. Primary. | Paper. Frozen for the forward test. |
| 4h engine | **momentum**: EMA 12/26 crossover with a 100 trend filter and a 2.5 ATR stop. Secondary. | Paper. |
| 4h engine | mean_reversion, breakout | Disabled: they lost out of sample after costs. |
| Fast engine (`tradebot fast`) | 1-minute dip mean reversion, 5-second loop | Paper only. **Rejected** by the audit. |
| Candidate | trend_ema 4h: EMA 10/30 above EMA 200 | Not adopted. Could run as a forward paper shadow only. |

### 1.3 Architecture

```
                 research_lab (offline): data audit, walk-forward, holdout, bootstrap, robustness
                                     |  frozen parameters only
 Binance klines --+                  v
 Jupiter Price v3 +--> data layer --> strategies --> RiskEngine ----------------> Broker interface
 Jupiter Tokens v2+    (staleness)   (signals)      hard limits, kill switch,       |-- PaperBroker (Jupiter quote, min-out fill)
                                                    single-use HMAC approval        +-- JupiterExecutionAdapter (live, DISABLED)
                                                                                          | verify -> simulate -> signer -> /execute
                                                                                          v
 SQLite ledger: intents (created/signed/submitted/confirmed/failed/expired/abandoned), fills, positions, equity, events
 Separate processes: 4h engine | read-only scanner | fast paper engine (own ledger) | signer service (own OS user, key)
 Safety files: data/KILL (all) | data/FAST_HALT (fast engine) | data/LIVE_HALT (live adapter) | signer HALT
```

### 1.4 How Claude is used

| Role | Mechanism | State |
|---|---|---|
| Weekly research analyst | `ClaudeAnalyst` proposes bounded parameter overrides. It can never change risk limits. | **Off** for the forward test (`research_enabled=false`). |
| Signal vetting | Claude reviews a signal; if unreachable, deterministic logic proceeds. | **Off** (`vet_signals=false`). |
| Daily report | Written narrative of the ledger | **Off** (`daily_report=false`). |
| Auto-applying overrides | Would re-tune the running strategy | **Off** (`auto_apply_overrides=false`). It must stay off during forward tests. |
| Claude Code, as development agent | Builds and audits code, runs research, writes reports | Active. It is denied access to keys, signer secrets and the build switch. |

Why Claude stays out of the execution loop:

- **Latency.** An API call takes seconds, while a quote lasts about a minute.
- **Cost.** Every call reduces net profit.
- **Non-determinism.** The same inputs can give different decisions, which cannot be backtested.
- **Prompt injection.** Market, token or news text can carry instructions.
- **Auditability.** A risk engine has to give the same answer every time.

## 2. What has been done so far

### 2.1 Core engine and execution

| Module | Files | What it does | Evidence |
|---|---|---|---|
| Trading loop | `tradebot/engine/bot.py` | Scans, gets strategy signals, runs risk checks, executes with an intent, manages exits, reconciles, reports | Integration and crash tests |
| Paper broker | `execution/paper.py` | Fills at Jupiter's guaranteed minimum output plus a network fee; a parametric model when offline | Selftest 10/10 |
| Shared interface | `execution/base.py` | `ExecutionAdapter` protocol shared by paper and live | Test |
| Live adapter | `execution/jupiter_adapter.py`, `tx_verify.py`, `simulation.py`, `live_controls.py` | Order to verify to simulate to sign to execute. v0 only; DFlow and OKX excluded; fills from chain. | 86 adversarial tests |
| Signer service | `tradebot_signer/` | Separate process holding the key; owner activation of at most 24 h; own caps and simulation; one signature per order; HMAC over loopback | 15 tests |
| Build switch | `execution/submission.py` | `MAINNET_SUBMISSION_ENABLED = False` | Pinned by a test |
| Ledger | `portfolio/ledger.py` | Atomic booking, intent state machine with an event log, unique order IDs | Mutation-checked tests |
| Crash recovery | `bot.reconcile_pending` | Settles orders from chain evidence only; asks the signer before abandoning; never marks a signed order failed | Crash tests |

### 2.2 Risk and safety guardrails

| Guardrail | Current default |
|---|---|
| Position size | ≤ 10% of equity per position, ≤ 50% total exposure, ≤ 5 positions, $5–$500 per order |
| Daily loss | 3%, which flattens and pauses until the next UTC day |
| Drawdown | 10% peak to trough trips the kill switch |
| Loss streak | 5 consecutive losses pauses entries for 4 h; 30-minute cooldown after any loss |
| Market quality | Slippage ≤ 100 bps, liquidity ≥ $250k, 24 h volume ≥ $500k, stale data > 300 s trips the kill switch |
| Approvals | HMAC, single use, 30 s lifetime; the kill switch and daily halt are re-checked right before execution |
| Live gate | Mode live + environment variable + armed file hashing `[risk]` and `[live]` + ≥ 30 paper trades over 14 days |
| Live controls | $10 per order, $25 wallet exposure, $5 daily loss, 10 orders and $50 per day, 0.02 SOL reserve, one order at a time |
| Fast engine gateway | $300 capital, $100 per position, $200 exposure, $2 maximum loss per trade, $15 daily loss, $40 drawdown |
| Process safety | Instance lock per ledger; separate halt files per engine |
| Claude Code permissions | Denied: `.env`, keypair files, armed and activation files, signer commands and secrets, broker skills, the build switch |

### 2.3 Integrations

- **Jupiter.** Price v3, Tokens v2 (`isSus`, organic score), Swap v2 `/order` and `/execute`. Checked against the official docs at commit `acbe702` (2026-10-28).
- **Binance.** REST klines and a WebSocket trade stream with heartbeat, stall detection and reconnect.
- **Solana RPC.** Signature status, blockhash validity, simulation, lookup tables, balances.
- **Rate limiting.** A token bucket per API, with backoff, jitter and respect for `Retry-After`.
- **Anthropic API.** Through `ClaudeAnalyst`; currently disabled.

### 2.4 Research and validation

| Item | Result |
|---|---|
| Data integrity | 0 mismatches over 2.36 million minutes compared with official Binance files |
| BTC holdout 2025–26, deployed tsmom | +2.4%, profit factor 2.18, max drawdown −2.2% at 10% sizing. Weak because of record momentum reversals. |
| Horizon audit: 20 combinations, walk-forward, 5 cost scenarios | Every strategy at 1 m to 1 h loses after costs. Gross edge −11 to +34 bps per trade. |
| 4h tsmom | +656 bps per trade, 95% CI −19 to +1626. Negative without its best 10% of trades. |
| 1d tsmom | +588 bps per trade, CI −12 to +1447. |
| Portfolio, 2024-10 to 2026-10, 4h tsmom | +21%, max drawdown −37%. Buy and hold: −6.5%, max drawdown −63%. |
| Aug-22 SOL replay trade | Artefact of the replay. A realistic entry loses $0.77. |
| Analogs of the fast rule, 186 dips | −27 bps per trade, CI −44 to −10 |

Tooling: walk-forward harness, pre-registered acceptance criteria, Bonferroni intervals, truncation lookahead tests, a
holdout look register (`results/holdout_looks.jsonl`), a forward-evidence collector, and the Time Machine research
skill pinned in `.claude/skills/`.

### 2.5 Operations

- Windows launchers: `windows/run_paper.bat`, `run_fast.bat`, `update.bat`. They blank key variables.
- Commands: `preflight`, `verify-assets`, `quote`, `wallet`, `selftest`, `scan`, `fast`, `fast-report`,
  `live-preflight`, `live-dry-run`, `live-status`, `live-halt`, `live-resume`, `resolve-intent`.
- Tests: **267 passed**. Selftest: **10/10**.
- Documents: `PRODUCTION_READINESS_AUDIT.md`, `FAST_ENGINE_RELIABILITY.md`, `STRATEGY_AUDIT_GO_NOGO.md`,
  `LIVE_EXECUTION_ADAPTER.md`, `research_lab/SPLITS.md`, `research_lab/HOLDOUT_DIAGNOSIS.md`.

## 3. What is still pending (prioritised)

| Phase | Step | Acceptance criterion | Owner |
|---|---|---|---|
| **P0: run on the PC (now)** | 1. Pull the branch on the Windows PC, run the tests and selftest | 267 passed, 10/10 | You |
| | 2. `tradebot preflight`, `verify-assets`, `live-preflight` | Jupiter key works; quotes for SOL, BTC and ETH; RPC reachable | You |
| | 3. `tradebot live-dry-run` buy and sell for each symbol, with a watch-only public key | All pass, or the reasons for refusal show which allow-list needs evidence-based adjustment | You, then Claude reviews the output |
| | 4. Record real Jupiter round-trip costs and latency | Measured costs replace the assumed 11 / 26 / 51 bps per side | Claude, from your output |
| | 5. Start the 4h engine in forward paper with frozen parameters; run the fast engine in paper only to measure costs | Running continuously; daily reconciliation clean | You |
| **P1: forward evidence (6–12 months)** | 6. Collect at least 30 forward closed trades | `forward_evidence.py` labels them LIVE PAPER | Automatic |
| | 7. Pre-registered review: tracking error against the backtest, and realised against assumed costs | Forward expectancy CI excludes zero and costs are within the base assumption, or a NO-GO is recorded | Claude and you |
| **P2: strategy research, in parallel, on development data only** | 8. Portfolio trend system: ensemble of horizons, volatility targeting, risk parity across BTC, ETH and SOL | Passes the pre-registered criteria, including Bonferroni, and does not depend on its top 10% of trades | Claude |
| | 9. trend_ema 4h as a forward paper shadow, parameters frozen now | Judged on forward data only | Claude builds it; you approve |
| | 10. Research into maker or limit execution, with any custody change explained before building | Measured cost reduction without new custody risk | Claude |
| **P3: live readiness (checklist items 9 to 17 in `LIVE_EXECUTION_ADAPTER.md`)** | 11. Signer under a separate Windows account with NTFS-restricted folder; dedicated wallet funded only with the micro-live budget | Preflight signer check OK | You |
| | 12. Verify signer behaviour on Windows | Signer tests pass on the PC | You and Claude |
| | 13. Alerts to your phone for halts, uncertain orders, mismatches and daily loss | Test alert received | Claude |
| | 14. Written authorisation, then a reviewed commit that turns the build switch on | Commit and updated pinned test | You authorise; Claude implements |
| **P4: supervised micro-live** | 15. $20–25, short activations, one order at a time, every order inspected in an explorer | Live fills within tolerance of the paper model over at least 10 orders | You supervise |
| **P5: scaling** | 16. Increase size only after live results track paper over at least 30 trades with no unexplained incident | Written scaling rule, limits raised in small steps | You decide |

**Engineering gaps not yet covered.** Key storage with Windows DPAPI or a hardware wallet. A paid RPC provider with a
failover. Ledger backups and a restore drill. Trade records exported for tax. A latency and cost dashboard. Tests that
the Windows launchers run correctly.

## 4. The "winning" framework

The bots that survive differ from standard scripts mainly in the discipline that keeps a strategy from trading on a
false edge, rather than in extra features. Recommendations, in priority order:

1. **Define winning measurably, and gate on it.**
   - Measure net of all costs: fees, spread, impact, network fees, slippage, and API spend.
   - Use risk-adjusted metrics: return divided by maximum drawdown, Sharpe ratio, and deflated Sharpe ratio after
     multiple-testing correction.
   - Compare with buy-and-hold of the same assets on a risk-adjusted basis.
   - Require a forward confidence interval that excludes zero.
   - Win rate and trade count are not goals.
2. **Validation is the edge.**
   - Pre-register acceptance criteria before running a test.
   - Use walk-forward with untouched holdouts, and log every look at the holdout.
   - Correct for multiple testing with the deflated Sharpe ratio or Bonferroni.
   - Run lookahead truncation tests and stress costs at two to three times the base.
   - Never tune on data later presented as validation.
   - Track paper against live continuously. If tracking error exceeds a set band, reduce risk automatically.
3. **Choose edges that survive costs.**
   - Slow time-series momentum on liquid assets breaks even at 146–356 bps per side, so it tolerates real-world
     execution.
   - Intraday taker strategies need edge above roughly 25–50 bps per round trip, and none was found.
   - Do not invest in speed for its own sake.
4. **Portfolio construction and risk algorithms.**
   - Size by volatility target, using realised or ATR volatility.
   - Spread risk equally across BTC, ETH and SOL, with a correlation cap.
   - Ensemble several trend horizons, for example 20, 60 and 120 days, to reduce dependence on a few large winners.
   - Cut exposure progressively as drawdown deepens, before the hard kill at 10%.
   - Use a fractional Kelly cap of at most a quarter, from shrunk estimates.
   - Add per-asset circuit breakers.
   - Hard limits stay as they are and are never loosened to get more trades.
5. **Execution quality, measured.**
   - Transaction cost analysis per fill: quote against reference against fill.
   - Statistics by router and time of day.
   - Slippage tolerance set from measured outcomes.
   - Priority-fee ceilings, and order splitting if sizes grow.
   - Limit or maker execution only after the custody implications are explained and accepted.
6. **Use AI where it adds value, outside the loop.** Claude can:
   - generate hypotheses and summarise regimes;
   - review anomalies, reconcile incidents and audit code;
   - draft pre-registered research plans.

   Sentiment from news, X, Reddit and YouTube is a candidate feature with no evidence yet. It must pass the same
   walk-forward and multiple-testing gates. Timestamps must be the time the information was available, not the time
   it happened. LLM output must never place or size a trade directly.
7. **Operations that prevent losses.**
   - Reconcile every cycle.
   - Settle every uncertain order from chain evidence only.
   - Alert for every halt.
   - Run the signer under a separate account with short activations.
   - Keep the micro-live budget small.
   - Scale only by written rule.
8. **Expectations.** A credible goal is a trend system that loses much less than holding in drawdowns and captures
   part of large moves. Nothing here can guarantee outperformance. Go live only when forward evidence supports it.
