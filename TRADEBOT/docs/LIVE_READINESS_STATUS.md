# Live readiness status (evidence-based)

Date 2026-11-03.

| Evidence class | Status |
|---|---|
| Historical results | `STRATEGY_AUDIT_GO_NOGO.md`, `OPPORTUNITY_LAB_RESULTS.md`, `FEASIBILITY_LONG_SHORT.md` (long/short and alternative horizons: all 12 pre-registered configurations rejected) |
| Forward paper results | Section 3 |
| Real fills | **None.** No real transaction has ever been signed or sent |

## 1. Trading path audit (4h engine)

| Step | Implementation | Reachable today? |
|---|---|---|
| Live data | `tradebot/data/live.py`: Binance 4h klines (signal instrument), Jupiter Price v3 and Tokens v2, DexScreener fallback; stale data trips the kill switch (`risk/engine.py`, `stale_data_sec` 300) | **Yes**, paper. Verified on a GitHub runner on 2026-11-03: live BTC/ETH/SOL prices every 60 s |
| Signal | `tradebot/strategies/tsmom.py` (daily at 00 UTC), `strategies/momentum.py` (4h closes), called from `engine/bot.py` | **Yes**, paper |
| Risk sizing | `tradebot/risk/engine.py`: limits, single-use HMAC approval with a 30 s lifetime, pending-buy reservation | **Yes** |
| Quote | Paper: `execution/paper.py`, Jupiter `/swap/v2/order` without taker (key, or `paper_keyless_quotes`), fill at the guaranteed minimum output plus network fee, parametric fallback. Live: `execution/jupiter_adapter.py` `quote()` / `prepare()` | Paper: **yes**. Live: dry-run only |
| Transaction verification | `execution/tx_verify.py`: programs, signers, transfers, approvals, mints, fees, expiry | Dry-run only (`tradebot live-dry-run`). **Never run against a real route** (no network from the build environment) |
| Simulation | `execution/simulation.py`: wallet balance changes via `simulateTransaction` | Dry-run only; never run for real |
| Separate signer | `tradebot_signer/` with own policy, owner activation (≤ 24 h), caps, idempotent signing | **No**: not deployed; refuses while the build switch is off |
| Submission | `jupiter_adapter._submit`, gated by `execution/submission.py` (`MAINNET_SUBMISSION_ENABLED = False`, verified) and `execution/live_gate.py` (mode, env, armed file; not armed, verified) | **No (by design)** |
| Confirmation | `jupiter_adapter._await_confirmation`, `lookup_signature`, `transaction_status` (blockhash at finalized commitment) | Tested with an in-memory chain only |
| Fill accounting | `jupiter_adapter.fill_from_chain` (balance changes on chain), `engine/bot._book_fill` (atomic) | Paper: yes. Live: tested in memory only |
| Exit | `engine/bot._manage_positions` + strategy exits, evaluated **in the same process every 60 s**; kill switch and daily halt flatten | Paper: yes. **No independent exit monitor and no resting on-chain stop**: if the process or machine stops, an open position has no stop |
| Reconciliation | `engine/bot.reconcile_pending` (chain evidence only; asks the signer before abandoning), `reconcile_balances` (mismatch trips the kill switch) | Paper: yes. Live: tested in memory only |

## 2. Tests and failure injection (this commit)

The full suite has **306 passed** (2026-11-03, including the long/short study accounting tests), and `tradebot selftest` passes 10/10. These cover:

| Check | Tests |
|---|---|
| Duplicate prevention | `test_crash_protection.py` (unique intent per order id), `test_live_adapter.py` (one signature per order) |
| Unknown outcomes | `test_crash_protection.py`, `test_live_adapter.py` (timeouts, `Failed` but landed, `Success` but failed on chain) |
| Restart recovery | `test_crash_protection.py`, `test_live_adapter.py` (crash between signing and persisting) |
| Stale-data rejection | `test_risk_engine.py`, `test_fast_reliability.py` |
| Partial failures | `test_live_adapter.py` (fill below minimum output, tampered signer response, RPC down during reconcile) |
| Kill switch | `test_kill_switch.py`, selftest |
| Live gate | `test_live_gate.py`, `test_safety_audit.py` |

The same tests, deliberately broken, were confirmed to catch:

- not persisting before submitting;
- abandoning without asking the signer;
- ignoring SOL drains;
- accepting a different signed message.

## 3. Strategy acceptance against forward records

| Requirement (source) | Needed | Forward evidence |
|---|---|---|
| Paper record before arming (`execution/live_gate.py`, `[engine]`) | ≥ 30 closed paper trades and ≥ 14 days | **0 trades.** No persistent forward paper ledger exists: none on the PC is visible here, and the runner ledger started 2026-11-03 07:07 UTC |
| Pre-registered review (`STRATEGY_AUDIT_GO_NOGO.md` §9) | ≥ 30 forward trades or 6 months; forward expectancy CI excludes 0; realised costs within base | **Not met** |
| Historical audit | The 4h/1d trend rules are positive but not significant after correcting for 20 tests; everything shorter-horizon is rejected | Historical only: does not count |

**No strategy qualifies for real money.** The missing evidence is at least 30 closed forward paper trades over at least
14 days, from a persistent engine with executable quotes. At about 0.1 trades/day across BTC, ETH and SOL, that is
roughly 6–10 months.

## 4. Capabilities

| Capability | Status |
|---|---|
| Spot buy and sell (USDC ↔ SOL, cbBTC, WETH via Jupiter) | Paper: yes. Live: built, disabled |
| Short positions | **No.** Selling only reduces a held position. A perpetual venue would be needed; the minimum changes are in `FEASIBILITY_LONG_SHORT.md` §1. Not built, because no long/short rule qualified |
| Leverage | **No** |
| Decision frequency | Loop every 60 s. tsmom decides once a day (00 UTC); momentum on 4h closes. About 0.1 trades/day across 3 assets historically. Not a seconds-based system |
| AI | Off. Not in the execution path |
| Signal vs executable asset | Signals on Binance BTCUSDT, ETHUSDT, SOLUSDT; executes cbBTC `cbbtcf3a…ij`, WETH `7vfCXTUX…voxs`, SOL `So111…112`. Measured basis 2026-11-03 07:07–07:09 UTC: −16 to −23 bps, tracked in every cycle |

## 4a. Exit protection: what happens on failure while holding a position

Exits (stops, strategy exits, kill-switch and daily-halt flatten) run **inside the engine process**, once per 60 s
cycle (`engine/bot._manage_positions`). Jupiter spot has no resting stop order in this design. So a stop exists only
while the engine runs and can reach its data, Jupiter and, live, its RPC and signer.

| Failure while holding | What happens | When the exit happens |
|---|---|---|
| **Engine process crashes** | Positions, stops and high-water marks are in the ledger (updated every cycle). The supervisor restarts the engine: `windows/run_paper.bat` loop after 30 s, systemd `Restart=always` after 15 s, Docker `restart: unless-stopped`. On start: pending intents are reconciled from chain evidence; live balances are compared with the ledger, and a mismatch trips the kill switch. | First cycle after restart, **at the post-restart price**, so a gap through the stop fills below it. **Verified:** `test_restart_while_holding_restores_stop_and_exits_when_breached` plus the crash tests |
| **Host down** (power, sleep, OS update) | Nothing runs, so no exit | Only when the host is back. **Unbounded.** Windows sleep is the most likely cause on a PC |
| **Internet down** | Prices go stale. After 300 s the kill switch trips and tries to flatten, which fails without network. It retries every cycle | When connectivity returns |
| **Binance candles down; Jupiter up** | Stale data trips the kill switch, then flattens through Jupiter | Within about 5 min (conservative: exits rather than holds) |
| **Jupiter down** | No quotes and no swaps; Jupiter is the only venue | When Jupiter returns |
| **RPC down (live)** | The live adapter needs RPC for block height, simulation and confirmation. Its fail-closed checks **also block exits** | When the RPC returns |
| **Signer down (live)** | No signature, so no exit | When the signer returns |
| **Exit transaction outcome unknown** | Symbol blocked; settled from chain evidence; never re-sent as a new transaction | After reconciliation, usually within the blockhash life of about 1–2 min |

**Loss while unprotected is bounded only by position size and the gap:**

- Paper: up to 10% of equity per position and 50% total.
- Planned micro-live: $10 per order and $25 exposure.
- The tsmom 25% disaster stop and the momentum ATR stop are enforced only while the engine runs.

**Minimum practical protection.** Steps 1–3 use existing tools; nothing new is subscribed to.

1. **An always-on host with supervision:** a small VPS, or the PC with sleep and automatic restarts disabled, running
   the existing `run_paper.bat` loop, systemd unit or Docker. This removes most process and host gaps.
2. **An out-of-band heartbeat alert.** The engine already writes an equity point every 300 s. A check on a second
   machine pages you if the newest point is older than 10 minutes while positions are open; you then sell in Phantom
   or jup.ag by hand. This covers host, process and internet loss on the engine side, provided Jupiter is up and you
   are reachable.
3. **A fallback RPC list for live exits:** two or three independent providers, so one RPC outage does not block
   reduce-only exits. All verification stays as it is.
4. **Deliberately not proposed yet:**
   - **Jupiter Trigger orders.** They move funds into Jupiter's order program until filled or cancelled. That is a
     custody change and needs your decision first. Stop-style semantics on Trigger are unverified here.
   - **A second process able to sign sells.** That would be a second execution path holding signing access.

**What still fails even with 1–3:**

- simultaneous Jupiter and engine outages;
- you being unreachable when paged;
- the alert path failing at the same time;
- gap risk between the last good price and the eventual exit.

The only control over the size of those losses is the position and exposure limit.

## 5. Live blockers, in order

1. **Forward evidence (section 3).**
2. **Persistent machine.** The forward-paper engine must run continuously on your PC or a VPS. GitHub runs are bounded experiments of at most 6 h. `windows/install_autostart.bat` starts the paper engine at sign-in and turns off mains-power sleep; it asks before each change.
3. **Independent exit protection.** No process-independent stop exists for live positions. Options:
   - a separate watchdog process that can only sell;
   - Jupiter Trigger orders, which change custody and need explaining and your approval first.
4. **Real-route verification:** `tradebot live-preflight` and `tradebot live-dry-run` must pass on the PC for buy and sell of each asset.
5. **Signer deployment:**
   - a separate Windows account;
   - a new dedicated wallet;
   - its policy reviewed;
   - Windows behaviour tested.
6. **Owner-established funded limits:** wallet public address, total allocation, maximum order, total exposure, daily loss, slippage.
7. **Your written authorisation:** a reviewed commit that turns on `MAINNET_SUBMISSION_ENABLED`, then `arm-live` and signer activation.
