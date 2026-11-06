# External systems review (agent-generated from read-only GitHub clones, 2026-11-02)

> Copied verbatim from the research session. Local paths under `/tmp/.../scratchpad/refs2/` refer to throw-away clones used for citation only; re-clone the named upstream repos to verify. One stale remark: item 7 of the Hummingbot pattern list mentions a '150 s intent expiry' in TRADEBOT; that time-based expiry was removed in commit d2c8fef (intents now resolve only on chain evidence).

# External systems review for TRADEBOT

Scope: evaluate six external systems for integration into the existing Python Solana spot bot (`/home/user/Mustafa.Ghazi/TRADEBOT`, pure Python + httpx/pydantic/solders, Jupiter Swap v2 `GET /swap/v2/order` + `POST /swap/v2/execute`, Price v3, Tokens v2, Binance klines, Solana JSON-RPC, SQLite ledger, independent risk engine; see its `README.md`).

Evidence policy: every factual statement below cites a file in a repository cloned read-only into this session. Anything not supported by repo text is marked **UNVERIFIED** and collected in the final list. Nothing in any cloned repo was executed. Network access was GitHub-only, so no live API, docs site or pricing page was checked.

Path abbreviations used in citations:

| Abbrev | Local path | Upstream |
|---|---|---|
| `AS/` | `/tmp/claude-0/-home-user-Mustafa-Ghazi/e3f55eb5-8aba-51de-9337-3ad3bb889195/scratchpad/refs/agent-skills/` | `jup-ag/agent-skills` |
| `R2/` | `/tmp/claude-0/-home-user-Mustafa-Ghazi/e3f55eb5-8aba-51de-9337-3ad3bb889195/scratchpad/refs2/` | (all clones below) |
| `R2/hummingbot/` | shallow sparse clone | `hummingbot/hummingbot` (VERSION `2.17.0`, `R2/hummingbot/hummingbot/VERSION`) |
| `R2/gateway/` | shallow sparse clone | `hummingbot/gateway` (`"version": "2.17.0"`, `R2/gateway/package.json`) |
| `R2/freqtrade/` | shallow sparse clone (`docs/`, `freqtrade/optimize`, `freqtrade/freqai`, `freqtrade/data`) | `freqtrade/freqtrade` |
| `R2/helius/<repo>/` | shallow clones | `helius-labs/helius-sdk` (`"version": "3.4.0"`), `helius-labs/core-ai`, `helius-labs/laserstream-sdk`, `helius-labs/websocket-widget`, `helius-labs/quickstart-portfolio-tracker`, `helius-labs/helius-rust-sdk` |
| `R2/birdeye/<owner>_<repo>/` | shallow clones | `birdeye-so/tradingview-example-js-api`, `birdeye-so/birdeyeterminal-docs`, `birdeye-so/resources`, `TheNang2710/birdeye-data-api-docs`, `fcrre26/birdeye-api-docs`, `nickatnight/birdeye-py`, `tigusigalpa/birdeye-go`, `sondnm/bds-workshops`, `aiopinions/birdeye-mcp-server`, `birdeyeinc/birdeye-api-docs` |
| `R2/social/<owner>_<repo>/` | shallow sparse clones | `xdevplatform/twitter-api-typescript-sdk`, `praw-dev/praw`, `xai-org/xai-sdk-python`, `googleapis/google-api-python-client`, `kurtmckee/feedparser`, `reddit-archive/reddit` |

---

## 1. Helius (Solana RPC provider: WebSockets, Enhanced WebSockets, LaserStream, SDK, "Core AI")

### What it is

Helius is a Solana RPC/data provider. Its public GitHub org (`gh api search/repositories q='org:helius-labs'`, run in this session) contains, among others: `helius-sdk` (TypeScript), `helius-rust-sdk` (Rust), `laserstream-sdk` (JS/Rust/Go gRPC client), `core-ai` ("Official Helius AI tooling repository"), `websocket-widget` ("landing page to test Helius Geyser Enhanced Websockets"), `quickstart-portfolio-tracker`, `atlas-txn-sender`, `atlas-priority-fee-estimator`. **No Python SDK appears in the org listing.**

### Streaming products found in the repos

**Standard Solana WebSockets** (`R2/helius/core-ai/helius-skills/helius/references/websockets.md` lines 7-22, 340-346):
- Methods: `accountSubscribe`, `logsSubscribe`, `programSubscribe`, `signatureSubscribe`, `slotSubscribe`, `rootSubscribe`. Plan: "Free+ (all plans)". Filtering: "Basic (single account or program)". Cost: "2 credits per 0.1 MB streamed".
- Endpoint: `wss://mainnet.helius-rpc.com/?api-key=YOUR_API_KEY`.
- "10-minute inactivity timeout — send pings every 30 seconds"; "`blockSubscribe`, `slotsUpdatesSubscribe`, or `voteSubscribe` — these are unstable and not supported on Helius" (same file, "Common Mistakes").
- SDK surface: `helius.ws.logsNotifications / slotNotifications / signatureNotifications / programNotifications / accountNotifications` (`R2/helius/helius-sdk/src/websockets/wsAsync.ts` lines 49-66).

**Enhanced WebSockets** (`transactionSubscribe`, enhanced `accountSubscribe`):
- Endpoint `wss://atlas-${net}.helius-rpc.com/?api-key=...` (`R2/helius/helius-sdk/src/rpc/index.ts` line ~258; also `R2/helius/websocket-widget/README.md`).
- `TransactionSubscribeFilter`: `vote`, `failed`, `signature`, `accountInclude` (OR), `accountExclude`, `accountRequired` (AND); `TransactionSubscribeConfig`: `commitment` (processed|confirmed|finalized), `encoding`, `transactionDetails` (full|signatures|accounts|none), `showRewards`, `maxSupportedTransactionVersion` (`R2/helius/helius-sdk/src/websockets/types.ts` lines 6-43). Notification carries `transaction`, `signature`, `slot`, `transactionIndex` (same file lines 48-57).
- Client keeps a 10,000-notification buffer per subscription and a 30 s keepalive ping (`R2/helius/helius-sdk/src/websockets/enhancedWs.ts` lines 11-15).
- Claimed latency: "1.5-2x faster streaming", "up to 50,000" address filters (`wsAsync.ts` lines 43-47, 93-95). "Faster (powered by LaserStream infrastructure)" (`websockets.md` line 17).
- **Plan requirement is inconsistent across Helius' own files**: `wsAsync.ts` lines 44-47 and 95 say "require a Business+ plan"; `helius-sdk/llms.txt` line 455 table marks Enhanced WebSockets available on Developer ($49/mo); `websockets.md` line 11 says "Developer+"; `core-ai/.../onboarding.md` line 146 says Developer 150 connections, Business 250, Professional 1,000, Agent "No". Treat the minimum paid tier as **UNVERIFIED (Developer vs Business)**; it is certainly not on the free/agent tier.

**Parsed Streams** ("all plans"): `wss://beta.helius-rpc.com/?api-key=` serving `parsedTransactionSubscribe` with server-side filters `programs` (max 10), `instructionNames` (max 50), `accounts.include` (max 100), `accounts.roles`, `includeFailed`, `includeCpi`; only `commitment: "confirmed"`; `details: "full" | "matched" | "raw"`; `blockTime` is "Currently always `null`" (`R2/helius/helius-sdk/src/websockets/parsedStreams.ts` lines 8-13, 20-50, 60-70). Example in `llms.txt` lines 315-331 subscribes to the Jupiter program's `route` instruction via `describeProgram(JUPITER_PROGRAM_ID)`.

**Pre-confirmations WS**: binary frames on `wss://beta.helius-rpc.com`, "A pre-confirmation is an early signal, not a guarantee" (`R2/helius/helius-sdk/src/websockets/preconfWs.ts` lines 4-6, 70-72).

**LaserStream (gRPC / Geyser)** (`R2/helius/laserstream-sdk/README.md`; `R2/helius/core-ai/helius-skills/helius/references/laserstream.md`):
- "High-performance gRPC client", "drop-in replacement for Yellowstone gRPC"; subscription types: accounts, transactions, blocks, slots, block metadata, transaction status, entries, account data slices; unary RPCs `getSlot`, `getLatestBlockhash`, `subscribeReplayInfo`; dynamic "Stream Write" filter updates; gzip/zstd compression.
- Replay window is stated two ways: README says "Backfill missed data up to 3000 slots (~20 minutes)"; `laserstream.md` line 11 says "24-hour historical replay: replay up to 216,000 slots". **Conflict → UNVERIFIED.**
- Plan: "Mainnet requires Business+ plan ($499+/mo); Devnet available on Developer+ plans"; "2 credits per 0.1 MB of streamed data (uncompressed)" (`laserstream.md` lines 15-16). Nine mainnet regions (`laserstream.md` lines 36-50).
- SDKs: TypeScript (Rust core via NAPI), Rust, Go. **No Python SDK** (README "Official SDKs" section; `grep -i python` on the README returns nothing). `helius-sdk/README.md` line 46: "LaserStream gRPC streaming is not part of this SDK".

**Helius Sender** (`R2/helius/core-ai/helius-skills/helius/references/sender.md` lines 7-45): "Available on ALL plans, including free tier", "Consumes ZERO API credits", "Default 50 TPS", requires `skipPreflight: true`, `maxRetries: 0`, and a tip to a Sender tip account of at least 0.0002 SOL (dual routing) or 0.000005 SOL (SWQOS-only).

### Authentication, pricing, rate limits (as stated in repos)

API key in URL query (`?api-key=`) for RPC/WS. Plans table from `R2/helius/helius-sdk/llms.txt` lines 446-458:

| | Free | Developer $49/mo | Business $499/mo | Professional $999/mo |
|---|---|---|---|---|
| Monthly credits | 1M | 10M | 100M | 200M |
| RPC rate limit | 10 req/s | 50 | 200 | 500 |
| DAS / Enhanced API | 2 req/s | 10 | 50 | 100 |
| Standard WS | yes | yes | yes | yes |
| Enhanced WS | No | yes | yes | yes |
| LaserStream (2 credits/0.1 MB) | No | Devnet | Devnet+Mainnet | Devnet+Mainnet |

`R2/helius/core-ai/helius-skills/helius/references/onboarding.md` lines 136-156 instead lists an **Agent** plan "$10 USDC one-time" with 1M credits, 10 RPS, 1/s `sendTransaction`, 5 WS connections, no Enhanced WS, and extra credits at $5/M for paid tiers. Whether a $0 "Free" tier still exists alongside/instead of the $10 "Agent" tier is **UNVERIFIED** (two Helius files disagree). Credit costs (same file lines 150-155): 0 for Sender; 1 per standard RPC call / `sendTransaction` / Priority Fee API / webhook event; 2 per 0.1 MB streamed (all WS tiers and LaserStream); 10 for `getProgramAccounts`, DAS, historical data; 100 for Enhanced Transactions API, Wallet API, webhook management. 429 means "too many requests or out of credits" (`llms.txt` line 440).

### "Helius Core AI"

It **does exist**: `helius-labs/core-ai`, "The official Helius AI tooling repository" (`R2/helius/core-ai/README.md` line 1-3). It is developer tooling for LLM agents, not a market-intelligence or trading product: packages `helius-cli` (account signup incl. USDC autopay, balances, tx parsing, webhooks, WS subscriptions), `helius-mcp` (MCP server with 10 tools: `heliusAccount`, `heliusWallet`, `heliusAsset`, `heliusTransaction`, `heliusChain`, `heliusStreaming`, `heliusKnowledge`, `heliusWrite`, `heliusCompression`, `expandResult`), `helius-skills` (Claude Code skills, including `helius-jupiter`, `helius-dflow`, `helius-okx`, `helius-phantom`), `helius-plugin`, `helius-cursor` (README "Packages" table and "Public Tool Surface"). The streaming MCP tools are "config generators, not live streams — gRPC streams cannot run over MCP's stdio" (`laserstream.md` lines 20-30). Nothing in it provides signals, prices or trading logic. There is no product literally called "Helius Core AI" beyond this repo name.

### Integration into the Python bot

- Standard and Enhanced WS are JSON-RPC over WebSocket; a pure-Python client (`websockets` or `httpx`-adjacent `wsproto`) with a 30 s ping and reconnect/backoff is ~150 lines. The bot already depends on `solders` for signing, so decoding `accountSubscribe` payloads for SPL token accounts is feasible; decoding AMM pool state (Raydium/Orca/Meteora layouts) is not in scope of any Helius file read here and would be a large, venue-specific effort.
- Parsed Streams would let the bot watch Jupiter `route` executions without decoding (filter by `programs` + `instructionNames`), on all plans (`llms.txt` lines 315-331).
- LaserStream needs `grpcio` + protobuf stubs (protos exist under `R2/helius/laserstream-sdk/javascript/proto` and `go/proto`); compiled wheels conflict with the project's stated "pure Python" constraint (bot `README.md`: "no NumPy: Windows Application Control blocks numpy's _pcg64"). Effort L, Business+ plan.
- Sender is irrelevant while execution goes through Jupiter `/swap/v2/execute` (Jupiter submits); it only matters if the bot moves to `/swap/v2/build` + self-submission.

### Relevance

- (a) Read-only scanner polling every 1-10 s: the bot's current inputs (Binance klines, Jupiter Price v3) need no Helius. Standard WS on the free/agent tier could replace `getSignatureStatuses` polling (`signatureSubscribe`) and push wallet balance changes (`accountSubscribe`) at zero marginal cost beyond 2 credits/0.1 MB; Parsed Streams give on-chain Jupiter flow if a flow-based signal is ever researched. Moderate value, S-M effort.
- (b) Seconds-level execution: `signatureSubscribe` shortens confirmation detection; Enhanced/LaserStream do not change execution quality because Jupiter already lands the transaction.

### Risks

Plan-tier ambiguity for Enhanced WS (Developer vs Business, see above); silent 10-minute idle disconnects; unfiltered `transactionSubscribe` "streams ALL network transactions, extreme volume" (`websockets.md`, Common Mistakes) and burns credits; `blockTime` null in Parsed Streams; pre-confirmations are explicitly not guarantees; no Python SDK for LaserStream; vendor lock-in of endpoint URLs.

---

## 2. Hummingbot (and Hummingbot Gateway)

### What it is

"Hummingbot is an open-source framework that helps you design and deploy automated trading strategies ... on many centralized or decentralized exchanges" (`R2/hummingbot/README.md`). Python with Cython extensions (`.pyx` files throughout `hummingbot/core` and `hummingbot/strategy`), pandas/numpy dependencies (imports in `hummingbot/core/data_type/order_book_tracker.py`, `hummingbot/strategy_v2/backtesting/backtesting_engine_base.py`). Gateway is a separate TypeScript/Fastify REST server for DEX access (`R2/gateway/README.md`).

### Clock / tick loop

`R2/hummingbot/hummingbot/core/clock.pyx`: `Clock(clock_mode, tick_size=1.0, ...)`; in real-time mode `run_til()` sleeps until the next tick boundary (`next_tick_time = ((now // tick_size) + 1) * tick_size`), then calls `child_iterator.c_tick(current_tick)` on every registered `TimeIterator` (connectors and strategies), catching and logging exceptions per iterator so one failing component does not stop the loop (lines 88-128). `backtest_til()` advances ticks synthetically (lines 130-155). Strategy docstrings confirm "Clock tick entry point, is run every second (on normal tick setting)" (`strategy/amm_arb/amm_arb.py` line 175; `strategy/strategy_v2_base.py` line 297).

### Connector and order-book architecture

- Base classes in `hummingbot/connector/`: `connector_base.pyx`, `exchange_base.pyx`, `exchange_py_base.py`, `client_order_tracker.py`, `budget_checker.py`, `time_synchronizer.py`.
- `OrderBookTracker` (`hummingbot/core/data_type/order_book_tracker.py`, class at line 231): `start()` spawns tasks `listen_for_order_book_diffs`, `listen_for_trades`, `listen_for_order_book_snapshots`, `listen_for_subscriptions`, a diff router, a snapshot router and `_update_last_trade_prices_loop` (lines 293-322). It keeps per-pair metrics (diffs processed/rejected, snapshots, trades, `LatencyStats` with 100-sample rolling window and 1-in-10 sampling, lines 26-150).
- `OrderBookTrackerDataSource` (`order_book_tracker_data_source.py`) is the abstract WS data source: `_connected_websocket_assistant`, `_subscribe_channels`, `_process_websocket_messages`, `_parse_order_book_diff_message`, `_parse_trade_message`, `_request_order_book_snapshots` (lines 71-235). Model: REST snapshot + WebSocket diffs/trades, no polling for CEX books.

### Paper trading

`hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx` wraps a **real connector's live `OrderBookTracker`** without credentials (`paper_trade/__init__.py`: `create_paper_trade_market` builds `PaperTradeExchange(tracker, connector_class, exchange_name)` from `non_trading_connector_instance_with_default_configuration`).
- Market orders are queued and executed after `TRADE_EXECUTION_DELAY = 5.0` seconds (line 144; `c_process_market_orders` lines 609-626), filled by walking the live book `order_book.simulate_buy(amount)` and charging the VWAP (`c_execute_buy` lines 445-525). Fees come from the exchange's `TradeFeeSchema` via `build_trade_fee` (`hummingbot/core/utils/estimate_fee.py` lines 10-46).
- Limit orders fill when a live order-book trade event crosses the order price (`c_match_trade_to_limit_orders` lines 895-941) or when the book crosses (`c_process_crossed_limit_orders`). Every tick: `c_process_market_orders()` then `c_process_crossed_limit_orders()` (lines 319-322).
- Balances come from `paper_trade_account_balance` (default includes `SOL: 100`, `USDC: 100000`; `hummingbot/client/config/client_config_map.py` lines 199-222); default `paper_trade_exchanges` are Binance, KuCoin, Kraken, Gate.io (same file).
- Inference (not stated in code): because `GatewayBase` has no `OrderBookTracker` and quotes by polling (`gateway_base.py`), the paper-trade mechanism does not apply to Gateway/Jupiter markets.

### Strategies

- **Pure market making** (`strategy/pure_market_making/pure_market_making.pyx`, `c_tick` lines 715-764): wait for markets ready → `c_create_base_proposal()` → `c_apply_order_levels_modifiers` → `c_apply_order_price_modifiers` → `c_apply_order_size_modifiers` → `c_apply_budget_constraint` → optional `c_filter_out_takers` → hanging-orders tracker → cancel on max age / cancel active / cancel below min spread → `c_execute_orders_proposal`. Supporting modules: `inventory_skew_calculator.pyx`, `inventory_cost_price_delegate.py`, `moving_price_band.py`.
- **Cross-exchange market making** (`strategy/cross_exchange_market_making/cross_exchange_market_making.py`, `tick` lines 361-418): readiness and conversion-rate checks, a `get_gateway_quotes` task, then `main()` per market pair; methods `hedge_filled_maker_order` (558), `check_if_price_has_drifted` (791), `get_market_making_size` (1034), `get_market_making_price` (1130), `check_if_still_profitable` (1401), `check_if_sufficient_balance` (1502).
- **amm_arb** (`strategy/amm_arb/amm_arb.py`): "basic arbitrage strategy which can be used for most types of connectors (CEX, DEX or AMM)" (docstring line 37). Each tick, if `ready_for_new_arb_trades()`, spawns `main()` (lines 173-193), which: builds proposals for both directions by concurrently calling `get_quote_price` and `get_order_price` on both markets (`strategy/amm_arb/utils.py` lines 18-62), adds each market's `network_transaction_fee` as a flat fee, keeps proposals with `profit_pct(account_for_fee=True) >= min_profitability`, then `apply_slippage_buffers` (price × (1 ± buffer)), `apply_budget_constraint`, `execute_arb_proposals` (lines 194-231, 232-260). This is the closest analogue to a Solana spot bot's "is the edge bigger than fees + slippage" gate.
- **Strategy V2** (`strategy/strategy_v2_base.py` `StrategyV2Base`, lines 176-191, 295-323, 707-760): V2 infrastructure (MarketDataProvider, ExecutorOrchestrator, actions queue) is always present; `on_tick()` → `update_executors_info()`, `update_controllers_configs()` (every `config_update_interval = 10` s), `determine_executor_actions()` = create + stop + store proposals → `executor_orchestrator.execute_action`. Controllers and executors are `RunnableBase` smart components with their own `control_loop()` sleeping `update_interval` (default 0.5 s; `ControllerBase` default 1.0 s) (`strategy_v2/runnable_base.py` lines 23-75; `strategy_v2/controllers/controller_base.py` line 196). Executors: `position_executor`, `dca_executor`, `grid_executor`, `order_executor`, `twap_executor`, `xemm_executor`, `arbitrage_executor`, `lp_executor` (`strategy_v2/executors/`). `TripleBarrierConfig` = `stop_loss`, `take_profit`, `time_limit`, `trailing_stop`, plus per-barrier order types (`strategy_v2/executors/position_executor/data_types.py` lines 32-41). Example controllers shipped at repo root `controllers/` (directional: `bollinger_v1`, `macd_bb_v1`, `supertrend_v1`, `dman_v3`; market making: `pmm_simple`, `pmm_dynamic`, `dman_maker_v2`; generic: `arbitrage_controller`, `grid_strike`, `stat_arb`, `xemm_multiple_levels`, ...).
- **V2 backtesting** (`strategy_v2/backtesting/`): `PositionExecutorSimulator.simulate` fills at the candle **close** (`entry_price = df.loc[start_timestamp, 'close']`), computes close-to-close cumulative returns minus `2 * trade_cost` (default `trade_cost=0.0002` per side in `run_backtesting`), no slippage or liquidity model (`executors_simulator/position_executor_simulator.py` lines 10-49; `backtesting_engine_base.py` lines 221-241). Less conservative than TRADEBOT's next-open fills with intrabar stops.

### Solana / Jupiter support via Gateway

- Hummingbot side: a single `Gateway` connector per network (`connector_name` e.g. `"solana-mainnet-beta"`; `dex_name` e.g. `"orca"`, `"jupiter"`; `trading_type` `"clmm" | "amm" | "router"`) (`hummingbot/connector/gateway/gateway.py` lines 1-11; `ConnectorType` SWAP/CLMM/AMM in `common_types.py` lines 21-24). `GatewayBase`: `POLL_INTERVAL = 1.0`, `BALANCE_POLL_INTERVAL = 60.0`, `SIGNATURE_POLL_INTERVAL = 2.0`, `SIGNATURE_POLL_TIMEOUT = 120.0`; NOT_FOUND is terminal only once the blockhash has expired (~90 s) (`gateway_base.py` lines 99-109, 877-934; `common_types.py` lines 32-33). `get_quote_price` calls Gateway `quote_swap` and returns its `price`; `get_order_price` is the same call (`gateway.py` lines 247-303). `_create_order` calls `execute_quote` (with `quote_id`) or `execute_swap`, retries up to `max_retries=10`, then tracks the returned `signature` (`gateway.py` lines 326-410). Prices are therefore **polled REST quotes**, not streamed.
- Gateway side (`R2/gateway/README.md`): "Standardized REST API ... DEXs (Uniswap, Jupiter, Raydium, Meteora, PancakeSwap, 0x)"; DEX table: Jupiter = Router only (no AMM/CLMM), Meteora CLMM, PancakeSwap (Solana) CLMM, Raydium AMM+CLMM; router routes `GET /trading/router/quote-swap` (returns `quoteId`), `POST /trading/router/execute-quote`, `POST /trading/router/execute-swap`; the README's router list also names `dflow`, `okx`, `titan`, and `src/connectors/` contains `dflow`, `okx`, `titan`, `orca` directories. "Built-in rate limiting (100 requests/minute)". Optional Helius RPC provider with "WebSocket transaction monitoring, Sender endpoints".
- **Jupiter is a Gateway router connector** (`R2/gateway/src/connectors/jupiter/`: `jupiter.ts`, `jupiter.config.ts`, `router-routes/quoteSwap.ts`, `executeQuote.ts`, `executeSwap.ts`; `tradingTypes = ['router']` in `jupiter.config.ts`). It calls **`/swap/v1/quote` and `/swap/v1/swap`** (`jupiter.ts` lines 219, 309, 390) — i.e. the Metis v1 flow that `jup-ag/agent-skills` marks deprecated (section 5) — and falls back to `lite-api.jup.ag` without a key ("deprecated Dec 31, 2025", `jupiter.ts` lines 13-16; `src/templates/connectors/jupiter.yml`: "With key: uses api.jup.ag (free tier: 60 req/min)"). Defaults: `slippagePct: 1`, `priorityLevel: 'veryHigh'`, `maxLamports: 1000000`, `restrictIntermediateTokens: true` (`jupiter.yml`). Solana network template: `swapProvider: jupiter/router`, `confirmRetryInterval: 1` s × `confirmRetryCount: 10`, priority fee bounds 0.1-1.0 lamports/CU (`src/templates/chains/solana/mainnet-beta.yml`).

### What is reusable as a design pattern (not code)

1. Fixed-tick clock with per-component exception isolation (`clock.pyx`) — TRADEBOT's engine loop can adopt "one tick, every component called, one component's exception never stops the others".
2. Proposal pipeline (`pure_market_making.pyx` c_tick) — build → modify levels → modify price → modify size → budget constraint → execute; clean seam for the independent risk engine.
3. `ArbProposal.profit_pct(account_for_fee=True)` + flat network fee + slippage buffer + budget check (`amm_arb`) — a fee-inclusive go/no-go that mirrors TRADEBOT's `verify-assets` round-trip costing but applied per cycle.
4. Executor objects with a triple barrier and their own control loop (`position_executor`) — a per-position state machine instead of a monolithic position manager.
5. Controller (signal) / Executor (execution) separation with `ExecutorInfo` reports (`strategy_v2_base.py`) — maps to TRADEBOT's strategy → risk → broker boundary.
6. Paper trading against the live book with an execution delay (`TRADE_EXECUTION_DELAY = 5.0`) — TRADEBOT's `PaperBroker` could add a configurable delay and re-quote at fill time rather than at signal time.
7. Gateway's signature-polling terminal semantics (NOT_FOUND terminal only after blockhash expiry) — already similar to TRADEBOT's 150 s intent expiry; confirms the design.

**Do not adopt wholesale**: Cython build toolchain, pandas/numpy (blocked on the target Windows host per TRADEBOT README), a Node.js Gateway service, and a Jupiter path still on `/swap/v1` with Metis-only routing. Integration effort to embed Hummingbot: L; to borrow patterns: S.

---

## 3. Freqtrade validation techniques

All citations are to `R2/freqtrade/`.

### Backtesting assumptions (fills, fees, slippage)

`docs/backtesting.md` lines 556-592 ("Assumptions made by backtesting"): entries at open price unless custom price logic; "All orders are filled at the requested price (no slippage) as long as the price is within the candle's high/low range"; exit-signal exits at next candle open; exit signal favored over stoploss; ROI exits compared to high but filled at the ROI level; "Stoploss exits happen exactly at stoploss price, even if low was lower, but the loss will be `2 * fees` higher"; stoploss evaluated before ROI; low before high for stoploss; trailing stop worst-case rules; evaluation order Exit-signal → Stoploss → ROI → Trailing stop. Code: `_get_close_rate_for_stoploss` (`freqtrade/optimize/backtesting.py` lines 597-650) implements the worst-case same-candle trailing rule ("price reaches stop_positive_offset and dives down", bounded by candle low).

Fees: `set_fee()` uses `config["fee"]` if set, otherwise `max(taker, maker)` from the exchange, logged as "worst case fee from exchange (lowest tier)" (`backtesting.py` lines 268-282); "Fee is applied twice for each trade, once when buying, once when selling" (`docs/configuration.md` line 186).

Trading limits: backtesting honors exchange min/max amounts but "has however no information about historic limits" (`docs/backtesting.md` lines 593-616).

Intra-candle accuracy: `--timeframe-detail 5m` re-evaluates active candles at a lower timeframe so slots free earlier and callbacks run per detail candle; "You can use this function as the last part of strategy development, to ensure your strategy is not exploiting one of the backtesting assumptions" (`docs/backtesting.md` lines 618-667; `init_backtest_detail` in `backtesting.py` lines 288-300).

### `lookahead-analysis`

`docs/lookahead-analysis.md`; code `freqtrade/optimize/analysis/lookahead.py` (`LookaheadAnalysis(BaseAnalysis)`, `minimum_trade_amount`, `targeted_trade_amount`, `entry_varHolders`/`exit_varHolders`). Method: a full baseline backtest, then "additional backtest runs for every entry and exit separately, with a pairlist of length 1", comparing "both dataframes (baseline and sliced) for any difference in columns' value" — detecting bias from changed indicator values and moved entries/exits "not looking at the strategy code itself". Forced options: `--cache none`, `--max-open-trades` ≥ number of pairs, wallet "basically infinite (1 billion)", `--stake-amount` 10000, protections off, market orders unless `--lookahead-allow-limit-orders`. Output columns `has_bias`, `total_signals` (default 20), `biased_entry_signals`, `biased_exit_signals`, `biased_indicators`. Caveats: unverified signals give false negatives; pairlist-dependent strategies give false positives; FreqAI targets are falsely flagged. Examples of bias: `shift(-10)`, absolute `iloc[]`, whole-dataframe `.mean()/.min()/.max()`, `ta.MACD(..., 1)`.

### `recursive-analysis`

`docs/recursive-analysis.md`; code `freqtrade/optimize/analysis/recursive.py` (default `startup_candle` list `[199, 399, 499, 999, 1999]`, `analyze_indicators()`). It recomputes `populate_indicators` with different `startup_candle_count` values and reports percentage variance of the last-row indicator values versus a long benchmark (recommend ≥5000 candles) — exposing recursive indicators (EMA-type) whose live values depend on warm-up length; "also carries out a simple lookahead bias check on the indicator values only". Only `populate_indicators` and `@informative` are checked.

### Hyperopt overfitting protections

`docs/hyperopt.md`: optuna NSGAIIISampler after 30 random initial epochs (line 7, "Reproducible results"); `--random-state` for reproducibility (lines 748-754); `--early-stop` after N epochs without improvement (line 507); "Decimals are limited to 3 decimal places ... every value more precise than this will usually result in overfitted results" (line 664); hold-out via `--timerange` (lines 525-531); warning that hyperoptable parameters used inside `populate_indicators` are not recalculated per epoch (line 236). Loss functions in `freqtrade/optimize/hyperopt_loss/` (Sharpe, Sharpe daily, Sortino, Calmar, max drawdown, profit-drawdown, only-profit, short-trade-duration, multi-metric); `MultiMetricHyperOptLoss` "applies a penalty for epochs with a low number of trades" (`docs/hyperopt.md` line 491). Post-hoc filtering of epochs by `only_best`, `only_profitable`, `filter_min_trades`, `filter_max_trades`, avg-time and avg-profit bounds (`freqtrade/optimize/hyperopt_epoch_filters.py` lines 9-80; CLI `hyperopt-list --min-trades`, `--profitable`, `--min-total-profit` in `docs/commands/hyperopt-list.md`).

### Walk-forward / time-range splitting

There is no built-in walk-forward in backtesting/hyperopt; splitting is manual with `--timerange` (`docs/backtesting.md` lines 137-167; `docs/hyperopt.md` lines 525-531). **FreqAI** implements a sliding window: `train_period_days` (training window) and `backtest_period_days` (inference window before retrain), e.g. 30-day train / 7-day test repeated across the range, producing one model per window (`docs/freqai-running.md` lines 106-112; `docs/freqai-parameter-table.md` lines 13-16). `set_freqai_targets()` is called once per window "so that the targets simulate dry/live behavior without look ahead bias", while features are computed once over the whole range so "you should be sure that features do not look-ahead" (`freqai-running.md` line 71). Live: `live_retrain_hours`, `expiration_hours` to refuse stale models (lines 15, 116-120). Freqtrade's own stance: "only forward-testing (dry-mode) can really confirm a strategy" (`docs/backtesting.md` line 647; `docs/strategy-101.md` lines 131-150).

### Trades per day, max open trades

`trades_per_day = round(len(results) / backtest_days, 2)` (`freqtrade/optimize/optimize_reports/optimize_reports.py` line 695), printed as "Total/Daily Avg Trades" (`bt_output.py` line 389). `max_open_trades` is required config; "Only one open trade per pair is possible" (`docs/configuration.md` line 166); backtest "Max open trades" = config value or number of pairs, whichever is lower (`docs/backtesting.md` line 432); entries blocked when slots are full are counted as "Rejected Entry signals" (`backtesting.md` line 460; `trade_slot_available` increments `rejected_trades`, `backtesting.py` lines 1306-1313); `stake_amount: "unlimited"` splits balance into `max_open_trades` buckets (`backtesting.md` line 46).

### Edge module

"The edge module has been deprecated in 2023.9 and removed in 2025.6. All functionalities of edge have been removed, and having edge configured will result in an error." (`docs/deprecated.md` lines 97-100). No `freqtrade/edge` directory exists in the checkout; a stale `docs/commands/edge.md` remains.

### What TRADEBOT can take from this (concrete)

TRADEBOT already uses next-open fills, intrabar stops, fees and slippage in its backtester and a walk-forward grid (bot `README.md`). Additions suggested by the Freqtrade evidence: (1) a lookahead test that recomputes signals on truncated histories and diffs them against the full-history signals; (2) a startup-length variance test for the EMA/tsmom indicators (recursive-analysis analogue); (3) worst-case fee selection instead of the assumed 25 bps; (4) report `trades_per_day` and a rejected-signals counter when `max_open_positions` is hit; (5) a minimum-trades filter on walk-forward candidates; (6) a "detail timeframe" pass using 1m Binance candles to re-check stop/TP fills on 1h signals. Effort S-M, pure Python.

---

## 4. Birdeye (Birdeye Data Services, BDS)

### Evidence quality

Official GitHub org `birdeye-so` contains `tradingview-example-js-api` (WebSocket examples), `birdeyeterminal-docs` (terminal UI docs), `resources` (brand assets), `plasma-sdk`, `grpc_ben` (`gh api search/repositories q='user:birdeye-so'`). **No official SDK or API-docs repository was found in the org.** The API facts below come from (a) the official WS examples, (b) two third-party mirrors of docs.birdeye.so: `TheNang2710/birdeye-data-api-docs` (changelog entries up to 2026-02-24) and `fcrre26/birdeye-api-docs` ("compiled from BirdEye's official API reference (February 2026)", its `README.md`), (c) third-party clients `nickatnight/birdeye-py`, `tigusigalpa/birdeye-go`, and the `sondnm/bds-workshops` workshop. Mirrors are secondary evidence; everything from them is "docs-mirror supported", not live-verified.

### Public API surface (docs mirror)

Base URL `https://public-api.birdeye.so`; auth header `X-API-KEY` plus `x-chain` (`R2/birdeye/fcrre26_birdeye-api-docs/README.md`; `R2/birdeye/tigusigalpa_birdeye-go/README.md` line 55; OpenAPI `securitySchemes.apiKeyAuth` in `R2/birdeye/TheNang2710_birdeye-data-api-docs/reference.md`).

- Price: `/defi/price`, `/defi/multi_price`, `/defi/price_volume/single|multi`, `/defi/history_price`, `/defi/historical_price_unix`, `/defi/v3/price/stats/single|multiple` (`TheNang2710 .../docs.md` accessibility table lines 1443-1470; `fcrre26 .../docs/defi/`).
- OHLCV: `/defi/ohlcv`, `/defi/ohlcv/pair`, `/defi/ohlcv/base_quote`, `/defi/v3/ohlcv`, `/defi/v3/ohlcv/pair`; v3 intervals `1s 15s 30s 1m 3m 5m 15m 30m 1H ... 1M`, `1s` retained "Up to 2 weeks", `15s/30s` "Up to 3 months", "Empty candles are not displayed" (`fcrre26 .../docs/defi/get-defi-v3-ohlcv.md`).
- Token security: `/defi/token_security` — tiers Starter/Premium/Business/Enterprise (not Standard/Lite) (`fcrre26 .../get-defi-token_security.md`; accessibility row line 1534 "not sui"); response includes `creatorAddress`, `creatorOwnerAddress`, `ownerAddress`, ... (same file, example).
- Trader / "smart money": `/defi/v2/tokens/top_traders` (all tiers incl. Lite; `time_frame` 30m-24h, `sort_by` volume|trade), `/trader/gainers-losers` (`type` yesterday|today|1W, `sort_by` PnL), `/trader/txs/seek_by_time` (`fcrre26 .../get-defi-v2-tokens-top_traders.md`, `get-trader-gainers-losers.md`; `birdeye-py/birdeyepy/resources/trader.py`). **Smart Money - Token List** `GET /smart-money/v1/token/list` released 2026-02-06, "Currently support only Solana", Starter+; params `interval` 1d|7d|30d, `trader_style` all|risk_averse|risk_balancers|trenchers, `sort_by` net_flow|smart_traders_no|market_cap, `limit` ≤ 20, `offset+limit ≤ 10000`; fields `net_flow`, `smart_traders_no`, `volume_buy_usd`, `volume_sell_usd`, `price_change_percent` (`TheNang2710 .../changelog.md`; `reference.md` lines 1232-1250).
- Wallet APIs are "in beta and support a low rate limit ... 5 requests per second and 75 requests per minute" (`docs.md` line 1546).
- Holder distribution `/holder/v1/distribution` (Solana, 2026-01-20), token list v3 with 1m/5m/30m intervals (`changelog.md`).

### WebSocket (official examples + mirror)

- URL `wss://public-api.birdeye.so/socket/${CHAIN}?x-api-key=${TOKEN}` with subprotocol `echo-protocol` and `Origin: https://birdeye.so` (`R2/birdeye/birdeye-so_tradingview-example-js-api/websocket_examples/simple/subscribe_price_token.js` last line; mirror lists `Origin`/`Sec-WebSocket-Origin: ws://public-api.birdeye.so`, `reference.md` lines 13-18).
- Messages: `SUBSCRIBE_PRICE` `{chartType, currency: "usd"|"pair", address}` → `PRICE_DATA` `{o,h,l,c,v,eventType:"ohlcv",type,unixTime,symbol,address}`; `SUBSCRIBE_TXS` `{address}` or `{pairAddress, address}`; `SUBSCRIBE_BASE_QUOTE_PRICE` `{baseAddress, quoteAddress, chartType}` → `BASE_QUOTE_PRICE_DATA`; `SUBSCRIBE_LARGE_TRADE_TXS` `{min_volume, max_volume}` → `TXS_LARGE_TRADE_DATA`; also wallet txs, token stats, new token listing, new pair (examples dir; `reference.md` lines 19-62; `fcrre26 .../docs/websocket/track-large-transactions.md`; OpenAPI `x-extras.websocket`).
- Limits: "Price, Transactions, and OHLCV: Up to 100 tokens per connection; Wallet Tracking: 1 address per connection"; "WebSocket access is available for Business Package subscribers, which includes 500 concurrent connections"; ping-pong recommended; Solana WS timeframes include `1s, 15s, 30s` (`docs.md` lines 4249-4258). Base-quote WS: one pair per connection (`reference.md` line 61).
- The pricing table says Premium Plus "includes 500 websocket connections" and Business "2000 websocket connections" (`docs.md` lines 2011-2021), which contradicts the "Business ... 500" sentence above → exact WS entitlement **UNVERIFIED**.

### Pricing, tiers, rate limits (docs mirror only)

`TheNang2710 .../docs.md` lines 2007-2021: Standard $0 / 30,000 CU / limited to 3 endpoints / 1 rps; Lite $39 / 1.5M CU / 15 rps; Starter $99 / 5M / 15 rps; Premium $199 / 15M / 50 rps; Premium Plus $250 / 20M / 50 rps + WebSockets; Business $499 (50M) / $699 (100M) / $1500 (300M) / $2300 (500M) at 100-150 rps, all APIs + WebSockets; Growth AutoScale and Enterprise custom. Rate limits are per account (line 2281); per-API caps e.g. `/defi/price` 300 rps, `/defi/ohlcv` 100 rps (lines 1905-1917). Every call costs compute units; batch endpoints cost `N^0.8 × base CU` (lines 355-362); monthly max usage is 5× the included amount, after which the account is suspended unless auto-renew is on (line 1549); overage billed pro-rata, suspension within a day on failed payment (line 3872). Which tier the TRADEBOT would need: price/OHLCV/top-traders are on all tiers; `token_security` and Smart Money need Starter+ ($99); WebSockets need Premium Plus or Business per the table.

### Integration into the Python bot

REST: `httpx` + `X-API-KEY`/`x-chain` headers, trivial (S). WS: Python `websockets` with `subprotocols=["echo-protocol"]` and an `Origin` header, 100 tokens per connection, ping/pong — pure Python (S-M). A Python wrapper exists (`nickatnight/birdeye-py`, retry/back-off, `defi/token/trader/wallet/pair/search` resources) but is third-party and would be an extra dependency.

### Relevance and risks

- (a) Scanner: yes if the universe grows beyond cbBTC/WETH/SOL — 1s OHLCV, recent trades, holder distribution, `token_security`, top traders and the Smart Money list are exactly scanner inputs; they overlap with Jupiter Tokens v2 `audit` fields (section 5) which the bot already uses for free.
- (b) Execution: no — data only.
- Risks: paid tiers for anything beyond basics; CU overage billing and suspension; docs mirrors may be stale or wrong (no official repo to verify against); "smart money" and security scores are opaque heuristics; one WS connection per base-quote pair; 1 rps on the free tier makes a 1-10 s multi-token scan impossible without paying.

---

## 5. Jupiter Swap v2 specifics (from `jup-ag/agent-skills`, `AS/skills/integrating-jupiter/SKILL.md` v1.2.0 and `AS/skills/jupiter-swap-migration/SKILL.md` v1.1.0)

### Auth and base URLs

"Base URL: `https://api.jup.ag`; Auth: `x-api-key` from developers.jup.ag (required for Jupiter REST endpoints)" (`SKILL.md` lines 41-42). Swap base `https://api.jup.ag/swap/v2`; Trigger `.../trigger/v2`; Recurring `.../recurring/v1`; Tokens `.../tokens/v2`; Price `.../price/v3` (lines 137, 205, 222, 235, 249).

### Order / execute flow, routers, mode, fees, gasless

- Endpoints: `/order` (GET), `/execute` (POST), `/build` (GET, "Metis-only raw instructions"). "For a read-only quote/price preview, call `GET /order` and omit `taker` — the response `transaction` is `null` and you read `outAmount`, `routePlan[].swapInfo.label`, and price-impact fields ... `/swap/v1/quote` is deprecated; use `/swap/v2/order` for quotes too." (lines 141-142).
- Routers: "4 routers compete — Metis (`metis`), JupiterZ (`jupiterz`), Dflow (`dflow`), OKX (`okx`). The `router` response field returns one of these values. `swapType` is `aggregator` (Metis, Dflow, or OKX) or `rfq` (JupiterZ). Response `mode` field: `"ultra"` (all routers, default params) or `"manual"` (restricted by optional params). `/build` uses Metis only." (line 143). Migration note: v2 `/order` response adds `router`, `mode`, `feeBps`, `feeMint` (`AS/skills/jupiter-swap-migration/examples/ultra-to-order.md`).
- Routing impact of params: `referralAccount` + `referralFee` disable JupiterZ only; `payer` (integrator gasless) restricts to Metis only; `receiver` must differ from `taker` (`400 "Receiver cannot be same as taker"`) (line 148).
- Fee model: "Variable by pair — 0 bps (Jupiter tokens/pegged), 2 bps (SOL-Stable), 5 bps (LST-Stable), 10 bps (most pairs), 50 bps (tokens < 24h). Referral fees: 50-255 bps (Jupiter retains 20%)." (line 139); `/build` has "no Jupiter fee" (`metis-to-order.md` Trade-offs). Migration table: Metis v1 "Jupiter fee: None" vs `/order` "Yes (variable by pair: 0-50 bps)"; RFQ "5-20 bps better on major pairs" (`metis-to-order.md`).
- Gasless: "Three paths — automatic (Jupiter-covered), JupiterZ (MM-covered), integrator-payer (`payer` param, Metis-only routing). Eligibility varies by balance, trade size, and parameters used." (line 144).
- `requestId` lifecycle: `GET /order` returns `transaction` (base64, null without `taker`) and `requestId`; sign locally; `POST /execute` with `{ signedTransaction, requestId }` (`AS/skills/integrating-jupiter/examples/swap.md`). "Signed payloads have ~2 min TTL. Transactions are immutable after receipt." (line 146). "Idempotency: Swap `/execute` accepts same `signedTransaction` + `requestId` for up to 2 min without duplicate execution." (line 358). Success response: `{ status: "Success", code: 0, signature, inputAmountResult, outputAmountResult, slot, totalInputAmount, totalOutputAmount }`; failure `status: "Failed"` with non-zero `code` and `error` (line 177). V2 renamed `inputAmount`/`outputAmount` → `inputAmountResult`/`outputAmountResult` (`ultra-to-order.md`). `swapMode` removed, only `ExactIn` (`jupiter-swap-migration/SKILL.md` checklist item 3).
- Error codes (lines 155-175): `-1` missing/expired cached order (retry: re-quote), `-2` invalid signed tx, `-3` invalid message bytes, `-1000` failed landing (retry), `-1001` unknown (retry), `-1002` invalid tx, `-1003` not fully signed, `-1004` invalid block height (re-quote), `-2000..-2004` RFQ failed landing / unknown / invalid payload / quote expired / swap rejected, `429` rate limited. Positive codes are program errors ("6001 = slippage", line 402).
- Hardening: 5 s quote timeout, 30 s execute timeout; retries only on transient/rate-limit failures with jitter (lines 355-357). The bot's README states the same timeouts, so TRADEBOT is aligned.

### Rate limits and API key tiers

"Swap API (dynamic, volume-based)": 24h execute volume $0 → 50 requests per 10 s; $10,000 → 51; $100,000 → 61; $1,000,000 → 165. "Quotas recalculate every 10 minutes. Pro plan does NOT increase Swap API limits." Other APIs: "Managed at portal level" (link only) (lines 336-349). On 429: exponential backoff with jitter, "Wait for 10s sliding window refresh" (line 351). Free-tier existence is implied ("free tier" named only in Gateway's `jupiter.yml`: "free tier: 60 req/min" — a third-party statement, not from Jupiter's repo). Portal tier names/prices beyond "Pro" are **UNVERIFIED**.

### Trigger (limit orders) and Recurring (DCA)

- Trigger v2: min order 10 USD; dual auth `x-api-key` + JWT (`POST /auth/challenge` → sign → `POST /auth/verify`, 24 h JWT; expiry does not cancel orders); custodial vault (Privy) per wallet, orders "invisible on-chain until execution", trigger on USD price, partial fills; create = `GET /vault/register` (409 if exists) → `POST /deposit/craft` (needs `orderType: "price"`, `orderSubType` single|oco|otoco) → sign → `POST /orders/price` with `depositRequestId` + `depositSignedTx`; cancel = `POST /orders/price/cancel/{orderId}` → sign → `POST /orders/price/confirm-cancel/{orderId}` with `cancelRequestId`; history objects use `orderState`/`rawState` (lines 203-217; `examples/trigger.md`).
- Recurring v1: fee 0.1%; min 100 USD total, min 2 orders, min 50 USD per order; endpoints `/createOrder`, `/cancelOrder`, `/execute`, `/getRecurringOrders`; "Token-2022 NOT supported"; time-based only (lines 220-229).
- Relevance: Trigger moves stop-loss/take-profit enforcement into Jupiter's custodial vault — conflicts with TRADEBOT's design that the independent risk engine and local signing control every exit; also requires depositing funds into a third-party vault. Not recommended for the risk model as documented in the bot README.

### Price v3

`GET /price/v3?ids=...`, max 50 mints; response keyed by mint with `usdPrice`, `blockId`, `decimals`, `priceChange24h`, `liquidity`, `createdAt` (conditional `launchpad`, `stockData`, `scaledUiConfig`); "NO `confidenceLevel` field (that was V2)"; unreliable tokens "are omitted from the response entirely ... Fail closed"; "Use `blockId` to check price recency" (lines 247-256; `examples/price.md`). The bot already fails closed on missing mints (bot README).

### Tokens v2

Endpoints `/search?query={q}` (comma-separated mints, max 100), `/tag?query={tag}` (`verified` or `lst`), `/{category}/{interval}` with categories `toporganicscore`, `toptraded`, `toptrending` and intervals `5m`, `1h`, `6h`, `24h`, and `/recent` (line 237). Trust fields: top-level `isVerified` (boolean), `organicScore` (0-100), `organicScoreLabel` (`high`/`medium`/`low`); `audit.isSus` "present ONLY when `true`" — check as `token.audit?.isSus === true`; flagged tokens also carry `isVerified: null`, `organicScoreLabel: "low"`; "Absence of `isSus` is NOT proof of safety". Other conditional `audit` fields: `mintAuthorityDisabled`, `freezeAuthorityDisabled`, `topHoldersPercentage`, `devBalancePercentage`, `devMigrations`, `devMints` ("all nullable, any may be absent; `devMigrations` is returned but missing from the current OpenAPI schema") (lines 239-242).

### Relevance

(a) Scanner: `GET /swap/v2/order` without `taker` is the correct read-only quote; the Swap API budget (50 req/10 s at $0 volume) caps a 1-10 s multi-pair quote scan at roughly 5 quotes/s total — size the universe accordingly; Price v3 (50 mints/call) and Tokens v2 `toptraded`/`recent` are cheap scanner inputs with limits "managed at portal level" (unstated numbers). (b) Execution: already integrated; the documented idempotency window (2 min) matches the bot's intent/resend design. Risks: per-pair `feeBps` up to 50 bps on new tokens eats small edges; `mode: "manual"` silently loses RFQ pricing when optional params are set; rate limits scale with executed volume so a paper-trading bot stays at the base quota.

---

## 6. Social / news intelligence (X/Grok, Reddit, YouTube, RSS) — brief

- **X API**: `xdevplatform/twitter-api-typescript-sdk` README: "This SDK is in beta and is not ready for production", "Only Twitter API V2 is supported", bearer-token/OAuth2 auth; streaming (`sampleStream`, `searchStream`) and `tweetsRecentSearch` / `tweetsFullarchiveSearch` exist in `src/gen/Client.ts`. No pricing or rate-limit numbers in the repo → **UNVERIFIED**. Python client not in this org listing.
- **Grok / xAI**: `xai-org/xai-sdk-python` is a gRPC SDK (Python ≥3.10) with server-side tools `web_search()` and `x_search()` ("Let Grok autonomously decide when to search the web, 𝕏, or execute code", README line 276; `examples/sync/server_side_tools.py` lines 5-19); quota exhaustion surfaces as `RESOURCE_EXHAUSTED` (README line 539). Pricing/free tier not stated → **UNVERIFIED**. Note xAI SDK needs `grpcio`.
- **Reddit**: `praw-dev/praw` uses a script-type OAuth app; PRAW "respects the `X-Ratelimit-*` headers", default `ratelimit_seconds` 5 s, will sleep up to 600 s on "You're doing that too much" errors (`docs/getting_started/ratelimits.rst`). No pricing in repo; the referenced rules page is on the Reddit API wiki → numbers **UNVERIFIED**.
- **YouTube Data API**: `googleapis/google-api-python-client` docs mention API keys and "quota" only generically (`docs/auth.md`, `docs/start.md`); quota units per call **UNVERIFIED**.
- **RSS/Atom**: `kurtmckee/feedparser` (BSD-2, `pip install feedparser`, parses RSS/Atom/JSON feeds) needs no auth; latency equals the poll interval and publisher delay.

Why these belong outside the 1-10 s loop: they are HTTP request/response or LLM-mediated (Grok) with per-account rate limits measured in requests per minute, not per second (PRAW backs off up to minutes; X and YouTube quotas are account-level); their content needs NLP/LLM processing that TRADEBOT confines to the advisory Claude role (bot README: Claude "can only reduce", default disabled during the forward test). They fit the research cadence (hours) or signal-vetting step, never the execution or scanner tick. Effort S for RSS (pure Python), M for X/Reddit/YouTube (auth + quota handling), M for Grok (gRPC dependency).

---

## Comparison table

| System | What it gives | Latency model | Cost / free tier (repo-stated) | Integration effort | Verdict: scanner (1-10 s poll) | Verdict: seconds-level execution |
|---|---|---|---|---|---|---|
| Helius standard WS | `accountSubscribe`/`logsSubscribe`/`programSubscribe`/`signatureSubscribe`/`slotSubscribe` push | WebSocket push (JSON-RPC) | All plans; 2 credits/0.1 MB; 10 RPC rps on Free/Agent; Free $0 vs Agent $10 one-time **UNVERIFIED** | S (pure Python WS client) | Useful for balance/signature events; not needed for current Binance+Jupiter signals | Yes for confirmation via `signatureSubscribe`; no change to landing (Jupiter submits) |
| Helius Enhanced WS | `transactionSubscribe` with 50k-address filters, parsed txs | WebSocket push, "1.5-2x faster" | Paid; Developer $49 vs Business $499 minimum **UNVERIFIED (conflicting files)** | S-M | Optional: on-chain Jupiter flow; credit burn risk | No added value for `/execute` path |
| Helius Parsed Streams | Decoded program/instruction-filtered txs (e.g. Jupiter `route`) | WebSocket push, `confirmed` only | "all plans"; 2 credits/0.1 MB | M | Good if flow-based features are researched | No |
| Helius LaserStream | Geyser gRPC: accounts, txs, blocks, slots, replay | gRPC stream, shred-level | Business+ for mainnet ($499+); 2 credits/0.1 MB; replay 20 min vs 24 h **UNVERIFIED** | L (grpcio, no Python SDK) | Overkill for 1-10 s polling | Not needed; Jupiter lands txs |
| Helius Sender | Dual-route tx submission, 0 credits, 50 TPS | HTTP send | All plans, tip ≥0.0002 SOL | S (only if moving to `/build`) | n/a | Only relevant if self-submitting via `/swap/v2/build` |
| Helius `core-ai` | CLI, MCP server, Claude skills for Helius APIs | n/a (tooling) | Same plans; MCP streaming tools are config generators only | n/a | Not a data source | Not an execution path |
| Hummingbot (client) | Clock/tick loop, OrderBookTracker, PMM/XEMM/amm_arb, Strategy V2 controllers/executors, live-book paper trading, V2 close-to-close backtester | 1 s tick; CEX books via WS; Gateway quotes polled every 1 s | Open source (Apache-2.0); exchange/RPC costs separate | L to adopt; S to borrow patterns | Patterns reusable (proposal pipeline, fee-inclusive arb gate, paper-fill delay, executor state machine) | Gateway path polls REST quotes at 1 s and signatures every 2 s; not faster than the bot's direct Jupiter v2 calls |
| Hummingbot Gateway | REST façade over Jupiter (router), Raydium, Meteora, Orca, PancakeSwap-sol, dflow, okx, titan | REST, 100 req/min built-in limit; confirm poll 1 s × 10 | Open source; Jupiter key "free tier: 60 req/min" (Gateway comment) | M (Node service) | No; adds a hop and uses deprecated `/swap/v1` | No; Metis-only v1 flow, no RFQ/gasless |
| Freqtrade techniques | Backtest assumptions, lookahead-analysis, recursive-analysis, hyperopt filters/loss functions, FreqAI sliding window, trades/day, rejected-signal counting | Offline | Open source (GPL-3) | S-M to re-implement the checks in TRADEBOT's pure-Python research module | Improves validation of scanner strategies | n/a |
| Birdeye BDS | Price, OHLCV down to 1s (Solana), trades, token_security, top traders, gainers/losers, Smart Money token list, holder distribution; WS price/txs/large trades | REST polling (per-API rps caps) + WebSocket push (100 tokens/connection) | Standard $0: 30k CU, 1 rps, 3 endpoints; Lite $39 15 rps; Starter $99 (needed for `token_security`, Smart Money); WS from Premium Plus $250 / Business $499 — docs-mirror only, **UNVERIFIED live** | S (REST), S-M (WS) | Yes for a broader token universe; overlaps Jupiter Tokens v2 audit fields which are free | No (data only) |
| Jupiter Swap v2 / Price v3 / Tokens v2 | Multi-router quotes (`metis`/`jupiterz`/`dflow`/`okx`), managed execution, gasless, idempotent `/execute`, prices with `blockId`/`liquidity`, token audit fields | REST request/response; Swap API 50 req/10 s at $0 volume | `x-api-key` required; Swap limits scale with executed volume; "Pro plan does NOT increase Swap API limits"; portal tiers/prices **UNVERIFIED** | Already integrated | Yes within ~5 quotes/s budget; Price v3 50 mints/call | Yes; current path; 2-min idempotency matches bot design |
| Jupiter Trigger / Recurring | Custodial limit orders (OCO/OTOCO), DCA | Server-side; JWT 24 h | Trigger min $10; Recurring 0.1% fee, min $100/$50 | M | n/a | Conflicts with independent risk engine + local custody; not recommended |
| X / Grok / Reddit / YouTube / RSS | Social/news text | HTTP polling or LLM calls; minutes-scale rate limits | Mostly **UNVERIFIED** in repos; RSS free | S (RSS) - M (others) | Outside the tick; research cadence only | No |

---

## UNVERIFIED claims (not supported, or contradicted, by repo evidence)

1. Helius minimum plan for Enhanced WebSockets: `helius-sdk/src/websockets/wsAsync.ts` says Business+; `helius-sdk/llms.txt`, `core-ai/.../websockets.md` and `onboarding.md` say Developer+.
2. Helius entry tier: `llms.txt` lists a $0 "Free" plan; `core-ai/.../onboarding.md` lists a "$10 USDC one-time" Agent plan with no Free row. Which is current is unknown.
3. Helius LaserStream replay window: `laserstream-sdk/README.md` "3000 slots (~20 minutes)" vs `core-ai/.../laserstream.md` "24-hour ... 216,000 slots".
4. Helius WebSocket connection counts per plan (5/150/250/1,000) appear only in `onboarding.md`; not cross-confirmed.
5. Helius latency claims ("1.5-2x faster", "shred-level", "40x faster than Yellowstone JS") are vendor statements with no measurements in the repos.
6. Existence of any Helius product literally named "Helius Core AI" beyond the `helius-labs/core-ai` tooling repo: none found.
7. Hummingbot paper trading not applying to Gateway/Jupiter markets: inferred from `GatewayBase` lacking an order-book tracker; no explicit statement found.
8. Hummingbot Gateway's current list of Solana router connectors (`dflow`, `okx`, `titan`, `orca` directories exist) vs the README DEX table (which omits them): README may be stale.
9. Jupiter API portal tiers and prices (other than "Pro plan does NOT increase Swap API limits"); "free tier: 60 req/min" comes from Gateway's `jupiter.yml`, not from Jupiter's repo; non-Swap API rate limits ("Managed at portal level") are not quantified.
10. Jupiter gasless eligibility thresholds (skill defers to external docs).
11. Birdeye pricing tiers, CU costs, per-API rps caps and WS connection counts: sourced only from third-party mirrors of docs.birdeye.so (`TheNang2710`, `fcrre26`); the mirrors also contradict each other internally on WebSocket entitlement (Business 500 vs Premium Plus 500 / Business 2000).
12. Birdeye `token_security` response field semantics and Smart Money labeling methodology: not described in any repo.
13. X API pricing/tiers, Reddit API pricing and numeric rate limits, YouTube Data API quota units, xAI/Grok pricing and `x_search` cost: not stated in the cloned SDK repos.
14. Any live behavior of any endpoint: no host other than GitHub was reachable from this container.
