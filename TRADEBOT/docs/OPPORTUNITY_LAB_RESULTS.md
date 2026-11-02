# Opportunity Lab: tests, research results and limitations

Date 2026-11-02. **Every result here is a simulation or a historical backtest. None is a real-money or live-paper
result.** The build environment cannot reach any market API, so no live forward data exists yet.

## 1. Evidence classes

| Class | What exists | Where |
|---|---|---|
| Real-money trading | **None** (forbidden; the lab cannot sign or send) | n/a |
| Live forward paper | **None yet**. DexScreener, GeckoTerminal, Jupiter and Solana RPC are blocked from this environment | Starts when the lab runs on your PC or VPS |
| Historical backtest on real data | Pre-registered study on a public dataset of 44,460 memecoin detection snapshots (June 2026) | Section 3; `opportunity_lab/results/real_data_signal_study.json` |
| Synthetic simulation | Monte Carlo over generated markets, including zero-edge markets. Tests mechanics and bias controls only | Section 4; `opportunity_lab/results/synthetic_*.json` |

## 2. Tests

| Suite | Result |
|---|---|
| `tests/test_opportunity_lab.py` | **32 passed** |
| Full repository suite | **299 passed** (267 before this work); `tradebot selftest` 10/10 |
| Planted-bug checks | All caught: security approving unknown facts; exits filling at the stop price instead of the observed gap price |

Coverage of the lab tests:

- **Isolation.** The lab cannot import the wallet, signer, submission switch, live adapter, live gate, main engine or
  main ledger, and never imports `Keypair`. This is checked by parsing every lab file.
- **Quotes.** Jupiter quotes never carry a `taker`.
- **Defaults.** Paper-only, with AI scoring off.
- **Sources.** DexScreener and GeckoTerminal parsing. Token-2022 extensions and transfer fee from RPC `jsonParsed`.
  Holder metrics exclude the pool account. HTTP 429 with `Retry-After` backs off and suppresses calls without raising.
- **Agent 1.** Every detector; ineligible opportunities are recorded with a reason.
- **Agent 2.** 13 rejection cases, including missing facts, mint or freeze authority, transfer hook, transfer fee,
  Jupiter `isSus`, unknown holders, concentration, a whale, honeypot (buy without sell), no route, unknown route and
  round-trip loss. Also: too new, low liquidity, liquidity withdrawal, buyers without sellers, and clean approval.
- **Agent 3.** The cost cap rejects a 160 bps route on $25 because fixed costs alone are 168 bps. A strategy is
  switched off after 30 losing trades.
- **Shadow AI.** A Grok score of 0 leaves an accepted trade accepted; a prompt-injection symbol is reduced to 10
  alphanumeric characters; the budget guard stops calls; without a key, no call is made.
- **Agent 4.** No fresh quote fails the entry; a price that ran fails it; a thin pool gives a partial fill; the same
  proposal cannot be entered twice; a gap through the stop fills at the observed price; no sell route leads to a
  write-off at zero; a liquidity drop triggers an exit.
- **Risk gateway.** Position, exposure, daily loss, the main `data/KILL`, and the lab halt.
- **Crash recovery.** After a simulated crash, positions and cash reload, interrupted entry intents are abandoned,
  there are no duplicate trades, and cash plus open cost equals capital plus realised P&L.
- **Single instance.** A second instance is refused.
- **Replay.** Point-in-time visibility and staleness are enforced; with no recorded facts, the security agent rejects.
- **Halt.** A halt with flatten exits everything and stops new entries.
- **Null market.** On a zero-edge market, expectancy is negative.

**Tested only against recorded or synthetic inputs, not live.** Live DexScreener, GeckoTerminal, Jupiter and RPC
responses; real 429 behaviour; Windows Task Scheduler; Linux systemd.

## 3. Real-data study: do the lab's filters, signals and exits make money? No.

**Data.** [ian05012/solana-memecoin-dataset](https://github.com/ian05012/solana-memecoin-dataset) (MIT). It has
44,460 detection snapshots of freshly graduated pump.fun tokens and GMGN-trending tokens, from 2026-06-10 to
2026-06-29. Features were captured at detection time. 15-minute USD candles cover up to 3 days after detection.

**Pre-registered design.** Fixed before any result was seen; script `opportunity_lab/studies/memecoin_signal_study.py`:

- **Entry:** the first candle that starts at or after detection; one entry per token.
- **Exits:** the lab's defaults (stop −12% with gap fills, target +30%, 240 minutes), with the stop checked before
  the target inside a bar.
- **Costs:** the lab's cost model at liquidity measured at detection.
- **Security filter:** liquidity ≥ L, age ≥ 30 min, top-10 holders ≤ 35%, LP burned ≥ 90%.
- **Variants:** 12 (liquidity floor $20k or $50k, size $25 or $100, signal "all", momentum_flow or strong_flow).
- **Correction:** Bonferroni α = 0.05/12.
- **Windows:** development 06-10 to 06-19, validation 06-20 to 06-24, holdout 06-25 to 06-29.
- **Excluded:** wallet "smart money" tags, which could have been assigned after detection.
- **Units fix:** after checking the field's distribution, and before any result, the momentum threshold was corrected
  to percent units.

**Results**, in bps per trade after modelled costs (x1), with the Bonferroni CI at x2 costs:

| Variant | Development | Validation | Holdout | Verdict |
|---|---|---|---|---|
| liq ≥ $20k, $25, all tokens (naive basket) | n=250, −510; CI x2 [−903, −293] | n=139, −129; [−661, +236] | n=153, −417; [−878, −102] | Rejected |
| liq ≥ $20k, $25, momentum_flow | n=197, −505 | n=88, −216; [−861, +241] | n=105, −368; [−923, +76] | Rejected |
| liq ≥ $20k, $25, strong_flow | n=167, −560 | n=79, −130; [−788, +486] | n=75, −274; [−907, +276] | Rejected |
| liq ≥ $20k, $100, all / momentum_flow / strong_flow | −497 / −490 / −545 | −120 / −207 / −122 | −402 / −352 / −258 | Rejected |
| liq ≥ $50k, any size, any signal | n = 2–25 per window; mixed signs, e.g. validation +234, holdout −796 | | | Rejected (too few trades; no consistent sign) |

**Reading:**

- **Every variant loses money after costs in development and holdout.** The momentum and buy-pressure signals are no
  better than buying every filtered token: the naive basket.
- **The sample flatters the strategies, and they still lose.** It contains only tokens that had already graduated or
  trended, which overstates what a scanner seeing every launch would get.
- **Why they lose:**
  - With a 12% stop and a 30% target, break-even needs about a 29% win rate before costs. Observed win rates were
    21–32%.
  - About three in four exits hit the stop.
  - 15-minute bars hide the intrabar path, so assuming the stop is hit first is conservative but realistic for gaps.
- **Validation was the least bad window everywhere.** No variant passed the pre-registered criteria.

## 4. Synthetic simulations (mechanics and bias controls only)

Pending: the synthetic Monte Carlo runs (normal, zero-edge null, 5% unsellable; 1x/2x/3x costs) are in progress and will be added here.

## 5. Grok: is it worth it?

- **Not measured, and not worth paying for yet.** There is no after-cost evidence that LLMs improve memecoin
  selection (`GROK_STYLE_RESEARCH.md` §1). The lab's deterministic signals show no edge on real data, and a filter on
  top of a losing signal set cannot create profit.
- **Cost:** Grok shadow scoring would cost about $10 per month at 100 tokens a day, or $90–$411 with X search
  (`GROK_STYLE_RESEARCH.md` §4). The integration is built and off.
- **When to consider it:** only after the forward paper record shows a deterministic strategy with positive
  expectancy. Then enable shadow scoring for 30+ scored trades and keep it only if the subset Grok favours beats the
  baseline after costs with a CI excluding zero.

## 6. Limitations

- **Survivorship in the real-data study:** selection of graduated and trending tokens. Our own recorder fixes this
  going forward.
- **15-minute bars:** intrabar order is unknown, so the stop is assumed first.
- **Cost model:**
  - DEX fee 30 bps per leg, impact from constant-product reserves, network $0.01, token-account rent $0.40,
    adverse-selection allowance 50 bps.
  - Rent is counted as a cost although it is refundable when the account is closed. This is conservative for $25 sizes.
  - MEV (sandwich) losses beyond the adverse allowance are not modelled; research shows they concentrate on
    pump.fun-curve and PumpSwap trades.
- **Holder concentration** excludes the single largest account as the presumed pool. Wallet clustering (bundles,
  snipers) is not yet implemented.
- **Synthetic profits are meaningless** and are reported only to show the machinery works.
