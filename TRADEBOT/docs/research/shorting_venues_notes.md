# Venue research notes (unverified working notes)

Compiled 2026-11-03 from search results only: the official pages could not be fetched from the build container. These are **working notes, not findings**. `docs/FEASIBILITY_LONG_SHORT.md` holds the checked version. Known correction: the official Hyperliquid funding page states a cap of 0.5%/hour, not 4%/hour.

# Shorting venues for a BTC/ETH/SOL bot, for a Jordan-resident owner: feasibility assessment

Research date: 2026-11-03. Every claim below was retrieved on that date.

## How to read this report

**Method.** Direct page fetches failed in this environment. These hosts returned DNS "ENOTFOUND": docs.jup.ag, hyperliquid.gitbook.io, bybit.com and jsc.gov.jo. Every claim therefore comes from **search-engine snippets** of the cited pages, not from reading the full pages. A snippet can be truncated or out of date, so check every number marked [OFFICIAL] on the live page before you commit money.

**Labels.**
- **[OFFICIAL]**: the claim is in a snippet from the venue's own domain, or a regulator's or government's domain (for example docs.jup.ag, developers.jup.ag, hyperliquid.gitbook.io, bybit.com, kraken.com, jsc.gov.jo, solana.com, wormhole.com, coinbase.com).
- **[SECONDARY]**: the claim comes from news outlets, law firms, analytics sites or third-party guides.
- **[UNVERIFIED]**: the claim comes from my prior knowledge or a single low-quality source, or the sources conflict.

**Not legal advice.** Section B summarises public sources on Jordanian law. It does not replace advice from Jordanian counsel.

---

## Executive summary

| Venue | BTC/ETH/SOL shorts? | Status for a Jordan resident (terms of service) | Bot access | Main caveat |
|---|---|---|---|---|
| **Jupiter Perps** (Solana) | Yes: SOL, ETH, wBTC. Long and short, up to 250x | Jordan is **not named** in any Jupiter restricted list I found. Sanctioned regions are barred, and a VPN may not be used to get around restrictions | On-chain Anchor program signed by your own key. No official Python SDK | 0.06% open and 0.06% close, plus price impact, plus an **hourly borrow fee that both longs and shorts pay**. Keeper-executed orders |
| **Drift → "Velocity DEX"** (Solana) | Reported: BTC, ETH, SOL up to 20x, settled in USDT | Terms not found. Reported **private beta, whitelist only** | `driftpy` Python SDK (Drift v2). Program `dRiftyHA39...` | **About $280–295M exploit on 1 Apr 2026.** Relaunch status uncertain. **Not recommended now** |
| **Hyperliquid** | Yes. Max leverage 3x–40x depending on asset | Terms name the **US and Ontario** plus sanctioned territories. **Jordan is not named** | Official Python SDK. "Agent" API wallets can trade but cannot withdraw | Market-manipulation incidents hit the HLP vault (JELLY, POPCAT). The bridge is secured by a small validator set |
| Binance Futures (USDⓈ-M) | Yes, up to 125x | Secondary sources say **futures are blocked for Jordan residents** (official confirmation not found) | API key, plus official and community Python libraries | Likely unavailable for derivatives |
| Bybit | Yes, about 100x | Official list (Section 11.3) names US, mainland China, HK, Singapore, Canada, North Korea, Cuba, Iran, and more. **Jordan is not in the visible official text or in secondary full lists** | API key, `pybit` | Feb-2025 hack of about $1.5B (reserves replenished) |
| OKX | Yes, up to 100–125x | About 20 restricted jurisdictions (Risk and Compliance Disclosure, §3). **Jordan status not found** | API key, `python-okx` | Check the eligibility checker before signing up |
| Kraken (Derivatives) | Yes, 50x reported (10x in the EEA) | Jordan appears in a Kraken derivatives-promotion country list whose meaning is ambiguous. **Unconfirmed** | API key | Eligibility depends on country, product and KYC |

**Jordanian law (Section B).** Law No. 14 of 2025 makes the JSC the licensing authority for virtual-asset service providers. The law's reach reportedly extends to foreign platforms that serve Jordanian residents. A secondary source says that **as of 22 Sep 2026 no VASP had been licensed**. I found no explicit rule on how derivatives or leverage are treated. The law targets providers, and I found no source that clearly penalises an individual for trading on a foreign platform or a self-custody DEX. Overall the position is a **grey area**, so get local counsel.

**Bottom line for the bot.**
- **Hyperliquid** is the cheapest and most programmable place for true shorts. Its terms do not name Jordan.
- **Jupiter Perps** is the lowest-friction option on Solana, because it uses the same wallet and stack the bot already has. It costs more, because of the borrow fee.
- Avoid **Drift/Velocity** until it relaunches publicly and has a track record.
- Treat the centralized exchanges as backups that need KYC. **Binance futures are likely closed to Jordan residents.**

---

## A. Perpetual-futures and short venues

### A.1 Jupiter Perps (Solana)

**1. Markets and leverage**

| Claim | Source | Label |
|---|---|---|
| Long or short perps on **SOL, ETH and wBTC**. BTC is the wrapped wBTC market, not native BTC | docs.jup.ag/user-docs/trade/perps/faq | [OFFICIAL] |
| Max leverage when opening a position is **250x** for SOL, ETH and wBTC; minimum 1.1x | docs.jup.ag perps FAQ / positions-and-collateral | [OFFICIAL] |
| The liquidation page lists a protocol-level max of 500x | docs.jup.ag/user-docs/trade/perps/liquidation | [OFFICIAL] (conflicts with 250x; the trade form is limited to 250x) |
| Max leverage raised to 250x for every asset on 9 Oct 2025 | messari.io, JLP asset page | [SECONDARY] |
| One position per asset per side | docs.jup.ag perps FAQ | [OFFICIAL] |
| Fills are at the oracle price ("the displayed price regardless of trade size") | docs.jup.ag perps docs | [OFFICIAL] |

**2. Fees**

| Item | Detail | Source | Label |
|---|---|---|---|
| Base fee | **0.06%** of notional to open and **0.06%** to close | docs.jup.ag/user-docs/trade/perps/fees | [OFFICIAL] |
| Price-impact fee | A linear part (trade size divided by a per-asset scalar) plus an additive part that applies when open interest is imbalanced past a threshold. Both are capped per asset. The parameters live in the custody account | docs.jup.ag perps fees; developers.jup.ag/docs/perps/custody-account | [OFFICIAL] |
| Borrow fee (charged **instead of funding**) | Compounds **hourly** and is deducted from collateral. **Both longs and shorts pay it.** It is driven by utilization. The docs' worked example uses a 0.012%/hr rate at about 20% utilization | docs.jup.ag perps fees | [OFFICIAL] |
| Borrow history | Gauntlet proposed SOL/ETH/BTC rates of 0.008%/hr (Aug 2024) and 0.007%/hr (Oct 2024), with a cap of about 70% APR. In Feb 2025, SOL was about **33% APR** and ETH/BTC about **14–15% APR** | discuss.jup.ag Gauntlet / borrowing-rate threads | [SECONDARY] (governance forum, dated) |
| Network | Solana base fee of 5,000 lamports per signature, plus an optional priority fee (compute-unit price × limit) | solana.com/docs/core/fees/fee-structure | [OFFICIAL] |
| Two transactions per action | One transaction submits the request, then a keeper fulfils it | docs.jup.ag perps docs | [OFFICIAL] |
| Minimum collateral of $10 | Appears only in a community MCP tool's spec | glama.ai jupiter-perps-mcp | [UNVERIFIED] |

**3. Programmatic access**

- **Program ID:** `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu`. The Carbon decoder on docs.rs, Kamino's scope-sdk codegen and Helius parsed-data all list this ID. [SECONDARY, consistent across 3+ sources]
- **Account model:** Jupiter's developer docs document the Position, PositionRequest, Custody and Pool accounts. [OFFICIAL] (developers.jup.ag/docs/perps/*)
  - Requests to open, close, resize or change collateral go through a PositionRequest PDA. A keeper then executes them.
  - The docs point to a community TypeScript example: `julianfssen/jupiter-perps-anchor-idl-parsing`.
- **No official Python SDK was found.** A Python bot would build Anchor instructions itself (for example with `anchorpy` or `solders` plus the IDL). [UNVERIFIED that no official SDK exists]
- **Signing:** orders are signed by the bot's **own self-custodied keypair**, with no API key. Community libraries exist (a C# library, an MCP server), so use them at your own risk. [SECONDARY]

**4. Geographic restrictions**

- The developer-platform terms require that you are not sanctioned or listed. They contain a "Prohibited Localities" section and **prohibit using a VPN to get around restrictions**. [OFFICIAL] (developers.jup.ag/docs/legal/terms-of-use; snippet did not show the country list)
- Jupiter's DTF product terms (1 Dec 2025) bar residents of Iran, North Korea, Syria, Cuba, Crimea/Donetsk/Luhansk, Russia, Belarus, Myanmar and Sudan. [OFFICIAL, different product]
- The Jupiter Mobile FAQ says the app is restricted in the US, China and sanctioned regions. [OFFICIAL]
- The Jupiter Card prohibitions list is a different product. It names 17 countries, including Iraq, Israel and Turkey. **Jordan is not on it.** [OFFICIAL]
- **Verdict:** Jordan is **not named** in any Jupiter list I found. [UNVERIFIED for the core jup.ag Perps terms, because the full list was not visible]

**5. Custody, counterparty and incidents**

- **Counterparty model:** the **JLP pool** is the counterparty to every trade. There is no order book. Positions are liquidated by keepers at the oracle price. [OFFICIAL] (docs.jup.ag liquidation / how-it-works)
- **Custody:** collateral stays in program-controlled custody accounts. You keep your key, but you carry smart-contract and oracle risk.
- **Incidents:** I found **no reported exploit of Jupiter Perps' own contracts in 2024–2026**. [SECONDARY]
  - The April 2026 Drift exploit drained vaults that held a lot of JLP. JUP fell about 3% on the news. [SECONDARY] (coinmarketcap AI-written stories, low quality)

### A.2 Drift Protocol, now "Velocity DEX" (Solana)

**1. Markets.** Reports after the relaunch say perps only: **BTC, ETH and SOL up to 20x**, plus HYPE and ZEC at 10x, settled in USDT. [SECONDARY/UNVERIFIED] (solanaguides / builtonbulk, Oct 2026)

**2. Fees.** The sources conflict, and none is official:

| Claimed fee | Source | Label |
|---|---|---|
| About 0.035% taker and about -0.0025% maker rebate (pre-hack schedules) | rfp.wiki | [SECONDARY] |
| Taker from 0.10%, stepping down with volume; maker rebate up to 2 bps | eco.com | [SECONDARY] |
| Velocity beta at "~0.02%" | coinfomania | [UNVERIFIED] |

Funding was hourly under Drift v2. [UNVERIFIED]

**3. Programmatic access**

| Item | Source | Label |
|---|---|---|
| Program ID `dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH` | drift-labs.github.io/v2-teacher | [OFFICIAL] |
| Python SDK `driftpy` on PyPI, Python 3.10 or later | pypi.org/project/driftpy | [OFFICIAL] |
| Signing uses a self-custodied keypair | — | — |
| Whether Velocity keeps the same program and SDK | — | [UNVERIFIED] |

**4. Geography.** I found no Drift or Velocity terms page in search results. [UNVERIFIED] Access is reportedly a **private beta for whitelisted users**. [SECONDARY]

**5. Incidents**

- **1 Apr 2026 exploit:**
  - Reported loss of about **$270–295M**, drained in about 12 minutes. [SECONDARY] (TRM Labs, Decrypt, Bloomberg Law, Unchained, Cointelegraph)
  - Attributed to DPRK-linked actors after a **six-month social-engineering campaign** against contributors, with admin/Security-Council powers abused. [SECONDARY]
  - TVL fell from about $550M to $252M. [SECONDARY] (DefiLlama, cited by news)
- **Recovery:**
  - Tether led a recovery plan of about $127.5–150M, and settlement moved from USDC to USDT. [SECONDARY] (The Block, Cointelegraph, The Defiant, 16 Apr 2026)
  - The insurance fund was reportedly not affected. [SECONDARY]
- **Rebrand and relaunch:**
  - Rebranded to **Velocity DEX** on 1 Jul 2026. [SECONDARY] (The Defiant, CryptoTimes)
  - A relaunch was reported for 29 Sep 2026. [SECONDARY/UNVERIFIED]
  - The project's pinned post says the public launch will follow once its security work is done. [SECONDARY]
- **Assessment:** **not suitable for the bot now.**

### A.3 Hyperliquid

**1. Markets and leverage**

| Claim | Source | Label |
|---|---|---|
| BTC, ETH and SOL perps exist (USDC-margined) | — | [OFFICIAL] |
| Max leverage "varies by asset, ranging from 3x to 40x" | hyperliquid.gitbook.io/.../perpetual-assets | [OFFICIAL] |
| Maintenance margin is half the initial margin at max leverage (2.5% at 20x). Leverage steps down in tiers as position size grows | hyperliquid.gitbook.io/.../margin-tiers | [OFFICIAL] |
| BTC 40x, ETH 25x (set after the March 2025 ETH whale event) | crypto.news | [SECONDARY] |
| BTC/ETH 50x, SOL 20x in 2026 | buildix.trade | [SECONDARY] (conflicts with the official "3x to 40x") |

**2. Fees and funding**

| Item | Detail | Source | Label |
|---|---|---|---|
| Perps fee, tier 0 | **0.045% taker / 0.015% maker**, based on 14-day volume | hyperliquid.gitbook.io/.../trading/fees | [OFFICIAL] |
| Staking discount | 5–40% for staked HYPE | same | [OFFICIAL] |
| Funding | Paid **hourly** at 1/8 of the 8-hour rate. Premium is sampled every 5 seconds. Clamp ±0.05% per 8h around the interest rate. **Cap 4%/hr** | hyperliquid.gitbook.io/.../trading/funding | [OFFICIAL] |
| Interest component | 0.01% per 8h, about 11% APR, paid by longs to shorts | dwellir, onekey | [SECONDARY] |
| Observed funding | BTC +0.0013%/hr (+11% APR) on 26 Sep 2026. Hourly maximum observed: 0.067%. Cross-exchange BTC average about 4.5–9.3% APR (Sep–Oct 2026) | defirate.com snapshots; spark.money | [SECONDARY] |
| Minimum order | About $10 notional | buildix.trade | [SECONDARY] |
| Gas | No gas to trade on HyperCore | — | [UNVERIFIED] |
| Bridge withdrawal | **1 USDC** fee | hyperliquid docs (bridge2) | [OFFICIAL] |
| Bridge minimums | 5 USDC deposit, 2 USDC withdrawal | eco.com | [SECONDARY] |

**3. Programmatic access**

- **SDK:** the official `hyperliquid-python-sdk` (GitHub hyperliquid-dex). [SECONDARY confirmation via Chainstack docs]
- **Agent (API) wallets:** the master account approves an agent with `approveAgent` (an EIP-712 user-signed action). The agent then signs orders but **cannot withdraw**. [SECONDARY] (Chainstack, OneKey)
- This is a good fit for a bot, because the bot key never controls withdrawals.
- **Custody:** you deposit USDC into the Arbitrum bridge contract. The account is self-custodial in the sense that only your key can withdraw.

**4. Geography**

- Third-party summaries of the Terms (§1.5) say only the **United States and Ontario** are named, plus anyone in sanctioned or embargoed territories. Citizens of those places are covered wherever they live. [SECONDARY] (datawallet, alphafinance, coinlatitude, learnhyper)
- The geoblock applies at the front end. The L1 itself does not block by IP. [SECONDARY]
- One summary says the terms expressly ban VPNs and location masking. [SECONDARY]
- **Jordan is not named.** [SECONDARY]

**5. Custody, liquidation and incidents**

| Item | Detail | Source | Label |
|---|---|---|---|
| Liquidation backstop | Liquidations go to the HLP / liquidator vault. Auto-deleveraging (ADL) is the last resort | hyperliquid.gitbook.io/.../auto-deleveraging | [OFFICIAL] |
| Bridge security | Withdrawals need sign-off from 2/3+ of validator power. L2Beat says funds can be stolen if a validator majority signs an invalid withdrawal | l2beat | [SECONDARY] |
| Mar 2025 – JELLY | Attempted manipulation. HLP was briefly down about $10M. Validators delisted JELLY and settled at a fixed price, which drew centralization criticism | thedefiant | [SECONDARY] |
| Mar 2025 – ETH 50x whale | HLP lost about $4M. Leverage was then cut to 40x/25x | crypto.news | [SECONDARY] |
| Jul 2025 – TST | Another manipulation event | yahoo/finance | [SECONDARY] |
| 10 Oct 2025 – ADL | **First cross-margin ADL in over two years** during the market crash. Some profitable shorts were force-closed | wublock substack, mexc news | [SECONDARY] |
| Nov 2025 – POPCAT | HLP took about **$4.9M** of bad debt, and deposits and withdrawals were paused briefly | The Block, Cointelegraph, The Defiant | [SECONDARY] |

### A.4 Binance Futures (USDⓈ-M)

| Item | Detail | Source | Label |
|---|---|---|---|
| Fees | VIP0 **0.02% maker / 0.05% taker**. A BNB discount gives about 0.045% taker | binance.com/support/articles/360033544231 (fee page) and secondary | [OFFICIAL (snippet) / SECONDARY] |
| Leverage | Up to **125x** on BTCUSDT | secondary | [SECONDARY] |
| Minimum notional | Reportedly 100 USDT for BTCUSDT | — | [UNVERIFIED] |
| Funding | Normally every 8h, with a 0.01%-per-period interest component. Hourly settlement on some contracts since May 2025 | spark.money | [SECONDARY] |
| API | API key with official connectors, `python-binance`, ccxt | — | [UNVERIFIED, prior knowledge] |
| **Jordan** | CitizenX and Finestel (2024) say **futures and derivatives are blocked for Jordan residents**, while spot, P2P and staking are allowed. No official Binance notice found | — | [SECONDARY] |
| Incidents | During the 10 Oct 2025 crash there was reportedly a USDe/wBETH depeg on Binance, followed by compensation | — | [UNVERIFIED, not confirmed by search] |

### A.5 Bybit

| Item | Detail | Source | Label |
|---|---|---|---|
| Fees | VIP0 USDT perps **0.055% taker / 0.02% maker**. "Actual fee rates may vary by region" | bybit.com/.../Benefits-of-the-VIP-Program | [OFFICIAL] |
| Leverage | About 100x on BTC/ETH | — | [SECONDARY] |
| Restricted list | Service Agreement §11.3, page dated 2026-05-07: US, mainland China, Hong Kong, Singapore, Canada, North Korea, Cuba, Iran, … (truncated). A KZ-language version is dated 2026-10-16 | bybit.com/en/help-center/article/Service-Restricted-Countries | [OFFICIAL, truncated] |
| Rest of the list | About 14 entries, including Uzbekistan, Syria, Sudan, Crimea, Sevastopol, Donetsk, Luhansk, and Dubai (global platform only). **Jordan not listed** | datawallet, coinperps | [SECONDARY] |
| API | `pybit` (official) | — | [UNVERIFIED, prior knowledge] |
| Incident | **21 Feb 2025: about $1.5B of ETH stolen** from a cold wallet, attributed to Lazarus. Reserves were replenished | NBC, Security Affairs, CNBC | [SECONDARY] |

### A.6 OKX

| Item | Detail | Source | Label |
|---|---|---|---|
| Fees | Regular user **0.02% maker / 0.05% taker** | datawallet, coinperps | [SECONDARY] (official Lv1 row not retrieved) |
| Leverage | Up to 100–125x, tiered by position size | — | [SECONDARY] |
| Restrictions | About 20 restricted jurisdictions in Risk and Compliance Disclosure §3 (updated 21 Mar 2026). VPN circumvention is prohibited | coinperps, coinex | [SECONDARY] |
| **Jordan** | Not mentioned anywhere I found | — | [UNVERIFIED] |
| API | `python-okx` (official), ccxt | — | [UNVERIFIED, prior knowledge] |

### A.7 Kraken (Derivatives)

| Item | Detail | Source | Label |
|---|---|---|---|
| Fees | Futures Tier 1 **0.02% maker / 0.05% taker** | kraken.com/pro/fees | [OFFICIAL] |
| Leverage | Up to 50x reported. EEA retail is limited to 10x | — | [SECONDARY] |
| Eligibility | Requires both KYC and geographic eligibility | support.kraken.com/articles/360023786632 | [OFFICIAL] |
| **Jordan** | Appears in a country list on Kraken's derivatives trading-competition terms. The snippet did not show whether the list is of included or excluded countries | support.kraken.com | [OFFICIAL but ambiguous] |

### A.8 Other venues that may serve Jordan

- **BitOasis** (UAE; acquired by CoinDCX in 2024) lists Jordan among the countries it serves. It is **spot only**, with no shorts. [SECONDARY]
- I found no evidence that Rain or CoinMENA serve Jordan.
- **No JSC-licensed Jordanian platform exists yet** (see Section B).

---

## B. Jordan: legal status of crypto trading and derivatives (as of 2026-11-03)

| # | Fact | Source | Label |
|---|---|---|---|
| 1 | **Law No. 14 of 2025 Regulating Dealings in Virtual Assets** was published in the Official Gazette on 16 Jun 2025 and took effect on **14 Sep 2025** | tamimi.com; alkhairattorneys.com | [SECONDARY] (gazette text not retrieved) |
| 2 | No entity may carry on virtual-asset activities **without a JSC licence** | jsc.gov.jo licensing regulation PDF; tamimi | [OFFICIAL (snippet)] |
| 3 | **VASP Licensing Regulation No. 94 of 2025**, issued under Art. 7 of Law 14. Applicants need JSC preliminary approval, then company registration, then a final licence. Only joint-stock companies qualify | jsc.gov.jo/Uploads/Files/The Virtual Asset Service Providers Licensing Regulation for the year 2025-.pdf | [OFFICIAL] |
| 4 | **Draft Executive Instructions for Virtual Asset Activities 2026** (127 articles) published for comment on 15 Jan 2026. They cover platform operators, custody, brokers and issuance. Whether a final version has been issued is not confirmed | jsc.gov.jo/News/en/12558; JSC Arabic draft PDF | [OFFICIAL] |
| 5 | Penalty for **unlicensed provision**: reported as up to 1 year in prison and/or JOD 50,000–100,000. Applies to providers | jordannews.jo; interlegal | [SECONDARY] |
| 6 | The law reaches **foreign platforms** that accept JOD, market to Jordanian residents, or make services accessible in the Kingdom | tamimi (law-firm analysis) | [SECONDARY] |
| 7 | **No VASP licensed** as of a JSC post dated 22 Sep 2026; applications still under review. Vexjo (a USDT OTC desk) says it completed all requirements in June 2026, but this is self-reported | jodrates.com; vexjo.com | [SECONDARY/UNVERIFIED] |
| 8 | **Derivatives and leverage:** I found no explicit ban and no explicit permission. GDF's consultation response says the treatment of derivatives is unclear. The draft text bars providers from **financing trading or using client assets as collateral without approval** | gdf.io consultation response; uianet | [SECONDARY] |
| 9 | **Individual users:** the penalties attach to unlicensed *providers*. One secondary source says P2P traders must move to licensed platforms. I found no source that penalises a resident for personal trading on foreign or self-custody platforms | globallawexperts; polymarket guide | [SECONDARY/UNVERIFIED] |
| 10 | **Central Bank of Jordan (CBJ):** warned the public in Nov 2019 that virtual currencies are not legal tender. It has **barred banks, exchange houses and payment providers from facilitating crypto**. In Jan 2022 the governor said trading could be allowed after legislation | jordantimes.com; freemanlaw; jordannews | [SECONDARY] |
| 11 | Under the 2025 framework the CBJ keeps payment-system authority. Banks may deal in virtual assets with prior CBJ approval | uianet; blockspot | [SECONDARY] |
| 12 | "CBJ Instructions No. 6/2026" letting banks deal with JSC-licensed VASPs | jodrates only; not found elsewhere | [UNVERIFIED] |
| 13 | Cabinet agreed in Oct 2025 to end the trading ban, with the JSC's framework to follow | agbi.com (Nov 2025) | [SECONDARY] |

**Practical reading (my synthesis, not legal advice):**
- Holding crypto is now inside a legal framework, but no domestic licensed venue exists yet.
- Offshore centralized exchanges serving Jordanians are unlicensed in Jordan, and the law aims to cover them.
- Leveraged derivatives have no defined home in the regime.
- A personal bot trading on a self-custody DEX does not appear to be a licensable "activity" by the user. Still, bank-rail restrictions apply to moving money in and out, and the status of leveraged trading is **not settled**.
- **Get written advice from Jordanian counsel before trading derivatives.**

---

## C. Wrapped-asset risk on Solana for the bot's current spot holdings

### cbBTC (Coinbase Wrapped BTC)

- **Solana mint:** `cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij`. [SECONDARY] (Solflare token page URL)
- **Issuer and backing:** Coinbase. Each cbBTC is backed 1:1 by BTC that Coinbase holds. [OFFICIAL] (cbBTC white paper, coinbase.bynder.com)
- **Mint and burn:**
  - A withdrawal of BTC from a Coinbase account to Solana mints cbBTC.
  - **Redemption means depositing cbBTC back into a Coinbase account.** The cbBTC is burned and BTC is credited.
  - Mint and freeze authority sit under Coinbase's key system.

  Sources: white paper [OFFICIAL]; coindesk and secondary sources [SECONDARY].
- **Who can redeem:** only verified Coinbase customers in jurisdictions Coinbase supports. A 2019 TechCrunch list included Jordan for crypto-to-crypto trading, but **current Coinbase support for Jordan is unverified**. [UNVERIFIED]
  - If the owner cannot hold a Coinbase account, the only exit is selling on the market.
- **Proof of reserves:** Coinbase runs a live cbBTC PoR page that updates about every minute and discloses BTC addresses. [OFFICIAL] (docs.cdp.coinbase.com)
  - Launch snapshot (early 2025): 26,525 BTC held against 26,461 cbBTC supply, of which **2,673 cbBTC was on Solana**. [SECONDARY] (crypto-economy, beincrypto; stale)
- **Risks:**
  - Custodial and issuer risk with a single company.
  - Coinbase can freeze tokens.
  - Redemption is permissioned.
- **Solana liquidity, from DexPaprika snapshots (Jul–Sep 2026)** [SECONDARY]:

  | DEX | Pool | Liquidity | 24h volume |
  |---|---|---|---|
  | Orca | cbBTC/USDC | about $5.0M | about $10.5M |
  | Orca | SOL/cbBTC | about $5.0M | — |
  | Orca | cbBTC/WBTC | about $1.3M | — |
  | Meteora | cbBTC/USDC | about $1.2M | — |
  | Meteora | cbBTC/LBTC | about $2.9M | — |

  Pools total roughly $15M or more. That is adequate for $100–$1,000 orders.

### WETH, Wormhole-bridged (`7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs`)

- **Bridge model:**
  - ETH is locked in the Wormhole Token Bridge on Ethereum, and wrapped tokens are minted on Solana once a Guardian-signed message (VAA) is verified. [OFFICIAL] (wormhole.com/docs/learn/security; l2beat)
  - **19 Guardians, quorum 13 of 19.** [OFFICIAL]
- **Redemption path:** you redeem permissionlessly through the Wormhole Token Bridge (Portal). The wrapped token is burned on Solana, the VAA is signed, and ETH is released on Ethereum. You need ETH on Ethereum to pay gas. [OFFICIAL/SECONDARY]
- **The Feb 2022 exploit:**
  - A signature-verification bug (a spoofed sysvar account through the deprecated `load_instruction_at`) let the attacker **mint 120,000 WETH without backing**. 93,750 WETH was bridged out. Loss was about $320–326M. [SECONDARY] (Merkle Science, Ackee, The Block)
  - **Jump Crypto restored 120k ETH the same day**, and Wormhole said all WETH was backed 1:1 again. [SECONDARY]
- **Depeg history:** during the exploit, Solana WETH traded about **$200 below ETH** on Mango Markets before re-pegging. [SECONDARY] (stockhead / forkast) I found no other major depeg.
- **Liquidity is THIN:**

  | Pool / measure | Size | Source | Label |
  |---|---|---|---|
  | Largest pool found: Meteora WETH/SOL | about $119–197K | DexPaprika, 28 Oct 2026 | [SECONDARY] |
  | Orca WETH/JitoSOL | about $38K | DexPaprika | [SECONDARY] |
  | Token-level aggregate | about $9.4M liquidity, $90M market cap | madeonsol | [SECONDARY] |

  - A $1,000 trade is fine, but the bot should **check quoted price impact on every swap**.
  - Wormhole WETH is a legacy wrapper on Solana now. A natively issued or NTT ETH token may have deeper liquidity, so worth checking. [UNVERIFIED]

### SOL

- Native SOL carries no wrapper risk. wSOL is just the SPL-token wrapper the native mint program uses for DEX swaps, and unwrapping to SOL is permissionless (closing the token account).
- It carries **no issuer or bridge risk**. [UNVERIFIED, standard Solana design]

### Relevance to shorts

- A perps venue removes wrapper risk from the *short* leg, because you post USDC/USDT collateral rather than holding wBTC, cbBTC or WETH.
- On Jupiter Perps, a long is collateralised in the underlying token (SOL, ETH or wBTC custody). A short is collateralised in a stablecoin. [UNVERIFIED, from the custody model]
- Stablecoin collateral brings its own issuer risk: USDC/USDT, and USDT settlement on Velocity.

---

## D. Round-trip cost comparison (open + close)

### Assumptions (all labelled)

1. **Fees:** the lowest retail tier, **taker on both legs**. A bot could post maker orders on order-book venues to cut costs.
2. **Spread/slippage, half-spread paid on each leg** [UNVERIFIED estimates for $100–$1,000 sizes]:
   - CEX (Binance/Bybit/OKX): BTC/ETH 0.005%, SOL 0.01%.
   - Kraken: BTC/ETH 0.01%, SOL 0.02%.
   - Hyperliquid: BTC/ETH 0.01%, SOL 0.02%.
   - Jupiter Perps fills at the oracle price, with **no spread**, but charges a price-impact fee. I assume 0.005% per leg at these tiny sizes. [UNVERIFIED]
   - Velocity: 0.05% per leg (new, thin book). [UNVERIFIED]
3. **Network:** Jupiter Perps needs about 4 Solana transactions (request plus keeper, for both open and close). I assume about **$0.02 in total**, including a modest priority fee. [UNVERIFIED]
   - Hyperliquid has no per-trade gas. The one-off 1 USDC withdrawal fee and Arbitrum gas are excluded.
   - CEX deposit and withdrawal costs are excluded.
4. **Holding cost per day:**
   - Funding venues: **0.03%/day**, which is 0.01% per 8h, about 11% APR, the typical positive baseline. [SECONDARY] (defirate/spark)
     - When funding is positive, **longs pay and shorts receive it**. The table shows it as a cost magnitude, so a short would usually *earn* this.
   - Jupiter borrow fee, **paid by both sides**: BTC/ETH about **15% APR = 0.041%/day**; SOL about **33% APR = 0.090%/day**. These are the Feb-2025 figures from Jupiter governance. [SECONDARY, dated]
5. **Velocity fee:** I use 0.035% taker. The range is 0.02–0.10%. [UNVERIFIED]

### Arithmetic

Round-trip % = 2 × (taker fee) + 2 × (half-spread or impact). Dollar cost = % × notional, plus fixed network cost.

| Venue | Asset | 2×taker | 2×half-spread / impact | Round-trip % | $100 position | $1,000 position | Holding cost/day ($100 / $1,000) |
|---|---|---|---|---|---|---|---|
| Hyperliquid | BTC, ETH | 2×0.045 = 0.090% | 2×0.01 = 0.02% | **0.110%** | **$0.11** | **$1.10** | ±$0.03 / ±$0.30 (funding) |
| Hyperliquid | SOL | 0.090% | 2×0.02 = 0.04% | 0.130% | $0.13 | $1.30 | ±$0.03 / ±$0.30 |
| Binance USDⓈ-M | BTC, ETH | 2×0.05 = 0.100% | 0.01% | 0.110% | $0.11 * | $1.10 | ±$0.03 / ±$0.30 |
| Binance USDⓈ-M | SOL | 0.100% | 0.02% | 0.120% | $0.12 * | $1.20 | ±$0.03 / ±$0.30 |
| OKX | BTC, ETH | 0.100% | 0.01% | 0.110% | $0.11 | $1.10 | ±$0.03 / ±$0.30 |
| OKX | SOL | 0.100% | 0.02% | 0.120% | $0.12 | $1.20 | ±$0.03 / ±$0.30 |
| Bybit | BTC, ETH | 2×0.055 = 0.110% | 0.01% | 0.120% | $0.12 | $1.20 | ±$0.03 / ±$0.30 |
| Bybit | SOL | 0.110% | 0.02% | 0.130% | $0.13 | $1.30 | ±$0.03 / ±$0.30 |
| Kraken | BTC, ETH | 0.100% | 0.02% | 0.120% | $0.12 | $1.20 | ±$0.03 / ±$0.30 |
| Kraken | SOL | 0.100% | 0.04% | 0.140% | $0.14 | $1.40 | ±$0.03 / ±$0.30 |
| **Jupiter Perps** | BTC, ETH | 2×0.06 = 0.120% | 2×0.005 = 0.01% | 0.130% (+$0.02 gas) | 0.13 + 0.02 = **$0.15** | 1.30 + 0.02 = **$1.32** | **−$0.041 / −$0.41** (borrow, always paid) |
| **Jupiter Perps** | SOL | 0.120% | 0.01% | 0.130% (+$0.02 gas) | $0.15 | $1.32 | **−$0.090 / −$0.90** |
| Velocity (ex-Drift) | BTC, ETH, SOL | 2×0.035 = 0.07% | 2×0.05 = 0.10% | 0.170% (+ about $0.01 gas) | about $0.18 | about $1.71 | ±$0.03 / ±$0.30 (funding) [UNVERIFIED] |

\* The Binance BTCUSDT minimum notional is reportedly 100 USDT [UNVERIFIED], so a $100 position sits at the minimum.

**Worked examples**

- **Hyperliquid BTC, $1,000:**
  - Fees: 1,000 × 0.045% × 2 = $0.90.
  - Spread: 1,000 × 0.01% × 2 = $0.20.
  - Total: **$1.10**.
- **Jupiter Perps SOL short, $1,000, held for 3 days:**
  - Open: 1,000 × 0.06% = $0.60. Close: $0.60.
  - Impact: about $0.10. Gas: about $0.02.
  - Borrow: 1,000 × 0.090% × 3 = $2.70.
  - Total: **about $4.02**.
- **The same SOL short on Hyperliquid, held 3 days:**
  - Trading costs: $1.30.
  - Funding: at +0.03%/day the short **receives** about 1,000 × 0.03% × 3 = $0.90.
  - Net: **about $0.40**.
- **Break-even:** Jupiter's borrow fee makes multi-day holds 2–4 times more expensive than on funding-rate venues while funding is positive. If funding turns sharply negative (shorts pay), the gap narrows.

**Comparison with the bot's current spot route (context, not part of the table).** A Jupiter-aggregated spot swap pays the pool fee, typically 0.01–0.30% per leg depending on the pool. It also pays price impact, which can be material for Wormhole WETH given its thin liquidity. [UNVERIFIED]

---

## Recommendations (synthesis)

1. **First choice for shorts: Hyperliquid.**
   - Lowest round-trip cost and an official Python SDK.
   - Agent wallets cannot withdraw, which limits the damage if the bot key leaks.
   - Jordan is not on its restricted list.
   - Risks: the small bridge validator set, and HLP/ADL events during manipulation or crash episodes. Keep only working capital on the venue.
2. **Solana-native alternative: Jupiter Perps.**
   - It reuses the bot's existing Solana wallet and RPC.
   - Its borrow fee suits **short holding periods** (hours to a couple of days).
   - The bot will need to build Anchor instructions itself and handle the keeper-request flow.
3. **Avoid Drift/Velocity** until a public relaunch, audits and several months of clean operation.
4. **CEX:**
   - Bybit and OKX do not appear to exclude Jordan, but this is unconfirmed. Check their eligibility at KYC before relying on them.
   - Binance futures are likely closed to Jordan residents.
   - All of them need KYC and API keys, and they are unlicensed in Jordan.
5. **Legal:** get advice from Jordanian counsel on (a) leveraged and derivative trading by a resident on foreign or self-custody venues, and (b) moving money through banks while no JSC-licensed VASP exists.
6. **Before going live, re-verify on the live pages:**
   - Hyperliquid perpetual-assets and fees pages.
   - Jupiter's fees, FAQ and Terms ("Prohibited Localities").
   - Bybit's restricted-countries page.
   - OKX's Risk and Compliance Disclosure §3.
   - Kraken's derivatives eligibility page.
   - Live borrow and funding rates.

---

## Source list

All accessed 2026-11-03, through search-engine snippets only. Direct fetch failed with ENOTFOUND for docs.jup.ag, hyperliquid.gitbook.io, bybit.com and jsc.gov.jo.

### Jupiter
- https://docs.jup.ag/user-docs/trade/perps/fees (and .md) [OFFICIAL]
- https://docs.jup.ag/user-docs/trade/perps/faq [OFFICIAL]
- https://docs.jup.ag/user-docs/trade/perps/positions-and-collateral [OFFICIAL]
- https://docs.jup.ag/user-docs/trade/perps/liquidation [OFFICIAL]
- https://station.jup.ag/guides/perpetual-exchange/how-it-works [OFFICIAL]
- https://developers.jup.ag/docs/perps/position-account [OFFICIAL]
- https://developers.jup.ag/docs/perps/position-request-account [OFFICIAL]
- https://developers.jup.ag/docs/perps/custody-account [OFFICIAL]
- https://developers.jup.ag/docs/perps/pool-account [OFFICIAL]
- https://developers.jup.ag/docs/legal/terms-of-use [OFFICIAL]
- https://jup.ag/mobile/terms [OFFICIAL]
- https://docs.jup.ag/user-docs/global/mobile/faq [OFFICIAL]
- https://dtf.jup.ag/file/jup-dtf-terms-of-service.pdf [OFFICIAL]
- https://jupiter-global-assets.jup.ag/Jupiter%20Card%20Prohibitions%20List.pdf [OFFICIAL]
- https://discuss.jup.ag/t/gauntlet-borrowing-rates-recommendations-08-02-2024-and-jupiter-competitiveness-with-binance/21091 [SECONDARY]
- https://discuss.jup.ag/t/gauntlet-updated-borrow-fee-recommendations-10-11-24/23213 [SECONDARY]
- https://discuss.jup.ag/t/jupiter-borrowing-rate-analysis-recommendations-02-04-25/34842 [SECONDARY]
- https://docs.rs/carbon-jupiter-perpetuals-decoder/latest/src/carbon_jupiter_perpetuals_decoder/lib.rs.html [SECONDARY]
- https://www.helius.dev/parsed-data/jupiter/perps [SECONDARY]
- https://glama.ai/mcp/servers/@diaorui/jupiter-perps-mcp [SECONDARY]
- https://messari.io/asset/jupiter-perpetuals-liquidity-provider-token [SECONDARY]

### Drift / Velocity
- https://drift-labs.github.io/v2-teacher/ [OFFICIAL]
- https://pypi.org/project/driftpy [OFFICIAL]
- https://trmlabs.com/resources/blog/north-korean-hackers-attack-drift-protocol-in-285-million-heist [SECONDARY]
- https://decrypt.co/363087 [SECONDARY]
- https://decrypt.co/366897/how-solana-exchange-drift-repay-users-295-million-crypto-hack [SECONDARY]
- https://cointelegraph.com/news/drift-protocol-exploit-preparation-preliminary-findings [SECONDARY]
- https://cointelegraph.com/news/tether-150m-recovery-drift-protocol [SECONDARY]
- https://www.theblock.co/post/397735/drift-127-million-tether-recovery-pivots-circle-usdc-usdt-280-million-exploit [SECONDARY]
- https://thedefiant.io/news/defi/drift-protocol-rebrands-to-velocity-dex-ahead-of-relaunch [SECONDARY]
- https://www.cryptotimes.io/2026/07/02/drift-rebrands-to-velocity-ahead-of-private-beta-launch/ [SECONDARY]
- https://solanaguides.com/best-solana-perp-dex-comparison-2026-drift-vs-jupiter-vs-pacifica [SECONDARY]
- https://builtonbulk.xyz/bulk-vs-drift/ [SECONDARY]
- https://www.rfp.wiki/crypto/exchanges-trading/crypto-trading-liquidity/flowdesk/drift-protocol [SECONDARY]
- https://eco.com/support/en/articles/15083167-drift-protocol-solana-perpetuals-dex-deep-dive [SECONDARY]
- https://coinfomania.com/velocitydex-goes-live-with-private-beta-offering-0-02-fees/ [UNVERIFIED]

### Hyperliquid
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees [OFFICIAL]
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding [OFFICIAL]
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/perpetual-assets [OFFICIAL]
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers [OFFICIAL]
- https://hyperliquid.gitbook.io/Hyperliquid-docs/trading/auto-deleveraging [OFFICIAL]
- https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/bridge2 [OFFICIAL]
- https://docs.chainstack.com/docs/hyperliquid-authentication-guide [SECONDARY]
- https://onekey.so/blog/ecosystem/hyperliquid-wallet-integration-guide/ [SECONDARY]
- https://www.datawallet.com/crypto/hyperliquid-supported-and-restricted-countries [SECONDARY]
- https://alphafinance.io/blog/hyperliquid-restricted-countries-and-regulation/ [SECONDARY]
- https://coinlatitude.com/hyperliquid-restricted-countries.html [SECONDARY]
- https://learnhyper.com/access/countries/ [SECONDARY]
- https://crypto.news/hyperliquid-will-adjust-leverage-limits-for-btc-and-eth-after-the-recent-50x-eth-liquidation-event/ [SECONDARY]
- https://www.buildix.trade/blog/hyperliquid-max-leverage-btc-eth-sol-all-pairs-2026 [SECONDARY]
- https://thedefiant.io/hyperliquid-to-compensate-jellyjelly-traders-and-strengthen-risk-protocols [SECONDARY]
- https://wublock.substack.com/p/hyperliquid-activates-cross-margin [SECONDARY]
- https://www.theblock.co/post/378606/hyperliquid-pauses-deposits-withdrawals-popcat-trading-scheme-speculation [SECONDARY]
- https://cointelegraph.com/news/hyperliquid-hlp-popcat-attack-3m-wipeout [SECONDARY]
- https://finance.yahoo.com/news/hyperliquid-hit-third-market-manipulation-113215395.html [SECONDARY]
- https://pr11038-production.previews.l2beat.com/bridges/projects/hyperliquid [SECONDARY]

### Funding data
- https://defirate.com/perp/bitcoin/snapshot/2026-09-26T0502/ [SECONDARY]
- https://defirate.com/perp/snapshot/2026-10-16T1822/ [SECONDARY]
- https://www.spark.money/tools/funding-rate-comparison [SECONDARY]

### Centralized exchanges
- https://www.binance.com/en/support/articles/360033544231 [OFFICIAL]
- https://citizenx.com/insights/binance-kyc-with-jordan-passport/ [SECONDARY]
- https://finestel.com/blog/binance-supported-banned-countries-2023/ [SECONDARY]
- https://bybit.com/en/help-center/article/Service-Restricted-Countries [OFFICIAL]
- https://www.bybit.com/kk-KZ/help-center/article/Service-Restricted-Countries [OFFICIAL]
- https://www.bybit.com/fr-FR/help-center/article/Benefits-of-the-VIP-Program [OFFICIAL]
- https://www.datawallet.com/es/cripto/países-con-bybit-restringido [SECONDARY]
- https://www.coinperps.com/ru/learn/bybit-restricted-countries [SECONDARY]
- https://www.coinperps.com/ru/learn/okx-restricted-countries [SECONDARY]
- https://www.coinex.com/en/academy/wiki/5419-okx-restricted-countries-what-you-need-to-know [SECONDARY]
- https://www.datawallet.com/crypto/okx-futures-review [SECONDARY]
- https://www.kraken.com/pro/fees [OFFICIAL]
- https://support.kraken.com/articles/360023786632-kraken-derivatives-eligibility [OFFICIAL]
- https://support.kraken.com/articles/15478742641300-derivatives-trading-competitions-terms-conditions [OFFICIAL]
- https://www.nbcnews.com/tech/crypto/hackers-steal-15-billion-exchange-bybit-biggest-ever-crypto-heist-rcna193273 [SECONDARY]
- https://securityaffairs.com/174514/cyber-crime/lazarus-stole-1-5b-from-bybit-cryptocurrency-heist.html [SECONDARY]
- https://apps.apple.com/app/id1521661794 (BitOasis) [SECONDARY]

### Jordan
- https://www.jsc.gov.jo/Uploads/Files/The%20Virtual%20Asset%20Service%20Providers%20Licensing%20Regulation%20for%20the%20year%202025-.pdf [OFFICIAL]
- https://www.jsc.gov.jo/News/en/12437 [OFFICIAL]
- https://www.jsc.gov.jo/News/en/12558 [OFFICIAL]
- https://www.jsc.gov.jo/Uploads/Files/مشروع التعليمات التنفيذية لأنشطة الأصول الافتراضية لسنة 2026.pdf [OFFICIAL]
- https://www.tamimi.com/our-knowledge/publications/eyes-on-2026/articles/cryptocurrency-comes-to-jordan-how-law-no-14-positions-the-kingdom-in-the-global-digital-economy/ [SECONDARY]
- https://www.uianet.org/en/news/virtual-assets-in-jordan-a-new-regulatory-era-an-analysis-of-the-legal-framework-governing-virtual-asset-services [SECONDARY]
- https://alkhairattorneys.com/the-regulating-dealings-in-virtual-assets-no-14-of-2025-jordans-leap-toward-formalizing-the-blockchain-digital-economy [SECONDARY]
- https://www.jordannews.jo/Section-113/All/Jordan-Introduces-2025-Licensing-and-Regulatory-Framework-for-Virtual-Asset-Service-Providers-VASPs-45922 [SECONDARY]
- https://www.gdf.io/wp-content/uploads/2020/12/GDF_JordanianConsultationResponseEN19.08.25.pdf [SECONDARY]
- https://www.agbi.com/finance/2025/11/jordan-to-have-crypto-currency-law-by-end-of-the-year/ [SECONDARY]
- https://jordantimes.com/news/local/central-bank-warns-against-using-bitcoin [SECONDARY]
- https://freemanlaw.com/cryptocurrency/jordan/ [SECONDARY]
- https://jordannews.jo/Section-29/Analysis/Will-the-Central-Bank-of-Jordan-ever-embrace-cryptocurrency-4482 [SECONDARY]
- https://jodrates.com/en/news/virtual-assets-roundup-oct-2026/ [SECONDARY/UNVERIFIED]
- https://vexjo.com/blog/en/is-crypto-legal-jordan/ [SECONDARY/UNVERIFIED]
- https://globallawexperts.com/crypto-regulation-jordan/ [SECONDARY]
- https://blockspot.io/country/jordan/ [SECONDARY]

### Wrapped assets and Solana
- https://coinbase.bynder.com/m/1303c2f4d78fc966/original/cbBTC-White-Paper.pdf [OFFICIAL]
- https://docs.cdp.coinbase.com/custom-stablecoins/core-concepts/proof-of-reserves [OFFICIAL]
- https://crypto-economy.com/coinbase-publishes-bitcoin-reserves-for-cbbtc-heres-what-you-need-to-know/ [SECONDARY]
- https://www.coindesk.com/price/cbbtc [SECONDARY]
- https://www.solflare.com/prices/coinbase-wrapped-btc/cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij [SECONDARY]
- https://techcrunch.com/?p=1826415 (Coinbase country list, about 2019) [SECONDARY]
- https://dexpaprika.com/solana/orca [SECONDARY]
- https://dexpaprika.com/solana/meteora [SECONDARY]
- https://dexpaprika.com/solana/pool/3potGhhaNyzvCNq1CGh2wLbZLLMjpdPtG1Hpj8q2DDVR [SECONDARY]
- https://madeonsol.com/token/7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs [SECONDARY]
- https://wormhole.com/docs/learn/security [OFFICIAL]
- https://l2beat.com/interop/protocols/wormhole-wtt [SECONDARY]
- https://www.merklescience.com/blog/hack-track-analysis-of-wormhole-token-bridge-exploit [SECONDARY]
- https://ackee.xyz/blog/2022-solana-hacks-explained-wormhole/ [SECONDARY]
- https://www.theblock.co/post/132909/wormhole-replenishes-its-blockchain-bridge-after-325-million-exploit [SECONDARY]
- https://forkast.news/solana-defi-meltdown-wormhole-loss-replaced/ [SECONDARY]
- https://solana.com/docs/core/fees/fee-structure [OFFICIAL]
