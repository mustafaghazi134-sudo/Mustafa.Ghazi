# Web research: Grok API, Solana data/security sources, memecoin risk, backtesting frameworks

Prepared 2026-11-02 for a paper-only Solana token opportunity-discovery lab (Python).

## How this was researched, and limits (read first)

- **Direct page fetches were blocked in this session.** WebFetch and curl could not reach docs.x.ai, x.ai, morphllm.com, or api.dexscreener.com (DNS failure or proxy 403). So **no page was opened directly and no API was called live**. Every fact below comes from **web-search result snippets** (WebSearch) of the cited URL, retrieved 2026-11-02.
- **Reddit is blocked to the search tool** (`reddit.com` returns "not accessible to our user agent"). I could not read Reddit threads directly. X/Twitter posts appear only when the search engine indexed them.
- Labels used:
  - **[OFFICIAL-SNIPPET]**: from a search snippet of the vendor's own docs or legal page. This is the strongest level available here. Confirm on the live page before you hard-code it.
  - **[SECONDARY]**: news, blog, vendor-marketing, or third-party tracker.
  - **[UNVERIFIED]**: from my background knowledge, or sources disagree. Must check live.
- No accounts were created, no API keys were used, and no money was spent. Instructions found in web pages were ignored.

---

## A. xAI Grok API

Note: xAI pages now carry the brand "SpaceXSI" / "SpaceX Super Intelligence API". The domains are unchanged: docs.x.ai, x.ai, api.x.ai. [OFFICIAL-SNIPPET, docs.x.ai, 2026]

### A1. Model lineup and token pricing (per 1M tokens, prompts under 200k tokens)

| Model ID | Input | Cached input | Output | Notes | Label / source |
|---|---|---|---|---|---|
| `grok-4.7` (flagship) | $2.00 | $0.50 | $6.00 | At 200k+ tokens: $4 / $1 / $12 | [OFFICIAL-SNIPPET] docs.x.ai/developers/pricing, /models/grok-4.7 |
| `grok-4.6` | $2.00 | $0.50 | $6.00 | Same rates as 4.7 | [OFFICIAL-SNIPPET] docs.x.ai/developers/pricing |
| `grok-4.5` | $2.00 | $0.30 | $6.00 | Third-party sources disagree on cached rate ($0.30 vs $0.50) | [OFFICIAL-SNIPPET] docs.x.ai/developers/models/grok-4.5 |
| `grok-4.3` (cheapest general model) | $1.25 | $0.20 | $2.50 | At 200k+: $2.50 / $0.40 / $5.00. 1M-token context. `reasoning_effort` values: none, low (default), medium, high, xhigh | [OFFICIAL-SNIPPET] docs.x.ai/developers/models/grok-4.3 |
| `grok-4.20-multi-agent-0309` | $1.25 | $0.20 | $2.50 | Bills every leader and sub-agent token, so real cost per request is much higher | [OFFICIAL-SNIPPET] docs.x.ai/developers/models/grok-4.20-multi-agent |
| `grok-4.20` reasoning (0309) | $1.25 | n/a | $2.50 | Long context: $2.50 / $5.00 | [OFFICIAL-SNIPPET] |
| "Grok 4.7 Fast" | $4.00 | ? | $12.00 | Listed for Cursor and Grok Build only | [OFFICIAL-SNIPPET], scope unclear |
| `grok-code-fast-1` | $1.00 | ? | $2.00 | Coding model | [OFFICIAL-SNIPPET] |
| `grok-4-1-fast-non-reasoning`, `grok-4-fast-*` | was $0.20 | | was $0.50 | **Retired 2026-05-15.** Requests to the old slug are now billed at grok-4.3 rates | [OFFICIAL-SNIPPET] docs.x.ai/developers/migration/may-15-retirement |

- **Batch API**: 20% off every token type for grok-4.7, 4.6, 4.5, 4.3, and the 4.20 variants. Results usually arrive within 24 h. Batch requests do not count toward rate limits. (Some third-party pages say 50%. That figure is wrong for the current docs.) [OFFICIAL-SNIPPET] docs.x.ai/developers/advanced-api-usage/batch-api (updated 2026-10-08)
- **US regional endpoint** `https://us.api.x.ai/v1` costs 1.1x the global price. [OFFICIAL-SNIPPET]
- **Reasoning tokens are billed as output.** For scoring, set reasoning effort to `none` on grok-4.3 to keep output cost predictable. [OFFICIAL-SNIPPET: the batch page says reasoning tokens are discounted as a token type. The "billed as output" detail is UNVERIFIED]

### A2. Server-side tools ("Agent Tools")

| Tool | Price | Label / source |
|---|---|---|
| `web_search` | $5 per 1,000 successful calls | [OFFICIAL-SNIPPET] docs.x.ai/developers/pricing |
| `x_search` | **Billed per item, not per call**: $5 per 1,000 posts fetched, plus $10 per 1,000 user profiles fetched, plus token costs | [OFFICIAL-SNIPPET] docs.x.ai/developers/tools/x-search; rates in effect as of 2026-09-21 |
| `code_execution` | No price found | [UNVERIFIED] |
| Failed tool calls | Not billed | [OFFICIAL-SNIPPET] tool-usage-details |

- Billing details for `x_search` [OFFICIAL-SNIPPET]:
  - Parent posts, quoted posts and every post in a fetched thread each count as a fetched post.
  - Counts are **not de-duplicated** across calls in one request.
  - Usage appears in `usage.server_side_tool_usage_details` (fields such as `x_search_calls` and posts fetched).
  - `cost_in_usd_ticks` gives the total cost of a request.
- `x_search` parameters [OFFICIAL-SNIPPET]:
  - `allowed_x_handles` or `excluded_x_handles`: up to 20 handles (10 via the Vercel AI SDK). The two are mutually exclusive.
  - `from_date` / `to_date` in YYYY-MM-DD.
  - These filters apply to keyword and semantic search, not to thread fetches.
  - The docs state no fixed number of posts per call. Measure it from a test response.
- **History**: "Live Search" (`search_parameters`) launched May 2025. Server-side tools became generally available Oct 2025. Agent Tools pricing dropped to "≤ $5 per 1k successful calls" (Grok 4.1 Fast announcement, Nov 2025). Current docs use the `tools` array, not `search_parameters`. [OFFICIAL-SNIPPET] docs.x.ai/developers/release-notes, x.ai/news/grok-4-1-fast. The exact retirement date of `search_parameters` is [UNVERIFIED].

### A3. Direct X (Twitter) data access (separate from Grok)

- The X API is now **pay-per-use with prepaid credits**. It launched formally around Feb 2026. Basic ($200/mo) and Pro ($5,000/mo) remain only for existing subscribers. [OFFICIAL-SNIPPET] docs.x.com/x-api/getting-started/pricing
- Prices:
  - Pilot read price: $0.005 per post read, $0.01 per user read.
  - Owned reads: $0.001 per resource from 2026-04-20.
  - Posting: $0.015 per post.
  - [OFFICIAL-SNIPPET for the owned-read change, via devcommunity.x.com 2026-04 announcement. The current standard post-read price was not visible: UNVERIFIED]
- Cap: 3M post reads per month on pay-per-use; above that requires Enterprise. One community post says 2M. [OFFICIAL-SNIPPET for 3M]
- Buying X API credits earns up to 20% back in xAI API credits. [OFFICIAL-SNIPPET]

### A4. Rate limits

- Tiers are based on cumulative xAI API spend since 2026-01-01 and never downgrade. Tier 0 = $0, T1 = $50, T2 = $250, T3 = $1,000, T4 = $5,000, then Enterprise. [OFFICIAL-SNIPPET] docs.x.ai/developers/rate-limits
- Limits are expressed as requests per second (RPS) and tokens per minute (TPM). Per-second RPS = RPM / 48 (minimum 2). Cached tokens count toward TPM.
- Examples:
  - grok-4.3: 37 RPS and 10M TPM at Tier 0, up to 208 RPS and 85M TPM at Tier 4.
  - grok-4.5/4.6/4.7: about 150 RPS and 50M TPM at Tier 0.
  - grok-4.20 multi-agent: 9 RPS and 2.5M TPM.
  - [OFFICIAL-SNIPPET]
- The management API can cap each key (qps, qpm, tpm). [OFFICIAL-SNIPPET, older page]

### A5. Endpoint and auth (OpenAI-compatible)

- Base URL: `https://api.x.ai/v1` (US-only: `https://us.api.x.ai/v1`).
- Auth header: `Authorization: Bearer $XAI_API_KEY`.
- Endpoints: `POST /v1/responses` and `GET /v1/responses/{id}` (preferred), plus `/v1/chat/completions`, which still works.
- The OpenAI Python SDK works with `base_url="https://api.x.ai/v1"`.
- [OFFICIAL-SNIPPET] docs.x.ai/developers/rest-api-reference/inference/responses, docs.x.ai/developers/quickstart, x.ai/api

### A6. Terms and acceptable-use points that matter here

- **The AUP has no blanket ban on trading.** It prohibits operating in a regulated industry without complying with its rules. Examples given: "unlawfully buy or sell securities or … provide or receive advice about securities, commodities, derivatives, or other financial products." [OFFICIAL-SNIPPET] x.ai/legal/acceptable-use-policy (the version shown was labelled "previous version, effective Aug 14, 2026". Check the current one.)
- The AUP prohibits "high-stakes automated decisions" affecting a person's legal or material rights or well-being. A personal paper-trading lab is unlikely to be in scope. A product giving signals to others could be. [OFFICIAL-SNIPPET; interpretation is mine]
- No training competing models on Output. No scraping or reselling Inputs or Outputs. [OFFICIAL-SNIPPET] x.ai/legal/terms-of-service-enterprise, AUP
- API data is kept 30 days for abuse review by default. Zero Data Retention (ZDR) is available but disables stateful Responses, Files and Batch. xAI does not train on API data without permission. [OFFICIAL-SNIPPET] docs.x.ai/developers/faq/security
- **Implication for the lab:**
  - Keep it paper-only and personal.
  - Do not publish Grok outputs as investment advice.
  - Do not redistribute raw X posts fetched by `x_search`. X content redistribution is also governed by X's developer terms [UNVERIFIED for the xAI-tool path].

### A7. Do LLMs improve crypto trade selection after costs? (evidence)

| Evidence | Finding | Label |
|---|---|---|
| Nof1 **Alpha Arena Season 1** (crypto perpetuals on Hyperliquid, Oct–Nov 2025, $10k each) | Qwen3-Max ≈ $12,231 and DeepSeek ≈ $10,489. **Grok 4 ≈ $4,208 (≈ −58%)**. Another report says Grok 4 finished last at $4,610. Very short window, n = 1 run per model | [SECONDARY] forklog.com, ithome.com. I could not verify against nof1's own leaderboard |
| Alpha Arena **Season 1.5** (US equities, Nov 19 – Dec 3 2025) | A "mystery model" later said to be Grok 4.20 returned +12.1%. Outlets call this a short-term, small-sample result | [SECONDARY] forklog.com |
| CLQT benchmark, arXiv 2606.29771 | Only 1 of 19 surveyed closed-loop LLM portfolio studies used an explicit transaction-cost model. **Cost-aware evaluation is rare** | [SECONDARY: arXiv, read via snippet] |
| "From Hypotheses to Factors", arXiv 2604.26747 | LLM-guided crypto factor discovery. The equal-weight long-short portfolio stays profitable out of sample after costs. **Alpha is concentrated in small assets** (value-weighted results are weak). This is the LLM as a research assistant, not a per-trade picker | [SECONDARY: arXiv snippet] |
| Meme-coin copy-trading multi-agent, arXiv 2601.08641 | The only evaluated model with a positive average copier return was about +3% after modelled frictions. A reviewer notes it does not handle adaptive adversaries | [SECONDARY: arXiv + PREreview] |
| "What LLM Trading Agents Actually Do in Production", arXiv 2609.05663 | 3,505 real-money Qwen3 vaults trading a 12-memecoin market on Base for 21 days. Useful as a design reference for evaluation | [SECONDARY: snippet; performance numbers not extracted] |
| LATTICE (2604.26235), CryptoBench (2512.00417), CryptoTrade (2407.09546) | These measure analysis quality or are method papers. No robust after-cost alpha | [SECONDARY] |

**Bottom line:**
- I found **no credible, independently verifiable evidence that Grok, or LLMs in general, improve memecoin or new-token trade selection after fees, slippage and MEV**.
- The best-supported use is LLM-as-feature-generator or analyst, followed by conventional, cost-aware out-of-sample testing.
- Treat any Grok score as one candidate feature and ablate it: run the strategy with and without it.

### A8. Community feedback on "Grok trading bots" (Reddit / X)

- **Reddit could not be read** (blocked to the search tool). No Reddit claims are reported here. This is a gap.
- What the indexed web shows [SECONDARY]:
  - **"Grok"-branded tokens and presales are a recurring scam vector.** Examples:
    - The 2023 GROK token fell 74% after ZachXBT fraud allegations (Benzinga).
    - 2026 fake "$GROK" presale domains such as grok73k.com, gro31q.com and GROK94K, where buyers cannot sell or withdraw (malwaretips, gridinsoft, thezeronet, 2026-06).
    - Musk has stated none of his companies will create a token. Any "official Grok coin" or "Grok trading bot" that asks for deposits should be presumed fraudulent.
  - **Prompt-injection risk is real.** Dexerto reports an X user tricked Grok, via Morse code, into making a linked trading bot send about $200k of crypto. The date is unverified. This is direct evidence for keeping LLMs away from wallets and reading external text only as untrusted data.
  - Cointelegraph's hands-on pieces conclude that Grok 3/4 "wasn't built for direct trading" and that traders "still can't rely on them for autonomous trading". [SECONDARY]
- **No independently verifiable track record of any retail "Grok trading bot" was found.**

---

## B. Solana market-data and token-security sources

### B1. DexScreener API

| Item | Detail | Label |
|---|---|---|
| Base | `https://api.dexscreener.com` | [OFFICIAL-SNIPPET] docs.dexscreener.com/api/reference (index about 200 days old) |
| Auth | None for the public endpoints. Paid tiers are mentioned in the terms | [OFFICIAL-SNIPPET] |
| `GET /token-profiles/latest/v1` | 60 req/min | [OFFICIAL-SNIPPET] |
| `GET /token-boosts/latest/v1`, `GET /token-boosts/top/v1` | 60 req/min each | [OFFICIAL-SNIPPET] |
| `GET /latest/dex/pairs/{chainId}/{pairId}` | 300 req/min. Full schema | [OFFICIAL-SNIPPET] |
| `GET /latest/dex/search?q=` | 300 req/min | [OFFICIAL-SNIPPET] (exact path is from memory: UNVERIFIED) |
| `GET /token-pairs/v1/{chainId}/{tokenAddress}` | Pools for a token | [OFFICIAL-SNIPPET] |
| `GET /tokens/v1/{chainId}/{addr1,addr2,…}` | Up to 30 addresses, 300 req/min | [OFFICIAL-SNIPPET] |
| WebSocket API | Streams profiles, boosts and community takeovers | [OFFICIAL-SNIPPET] docs.dexscreener.com/api/websockets |

- **Pair fields:** `chainId`, `dexId`, `url`, `pairAddress`, `labels`, `baseToken`, `quoteToken`, `priceNative`, `priceUsd`, `txns{m5,h1,h6,h24:{buys,sells}}`, `volume{m5,h1,h6,h24}`, `priceChange{…}`, `liquidity{usd,base,quote}`, `fdv`, `marketCap`, `pairCreatedAt` (ms epoch, nullable), `info`, `boosts`. [OFFICIAL-SNIPPET for txns, liquidity, marketCap, pairCreatedAt and boosts on the pairs endpoint. Some schema fields are optional or null. The token-pairs snippet did not show txns or liquidity: UNVERIFIED whether they are present there]
- **Solana `dexId` values:** `pumpfun` (bonding curve), `pumpswap`, `raydium` (with `labels` such as CPMM, CLMM, LaunchLab), `meteora` (labels such as DLMM, DYN, DBC), `orca`. [UNVERIFIED: from memory. Confirm from live responses]
- **ToS:** Commercial use is allowed. You may not build a product that competes with DexScreener or make the API available to third parties. The licence is revocable. Data and IP belong to DexScreener. [OFFICIAL-SNIPPET] docs.dexscreener.com/api/api-terms-and-conditions
- **Caveat:** Boosts are paid promotion. Treat "boosted" as a marketing signal, not quality.

### B2. GeckoTerminal (CoinGecko on-chain)

- **Keyless base:** `https://api.geckoterminal.com/api/v2`. Keyed plans use `pro-api.coingecko.com/api/v3/onchain/...`. [OFFICIAL-SNIPPET] docs.coingecko.com/docs/keyless-public-api
- **Rate limit (conflicting):**
  - Current keyless docs say **about 5 calls/min, dynamic, from a shared IP pool**. They also say keyless access is not for production or scheduled polling, and that 4xx/5xx responses count toward the limit. [OFFICIAL-SNIPPET, 2026]
  - The older GeckoTerminal FAQ says 30 calls/min (raised from 10 in Apr 2023). [OFFICIAL-SNIPPET, old]
  - The free **CoinGecko Demo key** gives 100 calls/min and 10,000 calls/month, but signup is required. [OFFICIAL-SNIPPET]
  - Plan for 5/min keyless.
- **Endpoints:**
  - `/networks/solana/new_pools`, `/networks/solana/trending_pools` [UNVERIFIED paths from memory. Standard GT v2]
  - `/networks/solana/pools/{pool}/ohlcv/{day|hour|minute}?aggregate=1|5|15&limit≤1000&before_timestamp=<epoch s>&currency=usd|token&token=base|quote|<addr>` [OFFICIAL-SNIPPET] docs.coingecko.com/reference/pool-ohlcv-contract-address
- **OHLCV notes:**
  - Each request covers at most a 6-month range. Page backwards with `before_timestamp`.
  - Intervals with no swaps are skipped (gaps).
  - History extends back to Sept 2021 (Jan 2025 changelog), but data **older than 6 months requires a paid plan (Analyst+)**.
  - [OFFICIAL-SNIPPET] docs.coingecko.com/changelog/extended-historical-data-for-onchain-ohlcv-endpoint
- **ToS:** geckoterminal.com/terms-conditions exists. Its content was not extracted [UNVERIFIED].

### B3. Jupiter: Tokens API v2 and Price API v3

- **Hosts:**
  - `api.jup.ag` is keyless at **0.5 RPS** ("testing, prototyping, AI agents"), or keyed via the `x-api-key` header from portal.jup.ag. The free key gives about 1 RPS (60/min).
  - `lite-api.jup.ag` deprecation is **postponed, new date TBA** (the original date was 2026-01-31). Its rate limit "will be reduced progressively". Only the hostname changes.
  - [OFFICIAL-SNIPPET] dev.jup.ag/portal/migrate-from-lite-api, developers.jup.ag/docs/portal/migration, changelog
- **Tokens v2** (`https://api.jup.ag/tokens/v2`):
  - `/search?query=` takes a symbol, name or mint.
  - `/tag?query=verified|lst`. [path UNVERIFIED]
  - `/{category}/{interval}` with category ∈ `toporganicscore`, `toptraded`, `toptrending` and interval ∈ `5m`, `1h`, `6h`, `24h`. [OFFICIAL-SNIPPET for category routes and toporganicscore. `toptrending` is UNVERIFIED]
  - `/recent` lists tokens ordered by **first pool creation**, not mint time.
  - [OFFICIAL-SNIPPET] developers.jup.ag/docs/tokens, /api-reference/tokens/recent
- **Fields:**
  - Market: `holderCount`, `fdv`, `mcap`, `usdPrice`, `liquidity`.
  - Organic score: `organicScore` (0–100) and `organicScoreLabel` (high, medium or low).
  - Verification: `isVerified`, `tags`.
  - Launch: `firstPool{id,createdAt}`, `launchpad`, `graduatedPool`, `graduatedAt`.
  - Trading stats: `stats5m`, `stats1h`, `stats6h`, `stats24h`, each with buyVolume, sellVolume, numBuys, numSells, numTraders and priceChange.
  - [OFFICIAL-SNIPPET]
- **`audit` object** (nullable; sub-fields are present only when applicable) [OFFICIAL-SNIPPET]:
  - `isSus` is present **only when the token is flagged**. Test whether the key exists. Not all risky tokens are flagged.
  - `mintAuthorityDisabled`, `freezeAuthorityDisabled`.
  - `topHoldersPercentage` on a 0–100 scale.
  - `devBalancePercentage`.
- **`fees` field:** not found in snippets. [UNVERIFIED]
- **Price v3** (`GET https://api.jup.ag/price/v3?ids=<up to 50 mints>`):
  - Fields: `usdPrice`, `blockId`, `decimals`, `priceChange24h` (in percent), `liquidity` (sometimes absent), `createdAt`.
  - Current price only, with no history.
  - Tokens that look unreliable after heuristic checks (organic score, liquidity, holders) are **omitted**. Tokens with no trade in 7 days are omitted.
  - [OFFICIAL-SNIPPET] dev.jup.ag/docs/price/v3
  - **Implication:** "missing from Price v3" is itself a risk signal.

### B4. Birdeye

- **Free "Standard" plan:**
  - 30,000 compute units (CU) per month at **1 RPS**, with access limited to **3 endpoints**. The snippets do not name which three. Whether they include `token_security` or `ohlcv` is [UNVERIFIED].
  - Rate limits apply per account across all APIs.
  - [OFFICIAL-SNIPPET] birdeye.so/data-api/pricing, docs.birdeye.so/docs/rate-limiting, bds-support article
- **Paid plans:** Lite $39, Starter $99, Premium $199, Business $499 per month (15–100 RPS).
- **x402 pay-per-request:** $0.003 per request in USDC. [OFFICIAL-SNIPPET] birdeye.so blog
- **Endpoints:** `GET https://public-api.birdeye.so/defi/token_security?address=` and `/defi/ohlcv`. Headers: `X-API-KEY` and `x-chain: solana`. Per-endpoint RPS caps are listed (token_security 150 RPS, ohlcv 100 RPS on higher plans; page about a year old). [OFFICIAL-SNIPPET for limits; host and header from memory: UNVERIFIED]
- **A key is required**, so the lab must not use Birdeye without the user's own signup.

### B5. Helius

- **Free plan:**
  - 1M credits/month, 10 RPC req/s, **2 req/s for DAS and Enhanced APIs**.
  - LaserStream WebSocket (standard methods).
  - [OFFICIAL-SNIPPET] helius.dev/docs/billing/plans
- **Paid plans:** Developer $49 (10M credits), Business $499, Professional $999. Overage is $5 per 1M credits.
- **Credit costs:**
  - DAS request: 10 credits.
  - Enhanced Transactions: 100 credits. This is legacy and in maintenance mode. Its replacement, "Parsed Events", costs 10 credits.
  - Webhooks: 1 credit per delivered event. Webhook create, edit or delete via API: 100 credits.
  - Free-plan webhooks are auto-disabled when the failure rate is ≥95% over 24 h.
  - Number of free webhooks: sources conflict (1 vs unlimited).
  - [OFFICIAL-SNIPPET] helius.dev/docs/billing/credits, /docs/webhooks, /docs/faqs/webhooks
- **Requires an API key** (signup).

### B6. Solana public RPC

- **Endpoint:** `https://api.mainnet-beta.solana.com`. Docs also list `api.mainnet.solana.com`.
- **Limits per IP:**
  - 100 requests per 10 s.
  - **40 requests per 10 s for a single RPC method.**
  - 40 concurrent connections.
  - 100 MB per 30 s.
- Errors: 429 when rate-limited, 403 when blocked. "Not intended for production." Limits may change without notice.
- [OFFICIAL-SNIPPET] solana.com/docs/references/clusters
- **Useful calls:**
  - `getTokenLargestAccounts(mint)` returns the top 20 token accounts. These are **token accounts, not owners**. Resolve owners, and exclude pool, bonding-curve and burn accounts. [top-20 count is UNVERIFIED, from memory]
  - `getAccountInfo(mint, {encoding:"jsonParsed"})`: for Token-2022 mints, extensions appear under `parsed.info.extensions[]` as `{extension: "<camelCase>", state: {...}}`. Examples: `transferFeeConfig`, `transferHook`, `permanentDelegate`, `defaultAccountState`, `nonTransferable`, `pausableConfig`, `mintCloseAuthority`, `metadataPointer`, `tokenMetadata`.
  - [OFFICIAL-SNIPPET for the structure, from the solana.com Scaled UI Amount guide. Exact key names for each extension are UNVERIFIED. Confirm against a live mint]

### B7. RugCheck

- **Base:** `https://api.rugcheck.xyz/v1`. Swagger is at api.rugcheck.xyz/swagger/index.html.
- **Endpoints:**
  - `GET /tokens/{mint}/report` (full report).
  - `GET /tokens/{mint}/report/summary` (summary: `score`, `score_normalised` 0–100, `risks[]` with name/level/description/score, `lpLockedPct`).
  - Full report adds `topHolders[]` (with `insider` flag), `markets[]` (with LP lock info), `mintAuthority`, `freezeAuthority`, `graphInsidersDetected`, `insiderNetworks`, `totalHolders`, and `token_extensions`.
  - [SECONDARY / UNVERIFIED: third-party SDKs (romankurnovskii/RugCheck-CLI, goat-mcp, apidog). No official docs were found in search]
- **Auth (conflicting):**
  - One SDK says single-token report and summary are public, with bulk and verified lists needing a paid key.
  - A guide says an `X-API-KEY` header is required.
  - An older wrapper used Solana-wallet sign-in.
  - [UNVERIFIED] Test without a key and expect 401 or 429.
- No numeric rate limit was found.

### B8. GoPlus Security: Solana token security

- **Endpoint:** `GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=<mint>`. Optional `Authorization: Bearer <access_token>` header. [OFFICIAL-SNIPPET for the endpoint concept and contract_addresses parameter (docs.gopluslabs.io/reference/solanatokensecurityusingget). The exact path is from memory: UNVERIFIED]
- **Fields** (each authority-type field is roughly `{status, authority[]}`):
  - `mintable`, `freezable`, `closable`, `balance_mutable_authority`
  - `transfer_fee`, `transfer_fee_upgradable`
  - `transfer_hook`, `transfer_hook_upgradable`
  - `default_account_state`, `default_account_state_upgradable`
  - `non_transferable`, `metadata_mutable`, `trusted_token`
  - `holders`, `holder_count`, `lp_holders`
  - `dex[]` (with tvl, price and volume windows), `creators`, `total_supply`
  - Since v0.0.3: `is_locked` and `locked_detail`. v0.0.4 added Streamflow locker support.
  - Covers both SPL Token and Token-2022.
  - [OFFICIAL-SNIPPET for balance_mutable_authority, transfer_hook(_upgradable), default_account_state(_upgradable), the lock fields (docs.gopluslabs.io/reference/response-detail-1, changelog, @goplus/sdk-node model). Other fields are UNVERIFIED]
- **Free tier (conflicting):**
  - Docs support page: 30 calls/min, free.
  - Pricing page: 150 CU/min and 30k CU/day.
  - Product pages: 100 call credits/min.
  - An access token is obtained via app_key + SHA-1 signature.
  - [OFFICIAL-SNIPPET]

### B9. pump.fun

- **No official public REST data API.**
  - The official GitHub covers the on-chain programs: the bonding-curve program and the PumpSwap AMM (`pAMMBay…`).
  - The website uses undocumented `frontend-api-v3.pump.fun`. v1 `frontend-api.pump.fun` is reported deprecated.
  - These endpoints are unsupported and may break or be blocked.
  - [SECONDARY] coinpaprika.com education, github.com/BankkRoll/pumpfun-apis
- **ToS on scraping:** not found [UNVERIFIED]. The safer path is on-chain data (RPC or indexers) or permitted third-party APIs.
- **Graduation mechanics:**
  - 1B supply, about 800M (793.1M "real") on the curve.
  - Graduation is triggered when the curve's real token reserves sell out (≈85 SOL net buys). Often quoted as about "$69k market cap", which is SOL-price dependent.
  - Since **2025-03-20** graduates migrate to **PumpSwap** (previously Raydium).
  - 2026 "BOOST": about 20% of migrated liquidity funds automatic buys in the first 5 minutes after migration [SECONDARY, single source].
  - Graduation rate jumped to 6.7%/day in late July 2026 (The Block, 2026-07-29).
  - [SECONDARY] soltokencreator.io, cryptomemecoin.com, theblock.co/post/347360
- **Jupiter routing:**
  - Evidence supports Jupiter routing **post-graduation** pools (PumpSwap, Raydium). [SECONDARY]
  - Whether Jupiter routes the **pre-graduation bonding curve** is unclear in 2026 sources [UNVERIFIED]. I recall Jupiter added pump.fun curve routing in 2024, but no current confirmation was found.

---

## C. Memecoin / new-token security research

### C1. Base rates (survival, graduation, fraud)

| Statistic | Value | Source and date | Label |
|---|---|---|---|
| Pump.fun tokens showing pump-and-dump or rug characteristics | **98.6–98.7%** of over 7M tokens (Jan 2024 – Mar 2025); fewer than 100k kept >$1k liquidity | Solidus Labs "2025 Rug Pull Report", May 2025 (soliduslabs.com/reports/solana-rug-pulls-pump-dumps-crypto-compliance). Pump.fun disputed it | [SECONDARY coverage of a primary report] |
| Raydium pools with "soft rug" characteristics | 93% of about 361k pools. Median rug ≈ $2,832; largest $1.9M | Solidus Labs 2025 | [SECONDARY] |
| Long-run pump.fun graduation rate | ≈1% of 18.67M launches (Jan 2024 – Jun 2026) | ankhlabs.co, citing CoinGecko | [SECONDARY] |
| Graduation, April 2026 cohort | 0.84% of about 763k launches ("mayhem mode" 0.37%) | pumpfundata.com | [SECONDARY] |
| Graduation (academic survival analysis) | 0.198% pooled over 832,941 launches (May–Jun 2026; lower bound, 6-min observation window). 0.63% in Sep–Oct 2025 | arXiv 2607.02823v3 | [SECONDARY: arXiv snippet] |
| Graduation spike after BOOST | 6.7% daily, late Jul 2026 (≈8x the June average) | The Block, 2026-07-29 | [SECONDARY] |
| Tokens ever >$10M / >$1M market cap | 18 / 96 (Dune, at the time of the article) | solanacompass.com | [SECONDARY] |
| Memecoin share of Solana spot volume | 40% in H1 2025, down to 16% in H1 2026 | 21Shares via solanacompass | [SECONDARY] |
| Chainalysis figure on tokens dumped within 24 h | **Not found.** No Chainalysis statistic was located | n/a | [UNVERIFIED] |

Academic datasets and models [SECONDARY: arXiv snippets]:
- **MemeTrans** (arXiv 2602.13480): over 41k pump.fun launches and 200M+ transactions (Dec 2024 – Mar 2025). Includes bundled-account traces and sniper holdings.
- **Catching the Rug** (arXiv 2608.20271, Aug 2026): 6.4M tokens. Predicts rugs from the **first 5 minutes** of trading. AUCPR ≈ 0.80 is reported by a review site, so verify.
- **The Memecoin Phenomenon** (arXiv 2512.11850).
- **MemeChain** (arXiv 2601.22185).
- **Cross-pipeline coverage** (arXiv 2609.18975): two pump.fun data pipelines do not see the same tokens. Your dataset has coverage bias.
- "Meme Coin Factories" (arXiv 2609.10246): title only [UNVERIFIED].

### C2. Token-2022 extension risks

| Extension | Risk | Detect via |
|---|---|---|
| Permanent delegate | The authority can transfer or **burn any holder's tokens**, and holders cannot revoke it. Reported 2026 automated "burn-after-buy" scam factories [SECONDARY, dev.to, Mar 2026, unverified claims]. Legitimate uses: regulated stablecoins such as PYUSD and USDG | jsonParsed `permanentDelegate`; GoPlus `balance_mutable_authority` |
| Transfer hook | Custom program runs on every transfer. It can let buys through and **revert sells** (honeypot). The hook program may be upgradable | `transferHook.programId`; GoPlus `transfer_hook(_upgradable)` |
| Transfer fee | The fee can be raised after launch (e.g. 0% to very high, up to the max fee configured). The withdraw authority collects it | `transferFeeConfig` (older and newer fee, maximumFee, authority) |
| Default account state = Frozen | New holder accounts start frozen. Only the freeze authority can thaw them, so you can buy but not sell | `defaultAccountState`; GoPlus `default_account_state` |
| Pausable | The issuer can halt all transfers | `pausableConfig` |
| Non-transferable | Soulbound: cannot be transferred or sold | `nonTransferable` |
| Mint close authority | The mint can be closed (with zero supply). Can confuse integrations | `mintCloseAuthority`; GoPlus `closable` |
| Classic SPL risks | Active **mint authority** (infinite mint). Active **freeze authority** (freeze buyers = honeypot). Mutable metadata. Unlocked or unburned LP (pull liquidity) | Jupiter `audit.mintAuthorityDisabled` / `freezeAuthorityDisabled`; RugCheck `lpLockedPct` |

- Sources: solana.com/docs/tokens/extensions (+ permanent-delegate, transfer-fees pages) [OFFICIAL-SNIPPET]; dev.to posts and paragraph.com/@quantumaudit [SECONDARY]. GoPlus default_account_state description [OFFICIAL-SNIPPET].
- **Screen rule** (common across sources):
  - Reject tokens with an active mint or freeze authority, unless the token is a known issuer.
  - Reject any of: permanent delegate, transfer hook, frozen default state, pausable, non-transferable, or a non-zero or upgradable transfer fee.
  - Then require **observed successful sells by unrelated wallets**.

### C3. Sandwich / MEV risk

- **Bitquery** (30 Oct 2025 – 7 Oct 2026):
  - 6.85M sandwich attacks by 2,545 bot wallets. About **$29.7M gross**, before costs.
  - **96% on pump.fun curve and PumpSwap**.
  - Victim rate per 10k Jupiter swaps: 27 at <0.5% slippage, 36 for exact-quote, **87–148 at 10–50% slippage**.
  - [SECONDARY: bitquery.io/investigations/solana-sandwich-attacks; cryptoslate]
  - **Implication for paper fills:** model memecoin buys with high slippage tolerance as paying extra adverse execution.
- **Academic** (arXiv 2609.28115): 28.0M sandwiches on Solana over 3 years, including against "protected" order flow. **96% now span multiple blocks.** [SECONDARY: arXiv snippet]
- Cumulative extraction estimate: $370–500M over 16 months (Ghost / sandwich.me via bloXroute). Cross-slot share is about 93% (bloXroute). Jito shut its public mempool in 2024. [SECONDARY]
- **Sniping**: about 52–53% of new pump.fun coins get an outside buy within about 1.3 s (Bitquery, preliminary, Oct 2026). [SECONDARY]

### C4. Wash trading, bundles, sniper clusters

- **Bundled launches:** devs pre-buy through many wallets in block 0. Bundler tools advertise up to 21 wallets in the same block.
  - Vendor estimates of sniper or bundle supply share: 5–20%, 15–35%, 20–50%. These conflict and **no rigorous measured average was found**. [SECONDARY]
  - Heuristic thresholds from analytics vendors: >50% of supply bought in block 0 = critical; fewer than 10 unique block-0 wallets = suspicious. (docs.mobula.io, docs.webacy.com) [SECONDARY]
- **Wash trading:**
  - Bitquery case study: about 200 bots, funded from one source with 0.5 SOL each, faked about $532k of volume in 12 h on one memecoin. [SECONDARY] (bitquery.io/blog/solana-volume-numbers-are-a-lie)
  - A market-wide share is not established. "93% of transactions inorganic" (Flip Research via CryptoSlate) is [UNVERIFIED].
- **Implication:**
  - Treat volume and txn counts as manipulable.
  - Prefer organic score (Jupiter), unique-trader counts, funding-source clustering, and holder concentration *after* linking bundled wallets.

---

## D. Backtesting / bot frameworks (what is relevant)

| Framework | Relevant features | Solana/Jupiter? | Docs |
|---|---|---|---|
| **Freqtrade** | `lookahead-analysis`: a `has_bias` column; allowing limit orders can cause false positives. `recursive-analysis`: indicator variance from startup candles, plus a simple lookahead check. Hyperopt warnings: decimal precision beyond 0.001 tends to overfit; optimizing only on winning pairs overfits. Docs say to run both analyses before dry-run, and that a clean result is no guarantee. Dry-run can differ from backtest because backtest assumes all orders fill | CEX via CCXT. **No Solana DEX or Jupiter support** [UNVERIFIED: from memory] | freqtrade.io/en/stable/lookahead-analysis/, /recursive-analysis/, /hyperopt/, /strategy-customization/, /strategy-101/ [OFFICIAL-SNIPPET] |
| **QuantConnect / LEAN** | Reality modelling: fee, slippage and fill models. **Defaults are optimistic**: zero fees for crypto in the default brokerage model, no slippage, no market impact. Paper trading fills market orders instantly and fully. Custom data must be timestamped by when it was actually available, to avoid lookahead | No Solana DEX connector found [UNVERIFIED] | quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts, …/transaction-fees/key-concepts, /docs/live-trading/live-reconciliation [OFFICIAL-SNIPPET] |
| **Hummingbot** | Gateway DEX middleware. **Jupiter is the default Solana router connector** (`jupiter/router`, mainnet-beta and devnet). v2.16.0 (2026-07-29) added Titan, DFlow and OKX DEX routers. Paper-trade mode exists for CEX connectors. **No paper mode for Gateway DEX connectors found** [UNVERIFIED] | Yes (live). Quote endpoints are useful for realistic paper quotes | hummingbot.org/gateway/connectors/, /gateway/commands/, /release-notes/2.16.0/ [OFFICIAL-SNIPPET] |

**Takeaway for the lab:**
- Reuse Freqtrade's *ideas*: lookahead and recursive checks, and hyperopt hygiene.
- Reuse LEAN's warning that defaults are optimistic: model fees, slippage and MEV explicitly.
- Jupiter quote calls (keyless 0.5 RPS) give realistic paper fill prices.

---

## Cost table: Grok for token scoring (computed from official list prices above)

Assumptions:
- 30-day month.
- 2,000 input + 300 output tokens per token scored.
- Reasoning effort `none`, so no hidden reasoning tokens.
- Prices from docs.x.ai/developers/pricing as seen via search on 2026-11-02.

**Per-call cost:**
- grok-4.3: 2,000 × $1.25/1M + 300 × $2.50/1M = $0.00250 + $0.00075 = **$0.00325**.
- grok-4.7: 2,000 × $2.00/1M + 300 × $6.00/1M = $0.00400 + $0.00180 = **$0.00580**.
- grok-4.3, with 1,500 of the 2,000 input tokens cached (shared system prompt): 500 × $1.25/1M + 1,500 × $0.20/1M + 300 × $2.50/1M = $0.000625 + $0.000300 + $0.000750 = **$0.001675**.

| Scenario | Calls/month | grok-4.3 | grok-4.3 + caching | grok-4.3 Batch (−20%) | grok-4.7 |
|---|---|---|---|---|---|
| (i) 100 tokens/day | 3,000 | 3,000 × 0.00325 = **$9.75** | 3,000 × 0.001675 = **$5.03** | **$7.80** | 3,000 × 0.0058 = **$17.40** |
| (ii) 1,000 tokens/day | 30,000 | **$97.50** | **$50.25** | **$78.00** | **$174.00** |

**(iii) Adding `x_search` for each token.** X search is billed **per post and profile fetched**, not per call: $0.005 per post and $0.01 per profile, plus the fetched text as extra input tokens. The number of posts per call is not documented, so two assumptions are shown:

- **Heavy:** 20 posts + 3 profiles + about 3,000 extra input tokens per token scored.
  - Search cost: 20 × 0.005 + 3 × 0.01 + 3,000 × 1.25e-6 = 0.100 + 0.030 + 0.00375 = **$0.1338**.
  - Total per token: $0.00325 + $0.1338 = $0.137.
  - 100/day: 3,000 × 0.137 ≈ **$411/month**. 1,000/day: ≈ **$4,110/month**.
- **Light:** 5 posts, 0 profiles, about 1,500 extra input tokens.
  - Search cost: 5 × 0.005 + 1,500 × 1.25e-6 = 0.025 + 0.001875 = **$0.0269**.
  - Total per token: $0.00325 + $0.0269 = $0.0301.
  - 100/day: **≈ $90/month**. 1,000/day: **≈ $904/month**.
- Using `web_search` instead of X search adds $0.005 per call: 1 call per token = $15/month at 100/day and $150/month at 1,000/day, plus the extra input tokens.

**Notes:**
- X search dominates cost. **Cap posts fetched, restrict handles, and run search only for tokens that already pass the cheap on-chain filters.**
- The model can make several searches in one request, and counts are not de-duplicated. Log `server_side_tool_usage_details` and `cost_in_usd_ticks`.
- The Batch API's 20% discount applies to token costs. Whether it also applies to tool fees is [UNVERIFIED].

---

## Full source list (all retrieved via web search on 2026-11-02; page dates where shown)

**xAI / X**
- https://docs.x.ai/developers/pricing (pricing; rates "in effect as of 2026-09-21")
- https://docs.x.ai/developers/models ; https://docs.x.ai/developers/models.md
- https://docs.x.ai/developers/models/grok-4.7 ; https://docs.x.ai/developers/grok-4-7 ; https://x.ai/news/grok-4-7
- https://docs.x.ai/developers/models/grok-4.5 ; https://docs.x.ai/developers/models/grok-4.3
- https://docs.x.ai/developers/models/grok-4.20-multi-agent ; https://docs.x.ai/developers/models/grok-4.20
- https://docs.x.ai/developers/models/grok-4-fast-non-reasoning ; https://docs.x.ai/developers/models/grok-code-fast-1
- https://docs.x.ai/developers/migration/may-15-retirement
- https://docs.x.ai/developers/rate-limits ; https://docs.x.ai/docs/key-information/using-management-api
- https://docs.x.ai/developers/advanced-api-usage/batch-api (updated 2026-10-08)
- https://docs.x.ai/developers/tools/x-search ; https://docs.x.ai/developers/tools/web-search ; https://docs.x.ai/developers/tools/tool-usage-details ; https://docs.x.ai/developers/cost-tracking ; https://docs.x.ai/developers/release-notes
- https://docs.x.ai/developers/rest-api-reference/inference/responses ; https://docs.x.ai/developers/quickstart ; https://x.ai/api
- https://x.ai/news/grok-4-1-fast ; https://x.ai/news/grok-4-fast
- https://x.ai/legal/acceptable-use-policy ; https://x.ai/legal/terms-of-service-enterprise ; https://x.ai/legal/terms-of-service ; https://x.ai/legal/faq-enterprise ; https://docs.x.ai/developers/faq/security
- https://docs.x.com/x-api/getting-started/pricing ; https://devcommunity.x.com/t/x-api-pricing-update-owned-reads-now-0-001-other-changes-effective-april-20-2026/263025
- Third-party price trackers (not relied on): https://costbench.com/software/llm-api-providers/xai-api/ ; https://www.morphllm.com/grok-api-pricing

**LLM trading evidence and community**
- https://arxiv.org/pdf/2606.29771 (CLQT) ; https://arxiv.org/html/2604.26747v1 ; https://arxiv.org/pdf/2604.26235 (LATTICE) ; https://arxiv.org/html/2512.00417v5 (CryptoBench) ; https://arxiv.org/abs/2407.09546 ; https://arxiv.org/pdf/2609.05663 ; https://arxiv.org/pdf/2601.08641 ; https://prereview.org/reviews/22679925
- https://forklog.com/en/news/ai-model-grok-4-2-triumphs-in-trading-tournament ; https://www.ithome.com/0/903/099.htm
- https://benzinga.com/z/35768291 ; https://cointelegraph.com/news/cz-fake-grok-coin-elon-musk-scam-tokens ; https://tools.malwaretips.com/url-scan/grok73k.com ; https://thezeronet.com/grok94k-scam-explained/ ; https://koinly.io/blog/grok-coin/ ; https://www.dexerto.com/entertainment/x-user-tricks-grok-into-sending-them-200000-in-crypto-using-morse-code-3361036/
- Cointelegraph "tried automating crypto trades with Grok 3" (via tradingview mirror)

**Data sources**
- https://docs.dexscreener.com/api/reference ; https://docs.dexscreener.com/api/websockets ; https://docs.dexscreener.com/api/api-terms-and-conditions
- https://docs.coingecko.com/docs/keyless-public-api ; https://docs.coingecko.com/reference/pool-ohlcv-contract-address ; https://docs.coingecko.com/changelog/extended-historical-data-for-onchain-ohlcv-endpoint ; https://apiguide.geckoterminal.com/faq ; https://apiguide.geckoterminal.com/changelogs ; https://docs.coingecko.com/docs/errors-and-rate-limits
- https://developers.jup.ag/docs/tokens ; https://dev.jup.ag/docs/tokens/v2/token-information ; https://developers.jup.ag/docs/api-reference/tokens/recent ; https://dev.jup.ag/docs/token-api/organic-score ; https://dev.jup.ag/docs/price/v3 ; https://developers.jup.ag/docs/api-reference/price ; https://dev.jup.ag/portal/migrate-from-lite-api ; https://developers.jup.ag/docs/portal/migration ; https://developers.jup.ag/docs/changelog
- https://birdeye.so/data-api/pricing ; https://docs.birdeye.so/docs/rate-limiting ; https://data.birdeye.so/docs/guides/api-access/rate-limiting ; https://birdeye.so/data-api/blog/detail/introducing-x402-on-birdeye-data-pay-per-request-api-access
- https://www.helius.dev/pricing ; https://www.helius.dev/docs/billing/plans ; https://www.helius.dev/docs/billing/credits ; https://www.helius.dev/docs/webhooks ; https://www.helius.dev/docs/faqs/webhooks
- https://solana.com/docs/references/clusters ; https://solana.com/docs/rpc ; https://solana.com/docs/tokens/extensions ; https://solana.com/docs/tokens/extensions/permanent-delegate ; https://solana.com/docs/tokens/extensions/transfer-fees ; https://solana.com/docs/tokens/extensions/scaled-ui-amount/integration-guide
- https://github.com/romankurnovskii/RugCheck-CLI ; https://apidog.com/pt/blog/rugcheck-api/ ; https://glama.ai/mcp/servers/@cryptoleek-team/goat-mcp/blob/d2e796192eac82c5c78f93be345183d7007d034f/rugcheck/src/api.ts
- https://docs.gopluslabs.io/reference/solanatokensecurityusingget ; https://docs.gopluslabs.io/reference/response-detail-1 ; https://docs.gopluslabs.io/changelog/token-security-api-for-solana ; https://docs.gopluslabs.io/reference/support ; https://app.unpkg.com/@goplus/sdk-node@1.1.1/files/dist/gen/model/ResponseWrapperSolanaTokenSecurityResult.js
- https://coinpaprika.com/education/pumpfun-api-solana-dex-data/ ; https://github.com/BankkRoll/pumpfun-apis ; https://www.theblock.co/post/347360 ; https://www.soltokencreator.io/blog/pump-fun-graduation-explained ; https://cryptomemecoin.com/blog/pump-fun-graduation-explained

**Security research**
- https://www.soliduslabs.com/reports/solana-rug-pulls-pump-dumps-crypto-compliance ; https://thedefiant.io/news/defi/alarming-99-of-memecoin-launches-on-pumpfun-are-pump-and-dumps-or-rug-pulls-report ; https://beincrypto.com/pump-fun-tokens-scams-solidus-labs-report/
- https://www.theblock.co/news/defi/2026-07-29-pump-fun-token-graduation-rate-jumps-boost-changes-launch-incentives-409815 ; https://arxiv.org/html/2607.02823v3 ; https://pumpfundata.com/blog/pumpfun-graduation-rate-analysis ; https://ankhlabs.co/articles/pump.fun-token-launches ; https://solanacompass.com/news/pumpfun-launched-42000-tokens-in-one-day-fewer-than-2-will-ever-reach-a-dex
- https://arxiv.org/html/2602.13480v1 (MemeTrans) ; https://arxiv.org/html/2608.20271v1 (Catching the Rug) ; https://arxiv.org/pdf/2512.11850 ; https://arxiv.org/pdf/2601.22185 ; https://arxiv.org/pdf/2609.18975
- https://dev.to/ohmygod/solanas-permanent-delegate-burn-scam-how-token-2022-extensions-power-2026s-largest-automated-rug-4579 ; https://dev.to/mrwizardlyloaf/token-2022-traps-that-drain-ai-trading-agents-and-how-to-screen-them-33bo ; https://paragraph.com/@quantumaudit/ranking-token-2022-extensions-risk
- https://bitquery.io/investigations/solana-sandwich-attacks ; https://cryptoslate.com/solana-sandwich-bots-took-29-7-million-before-costs-bitquery-estimates/ ; https://arxiv.org/html/2609.28115v1 ; https://bloxroute.com/pulse/a-new-era-of-mev-on-solana/ ; https://solanacompass.com/learn/accelerate-25/scale-or-die-at-accelerate-2025-the-state-of-solana-mev
- https://bitquery.io/blog/solana-volume-numbers-are-a-lie ; https://docs.mobula.io/almanac/detecting-snipers-bundlers ; https://docs.webacy.com/glossary/snipers-bundlers ; https://solanacompass.com/news/21shares-solanas-h1-2026-network-revenue-fell-87-as-memecoin-fees-collapsed

**Frameworks**
- https://www.freqtrade.io/en/stable/lookahead-analysis/ ; https://www.freqtrade.io/en/stable/recursive-analysis/ ; https://www.freqtrade.io/en/stable/hyperopt/ ; https://www.freqtrade.io/en/stable/strategy-customization/ ; https://www.freqtrade.io/en/stable/strategy-101/
- https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts ; https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/transaction-fees/key-concepts ; https://www.quantconnect.com/docs/live-trading/live-reconciliation
- https://hummingbot.org/gateway/connectors/ ; https://hummingbot.org/gateway/commands/ ; https://hummingbot.org/release-notes/2.16.0/
