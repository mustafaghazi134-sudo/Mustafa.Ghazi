# Feasibility: long and short trading, alternative strategies, and wrapped assets

Date 2026-11-03.

| Question | Answer |
|---|---|
| Can we trade long and short? | Technically yes, on a perpetual-futures venue. **No short or long/short rule earned its costs in this study**, so nothing has been built. |
| Is there a better strategy than the current one? | **No.** All 12 pre-registered configurations are rejected; none reaches even the forward-paper tier. |
| Verdict | Keep the current approach in **paper only**. No qualifying strategy has been found for real money, so no funded limits are requested. |

Inputs:

| Input | Location |
|---|---|
| Pre-registration (committed before any result) | `docs/LONGSHORT_PREREGISTRATION.md`, commit `c17b93a` |
| Results | `research_lab/results/longshort_study.json` (script `research_lab/longshort_study.py`, seed 20261103) |
| No-edge calibration | `research_lab/results/longshort_null_calibration.json` |
| Wrapper tracking | `research_lab/results/wrapped_tracking.json` |
| Funding history, pool data, official pages | `research_lab/data_ext/`, fetched by read-only GitHub Actions jobs |

The paper experiment (run #3, `paper_evidence/run-41594871774/`) was not touched.

---

## 1. Can the existing architecture support genuine long and short positions?

### 1.1 What the system is today

It is **spot only, long or flat**:

- `Position.qty` is always positive.
- Portfolio accounting debits cash for the full notional (`portfolio/accounting.py`).
- A `SELL` order may only reduce a held position (`risk/engine.py`, `reduce_only`).
- Live execution swaps tokens through Jupiter on Solana. `execution/tx_verify.py` allows only the Jupiter swap programs.
- Balances are reconciled from token balances in the wallet.

You cannot short on a spot swap venue. A genuine short needs a **perpetual future** or a **borrow-and-sell margin market**.

### 1.2 Minimum changes for long/short (not built)

| Area | Change |
|---|---|
| Venue adapter | A new `PerpBroker`: order placement, cancel, venue-side position, funding, mark price |
| Position model | Signed quantity; collateral and margin; unrealised P&L at mark; funding or borrow accruals; liquidation price |
| Accounting and ledger | Margin accounting instead of spot cash; funding events; realised P&L on partial closes and flips |
| Risk engine | Short entries; leverage cap 1x; minimum distance to liquidation; funding-cost limit; daily loss includes funding |
| Exits | Resting **reduce-only stop orders on the venue**. Hyperliquid documents Stop Market, Stop Limit and Trailing Stop orders (official order-types page). This would also close readiness blocker 3, "no process-independent stop". |
| Signing | Hyperliquid signs EIP-712 messages with an "API wallet" that a master account approves (official). The current `tradebot_signer` signs Solana transactions only. On Jupiter Perps, `tx_verify` would need the perps program and its request accounts allowlisted and decoded. |
| Reconciliation | Positions from the venue API, not wallet balances. Jupiter Perps fills are asynchronous: a keeper executes requests. |
| Paper mode | A perp paper broker with funding accrual and mark-price data |

**Estimate:** 2–4 weeks of engineering and failure-injection testing before paper trading on it. It is **not worth building**, because no long/short rule qualified (section 2).

### 1.3 Venues, costs and availability for a Jordan-based user

Labels:

- **[O]** checked on the official page fetched today (`research_lab/data_ext/official_pages/`).
- **[S]** secondary sources only.
- **[M]** measured from fetched data.

| Venue | Markets | Cost | Bot access | Availability for Jordan | Notes |
|---|---|---|---|---|---|
| **Hyperliquid** | BTC, ETH, SOL perps | Taker 0.045%, maker 0.015% at the base tier [O]. Funding paid hourly, capped at 0.5%/h [O] (the research agent's "4%/h" was wrong). Measured funding 2024–2026: about **12–14% a year paid by longs** for BTC, ETH and SOL [M]. | Official API; API wallets sign on behalf of the account [O] | Official terms could not be read (JavaScript app). Secondary sources name the US, Ontario and sanctioned places; **Jordan is not named** [S]. | Not Solana: USDC must be bridged from Arbitrum. Vault losses in market-manipulation events (JELLY Mar 2025, POPCAT Nov 2025) [S]. |
| **Jupiter Perps** (Solana) | SOL, ETH, BTC | 0.06% to open and 0.06% to close [O], plus price impact, plus an hourly borrow fee that **both sides pay** [O]. Borrow fee level about 15% a year for BTC/ETH and 33% for SOL in Feb 2025 [S]. | Program accounts, keeper-executed; no official Python SDK [S] | The terms restrict sanctioned or listed persons [O]; Jordan is not named | Borrow fee on both sides makes long holding expensive |
| **Binance USD-M** | All three | Taker about 0.05% [S]. Measured funding 2024–2026: **5–7% a year paid by longs** for BTC and ETH, 5% for SOL [M]. | Mature API | **Unverified.** Secondary sources say Jordan residents are blocked from futures; the official terms page returned no content | — |
| **Bybit** | All three | Taker 0.055% [S] | Mature API | Jordan not in restricted lists seen in secondary sources; official page fetch failed | $1.5B hack, Feb 2025 [S] |
| **Kraken** | — | — | — | The fetched official page lists derivative restrictions for UK retail and Canada; Jordan is not mentioned [O]. Eligibility is unconfirmed | — |
| **Drift** | — | — | — | — | **Excluded.** About $280–285M exploit on 1 Apr 2026 (multiple news outlets) |

**Regulation in Jordan.**

- Law No. 14 of 2025 on Virtual Assets took effect on 14 Sep 2025.
- Providers need a Jordan Securities Commission licence (Regulation No. 94 of 2025), and unlicensed provision is a criminal offence [S: Al Tamimi, UIA, Jordan News; the JSC PDFs are on jsc.gov.jo but could not be fetched here].
- None of the sources found says whether a **resident individual** may use an unlicensed foreign venue or trade leveraged derivatives.
- **This is unresolved. Get advice from a Jordanian lawyer before opening any derivatives account.** No account was opened and nothing was funded.

**Cost per $1,000 position (perpetuals, taker):**

| Item | Hyperliquid | Jupiter Perps |
|---|---|---|
| Round-trip fee | about $0.90 | about $1.20 plus price impact |
| Slippage | about $0.2–0.6 | — |
| Holding cost | Long pays about $0.33–0.38 a day at 2024–26 funding; short receives it | Both sides pay about $0.41 a day (BTC/ETH) or $0.90 a day (SOL) [S] |

For a long-biased trend strategy, perpetual funding (5–14% a year) costs **more than the current spot route** (about 5.5% a year of deployed capital in trading costs, section 2).

---

## 2. Alternative strategies and horizons (pre-registered, run once)

**What was tested:**

- 6 rules with fixed parameters, nothing fitted.
- Each run long/short on a perpetual (8 bps per side plus actual Binance funding) and long-only on Jupiter spot (26 bps per side).
- 12 configurations, Bonferroni α = 0.0042.
- Fill at the next bar's open; one-bar delay and 2x cost stress checked.

**Windows:**

- **E**, evaluation: 2024-01-01 to 2026-10-24.
- **P**, earlier: 2019 or 2022 to 2023.

**Neither window is untouched; see the pre-registration.**

**Lookahead truncation test:** passed for every candidate and asset.

**Buy-and-hold, equal weight:**

- E: +12.5% a year, max drawdown −64%.
- P: +87.6% a year, max drawdown −85%.

**Results, window E (annualised, net):**

| Candidate | Bar | Long/short perp | Short leg (E, % of capital) | Long-only spot | Round trips a day (L/S) | Verdict |
|---|---|---|---|---|---|---|
| C1 tsmom (deployed rule) | 1d | −5.3% (P +45%) | −34.7 | **+15.3%**, Sharpe 0.60, DD −32%, p 0.17 (P +84%, p 0.003) | 0.21 | Both rejected |
| C2 EMA 12/26 (deployed momentum) | 4h | −6.0% (P +22%) | −37.8 | +2.0%, fails stress (−18.8%) and delay (−7.5%) | 0.75 | Both rejected |
| C3 Donchian 20/10 | 1d | −17.7% | −27.6 | −1.7% | 0.09 | Both rejected |
| C4 EWMAC ensemble | 1d | −12.5% | −23.6 | +6.1% (only 26 round trips) | 0.05 | Both rejected |
| C5 EMA 24/96, the "more frequent" test | 1h | −16.1% | −38.3 | −18.1% (costs 107% of capital vs gross 65%) | 1.20 | Both rejected |
| C6 1h mean reversion | 1h | −42.1% | −86.5 | −46.0% | 1.74 | Both rejected |

**Findings:**

1. **The short side lost money in every candidate, in both windows.** In E the short legs lost 24–87% of allocated capital. Crypto down-trends in this sample reversed sharply, and trend shorts were squeezed. Funding paid to shorts did not make up for it. The direction-randomised null puts every long/short candidate at or below the 85th percentile in E, against the 95th required: the long/short direction calls were no better than chance.
2. **More frequent trading made results worse.**
   - C5 at 1h had positive gross return, but costs exceeded it on both venues.
   - C6 mean reversion lost before costs.
   - This repeats the earlier audit's rejection of short-horizon rules (`STRATEGY_AUDIT_GO_NOGO.md`).
3. **The best result is the current daily tsmom rule, long-only on spot:**
   - it passes stress costs, delay, window P, 2 of 3 assets and ≥ 30 round trips;
   - but in E its p-value is 0.17;
   - so it is **not significant**, does not reach Tier B (p < 0.05), and beat buy-and-hold by only about 3 percentage points a year in E, though with half the drawdown.
4. **The 4h momentum rule's evidence is weak.** Long-only it turns negative under stress costs and with one bar of delay.
5. **No-edge calibration** (section 2.1): on markets with no edge, some configurations show large positive returns by chance; one random walk gave C3 +25% a year. A single good backtest is not evidence.

### 2.1 No-edge calibration

This check was added after the pre-registration and changes no criterion. The same code and criteria were run on
10 simulated markets with **no edge by construction**: fat-tailed hourly returns with zero log drift, BTC/ETH/SOL-like
volatility and the same calendar spans.

| Result over 120 configuration-runs | Count |
|---|---|
| Tier A passes | 0 |
| Tier B passes ("worth forward paper") | **3**, about 2.5% per configuration |

**What this means:**

- With 12 configurations, a no-edge world would give at least one Tier B result about **26% of the time**.
- So even a Tier B result here would have been weak evidence. The strict Tier A bar is justified.
- The daily long-only rules showed positive window-E returns on 6–9 of the 10 random walks. Volatility alone gives
  zero-log-drift prices an upward arithmetic drift, so a positive long-only backtest on these assets is easy to get by
  chance.

---

## 3. Execution readiness is not evidence of an edge

| What we have | What it shows | What it does **not** show |
|---|---|---|
| 301 + 5 tests and selftest 10/10 | The code does what its specification says, including failure handling | That any trade will be profitable |
| Keyless Jupiter quotes (HTTP 200) | Executable routes exist at the moment of quoting | That fills match quotes at the moment of a real signal, or under stress |
| Worst-case round trip at $100 (SOL 54, cbBTC 71, WETH 120 bps) | The cost bound used by paper fills | Realised slippage; the bound is worst-case and real fills are usually better |
| Paper runner on live data | The engine runs end to end on live prices | Any edge. 0 forward trades so far, because all three assets are in down-trends |
| Historical tests (this study and the earlier audit) | What the rules would have done on past data, with costs | Future returns. No result is significant after correcting for the number of tests |

**Profitability evidence today: none that qualifies.** Execution readiness for paper is good. For live it is built and gated off.

---

## 4. Wrapped assets: rechecked

The earlier claim was based on a few minutes of similar prices, and that is **not enough**. The new evidence:

| Asset | Tracking of pool price vs Binance close | Liquidity (GeckoTerminal, 2026-11-03) | Redemption and backing |
|---|---|---|---|
| **cbBTC** | 6 months daily, two deepest pools:<br>• median gap 17–19 bps<br>• 99th percentile 59–72 bps<br>• max 145 bps<br>6 weeks hourly: max 126–152 bps (2026-11-01 03:00) | cbBTC/USDC Orca $5.0M (24h volume $10.6M); cbBTC/SOL Orca $7.4M; several more pools above $1M | Only **Coinbase customers** can redeem, by depositing into a Coinbase account [O: coinbase.com/cbbtc]. Whether Coinbase serves Jordan residents is unverified (the help page refused the fetch). Coinbase can freeze the token [S]. |
| **WETH (Wormhole)** | 4.5–6 months daily:<br>• median gap 9–15 bps<br>• 99th percentile 44–86 bps<br>• max 248 bps<br>Hourly max **428 bps** (2026-10-04 18:00, WETH/SOL pool) | WETH/SOL Orca $6.3M. **Direct USDC pools are only $94k–205k**, so USDC→WETH routes through SOL in two hops. That explains the 120 bps worst-case round trip at $100. | Backed by the Wormhole bridge, 19 Guardians [O]. A 120k-WETH exploit in 2022 was made whole by Jump [S]. Redemption means bridging back to Ethereum, which carries bridge risk. |
| **SOL** | Native asset, no wrapper | Deep | No redemption risk |

**Conclusion:**

- **Normal conditions:** cbBTC and SOL track closely enough for a daily rule at orders of $100–1,000. WETH tracks less well and costs more to trade.
- **Stress conditions: not established.** The sample covers about 6 months with no bridge or custodian incident, and redemption by this user is not established.
- **What the engine already does:** it blocks new entries when the Jupiter-to-Binance gap exceeds 100 bps (`max_basis_bps`); exits are never blocked.
- **Remaining risks:** a custodian freeze (cbBTC) or a bridge failure (WETH) is a loss up to the position size. These assets should stay small, and cbBTC is preferable to WETH.

Limitations: GeckoTerminal prices are last-trade pool prices, not executable quotes. The DefiLlama long-history request failed (HTTP 400), so there is no multi-year tracking history.

---

## 5. Verdict

### Keep the current approach in paper. No qualifying alternative was found, and no strategy qualifies for real money.

- **Long/short:** rejected. Do not build the perpetuals adapter now. Every short leg lost money. Funding on perpetuals makes long holding dearer than on spot. Legal availability in Jordan is unresolved.
- **More frequent trading:** rejected at 4h and 1h for both trend and mean reversion. Costs consume the gross edge.
- **Current approach:** tsmom 1d is the best supported, but **unproven**. The 4h momentum rule is weaker; it fails cost stress and delay as long-only.

| Item | Current approach (tsmom 1d + momentum 4h, spot, long or flat) |
|---|---|
| Expected trade frequency | tsmom about 0.09 round trips a day across 3 assets (about 2.7 a month); momentum about 0.3 a day when trends turn up. **Zero while all three assets are in down-trends**, as now. |
| Validation time | 30 forward tsmom round trips take about **11 months**. Proving an edge of this size statistically is a different matter: a Sharpe of 0.6 needs about **11 years** of daily data for t = 2. Forward paper can confirm costs and execution, and can catch a broken rule, within months. It **cannot prove profitability within a year.** |
| Minimum practical capital | About $300–1,000:<br>• engine order minimum $5; position cap 10% of equity, total exposure 50%;<br>• at $1,000 orders are about $100, where measured worst-case round trips are 54–120 bps;<br>• network fees about $0.01–0.02 per swap. |
| Capital fraction compatible with the 10% drawdown kill | Historical max drawdown at full allocation: −32% (E) and −50% (P). That makes **20–30% average exposure** compatible, which the current 10%-per-position limits roughly give. |
| Total operating costs | • Trading costs about 5.5% a year of deployed capital, about $17 a year at $1,000 equity and 30% exposure<br>• Network fees about $1–2 a year<br>• Data $0 (Binance and Jupiter keyless, free RPC)<br>• Claude API off<br>• Host: your PC $0, or a small VPS at about $5–10 a month if you choose one |
| Expected size of outcome (historical, not a promise) | E: +15% a year on deployed capital, so about +$45 a year on $1,000 at 30% exposure, with about 10% account drawdowns. It could equally lose. |

**Funded limits are not requested**, because there is no qualifying configuration to review.

### What would change this verdict

- A forward paper record of at least 30 tsmom round trips, with realised costs at or below base and positive expectancy.
- That is necessary but not sufficient: the review in `STRATEGY_AUDIT_GO_NOGO.md` §9 also applies.
- Any new strategy idea needs its own pre-registration before it is tested. The six rules here are closed.

## 6. Deployment preparation done in this step (funding-independent)

| Item | Blocker it addresses |
|---|---|
| `windows/install_autostart.bat`: starts the **paper** engine at Windows sign-in and stops sleep on mains power, asking before each change | "Persistent machine" (`LIVE_READINESS_STATUS.md` §5, item 2): forward evidence needs an engine that keeps running |
| Read-only data and official-page fetch workflows | Venue verification and funding history |

**Deliberately not built:** a perpetuals adapter and live exit infrastructure. Neither resolves a blocker for a strategy that qualifies today.
