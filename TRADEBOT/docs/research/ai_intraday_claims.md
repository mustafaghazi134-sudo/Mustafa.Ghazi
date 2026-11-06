# LLMs for intraday trading: what people do, and how credible the profit claims are

Research date: 2026-11-03. Read-only desk research. Nothing was signed up for, bought, or run.

**How the sources were read.** WebFetch and direct HTTP access to nof1.ai, arxiv.org, X and news sites were blocked from this container. Papers, X posts and news articles were therefore read through search-engine excerpts. GitHub READMEs were read in full through raw.githubusercontent.com.

**Labels**
- **[PRIMARY]** means the original source: an official post, the paper itself, the repository README, or the author's own post.
- **[SECONDARY]** means press, blogs or aggregators.
- **(excerpt)** means a primary source was seen only through a search excerpt, not read in full.

Many dollar figures differ between outlets because they were taken at different snapshot times. The report shows ranges where they conflict.

---

## (a) Viral profit claims, traced to their sources

| # | Claim | Original source (as far as it could be traced) | Evidence shown | Live / paper / backtest | Independently verifiable? | Commercial incentive | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | "Claude turns $2,000 into $12,000 overnight on Polymarket" (Apr 2026) | Trader "Mary Evan", reported by Finbold: https://finbold.com/claude-turns-2000-to-12000-overnight-on-polymarket-here-is-how/ [SECONDARY] | Self-reported, with screenshots. No wallet address. Claude built a wallet-monitoring terminal; trades were copied through a Telegram copy-trading bot. | Live (claimed), 1 night | No. The copied wallets are not named. | Yes: a Telegram copy-bot funnel. Crypto media coverage drives traffic. | **Unverified / promotional.** Claude wrote tooling; profitable wallets made the bets. A critique is at https://medium.com/@info_38412/claude-didnt-earn-10k-overnight-and-that-isn-t-how-markets-work-b2bb2717aa76 [SECONDARY] |
| 2 | Claude bot turns $1,000 into $14,216 in 48 hours (+1,322%), while an "OpenClaw" bot was liquidated (Mar 2026) | Only reposts were found (Cryptopolitan, MEXC news, laikalabs guide). Original X poster not identified. [SECONDARY] | Headline numbers only. Coverage says "on-chain verification and strategy disclosure did not follow". | Live (claimed), 48 hours | No | Unknown (original not found) | **Unverified** |
| 3 | Student's Claude bot: $1,430 into $238,006 in 11 days, 366 trades, 62% win rate | Threads/Instagram "businessbulls"; repeated by letsdatascience.com [SECONDARY] | Anecdote. No wallet address. | Live (claimed) | Possible in principle, because Polymarket settles on-chain, but no address was given | Engagement-farming accounts | **Unverified** |
| 4 | "Claude-powered bot turned $1 into $3.3M on Polymarket" (Aug 2025 to Apr 2026) | Mario Nawfal X post of Apr 3, 2026, per https://www.cryptotimes.io/2026/04/03/claude-powered-bot-turned-1-into-3-3m-on-polymarket/ [SECONDARY] | Video of a wallet's P&L. One paused frame shows a $868,862 wallet, which does not match $3.3M. | Live | The wallet P&L may be checkable on-chain. The "$1 start" and "Claude-powered" parts cannot be checked. | Influencer reach | **Unverified.** The LLM attribution in particular has no evidence. |
| 5 | "A Chinese student built a Claude bot… up $385K in 3 weeks, 17,000 trades, mostly BTC 5- and 15-minute markets" | https://x.com/kirillk_web3/status/2037561424659021852 [PRIMARY (excerpt)] | Link to a Polymarket trader profile, which is on-chain | Live | The wallet P&L is plausibly real (on-chain). The claim that Claude makes the decisions is not verifiable. | Yes: the same post links a "Fast Copy Trading Bot" | **Promotional.** It is at most partially verified, for the P&L only. |
| 6 | Grok prompts: $1,000 into about $16,000 in about 10 weeks on prediction markets | X article by @AleiahLock, mirrored at https://youmind.com/zh-CN/landing/x-viral-articles/grok-prediction-market-prompts-strategy ; coverage at https://journalducoin.com/technologies/ia-marches-predicitifs-trader-1-000-16-000-grok/ [SECONDARY] | Narrative only. No trade records. Manual prompting, not a bot. | Live (claimed) | No | Yes: the prompt text contains a **1win gambling-affiliate referral link** | **Promotional** |
| 7 | DeepSeek bot: 0.1 SOL into 52 SOL (about $12.5k) in 5 days of Pump.fun memecoin trading (Jan 28, 2025) | X user "Nonzee", reported by https://es.beincrypto.com/inversor-programa-deepseek-ganar-criptomonedas/ [SECONDARY] | Self-reported. No wallet in the coverage. | Live (claimed) | No | DeepSeek hype window. More than 75 scam "DeepSeek" tokens appeared the same week (https://decrypt.co/303037/scammy-deepseek-ai-crypto-tokens-solana-ethereum) | **Unverified** |
| 8 | "Redditor's $100 ChatGPT trading experiment trounces benchmarks with 24% return" (Aug 2025) | Nathan Smith's Substack and GitHub, LuckyOne7777/ChatGPT-Micro-Cap-Experiment [PRIMARY] | Public weekly logs, trade CSVs, and an evaluation PDF | **Live, real money** (micro-cap stocks; daily/weekly, not intraday) | Largely yes: logs were published forward-only | Low (a research project; links his LIBB framework) | **Verified, and the headline reversed.** It was +24% around week 4. At week 26 the account was **$82.88 (-17%)**, with max drawdown -50.33% and a negative Sharpe ratio (https://hackernoon.com/can-chatgpt-outperform-the-market-week-26) [PRIMARY (excerpt)]. Week 16: portfolio $80.23 against the S&P 500 at $107.40. |
| 9 | Redditor: ChatGPT and Grok "doubled my portfolio in days", average 23% per trade | https://www.financemagnates.com/trending/redditor-claims-chatgpt-is-his-personal-trade-whisperer/ [SECONDARY] | A Reddit post | Live (claimed) | No | Unknown | **Unverified** |
| 10 | Polymarket wallet "0x8dxd": $313 into $414k–$550k in about a month on BTC 15-minute markets | https://beincrypto.com/arbitrage-bots-polymarket-humans/ [SECONDARY] | A public Polymarket wallet | Live | Partly: the wallet is on-chain, but the outlets disagree on the ending figure | Copied heavily by bot sellers | **Partially verified, but NOT an LLM bot.** It is a latency-arbitrage bot that exploits a reported 20–35 ms lag behind spot prices. It is often misattributed to "AI/Claude". |
| 11 | "Grok 4.20 turned $10,000 into $12,000 in 2 weeks" (Alpha Arena 1.5) | nof1 leaderboard; https://news.aibase.com/news/23442 [SECONDARY] | Public live dashboard on Hyperliquid | Live, real money | Mostly: run on Hyperliquid and published by nof1 | nof1 is a venture-funded startup (reportedly raised $15M); xAI promotion | **Partially verified.** The return is real, but it is one aggregate out of 32 accounts in a contest where most lost (see b). |
| 12 | DeepSeek "LLM-trader-test" bot: "+17% results mentioned in recent articles" | README of https://github.com/kojott/LLM-trader-test [PRIMARY] | Dashboard at llmtest.coininspector.pro | Paper trading. Hyperliquid live mode is "code-complete but needs battle-testing". | No | Yes: the "+17%" prompts are **Patreon-only** | **Promotional** |

**Pattern across items 1–7.** No claim came with a broker statement, an audited wallet tied to the claim, or a reproducible trade log. Several posts carry a referral, copy-bot or affiliate link.

Where an on-chain wallet does exist (items 5 and 10), two problems remain:
- What the trader had to gain from the post is unknown.
- The "LLM did the trading" part is never shown. Usually the LLM wrote code for an arbitrage or copy-trading bot.

Population base rates on Polymarket:
- About 84.1% of 2.5M wallets are unprofitable (Sergeenkov/The Defiant, 2026) [SECONDARY].
- 0.55% of maker wallets and 0.26% of taker wallets captured half of all profits, Dec 2025 to Feb 2026 (Solidus Labs via CoinDesk) [SECONDARY].
- 14 of the top 20 earners are bots [SECONDARY].

---

## (b) nof1.ai Alpha Arena: the facts

### Season 1 (crypto). About Oct 17/18 to Nov 3, 2025, roughly 17 days

- **Venue and instruments:** Hyperliquid perpetuals on BTC, ETH, SOL, BNB, DOGE and XRP [SECONDARY: bitcoin.com, blocmates, cryptomania].
- **Capital:** $10,000 of real money per model, six models, $60,000 in total [SECONDARY, consistent across sources].
- **Prompt and inputs:** Every model got the same prompt and the same numeric market data (prices, volume, indicators, account state). No news input was reported [SECONDARY].
- **Rules:** Each model chose its own leverage, sizing, stop-loss/take-profit and an "invalidation condition", with no human intervention.
- **Inference cadence:** Roughly every 2–3 minutes, with leverage allowed from 1x to 20x. These come from community *reconstructions* of the prompt, not an official release (gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83) [SECONDARY]. A Biteye researcher reportedly found the prompt embedded in the web page (Odaily) [SECONDARY].
- **Transparency:** The live dashboard showed account value, P&L, fees, win rate, Sharpe, every trade and the model "chat" (reasoning). The accounts were real Hyperliquid accounts, and community tools such as wquguru/nof0 `snapshot:nof1` and Dixter999/nof1-tracker scraped nof1's API [PRIMARY: those READMEs]. **No official list of wallet addresses was found**, and on-chain reconciliation was not independently confirmed in this research.
- **Final standings**, per a forklog list attributed to founder Jay Azhang; other outlets differ by snapshot [SECONDARY]:

| Rank | Model | Final value | Return | Notes |
|---|---|---|---|---|
| 1 | Qwen3 Max | $12,231.82 | +22.3% | About 43 trades, ~30% win rate, concentrated BTC positions at up to about 25x [SECONDARY] |
| 2 | DeepSeek Chat V3.1 | $10,489 | +4.9% | Led most of the season; peaked above +35% early [SECONDARY] |
| 3 | Claude Sonnet 4.5 | $5,799 | -42% | Reported as nearly 100% long [SECONDARY] |
| 4 | Gemini 2.5 Pro | $5,445 (other sources: -56.7%) | -46% to -57% | **About 238 trades and about $1,331 in fees (over 13% of starting capital)** [SECONDARY] |
| 5 | Grok 4 | $4,208 | -58% | |
| 6 | GPT-5 | $4,126 (other sources: $3,733) | -59% to -63% | |

- **Aggregate result:** About $42.3k left of $60k, roughly **-30%** across the field. This is my own sum of the secondary figures above.
- **Official framing:** Azhang's announcement called Season 1 "our first experiment in understanding how LLMs handle the noisy, adversarial, non-stationary world of markets" (https://x.com/jay_azhang/status/1985481491078328621) [PRIMARY (excerpt)].
- **What analysts concluded** [SECONDARY]:
  - The sample is too small: 6 agents, 17 days, one market regime, about 40–240 trades each.
  - There was no significance testing and no replication.
  - Fees and overtrading explain a large share of the losses. Models in drawdown had made 84–100 trades by mid-season, against 20–40 for the leaders.
  - Early leaders regressed toward the mean.
  - Low trade count plus high leverage on a single BTC position made the winner look like a concentrated directional bet. That is not evidence of skill.
  - Early in the season, BTC buy-and-hold beat most models (https://www.okx.com/learn/alphaarena-model-launch-ai-crypto-trading ; https://www.buildix.trade/blog/nof1-alpha-arena-lessons-ai-trading-byok-strategy-advisor-2026).

### Season 1.5 (US equities). About Nov 19/20 to early Dec 2025

- **Instruments:** US stock exposure, reported as tokenized/perp equities via trade.xyz (Hyperliquid HIP-3) [SECONDARY].
- **Models:** 8, including GPT-5.1, Gemini 3 Pro, DeepSeek V3.1, Kimi K2 Thinking, Qwen3 Max, Claude Sonnet 4.5, Grok 4, and a "mystery model" later revealed as Grok 4.20 [SECONDARY].
- **Four parallel modes, $10k each:** "New Baseline", "Monk Mode" (minimal instructions), "Situational Awareness" (models see the rankings) and "Max Leverage". That gives **32 accounts and about $320k** [SECONDARY: gncrypto].
- **Result:** Grok 4.20 ranked first, with an aggregate return of +12.11% (about $12.2k per $10k). A mid-season snapshot showed +22.38% [SECONDARY: kucoin, techbloat, aibase]. Grok variants held 4 of the top 6 to 10 places.
- **Across all runs, AI models finished in profit in only about 6 of 32 runs** [SECONDARY: Bloomberg-derived write-ups]. Treat the winner's return as **selection from a mostly losing field**.

### 2026

- **Bloomberg, May 6, 2026**, "AI Bots Auditioning for Wall Street Trading Are Mostly Losing" (https://www.bloomberg.com/news/articles/2026-05-06/ai-bots-auditioning-for-wall-street-trading-are-mostly-losing) [SECONDARY, quoting PRIMARY]. Its findings:
  - Most systems lose money and trade too much.
  - They make "wildly different decisions when given identical instructions".
  - Azhang is quoted: "LLMs can't really make money by themselves" without "a very sophisticated harness and scaffolding and data platform".
  - **Season 2** was announced to add web search, longer deliberation, more data and multi-step actions.
- **No Season 2 results were found** as of this research.
- An "Alpha Arena S03" Bali final (Aug 2026, $100k prize, MEXC-backed) was a **human demo-trading** event. Its link to nof1 is unclear, and it is not LLM evidence [SECONDARY].

---

## (c) Open-source implementations actually used

READMEs read in full. Star counts are from the GitHub search API on 2026-11-03.

| Repo (stars) | What the LLM does in the loop | Cadence / latency | Execution | Published LIVE track record? | Notes |
|---|---|---|---|---|---|
| TauricResearch/TradingAgents (~120k) | Analyst agents for fundamentals, sentiment (news, StockTwits, Reddit), news and technicals. Then a bull/bear researcher debate, a trader agent, a risk team, and a portfolio manager who approves or rejects. The LLM does **signal + sizing + risk**. | One decision per ticker per date, with many LLM calls (minutes per decision). Daily, **not intraday**. | "Simulated exchange"; backtest grid since v0.5 | **No.** README: "designed for research purposes", "not … a strategy" [PRIMARY] | Paper (arXiv 2412.20138) headline: AAPL +26.62%, Sharpe 8.21. An independent recomputation says the paper's buy-and-hold baseline (-5.23%) should be about **+9.1%** (https://dev.to/trow126/the-most-starred-llm-trading-paper-claims-buy-and-hold-lost-523-it-actually-gained-912-1jj6) [SECONDARY] |
| virattt/ai-hedge-fund (~64k) | "Investor persona" agents and analysts produce signals; risk and portfolio manager agents size positions | Daily sessions | Paper funds and backtests | **No.** "Educational… not intended for real trading" [PRIMARY] | |
| NoFxAiOS/nofx (~13k) | **Frontier**: the LLM proposes trade plans (entry, stop-loss, targets, leverage, size) and a human approves each order. **Autopilot**: the LLM decides and routes orders autonomously. | Minutes-level loops | Live on Binance, Hyperliquid, OKX and Bitget (Bybit, Gate, Aster and Lighter in the runtime) | No audited record. It links a "live dashboard" at vergex.trade. | README has **exchange referral links** (Binance, Hyperliquid, Bitget, Aster, Lighter) and paid "Claw402" data routes [PRIMARY]. Commercial incentive. |
| wquguru/nof0 (~2.8k), Alpha Arena clone | Prompt-centric arena in which each LLM outputs trades on Hyperliquid | Alpha-Arena style (minutes) | Backend "70%" complete | **No** [PRIMARY] | Mainly a front-end plus a scraper of nof1 data |
| 195440/nof1.ai (~0.7k) | Autonomous LLM agent picks direction and leverage. Hard-coded stops: account stop, -30% extreme stop, drawdown at 20/30/50% for warn / no new positions / force close. | `TRADING_INTERVAL_MINUTES=20` | Live exchange APIs | No | Telegram group offers a "**60% fee rebate**", i.e. referral income [PRIMARY] |
| kojott/LLM-trader-test (~0.1k) | DeepSeek reads 4H/1H/15M indicators and outputs entry, stop-loss, take-profit and risk of 1–2% | Every 15 minutes | Paper, against Binance data. Hyperliquid mainnet is in beta. | Dashboard only; the author asks volunteers to test with real money | Best prompts are Patreon-gated (see a.12) |
| Polymarket/agents (~3.8k, official) | Retrieval over news, betting data and web search, then an LLM forecast or market selection, then a CLOB order | Seconds to minutes per call. Repo last pushed Nov 2024. | Live Polymarket API | **No** [PRIMARY] | A framework, not a strategy |
| ryanfrigo/kalshi-ai-trading-bot (~0.6k) | One LLM call per decision gives a directional probability. Quarter-Kelly sizing, a 3% max position, 10% daily-loss and 15% drawdown kill switches. | `--interval 300` to 900 s | Live Kalshi orders | **Yes, and it is negative.** First forward-only verdict on the operator's live account (Oct 2, 2026, n=55): "**NO MEASURED EDGE**", won 58% against 60% implied (edge -1.9 points), Brier 0.2356 [PRIMARY] | Rare honest track record. README admits the earlier "5-model ensemble" claim was false. |
| HKUDS/AI-Trader (~23k) | Agent-native platform: agents post signals and paper-trade ($100k paper; Polymarket paper since Mar 2026) | Varies | Simulated | **Paper only** [PRIMARY] | Companion benchmark paper in (d) |

**Where the LLM sits in practice**
1. **Discretionary signal generator on a minutes-scale timer.** Alpha Arena and its clones work this way. The LLM reads indicator snapshots and emits direction, leverage and stops; deterministic code executes.
2. **Research or debate layer at daily frequency.** TradingAgents and ai-hedge-fund work this way.
3. **Code author for non-LLM bots.** This is the most common pattern behind the Polymarket "Claude bot" stories: arbitrage, latency or copy-trading bots, where the edge is in milliseconds and the LLM is not in the hot path.

LLM inference takes seconds, so LLMs cannot compete in the sub-second Polymarket latency niche (reported lags of about 20–35 ms) [SECONDARY].

---

## (d) Rigorous evidence, 2024–2026

| Study | Design | Key finding |
|---|---|---|
| **Prediction Arena**, arXiv 2604.07355 [PRIMARY (excerpt)] | 6 frontier models traded **real capital** on Kalshi and Polymarket for 57 days (Jan 12 to Mar 9, 2026) | Kalshi: average **-22.6%**, range -16.0% to -30.8%. Polymarket: average about -1.1%. |
| **LiveTradeBench**, arXiv 2511.03628 (UIUC) [PRIMARY (excerpt)] | 21 LLMs, **live** for 50 trading days (Aug 18 to Oct 24, 2025), on US stocks and Polymarket; agents output allocations | Rank on general benchmarks does not predict trading (Spearman ρ about 0.05 for stocks and **-0.38** for Polymarket against LMArena score). Models show stable "styles". Costs and turnover are not reported. |
| **What LLM Trading Agents Actually Do in Production**, arXiv 2609.05663 (DXRG) [PRIMARY: repo README] | 3,505 user-funded vaults (Qwen3-235B, Base memecoins, 21 days, about $20M volume). Plus 500–599 agents on Hyperliquid perps for 69 days: 231,638 turns, 14,596 fills, **mostly paper at live prices**. | **No directional edge at fleet level.** Sizing ignores volatility: median leverage is 5.0x in every volatility bucket. 43% of positions reached +300 bps but **49% of those closed negative** (median capture 2%). **A fixed 2%/4% bracket beat every LLM discretionary exit** by +39 bps per position. A leaderboard shown in the prompt drove 46.5% of entries. Three frontier models tied on decision quality but differed widely on consistency (35% vs 90%+ flip rate). Caveat: the authors' lab sells the product. |
| **StockBench**, arXiv 2510.02209 [PRIMARY (excerpt)] | Daily trading of top DJIA stocks with prices, fundamentals and news, in a post-cutoff window | **Most LLM agents fail to beat buy-and-hold** on return and risk-adjusted return. Financial QA skill does not transfer to trading. |
| **AI-Trader**, arXiv 2512.10971 (HKU) [PRIMARY (excerpt)] | 6 LLMs, live, on US stocks, A-shares and crypto at several frequencies | "General intelligence does not automatically translate to effective trading". Returns were poor and risk management weak when fully autonomous. |
| **Agent Market Arena / "When Agents Trade"**, arXiv 2510.11695 [PRIMARY (excerpt)] | Lifelong live paper-trading benchmark on stocks and crypto | Agent architecture matters more than backbone (figures not retrieved) |
| **FINSABER**, arXiv 2505.07078 [PRIMARY (excerpt)] | Backtests over 2000–2024 on a broad universe, controlling survivorship, look-ahead and data snooping | Previously reported LLM advantages **"deteriorate significantly"**. LLMs were too conservative in bull markets and too aggressive in bear markets. |
| **Profit Mirage**, arXiv 2510.07920 [PRIMARY (excerpt)] | Leakage audit plus FinLake-Bench (2,000 "did the market rise on date T?" QA pairs) | Strong backtests **collapse after the model's knowledge cutoff**; this is attributed to memorized outcomes |
| **Detecting Lookahead Bias in LLM Forecasts**, arXiv 2512.23847 [PRIMARY (excerpt)] | Memorization probe on the headline-to-next-day-return setup | Memorization accounts for about **37%** of the apparent LLM forecasting effect |
| **Look-Ahead-Bench**, arXiv 2601.13770 [PRIMARY (excerpt)] | Compares standard and point-in-time LLMs in-sample (2021) against out-of-sample (2024) | Standard LLMs show alpha decay out of sample; point-in-time models are more stable |

**Implications for the claims above**
- Any LLM **backtest** over a period before the model's training cutoff is contaminated by default. Only forward or live results count.
- Intraday crypto adds three more problems: fee drag, funding costs, and leverage-driven ruin. All three dominated Alpha Arena losses.
- No study found **net-of-cost** outperformance by LLM agents over simple baselines that holds up across a period long enough to matter (months, multiple regimes, many agents).

---

## (e) Conclusions

**Verified (documented live data or published papers)**
- **Live LLM trading contests mostly lose money.** Alpha Arena S1: 4 of 6 models lost 42–63%, and the field lost about -30% in aggregate. S1.5: roughly 6 of 32 runs were profitable. Prediction Arena: -16% to -31% on Kalshi over 57 days. Several of these figures come from secondary reporting of public dashboards.
- **The winners look like concentrated, leveraged bets, and the samples are tiny.** Qwen3 Max won S1 with about 43 trades in 17 days; Grok 4.20 won S1.5 out of 32 accounts. Neither result separates skill from luck. Neither organizer published a significance test.
- **Overtrading and fees are a measured failure mode.** The highest-frequency model (Gemini 2.5 Pro, about 238 trades) paid fees of about 13% of capital. DXRG's production data shows LLM sizing ignores volatility, and simple fixed brackets beat LLM exits.
- **General model quality does not predict trading results** (LiveTradeBench, AI-Trader, StockBench).
- **Backtested LLM edges largely disappear under leakage controls** (FINSABER, Profit Mirage, lookahead-bias studies). Even the most-starred framework's headline backtest has a disputed baseline.
- **The few honest live records are negative or flat.** Examples: the Kalshi bot's "no measured edge" verdict (n=55), the ChatGPT micro-cap experiment ending at -17% after viral "+24%" headlines, and DXRG's "no directional edge at fleet level".

**Claimed only**
- **None of the viral "$1k into $12k–$238k" LLM-bot claims has been independently verified.** At least 7 distinct claims were traced. Original sources rarely give a wallet, and when they do (a) the LLM's role is unproven and (b) the post often carries a copy-bot, affiliate or Patreon link.
- **The profitable bots that are real (on-chain) are mostly not LLM traders.** The verifiable Polymarket winners (for example 0x8dxd) are latency-arbitrage bots working in milliseconds. In those stories the LLM, if involved at all, wrote the code.

**Bottom line.** Use LLMs in intraday trading as research, coding and feature tools inside a deterministic, cost-aware risk framework. Treat any profit claim without a forward-only live record (wallet or broker statement, months long, net of fees) as marketing.
