# Intraday direction: independent recommendation

Date 2026-11-03.

| Source of evidence | Location |
|---|---|
| Intraday edge-ceiling test (pre-registered, run once) | `docs/INTRADAY_EDGE_CEILING_PREREG.md`, `research_lab/results/intraday_edge_ceiling.json` |
| Earlier intraday audit | `research_lab/results/strategy_audit.json` |
| Long/short study | `docs/FEASIBILITY_LONG_SHORT.md` |
| Survey of claims about AI trading | `docs/research/ai_intraday_claims.md` |

## 1. The real problem

The obstacle is not the AI model, the tooling or the timeframe. It is **cost compared with the move that can be
predicted**.

| Route | Round-trip cost | Predictable move per trade (BTC/ETH/SOL, public 1-minute data) |
|---|---|---|
| Jupiter spot (current) | 25–120 bps, measured | — |
| Perpetual taker (Hyperliquid base tier) | about 11 bps | — |
| Perpetual maker | about 3 bps, before adverse selection | — |
| Edge-ceiling test, held-out 2025–26: 7 features (order-flow imbalance, short-term returns, volatility bursts, funding) × 5 minutes to 4 hours | — | **0 to 1.3 bps**, even in the most extreme 10% of each feature. The effect is real and stable: a small short-horizon reversal, present in both train and holdout. It is an order of magnitude smaller than cost. **0 of 28 tests pass**, including the maker bound. |
| Earlier audit: breakout, trend and dip rules on 1-minute to 15-minute bars | — | Gross 0–13 bps per trade |

**Seconds-scale trading** is the domain of co-located market makers and latency arbitrage. The few verifiably
profitable "bots" in public, such as on Polymarket, are millisecond latency-arbitrage systems, not language-model
traders. A home PC in Jordan cannot compete there.

## 2. Evidence on Claude, Grok and other AI systems for intraday trading

Full table and sources: `docs/research/ai_intraday_claims.md`.

### Alpha Arena Season 1 (nof1, 18 Oct – 3 Nov 2025)

- **Setup:** six language models, $10,000 of real money each, Hyperliquid perpetuals.
- **Results:**
  - Qwen3 Max won, ending at about **$12,232 (+22%)** after concentrated, leveraged BTC positions on about 43 trades.
  - DeepSeek ended at about +5%.
  - Claude, Gemini, Grok and GPT-5 lost about 42–63%. The field as a whole lost about 30%.
- **No significance testing; 17 days.** This is probably the "$10k→$12k" story. It is one lucky, leveraged run in a
  field that lost money.
- **Later reporting:**
  - Season 1.5, US stocks: about 6 of 32 accounts were profitable.
  - Bloomberg, May 2026, quoting the founder: "LLMs can't really make money by themselves."

### Viral "$1k → $12k–$238k" claims

- Seven were traced to their original posts.
- **None has a broker statement or an attributable wallet** showing that the language model made the trades.
- Several are tied to selling something: copy-trading bots, a gambling-affiliate link, paid prompts.
- **Verdict: unverified or promotional.**

### Research benchmarks

- Prediction Arena: real-money Kalshi accounts lost 16–31% over 57 days.
- LiveTradeBench: 21 models; general benchmark rank does not predict trading results.
- StockBench and AI-Trader: most agents do not beat buy-and-hold.
- Leakage studies (FINSABER, Profit Mirage): backtests of language models over periods before their training cutoff
  are unreliable.

### Conclusion

**A language model in the decision loop is not an edge.** Claude remains useful offline: for research, code, review
and running pre-registered studies. It should not make trading decisions in the execution path.

## 3. What to reuse, change or replace

| Keep and reuse | Change | Stop |
|---|---|---|
| Risk engine pattern: limits, single-use approvals, kill switch, daily halt | Venue adapter: from a Solana DEX to a regulated broker or exchange with short selling | Crypto intraday signals built from public bars. They failed the ceiling test |
| Ledger, intent state machine, reconciliation from evidence, crash recovery | Market data: US equities minute bars and a relative-volume screen | Crypto long/short trend. Short legs lost in every candidate |
| Telemetry, paper broker, the forward-evidence pipeline | Session calendar: NYSE hours, close flat by end of day | A language model in the decision loop |
| Research harness: pre-registration, bootstrap, direction-randomised and no-edge nulls, lookahead tests | — | Memecoin and Opportunity Lab trading (rejected earlier) |
| GitHub Actions bounded jobs for data and evidence | — | — |
| The day-trader-skill tools, installed 2026-11-03: ORB entry, exit and Time Machine for US stocks | — | — |
| The crypto daily tsmom paper run: zero cost, keeps collecting evidence | — | — |

## 4. Recommendation: the most credible route

**Move the intraday effort to US equities.** The strategy is the published 5-minute opening-range breakout (ORB)
limited to "stocks in play", traded long and short through a regulated broker. **Paper first.** The first step is a
test on the period **after publication**.

**Why this route:**

- It is the only intraday long/short strategy found with **published evidence net of commissions**:
  - Zarattini, Barbon and Aziz, about 7,000 US stocks, 2016–2023;
  - top-20 "stocks in play": Sharpe 2.81 and about 36% annual alpha by their own account;
  - it won an award at the University of St. Gallen;
  - **third-party replications agree**: QuantConnect reports Sharpe about 2.4.
- **2024 to 2026 is genuine out-of-sample data for it.** The rules were fixed and published from 2016–2023 data. That
  is the clean test this project has lacked for every other strategy.
- **Costs are a different order:**
  - US stock commissions are about $0.0035 a share, around 1 bp on a $40 stock;
  - "stocks in play" move several percent intraday;
  - so the cost-to-move ratio is roughly 10–50 times better than in crypto.
- **Venue:** a regulated broker with short selling.
  - Interactive Brokers reportedly accepts residents of Jordan [secondary sources; confirm with IBKR].
  - That is legally clearer than an offshore crypto perpetuals exchange, whose status under Jordan's Law No. 14 of 2025
    is unresolved.
- **Tool fit:** the newly installed `trader-scanner`, `trader-entry`, `trader-exit` and `trader-timemachine` skills
  implement this kind of setup for US stocks. They can be used for checking and paper work. TRADEBOT's risk, ledger and
  recovery layers carry over.

**What is uncertain, with how to resolve each:**

| Uncertainty | How to resolve |
|---|---|
| **Decay after publication:** crowding since 2024 | The pre-registered 2024–26 test |
| Data quality and survivorship, including delisted stocks | Use a data source that includes delisted symbols |
| **Slippage at the open** on fast, gapping stocks | Model it pessimistically; measure it in paper trading |
| **Pattern-day-trader rule:** a US margin account under $25,000 is limited to 3 day trades in 5 days. IBKR's pages indicate it applies to its customers for US securities; whether it applies to a non-US resident's account is not confirmed. FINRA proposed replacing the rule in 2025; current status unverified. | Confirm with IBKR |
| IBKR eligibility for Jordan, and any Jordanian rule on foreign brokerage | Confirm with IBKR and local counsel |

If the $25,000 rule applies, **long/short intraday in US stocks needs at least about $25,000 in a margin account**.
Below that, the realistic options are a long-only cash account with settlement limits, or waiting.

### Next practical milestone

1. **Pre-register** the paper's exact ORB rules.
2. **Run a replication on 2024-01 to 2026-10 only**, with:
   - realistic commissions;
   - pessimistic slippage at the open;
   - delisted stocks included;
   - the same no-edge nulls and acceptance rules as before.

| If it passes | If it fails |
|---|---|
| Two to three months of forward paper trading on an IBKR or Alpaca paper account. Only then discuss funded limits | Stop. No intraday route is viable for this setup; keep only the daily crypto rule in paper |

**Data needs:**

- one-minute or five-minute bars for the whole US stock universe since 2024, delisted names included;
- a free route may exist through a broker data API on an account the owner opens;
- otherwise one month of a paid minute-data plan, roughly **$30–200**. This needs the owner's approval.

Engineering: about 1–2 working sessions for the replication. No broker connection or orders are involved.

## 5. Using `/goal`

This Claude Code build has `/goal`. The command's help text confirms it.

| Form | What it does |
|---|---|
| `/goal <condition>` | Sets a condition. A built-in stop hook keeps Claude working until an evaluator judges the condition met; then the goal clears itself |
| `/goal clear` | Ends a goal early |

- **Scope:** it lasts for one session only and needs hooks enabled in a trusted workspace.
- **Use it only for bounded, checkable milestones.** For example:
  `/goal docs/ORB_REPLICATION.md is committed with the pre-registration, 2024–2026 holdout results and a verdict`.
- **Never** give it an open-ended condition such as "until a profitable strategy is found". That makes it search until
  something passes by chance, which is the multiple-testing failure the controls above exist to prevent, and it spends
  usage without limit.
