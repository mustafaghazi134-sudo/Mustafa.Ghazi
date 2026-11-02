# Opportunity Lab: architecture and security boundaries

The Opportunity Lab is an independent, **paper-only**, four-agent experiment for discovering Solana token
opportunities. It lives in `opportunity_lab/`, runs as its own process (`python -m opportunity_lab`), and shares
nothing writable with the BTC/ETH/SOL bot.

## 1. Pipeline (one cycle every `scan.poll_sec`, default 30 s)

```
 sources (read-only, rate-limited)                    lab ledger: data/lab/lab.sqlite (WAL)
 DexScreener | GeckoTerminal | Jupiter Tokens v2 |     snapshots, opportunities, assessments,
 Jupiter /order (no taker) | Solana RPC                proposals, intents, positions, trades, events, equity
        |                                                         ^
        v                                                         |
 Agent 1 SCANNER ---- every observation recorded with observed_ts + source
   detectors: volume_surge, liquidity_growth/drop, momentum, breakout, buy_pressure, migration (+ new_token tag)
   eligibility: price present, liquidity >= discovery floor, routable quote token
        |  (only opportunities that match a strategy spend API budget on the next step)
        v
 Agent 2 SECURITY ---- fail closed: missing critical data = reject
   mint/freeze authority, Token-2022 extensions + transfer fee, Jupiter isSus, holder concentration,
   age, liquidity, liquidity withdrawal, buyers-without-sellers, buy route AND sell-back route, round-trip loss, impact
        |  approved only with no critical/high flag
        v
 Agent 3 ANALYST (deterministic) ---- optional Claude/Grok scores in SHADOW mode only
   strategies: momentum_volume, breakout, buy_pressure, migration_momentum
   all-in cost estimate, size under fixed limits, reward/risk after costs, edge calibration from own trades
        |  accept / reject with reasons
        v
 Agent 4 PAPER EXECUTOR + EXIT MANAGER
   risk gateway -> unique intent -> latency -> fresh quote? route? price ran? thin pool (partial)?
   -> simulated fill with all costs -> monitor -> exits (liquidity, stop, vol stop, trailing, target,
   momentum reversal, time, emergency flatten) at OBSERVED prices -> unsellable -> write-off
        |
        v
 report: net P&L after costs, funnel, rejections, failure rates, breakdowns, naive benchmark, AI shadow comparison
```

## 2. Modules

| File | Responsibility |
|---|---|
| `config.py` | `lab.toml` schema. Mode is `paper` and nothing else exists. Separate data directory, halt file and limits |
| `models.py` | Snapshot (with `observed_ts`), mint facts, route check, opportunity, assessment, proposal, position |
| `ledger.py` | SQLite ledger. Unique intent keys prevent duplicate paper orders; illegal intent transitions raise; atomic commits |
| `risk.py` | Deterministic gateway: position, exposure, open-position count, daily entries, token cooldown, daily loss, drawdown (writes the halt), loss-streak pause, lab halt, the main bot's `data/KILL` |
| `costs.py` | DEX fee by venue, constant-product impact at observed liquidity, Token-2022 transfer fee, network fee, token-account rent, adverse selection, 1x/2x/3x stress |
| `sources/` | `dexscreener.py`, `geckoterminal.py`, `onchain.py` (RPC + Jupiter, no taker), `synthetic.py` (test market, optional zero-edge mode), `replay.py` (point-in-time replay of recorded data) |
| `agents/` | `scanner.py`, `security.py`, `analyst.py`, `executor.py` |
| `ai/scorers.py` | Optional Claude and Grok shadow scorers with sanitised numeric inputs and a daily call and spend budget |
| `engine.py` | Orchestration, single-instance lock, recovery, per-cycle JSON events, end-of-test liquidation |
| `backtest.py` | Walk-forward on recorded data, synthetic Monte Carlo, unsellable injection, cost stress, Bonferroni |
| `report.py` | Metrics and benchmarks |
| `studies/memecoin_signal_study.py` | Pre-registered real-data study on a public dataset |
| `cli.py` | `run`, `scan`, `report`, `backtest`, `halt`, `resume`, `status` |

## 3. Security boundaries (enforced by tests)

| Boundary | How it is enforced |
|---|---|
| No keys, no signing, no submission | The lab imports nothing from `tradebot.execution.{jupiter_adapter, signer_protocol, submission, wallet, jupiter_live, live_gate}`, `tradebot_signer`, the main bot engine or the main ledger, and never imports `Keypair`. `tests/test_opportunity_lab.py::test_lab_cannot_reach_wallets_signers_or_submission` parses every lab file to check this. |
| Quotes cannot create transactions | Jupiter `/order` is called **without `taker`**, so it returns a price quote and no transaction (test inspects the request parameters) |
| The main bot is untouched | Own package, own config, own `data/lab/` directory and SQLite file, own lock and halt file. The only shared file is `data/KILL`, which the lab **reads** to stop new entries; it never writes it |
| AI cannot trade | Scores are attached to an already-decided proposal and never read by the gateway, sizing or exits. Inputs are numbers and a 10-character alphanumeric symbol only, because token names and descriptions are attacker-controlled (prompt injection). Off by default; daily call and spend caps. Tested: an AI score of 0 leaves an accepted trade accepted |
| Untrusted market data | Every source failure returns "unknown", and unknown critical facts reject. A failing source never stops the cycle: the error is recorded and that source is skipped |
| Rate limits | The main bot's `RateLimiter`: a token bucket per API, exponential backoff with jitter, `Retry-After` as a floor, local suppression during backoff, HTTP 429 counted |
| Crash safety | Exits commit trade, closed position and cash in one transaction. Entry intents left open by a crash are abandoned on restart and their reservation released. Open positions and cash reload from the ledger. A second instance is refused by an OS file lock |
| Duplicate orders | Entry intent key `entry:<proposal id>` and exit key `exit:<position id>` are UNIQUE in SQLite |

## 4. Information timing (look-ahead control)

- A snapshot's `observed_ts` is when the lab received it. Decisions only use observations with
  `observed_ts <= now`. Source-provided timestamps are stored but never used for decisions.
- Entries fill after `entry_latency_sec`, using the first observation at or after that time. A missing or stale
  observation fails the entry.
- Exits use the observed price, so a gap through a stop fills below the stop. A position with no sell route or no
  data is written off at zero after `unsellable_writeoff_min`.
- Replay applies the same rules to recorded data. Facts and routes are replayed only from what was recorded before
  the replay time; anything else is unknown, and the security agent rejects it.
- The synthetic market's zero-edge mode (`--null`) checks this end to end. With nothing predictable, the lab must
  lose about its costs. It does (`OPPORTUNITY_LAB_RESULTS.md`).

## 5. Default limits (`[risk]` in `lab.toml`; paper money)

| Limit | Default |
|---|---|
| Paper capital | $1,000 |
| Position | $25 |
| Exposure | $100 |
| Open positions | 4 |
| Daily loss | $50 (stops new entries) |
| Drawdown | $150 (writes the lab halt) |
| Daily entries | 20 |
| Token cooldown | 120 min |
| Loss-streak pause | 4 losses, 240 min |
| Exits | stop 12%, target 30%, trailing 10% (active from +8%), volatility stop 3 sigma, max hold 240 min, momentum reversal −6% (5 min), liquidity drop 30%, unsellable write-off after 60 min |
