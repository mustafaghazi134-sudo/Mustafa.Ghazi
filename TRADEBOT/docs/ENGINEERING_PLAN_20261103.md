# Engineering response: experiment review, data-access fixes, Helius, gate audit

Written 2026-11-03 at about 17:50 UTC.

- The fixes are on a **separate branch**, `claude/lab-data-access-fixes`, at commit `cbacfdf`. It is not merged.
- Nothing in the running experiment was changed.

## 1. Status of both running experiments

| Workflow | Run | Started (UTC) | This run ends (UTC) | Whole chain ends | State |
|---|---|---|---|---|---|
| `opportunity-lab` (paper) | 41651911331 | 14:15 | ~19:35 (320 min) | **2026-11-06 14:20** (`deploy/lab_run.trigger`) | Running; every step so far succeeded |
| `paper-trading` (4h engine, paper) | 41643669521 | 13:21 | ~18:55–19:05 | **No end date.** Commit `a3880c4` made it chain continuously until `PAPER_STOP`, a dead engine or a tripped kill switch | Running; checkpoints at +4 to +240 min succeeded |

- **Each chained run checks out the experiment branch again.** Any code pushed to `claude/inspiring-edison-6xsm35`
  therefore changes the experiment at its next run. This is why the fixes are on a separate branch.
- **The repository is public.** GitHub-hosted runner minutes are free, so the continuous chains cost nothing. But
  everything committed, including evidence, is publicly visible. No wallet address of yours has been committed.

## 2. What is working correctly

- **Both workflows run, chain and commit or upload evidence.** Market data is live:
  - the lab diagnostic got pairs 1–2 minutes old, live Solana slots and Jupiter quotes;
  - the paper engine logs prices every minute.
- **The security agent fails closed.** Missing data never approves a token.
- **Genuine risk filters reject real problems:** low liquidity, too new, excessive round trip, excessive price impact.
- **No secrets anywhere:** no keys or seed phrases in tracked files, the git history or workflows. No workflow uses
  repository secrets.
- **The paper engine's basis gate works:** it blocked BTC entries twice because cbBTC's price differed from Binance's
  by more than 100 bps.

## 3. Confirmed bugs and their impact

| # | Bug | Evidence | Impact |
|---|---|---|---|
| 1 | Public RPC refuses `getTokenLargestAccounts` | HTTP 429 for 10/10 tokens (diagnostic before the fix) and 4/4 (after) | `holders_unknown` on every token. **No token can be approved; zero trades in the 72 h run is determined by data access** |
| 2 | One rate-limit bucket shared by all RPC methods | `getAccountInfo` returned HTTP 200 when probed directly, but the lab's own call was blocked by the backoff from the holder-lookup 429s | False `facts_missing` (10/35 in the experiment, 6/10 in the diagnostic) |
| 3 | A quote that got no answer was recorded as "no route" | 7 of 12 Jupiter calls throttled locally; for PEACE the lab recorded "no buy route" while a direct probe returned a route | False `no_route` and **false `honeypot_suspect`** (6/10 in the diagnostic) |
| 4 | Failed facts were cached as if valid | `_CachedFacts` cached any result whose authorities were known, even with holders missing | A failed holder lookup was never retried within the cache window |
| 5 | An exit with a throttled quote started the "unsellable" write-off timer | `executor._manage` treated a throttled quote as a confirmed missing route | A paper position could be written off for an API outage |

**Results to keep on record (unchanged):**

- 35 of 35 original candidates were rejected, and all 35 had missing-data flags (25 `holders_unknown` + 10
  `facts_missing`).
- In the 10-token diagnostic, 8 showed genuine risk; 2 (FLOAT, STONK) were rejected only because data was missing.
  **Their safety is unverified.**
- No legitimate candidate has been confirmed, so no valid paper P&L exists yet.

## 4. Fixes on `claude/lab-data-access-fixes` (thresholds unchanged)

| Fix | Files / functions | What it does |
|---|---|---|
| **A. Independent rate limits** | `opportunity_lab/sources/base.py`: new `CallResult`, `Http.call()`, `_bucket()`, `_once()` | One limiter bucket per endpoint or RPC method (for example `solana_rpc:getTokenLargestAccounts`). Bounded retries that wait for that bucket's own backoff, with a hard cap on total wait. Each outcome is labelled: `ok`, `http_error`, `rate_limited`, `throttled_local`, `network_error` or `invalid_json`. A 429 is recorded as "no answer", **never as evidence about the token** |
| **B. Correct classification** | `sources/onchain.py`: `_quote()`, `route_check()`; `agents/security.py`: `assess()`; `agents/executor.py` | Quote with no answer (429, throttle, timeout, 5xx) → `route_unknown` / `retry_pending`, still blocking. `no_route` and `honeypot_suspect` only when Jupiter **answered** and confirmed there is no route. Entries are deferred at most 3 times, then failed as a data gap. Exits are deferred without starting the write-off timer |
| **C. Provenance** | `models.py`: `MintFacts.data_status`, `indexer_audit`, `RouteCheck.status` / `retry_pending`, `RiskAssessment.decision_basis`; `onchain.mint_facts()`; `report.py` | For authorities, holders, indexer, buy and sell: source, request time, HTTP status, outcome and state (`confirmed` / `missing` / `failed`). Every decision records its basis: `evidence`, `missing_data_only` (safety **unknown**) or `approved`. The report counts them. Jupiter's audit fields are recorded **only** and do not affect decisions |
| Caching | `engine._CachedFacts` | Caches only fully confirmed facts, so failed lookups are retried |

**Test results:**

- **Offline:** 20 new tests in `tests/test_lab_data_access.py`. **335/335 pass**, including the existing 32 lab tests.
  They prove that:
  - a 429 on the holder lookup leaves the mint lookup working (also when the 429 is inside an HTTP 200 RPC error);
  - one refusal is recovered by retry;
  - 429, 5xx, timeout and local throttle on the sell quote give `route_unknown`, never `honeypot_suspect` or
    `no_route`;
  - a confirmed missing route (HTTP 400 or 200 with a "no route" body) still gives `honeypot_suspect` or `no_route`;
  - every lookup records source, time, HTTP status and state;
  - `decision_basis` separates missing data from evidence;
  - Jupiter's holder figure is not used to decide;
  - failed lookups are not cached;
  - the thresholds are unchanged.
- **Live, isolated:** the diagnostic on the fix branch only, 17:43 UTC
  (`lab_diagnostics/security_diag_20261103T174309Z.txt` on that branch):

| Item | Before the fix (10 tokens) | After the fix (4 tokens) |
|---|---|---|
| Mint lookups confirmed | 4/10 (6 false `facts_missing`) | **4/4** |
| Buy and sell quotes answered | false honeypot on 6/10 | **4/4 HTTP 200**, no false honeypot or no-route |
| Real round trip and price impact measured | rarely | price impact over the limit 3/4, round trip over the limit 2/4 |
| Holder lookups | 10/10 failed (429) | **4/4 failed** (429 or backoff). Code cannot fix this; it needs a different data source |

Fewer tokens are assessed per cycle because calls now wait for their own backoff instead of failing fast. That is a
deliberate trade of speed for correctness.

## 5. Helius

**Facts, from Helius's own billing docs:**

- Free plan: **$0/month, 1M credits/month, 10 RPC requests/s**, effective 7 April 2026.
- Free-plan limits for DAS and enhanced APIs are lower, 2 requests/s; the lab does not use those.
- Paid plans: Developer $49/month (10M credits, 50 requests/s); Business $499.
- No method-specific limit for `getTokenLargestAccounts` is published.

**Expected use:**

- 2 standard RPC calls per assessment, about 1 credit each.
- About 100 assessments an hour, so about 200 credits/h and **about 150k credits a month, about 15% of the free
  allowance**.
- The 10 requests/s limit is well above the lab's own budget of 2/s.
- Over-limit behaviour on a free account with no card on file is **unverified**. Check it at sign-up.

**Integration review:**

- `OnchainFacts` already switches to `https://mainnet.helius-rpc.com/?api-key=…` when `HELIUS_API_KEY` is set. The URL
  format is correct.
- The key sits in the URL, but no log, result record or snapshot prints the URL. The fix branch records sources as
  `helius:getTokenLargestAccounts`, without the URL.
- **One gap:** the `opportunity-lab` workflow does not pass the secret. It would need
  `HELIUS_API_KEY: ${{ secrets.HELIUS_API_KEY }}`; GitHub masks secret values in logs.

**Alternatives:**

- **Jupiter's `audit.topHoldersPercentage`.** Free and already fetched, but documented as **pool-inclusive**, so it
  is not comparable with our "exclude the pool vault, top 10 ≤ 35%" rule. Using it to decide would change the rule. Keep
  it as a recorded cross-check.
- **Other RPC providers.** Similar free tiers with keys; none is verified here.

**Recommendation: Helius free tier,** for on-chain holder data only. It needs your action: an account and a
repository secret.

## 6. Audit of commit `a3880c4` (made by your local Claude session; not changed)

**What changed:**

- `tradebot/execution/live_gate.py`: arming now also requires paper coverage, computed as
  (equity snapshots × snapshot interval) ÷ elapsed time. Thresholds were changed from **30 trades / 14 days** to
  **5 trades / 60 days / 80% coverage**, in `tradebot/config.py` (defaults and example) and `deploy/paper_ci.toml`.
- `ledger.count_equity()` was added.
- `tradebot status` now shows coverage.
- Tests for the new gate were added.
- The paper workflow now chains itself indefinitely.

**Why:** the commit says "30 trades in 14 days was unreachable at ~0.1 trades/day (6–10 months)".

**Does it weaken evidence?**

- **Yes, for strategy evidence.** Five trades carry essentially no statistical information. The old 30-trade gate
  was not proof of an edge either, but it was 6× stronger.
- The 60-day / 80% requirement adds genuine *operational* evidence: the engine actually kept running.
- **Net effect:** unattended live arming (within the `[live]` caps of $10/order, $25 exposure, $5/day loss) becomes
  possible after about 60 days instead of 6–10 months, on a strategy that has **no** qualifying evidence.
  `STRATEGY_AUDIT_GO_NOGO.md` §9 is referenced, but the code does not enforce it.

**Additional risks:**

1. **Coverage can be overstated.** Duplicate engines writing to one ledger count twice. Changing
   `equity_snapshot_interval_sec` distorts the figure. It counts snapshots, not gaps.
2. **The GitHub paper record does not count where arming happens.** It lives in the runner's cache, not on your PC
   where `arm-live` would run. Cache eviction can also reset it.
3. **The paper chain runs indefinitely with no end date.** It is cost-free on a public repo, but it needs
   `PAPER_STOP` to end.
4. **`--force` still bypasses everything.** That is unchanged, and a deliberate design choice.

**Recommendation: replace, do not simply revert,** after the 72 h run. Keep the coverage requirement as an
additional operational check, and restore an evidence requirement. No change is made now: the running paper chain
checks out this branch, and you asked for it to stay untouched.

### Recommended approval criteria for autonomous real-money trading (all required)

1. **Execution proven:**
   - the supervised `exec-test` round trip completed and reconciled with the chain;
   - zero unresolved intents;
   - the transaction checker accepts the real routes, including the open SOL-sell issue.
2. **Operations proven:**
   - ≥ 60 days of paper on the *same* machine and ledger that will trade, with **≥ 90% coverage** measured as gaps
     between snapshots, not as a count;
   - no unexplained kill or halt;
   - clean balance reconciliation;
   - an out-of-band heartbeat alert working;
   - independent exit protection.
3. **Strategy evidence (pre-registered):**
   - ≥ 30 closed forward paper trades, or ≥ 6 months;
   - net expectancy after **realised** costs > 0, with the bootstrap 95% CI lower bound > 0;
   - realised costs within the modelled base;
   - maximum drawdown within the 10% kill;
   - the forward result not below the 5th percentile of the backtest distribution.
4. **Owner:** written approval of the wallet, allocation and loss limits; a separate signer account; a short
   activation window; caps no larger than the `[live]` defaults for the first 30 live days.

For a strategy trading about 0.1 times a day, criterion 3 takes months. That is the honest cost of evidence.

## 7. Actions, by approval level

**No approval needed. Done, or to do:**

- Read-only status checks.
- The isolated diagnostics.
- The fix branch and its tests.
- Reports.
- The final-snapshot review after the run.

**Your approval needed:**

- (a) Creating a Helius free account and adding `HELIUS_API_KEY` as a **GitHub repository secret**. Do this yourself
  in GitHub: Settings → Secrets and variables → Actions. Never paste the key in chat.
- (b) Merging `claude/lab-data-access-fixes` and passing the secret to the lab workflow.
- (c) Restarting the lab for a new pre-registered run after 2026-11-06 14:20 UTC.
- (d) Replacing the `a3880c4` gate with the criteria above.
- (e) Whether the paper chain should keep running indefinitely.

**Prohibited without separate explicit approval:**

- wallet access, signer setup or activation, signing, funding, real-money execution;
- arming the live gate, including with `--force`.

## 8. Immediately after the 72-hour run (2026-11-06 14:20 UTC)

1. The final lab snapshot is committed automatically, and the chain stops at the end time.
2. A read-only review of the final funnel and rejection counts. The old code does not record `decision_basis`, so the
   categories will be inferred from flags, as in this report.
3. If you approve (a)–(c): merge the fixes, add the secret, run one short isolated diagnostic to confirm that holder
   lookups succeed on Helius, then start a new 72 h run with **the same thresholds**.
4. Judge that run on approved candidates with confirmed data, their routes, simulated entry and exit, fees, and paper
   P&L if trades occur.
5. **The paper engine is not part of the 72 h run.** It keeps chaining unless you stop it.
