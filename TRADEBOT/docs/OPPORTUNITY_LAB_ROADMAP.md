# Opportunity Lab: roadmap to a credible forward-tested system

Current state, 2026-11-02:

- The four-agent paper lab works end to end on synthetic and replayed data, with 32 tests passing.
- On a zero-edge synthetic market it loses about its costs, as it must, so the harness shows no look-ahead bias.
- A pre-registered real-data study rejected every tested signal, even on a sample biased in the signals' favour.
- No live data has been collected yet, because this build environment cannot reach the market APIs.

**Nothing here is close to real-money readiness.** The order below is deliberate: evidence first, then features.

## Phase 1: start collecting forward evidence (now, on your PC or a VPS)

| # | Step | Done when |
|---|---|---|
| 1 | Run `python -m opportunity_lab scan --minutes 10` and fix any live-schema mismatch in the source parsers | The scan shows tokens, security verdicts and no repeating `source_error` |
| 2 | Run `python -m opportunity_lab run --source live` continuously, with `require_calibrated_edge = false` (exploration) | At least 30 days of snapshots recorded |
| 3 | Check rate-limit counters weekly; add a free Helius key if the public RPC throttles | `rate_limited_429` near zero |
| 4 | Keep every `data/lab/lab.sqlite` | The archive is the survivorship-free dataset, since it records tokens that later died |

## Phase 2: validate on our own recorded data (after about 30 days)

| # | Step | Done when |
|---|---|---|
| 5 | `python -m opportunity_lab backtest --replay-db data/lab/lab.sqlite`: walk-forward 50/25/25, Bonferroni across variants, 1x/2x/3x costs, 5% injected unsellable rate | Report saved |
| 6 | Pre-register any new variant in `OPPORTUNITY_LAB_RESULTS.md` **before** running it; record every holdout look (`data/lab/holdout_looks.jsonl`) | No holdout used twice |
| 7 | Compare paper trades with the naive basket and with cash | A strategy must beat both after 2x costs |
| 8 | If Grok or Claude shadow scoring is enabled: compare the AI-favoured subset with all trades on at least 30 scored trades | Keep paying only if the subset's after-cost expectancy is higher and its interval excludes zero |

**Promising only if:** validation and holdout expectancy CI lower bound > 0 at 2x costs, at least 30 trades in each,
profit factor ≥ 1.3, and better than the naive basket. Even then this is "promising", not proven. The next step is
more forward paper trading, not money.

## Phase 3: close known data gaps (in parallel; improves the security agent)

| Gap | Approach |
|---|---|
| Launch cohort completeness: we only see what DexScreener and GeckoTerminal list | On-chain recorder of pump.fun creates and PumpSwap pool creations (program logs, IDLs from the pump.fun public docs), with block time, slot and arrival time |
| Bundled and sniper wallets, funding-source clusters | Helius parsed transactions on the first buyers; link wallets funded from one source; recompute holder concentration over linked groups |
| "Observed sells by unrelated wallets" (honeypot defence beyond quotes) | Count successful sells by wallets outside the creator's funding cluster |
| LP lock and burn | RugCheck (if its API proves keyless and stable) or pool-account reads |
| Independent second opinion | GoPlus Solana token security (free tier) for transfer-hook upgradability, mutable balance authority and closable |
| Historical price paths for replay | GeckoTerminal OHLCV per pool (keyless about 5/min; under 6 months free) for tokens the scanner recorded |

## Phase 4: before any real-money discussion (not planned; listed so nothing is skipped)

1. At least 3 months of forward paper results meeting the Phase 2 criteria on data not used to design the strategy.
2. Realised paper costs reconciled with real Jupiter quotes for the same sizes. This uses the main bot's existing
   read-only quote tooling.
3. The strategy moved behind the main bot's existing live controls: separate signer service, build-level submission
   switch, owner activation, $10 per order. The lab itself never gains signing ability.
4. Explicit written authorisation from the owner. Mainnet activity remains forbidden until then.

## Not recommended, and why

- **Raising trade counts, lowering costs or loosening filters to get more trades.** Costs are what kill these
  strategies; the real-data study lost money even on biased data.
- **Letting Grok or Claude pick or size trades.** There is no after-cost evidence for it, and prompt injection
  through token metadata is a documented attack.
- **Trading pump.fun bonding-curve tokens younger than 30 minutes.** Sniping, bundling and sandwiching dominate there,
  and 96% of measured sandwiches hit that venue.
- **Running third-party "Grok bots".** None in the candidate list trades on Solana, and the popular "Solana sniper"
  alternative found during the search ships an install-time key stealer.
