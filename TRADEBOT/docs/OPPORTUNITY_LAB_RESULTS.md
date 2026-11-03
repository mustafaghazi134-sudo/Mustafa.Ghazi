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

These runs use synthetic markets of 80 tokens over 24 h, polled every 30 s, with the full four-agent pipeline.

**About 1 in 3 synthetic tokens is a trap:** honeypot, rug, live mint authority, live freeze authority or fee trap. The
"pump_dump" archetype is given a built-in upward drift during its pump, so the normal market contains an engineered,
exploitable pattern. **A profit on it shows the machinery works, nothing about real markets.**

Commands (results in `opportunity_lab/results/synthetic_*.json`):

```bash
python -m opportunity_lab backtest --seeds 12 --seed 200 --hours 24 --tokens 80 --stress 1 2 3
```

```bash
python -m opportunity_lab backtest --null --seeds 20 --seed 100 --hours 24 --tokens 80 --stress 1 2 3
```

```bash
python -m opportunity_lab backtest --seeds 12 --seed 200 --hours 24 --tokens 80 --stress 1 --unsellable 0.05
```

| Run | Seeds | Trades | Expectancy, bps per trade (95% CI) | Win rate | Trades on trap tokens |
|---|---|---|---|---|---|
| Normal market, x1 costs | 12 | 98 | +2,706 [+2,591, +2,804] (engineered pumps) | 100% | **0** |
| Normal market, x2 and x3 costs | 12 | **0** | n/a: every proposal exceeds the 300 bps cost cap | n/a | 0 |
| **Zero-edge (null) market, x1** | 20 | 130 | **−167 [−340, +21]** | 28% | **0** |
| Null market, x2 and x3 | 20 | 0 | n/a (cost cap) | n/a | 0 |
| Normal market with 5% of held tokens losing their sell route | 12 | 98 | +2,183 [+1,610, +2,646] | 96% | 0 |

**What these show:**

1. **No look-ahead or fill bias found.** On markets where nothing is predictable, the lab loses about its modelled
   costs, and the point estimate is negative. An earlier 16-seed, 12-hour null run gave −396 bps [−586, −176].
   `tests/test_opportunity_lab.py::test_null_market_does_not_produce_profit` keeps this check in the suite.
2. **The security agent stops every trap that reaches it.** Across 44 market simulations, no paper trade was opened
   on a honeypot, live-authority or fee-trap token. Most traps never matched a strategy; those that did were rejected,
   honeypots mainly through the missing sell route.
3. **Write-offs bite as designed.** Making 5% of held tokens unsellable cut the expectancy by about 520 bps, from
   +2,706 to +2,183 bps.
4. **Cost stress acts as a gate, not just a haircut.** At 2x costs, fixed costs on a $25 position alone reach about
   336 bps, which exceeds the 300 bps cap, so the lab does not trade at all. The real-data study (section 3) is
   where the same trades are re-priced at 2x and 3x.

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
