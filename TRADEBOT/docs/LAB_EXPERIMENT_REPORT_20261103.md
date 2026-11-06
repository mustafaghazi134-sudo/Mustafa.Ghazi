# 72-hour Opportunity Lab experiment: status and findings (read-only review)

Reviewed 2026-11-03 at about 17:10 UTC. **Nothing in the running experiment was changed, restarted or interrupted.**

Sources:

- `lab_evidence/latest.txt`: the early snapshot, 14:36 UTC.
- `lab_diagnostics/security_diag_20261103T170138Z.txt`: a separate read-only diagnostic job, described in section 2.
- `paper_evidence/run-41643669521/`: paper engine checkpoints.

## 1. Health of the running experiment

| Check | Result |
|---|---|
| Opportunity Lab workflow (run 41651911331, started 14:15 UTC) | **Running.** All steps so far succeeded. Now in "wait for the run to finish". Next run is dispatched automatically; end time 2026-11-06 14:20 UTC. |
| Paper-trading workflow (run 41643669521, 4h engine, started 13:21 UTC) | **Running.** All checkpoints so far succeeded. |
| Snapshots committed | **Lab:** only 2 per run, at about +20 min and at the end, so roughly every 5.3 h. The next commit is due around 19:35 UTC. **Paper engine:** checkpoints are artifacts, not commits; they are read through the evidence relay. |
| Market data current | **Lab:** the diagnostic at 17:01 UTC, running the same code, received live pairs (some 2 minutes old), Solana slot 464,975,237, and live Jupiter quotes. The experiment's own data timestamps are not visible in its snapshot. **Paper engine:** live prices every minute up to its 16:22 UTC checkpoint (BTC 77,257, ETH 2,281.7, SOL 92.83). |
| Private keys or seed phrases exposed | **None found.** Checked tracked files and the full git history for: 64-byte key arrays, base58 keys, seed phrases, `jup_` API keys and key assignments. No key or wallet files are tracked. No workflow uses repository secrets (only the job's own `github.token`). |
| Strategy changed during the experiment | No. |

**Observations, not changes:**

- **Leftover paper checkpoints.** The paper runner keeps its data folder between runs. Its relayed folder therefore mixes
  this run's checkpoints (cp1–cp6) with older ones from run 3 (cp7, cp8, final). Read the timestamps.
- **The live gate was changed by another session.** Commit `a3880c4` changed the requirement to arm the engine from
  "≥ 30 paper trades over ≥ 14 days" to "≥ 60 days of paper history, engine running ≥ 80% of that time, and ≥ 5 closed
  trades". It needs fewer trades over a longer time. It is reported here and was **not** modified.

## 2. Why every token was rejected

### The experiment's own figures (14:36 UTC)

- 454 opportunities, 75 eligible, **35 assessed, 35 rejected (100%)**, 0 proposals, 0 trades.
- A token can carry several reasons, so the counts below add up to more than 35:

| Reason | Tokens | Share of the 35 |
|---|---|---|
| `holders_unknown` | 25 | 71% |
| `too_new` | 16 | 46% |
| `low_liquidity` | 15 | 43% |
| `price_impact` | 12 | 34% |
| `no_route` | 11 | 31% |
| `facts_missing` | 10 | 29% |
| `round_trip_loss` | 3 | 9% |
| `liquidity_withdrawal` | 2 | 6% |
| `token2022_transferHook` | 2 | 6% |
| `no_sellers` | 1 | 3% |

### Key finding: no token can pass under the current data access

In the code, `facts_missing` and `holders_unknown` are mutually exclusive, and **25 + 10 = 35**. So **every one of the
35 tokens** carried a missing-data flag that blocks approval on its own. The diagnostic shows why:

| Flag | Cause, measured | Classification |
|---|---|---|
| `holders_unknown` | The public RPC `api.mainnet-beta.solana.com` answered `getTokenLargestAccounts` with **HTTP 429 "Too many requests for a specific RPC call" for 10/10 tokens (100%)** | **API limitation.** Fail-closed by design |
| `facts_missing` | A direct `getAccountInfo` probe returned **HTTP 200 for 10/10**. The lab's own call was blocked because the 429s on the holder call pushed its *shared* Solana-RPC rate limiter into backoff | **API limitation, made worse by a design detail** (one limiter for all RPC methods) |
| `honeypot_suspect` / `no_route` | The lab's own **local** Jupiter budget throttled 7 of its 12 quote calls. A throttled sell quote is recorded as "no sell route" and so flagged as a honeypot. For PEACE the lab recorded "no buy route (local throttle)" while a direct probe returned a valid route (HTTP 200) | **Misclassification of a data gap.** It is not evidence of a honeypot |
| `low_liquidity` (< $50,000), `too_new` (< 30 min), `round_trip_loss` (> 250 bps), `price_impact` (> 150 bps), `liquidity_withdrawal`, `token2022_transferHook`, `no_sellers` | Observed on real data | **Legitimate risk filters.** Keep them |

### Diagnostic sample: 10 tokens, same thresholds, 17:01 UTC

| Token | Liquidity | Age | Blocking flags | Verdict |
|---|---|---|---|---|
| NALA | $37k | 89 min | low_liquidity, round_trip_loss (271 bps), holders_unknown | Real risk |
| CAPY | $40k | 22 h | low_liquidity, facts_missing, honeypot_suspect | Real risk (liquidity) |
| MOVEMBER | $40k | 44 h | low_liquidity, holders_unknown, honeypot_suspect | Real risk (liquidity) |
| MOGMAS | $26k | 5 h | low_liquidity, facts_missing, honeypot_suspect | Real risk (liquidity) |
| PEACE | $38k | 2 min | low_liquidity, too_new, facts_missing, no_route | Real risk |
| CHOO | $54k | 2 min | too_new, holders_unknown | Real risk (too new) |
| ARMPASS | $115k | 2 min | too_new, facts_missing, honeypot_suspect | Real risk (too new) |
| UDTF | $943k | 2 min | too_new, holders_unknown | Real risk (too new) |
| **FLOAT** | **$416k** | **5.8 days** | facts_missing, honeypot_suspect | **Data limitations only** |
| **STONK** | **$2.34M** | **83 days** | facts_missing, honeypot_suspect | **Data limitations only** |

**Summary:**

- **8/10 (80%)** had at least one genuine risk flag:
  - low liquidity: 5 (50%);
  - too new: 4 (40%);
  - excessive round trip: 1 (10%).
- **2/10 (20%)** were rejected *only* because data was unavailable. Whether they are safe is **unknown**, not
  established.
- **10/10 (100%)** had at least one missing-data flag.

**Legitimate candidates: none confirmed.** FLOAT and STONK are the only ones not excluded by an observed risk, and
their holder concentration, mint authorities and sell routes are still unverified.

**Buy/sell routes, simulated entry and exit prices, fees and slippage:**

- **Not available for any approved candidate**, because none was approved.
- NALA's quoted round trip was −271 bps at $25 (above the 250 bps cap).
- No sell quotes were obtained for FLOAT or STONK (locally throttled).

**Paper P&L:** none, because there were no trades. Lab equity is $1,000.00.

## 3. Bottom line for the experiment as configured

On the free public RPC the holder-concentration lookup is always refused. The security agent correctly fails closed
on that missing data. So **the experiment cannot approve any token, and zero trades over the full 72 hours is
determined by data access, not by the market.**

It still records discovery and filter data, and it still runs safely. It cannot show that the system "identifies safe,
executable opportunities" until holder and route data are obtainable.

**Options after this run.** None of them weakens a security rule.

1. **A data source that answers `getTokenLargestAccounts`.** The lab already supports `HELIUS_API_KEY`.
   - Helius has a free tier, but it needs an account: **your decision**.
   - It would be a GitHub repository secret, a change to the experiment's setup.
2. **Correctness fixes in the lab code.** Each keeps it fail-closed.
   - Separate rate-limit buckets per RPC method, so one refused method does not block the others.
   - Treat a locally throttled quote as `route_unknown` (a data gap, re-assessed later), not as `no_route` or
     `honeypot_suspect`.
   - Report eligible-but-unassessed tokens (40 of 75) as budget-limited.
3. **Keep the thresholds:** $50k liquidity, 30-minute minimum age, 250 bps round trip, 150 bps price impact. These
   accounted for the genuine rejections.

Applying 1 or 2 means changing or restarting the experiment, so they wait for your decision or the end of the run.

## 4. Next execution test (read-only preparation): status

| Check | Status |
|---|---|
| SOL balance | Not verified on chain. Needs your wallet's **public** address. You reported 0.00663 SOL (about $0.61) |
| USDC balance | Not verified: same reason |
| Jupiter SOL→USDC sell route for your wallet | Not checked. Jupiter builds no transaction for an empty wallet; your 0.00663 SOL is enough for it to build a 0.003 SOL sell |
| Transaction simulation | Not run for your wallet |
| Required fees | Measured on other wallets: about 5,000 lamports + 17–2,054 priority per Metis transaction. Temporary wrapped-SOL rent (about 0.00204 SOL) is refunded in the same transaction |
| Unresolved transaction-checker issue | On a third-party wallet the real Metis SOL→USDC route passed SOL through a pre-existing wrapped-SOL account that is not the wallet's standard one. **Our checker refuses that** (fail-closed). Whether your wallet gets the same route is unknown |

**To run it, read-only, without signing,** run this in `cmd` in your TRADEBOT folder. Each command is separate:

```bash
set TRADEBOT_CONFIG=deploy\paper_ci.toml
```
```bash
set TRADEBOT_DATA_DIR=%TEMP%\tradebot_dryrun
```
```bash
.venv\Scripts\python -m tradebot live-dry-run --wallet YOUR_PUBLIC_ADDRESS --symbol SOL --side sell --amount 0.003
```

It will report `live_controls_would_block: SOL reserve`; that is expected at your balance.

## 5. Restrictions kept

No live trading, no signer activation, no wallet funding, no permission changes, no strategy deployment, and no
change to the running experiment.

The only additions were:

- a separate read-only diagnostic job (`lab-security-diag`);
- an evidence-relay read of the paper engine's checkpoints.
