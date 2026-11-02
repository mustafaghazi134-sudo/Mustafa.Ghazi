# Grok-style trading bots: independent research

Date 2026-11-02. Full appendices:

- [`research/grok_repos_due_diligence.md`](research/grok_repos_due_diligence.md): repositories, alternatives and
  datasets, with file and line evidence.
- [`research/web_sources_2026-11-02.md`](research/web_sources_2026-11-02.md): xAI API, data sources, security
  statistics, frameworks and the full source list.

**How it was done.**

- Repositories were cloned into isolated folders and read statically. Nothing from any of them was installed,
  imported or run. GitHub metadata came only from the search API.
- Web facts come from search-result snippets of the cited pages, retrieved 2026-11-02. This build environment blocks
  direct access to docs.x.ai, DexScreener, Jupiter, GeckoTerminal, Solana RPC and Reddit, so no page was opened and
  no API was called live.
- Every price and limit below should be re-checked on the live page before it is relied on.
- No account was created, no key used and no money spent.

## 1. Bottom line

1. **None of the five suggested repositories trades on Solana in its public code, and none shows any verifiable
   profit.** Two are well-built paper skeletons, one is an agent-instruction pack, one is a careful Hyperliquid (not
   Solana) desk, and one contains no code at all.
2. **A popular "Solana sniper" alternative found during the search is malicious.** `mortdeus/solana-copy-sniper-mev-trading-bot`
   has 4,513 stars. Its npm lockfile pulls an install-script package from a third-party GitHub release that no code
   imports. That is the standard install-time key-stealer pattern. Do not install it.
3. **There is no credible evidence that Grok, or LLMs generally, improve memecoin or new-token selection after costs.**
   - Grok 4 reportedly lost about 58% in Alpha Arena's crypto season (secondary sources).
   - Only 1 of 19 surveyed LLM-trading studies modelled transaction costs.
   - "Grok" tokens and presales are a recurring scam vector.
   - A reported prompt injection made Grok-linked tooling send about $200k.
4. **The base rates are hostile.**
   - About 98.6% of pump.fun tokens showed pump-and-dump or rug characteristics (Solidus Labs, 2025).
   - Only about 0.2–1% graduate.
   - About 96% of 6.85M measured Solana sandwich attacks hit pump.fun-curve or PumpSwap trades (Bitquery).
5. **Decision for the lab:**
   - The core is deterministic.
   - Grok and Claude are optional shadow scorers, off by default, never in the decision path.
   - Grok is worth paying for only if a shadow comparison shows the trades it favours beat the deterministic baseline
     after costs.

## 2. The five candidate repositories

| Repo | Stars, last push, license | Real trades? | AI role | Red flags | Verdict |
|---|---|---|---|---|---|
| [zostaff/grok-trading-desk](https://github.com/zostaff/grok-trading-desk) | 17, 2026-08-30, **no license** | No: crypto executor raises `NotImplementedError`; dry-run records buys without P&L | About 10 Grok agents score; code vetoes and sizes | 22 commits in 45 minutes; asks Grok to judge wallet clusters it is never shown | Ideas only (no license) |
| [zostaff/grokbot-pumpfun](https://github.com/zostaff/grokbot-pumpfun) | 65, 2026-08-27, MIT | No: live executor is a stub; dry-run uses curve math with fee and slippage | 4 Grok agents feed a code scoring matrix | Stale venue model: Raydium instead of PumpSwap, hard-coded 1% fee; uses unofficial pump.fun frontend API | Selective reuse (curve math, risk-limit layout) after correction |
| [hoodpumps/Grok-The-Trencher-4.5](https://github.com/hoodpumps/Grok-The-Trencher-4.5) | 4, 2026-07-09, MIT | **No code at all** (README, architecture.md, LICENSE) | Claimed only | README tells users to put a private-key byte array in an env variable for code that isn't public | **Avoid** |
| [swcstudiospace/PumpGrok](https://github.com/swcstudiospace/PumpGrok) | 34, 2026-11-02, AGPL-3.0 | No: live adapter refuses; paper buy and sell refuse until a curve reader is verified | Agent instructions; a human approves every ticket | Marketing ("1-Click… Jupiter execution") overstates the code; very process-heavy | Ideas only: walk-forward/holdout harness, key-variable bans, log redaction |
| [galleonlabs/hypergrok-trading-desk](https://github.com/galleonlabs/hypergrok-trading-desk) | 275, 2026-11-01, MIT | Yes, but on **Hyperliquid perps**, through the user's own agent with human approval | Research and proposal roles | None found; careful key handling | Ideas only: hashed, gap-aware research datasets |

None of the five showed drainer code. All of them were built with heavy AI assistance in short bursts.

## 3. Stronger alternatives, and what we reuse

| Component | Source | Use in the lab |
|---|---|---|
| Solana RPC client and types | `solana-py` (MIT), `solders` (Apache-2.0, already a dependency) | `solders` for constants and decoding; plain JSON-RPC over `httpx` for reads |
| pump.fun / PumpSwap program IDLs | `pump-fun/pump-public-docs` (no license: read only), `chainstacklabs/pumpfun-bonkfun-bot` (Apache-2.0) | Reference for a future on-chain launch recorder; their trading code is not used |
| Market data | DexScreener, GeckoTerminal, Jupiter HTTP APIs, called directly | `opportunity_lab/sources/` |
| Backtest discipline | Freqtrade (GPL: ideas only), QuantConnect's warning that default fills are optimistic | Point-in-time replay, null-market check, walk-forward, cost stress |
| Execution and agent frameworks | Hummingbot (Gateway: Jupiter router), solana-agent-kit (TypeScript), elizaOS | Not needed for a paper lab; not used |

**Built ourselves:**

- the four agents;
- the cost and fill model, including failed, partial and unsellable outcomes;
- the lab ledger and crash recovery;
- the replay and validation harness;
- the real-data study.

**Deliberately not reused:** any repository's signing or execution path, unvetted npm packages, and any "sniper"
repository distributed through Telegram funnels.

## 4. xAI Grok API: what it would cost and what it allows

| Item | Finding (official-doc snippets unless noted) |
|---|---|
| Endpoint | OpenAI-compatible, `https://api.x.ai/v1` (`/chat/completions`, `/responses`), `Authorization: Bearer` |
| Cheapest general model | `grok-4.3`: $1.25 input / $0.20 cached / $2.50 output per 1M tokens |
| Flagship | `grok-4.7`: $2.00 / $0.50 / $6.00 |
| Retired | `grok-4-fast` and `grok-4-1-fast` slugs, retired 2026-05-15 and billed at grok-4.3 rates |
| Batch API | 20% discount |
| `web_search` tool | $5 per 1,000 calls |
| `x_search` tool | $5 per 1,000 **posts fetched** plus $10 per 1,000 profiles. Not per call, and not de-duplicated |
| Rate limits | Tiered by cumulative spend; grok-4.3 at tier 0: 37 req/s |
| Terms | No blanket trading ban. Prohibits unlawful securities trading or advice and "high-stakes automated decisions". Keep use personal, paper-only, and do not redistribute X posts |

**Monthly cost of shadow scoring.** Each call is about 2,000 input and 300 output tokens on grok-4.3:

| Tokens scored per day | Grok, no search | Grok with X search (5–20 posts per token) | Claude, no search |
|---|---|---|---|
| 100 | ≈ $10 (≈ $5 with caching) | ≈ $90 – $411 | Price not verified here; set `claude_*_per_mtok` from the current price page |
| 1,000 | ≈ $98 (≈ $50 with caching) | ≈ $904 – $4,110 | as above |

**Without Grok, the lab costs $0** in API fees. It uses keyless DexScreener, GeckoTerminal, Jupiter and public RPC.
The only costs are the machine and, optionally, a paid RPC.

## 5. Data sources the lab uses, and their limits

| Source | Auth | Limit used by the lab (published) | Role |
|---|---|---|---|
| DexScreener | none | 0.5 req/s profiles and boosts (60/min); 2 req/s pairs (300/min) | Discovery, pair snapshots: price, liquidity, m5/h1 volume and buys/sells, age |
| GeckoTerminal | none | 0.075 req/s (keyless about 5/min, 2026 docs) | New and trending pools |
| Jupiter Tokens v2 / Swap v2 `/order` (no taker) | key optional | 0.2–0.25 req/s (keyless 0.5 req/s shared) | `isSus` (present only when flagged), organic score; read-only buy and sell-back quotes |
| Solana RPC | none / Helius key | 2 req/s (public: 40 per 10 s per method) | Mint and freeze authority, Token-2022 extensions, largest holders |
| Birdeye, Helius, RugCheck, GoPlus | keys or unverified | off by default | Candidates for extra independent signals |
| pump.fun | no official API | not used | The unofficial frontend API is unsupported; launches are observed through DexScreener and GeckoTerminal |

## 6. Security research applied to the risk agent

Every rule here is in `opportunity_lab/agents/security.py` and fails closed:

- **Reject:**
  - an active mint or freeze authority;
  - Token-2022 permanent delegate, transfer hook, frozen default state, pausable, non-transferable, or a transfer fee;
  - a token Jupiter marks suspicious;
  - a buy route with no sell route (honeypot);
  - liquidity withdrawal;
  - top-10 or single-holder concentration;
  - a pair younger than 30 minutes;
  - too little liquidity;
  - a high round-trip loss or price impact;
  - any missing critical fact.
- **Holder metrics exclude the largest account,** which is presumed to be the pool vault. This is recorded as a
  heuristic in each assessment.
- **Not yet covered:**
  - linking bundled or sniper wallets through funding sources;
  - observed sells by unrelated wallets;
  - LP lock status.

  These need an indexer key or an on-chain recorder; see the roadmap.
- **Volume and transaction counts are treated as manipulable.** Wash trading is documented in individual cases, so
  the lab never trades on volume alone.
- **Paid DexScreener boosts are recorded as `paid_promotion`** and are never a signal.

## 7. Public historical datasets

No public GitHub dataset contains every launch with prices from launch onward, dead tokens included. The most useful
ones:

| Dataset | Content | Bias | Use here |
|---|---|---|---|
| [ian05012/solana-memecoin-dataset](https://github.com/ian05012/solana-memecoin-dataset) (MIT) | 44,460 detection snapshots with features at detection time, plus 15-min candles, June 2026 | Selected graduates and trending tokens (survivorship) | **Used**: real-data study (`OPPORTUNITY_LAB_RESULTS.md`), an upper bound |
| Solid-Sea/pump-gold-dataset | 1.3M pump.fun tokens, 93% "dead" | No timestamps | Base rates only |
| DeFiLabX/SolRPDS (CC BY 4.0) | about 67k Raydium pools, 2021–24, with rug labels | Pre-PumpSwap era | Possible future check of the security agent |
| mahad1z/memecoin-survival | SQL to rebuild a complete October 2026 launch cohort | Needs a Coinbase CDP SQL account | Method reference for a survivorship-free cohort |

The lab's own recorder (`snapshots` table) is the survivorship-free data source going forward. It keeps every
observation, including tokens that later died.
