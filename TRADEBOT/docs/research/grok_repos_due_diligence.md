# Due diligence: "Grok" / Solana trading-bot repos, alternatives, and historical datasets

Prepared 2026-11-02 for a paper-only Solana opportunity-discovery lab (Python).

**Method.** Every repo was cloned with `git clone --depth 50` (datasets with `--depth 1`) into its own empty directory under `<research scratch>/repos/<owner_repo>/src` or `<research scratch>/data/<owner_repo>/src`. Review was static only: cat, grep, find, git log, and `unzip -l`. Nothing from any repo was installed, built, imported or run. Two read-only helpers were used on the dataset files only: a parquet peek script kept outside the clones (`<research scratch>/tools/pq_peek.py`, run with `python3 -I`) and `zcat`/`head`/`cut`. GitHub metadata came only from `gh api search/repositories`, `search/code` and `search/issues`. Metadata was read on 2026-11-02 UTC.

**Uncertainty conventions.**
- "Verified" means seen in code or data.
- "Claim" means a README or marketing statement.
- "Unverified" means it could not be checked statically.

The xAI model names in these repos (`grok-4.3`, `grok-4.6`, `grok-4.20-0309-non-reasoning`, "Grok 4.5") postdate or sit at the edge of this reviewer's knowledge. They are reported as written, not confirmed against xAI's model list.

---

## 1. Summary of the 5 candidates

| # | Repo | Exists? | Stars / forks | Created / last push | License | Executes real trades? | LLM role | Red flags | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | zostaff/grok-trading-desk | Yes, 47 files, 22 commits | 17 / 8 | 2026-08-30 / 2026-08-30 | **None** (no LICENSE file; GitHub reports null) | **No** for crypto: the executor raises `NotImplementedError`. Stocks: Alpaca bracket orders, paper by default | About 10 Grok "agents" give JSON scores. Hard vetoes, scoring and sizing are code. A `grok-4.6` "checker" can veto | No malicious code found. All 22 commits came within about 45 minutes (AI-generated burst). LLM is asked to judge wallet-graph manipulation from aggregate stats it cannot actually see. Relies on xAI `search_parameters` live search, possibly deprecated (unverified). No license | **Ideas only** (no license means no legal reuse) |
| 2 | zostaff/grokbot-pumpfun | Yes, 63 files, 37 commits | 65 / 28 | 2026-08-26 / 2026-08-27 | MIT | **No**: `LiveExecutor` raises `NotImplementedError`. Dry-run simulates fills on pump.fun curve math | 4 Grok agents (auditor, narrative, timing, checker) feed a code scoring matrix. The checker (`grok-4`) is a veto gate | No malicious code found. Burst-built in 2 days. Russian-language docs. Stale venue model (says graduation goes to Raydium, not PumpSwap; hardcoded 1% fee). Uses unofficial `frontend-api.pump.fun` | **Ideas / selective reuse** (MIT): curve math, risk limits, JSONL log/replay patterns |
| 3 | hoodpumps/Grok-The-Trencher-4.5 | Yes, but **no code**: 3 files (README, architecture.md, LICENSE), 5 "Add files via upload" commits | 4 / 1 | 2026-07-09 / 2026-07-09 | MIT (license on an empty repo) | **Unknown / not demonstrable**: the README claims PumpPortal execution, but no code exists | Claim: "Grok 4.5 via OpenRouter decides buy/watch/skip". Nothing verifiable | Vaporware: marketing shell, single-repo account with a proton.me author, X handle promotion. The README tells users to put `WALLET_PRIVATE_KEY` as a byte array in env for code that is not public. Internal contradictions (moonbase thresholds differ between README and the deleted `docs.zip`) | **Avoid** (no code; if code is offered privately, treat it as a likely drainer until proven otherwise) |
| 4 | swcstudiospace/PumpGrok | Yes, 974 files, 133 commits | 34 / 8 | 2026-08-26 / 2026-11-02 (active) | AGPL-3.0 (copyright Spectrum Web Co LLC). Vendors MIT code from #2 | **No**: the live adapter is a refusing stub. Paper buy/sell commands also refuse until a "curve reader is verified". Replay-driven paper adapter exists | Agent instructions ("10 seats") for host LLM runtimes (Grok Bot, Claude Code, Cursor). Python CLI does reads only. Config names `grok-4.3` and `grok-4.20-0309-non-reasoning`. A human must approve every ticket by ID | No malicious code found; strong anti-key posture: the loader refuses env vars named `*PRIVATE_KEY*`, `SEED` and similar. Heavy AI-agent process overhead (`.planning/`, `.receipts/`). CI runs on self-hosted runners. Marketing ("1-Click… 8 Man… as seen on X", "Jupiter execution") overstates what the code does | **Ideas only**, maybe selective reuse if AGPL is acceptable: backtest harness design (walk-forward, holdout, trial accounting), secret redaction, on-chain read tools |
| 5 | galleonlabs/hypergrok-trading-desk | Yes, 134 files, 50 commits | 275 / 58 | 2026-08-16 / 2026-11-01 (active) | MIT | **Yes, via the user's agent**: skills instruct the host agent to place orders through the official `hyperliquid-python-sdk==0.24.0` with a trade-only API wallet, after human approval. Bundled scripts are read-only | LLM "roles" research, size and propose. The human approves by ticket ID. A PreToolUse hook asks before Bash commands that look like exchange writes | No malicious code found. Careful key guidance (API agent wallet, secret store, never in chat). Fork/star ratio is fairly high (cause unknown). **Not Solana**: Hyperliquid perps only | **Ideas only** for us (wrong venue). Its `research_dataset.py` provenance-bundle pattern is worth copying |

Bottom line: none of the five executes on-chain Solana trades in public code, and none shows evidence of profitability. Two (#1, #2) are well-structured, AI-generated skeletons with honest stubs. #4 is an AGPL agent-instruction pack with a substantial paper/replay harness. #5 is a careful Hyperliquid (not Solana) agent desk. #3 has no code at all.

**No candidate showed drainer indicators in its public code.** A drainer was found in the alternatives search; see section 3, `mortdeus/solana-copy-sniper-mev-trading-bot`.

---

## 2. Detailed per-repo findings

### 2.1 zostaff/grok-trading-desk
Clone: `<research scratch>/repos/zostaff_grok-trading-desk/src`

**Metadata and activity**
- 22 commits by `zostaff <<email redacted>>`, all on 2026-08-30 between 15:58 and 16:42 (+0300), about one commit every 2 minutes. This is a generated or bulk-committed project, not organic development.
- The owner account has 21 repos, most created and pushed the same day: betting, Polymarket and HFT bots, `quant-memcoins-researcher`, `gromo` (created 2026-11-02), and others. This is a burst-repo pattern. `ai-quant-researcher` has 184 stars on a one-day repo.
- No LICENSE file. GitHub `license: null`.

**Architecture** (`README.md:19-65`, `src/desk.py`)
- Four asyncio loops: crypto, stock (Alpaca, RTH), exit (every 4h) and allocator (every 24h).
- Crypto path, step by step:
  - `src/crypto/scout.py` is a PumpPortal WebSocket scout (`wss://pumpportal.fun/api/data`, line 280). Two stages: a create-event filter, then a trade-subscription watch window.
  - `auditor.py`, `narrative.py` and `crypto_pulse.py` are Grok agents.
  - `crypto_scoring.py` holds hard vetoes in code.
  - `crypto_checker.py` is a "deep" model adversarial gate.
  - `crypto_executor.py` is a stub.
- Shared modules: `shared/risk.py` (one portfolio across markets), `shared/memory.py` (outcome recall injected into prompts), `shared/log.py` (append-only JSONL).

**LLM integration**
- Endpoint: `https://api.x.ai/v1/chat/completions` (`src/base_agent.py:149`, `config.example.yaml:8`).
- Defaults: `grok-4.3` (fast) and `grok-4.6` (deep) (`src/base_agent.py:154`). The README states `grok-4-fast` and `grok-4` were "retired 2026-05-15" (claim, unverified).
- Uses strict `json_schema` output and `prompt_cache_key`. Reads cost from `usage.cost_in_usd_ticks`.
- Uses xAI live search (`search_parameters`, `base_agent.py:203-247`). Reviewer note: xAI announced deprecation of `search_parameters` Live Search in favour of server-side agent tools in late 2025. Unverified whether it still works; if not, every "news/X" agent answers from its prior.
- Who decides: the LLM produces scores and booleans. Code applies hard vetoes, the score threshold and sizing (`shared/risk.py`). The LLM checker must approve. A parse or HTTP failure returns a pessimistic fallback, e.g. auditor `fallback()` sets coordinated_buys=True (`src/crypto/auditor.py:~88`).

**Design flaw (demonstrable)**
- The auditor prompt (`src/crypto/auditor.py:15-30`) asks the model to detect "wallets funded from a common source" and wash trading.
- The facts passed (`auditor.py:57-75`) are only aggregates: holders, top10 %, buys/sells, unique traders. No wallet graph or funding edges are supplied.
- The model is therefore asked to infer things it cannot observe, except via X search.

**Execution and wallet**
- `src/crypto/crypto_executor.py:1-37` is a docstring plan: load a base58 key via `solders.keypair.Keypair.from_base58_string`, use Jito bundles, then the curve or Raydium/Jupiter.
- Lines 64-74 show every method raising `NotImplementedError`.
- `config.example.yaml:26` holds the `wallet_key` placeholder.
- `desk.py:209-216`: dry-run logs a "buy" with `tx_id="DRY_RUN"` and does **not** create a position. Dry-run therefore produces no simulated PnL for crypto (inferred from code; no fill or price simulation found).
- Stock path: `src/stocks/stock_executor.py` uses `alpaca-py`, paper by default. Live needs `mode: live` plus the `--i-understand-the-risk` flag (README:223-235).

**Venues:** pump.fun via PumpPortal websocket only. No Raydium, Jupiter or PumpSwap code.

**Dependencies** (`requirements.txt`): websockets, httpx, pydantic, pyyaml, solders, solana, alpaca-py, pytest, pytest-asyncio. All mainstream, none suspicious.

**Security scan**
- No eval/exec/base64 blobs, subprocess calls, postinstall hooks or lines over 500 chars.
- No hardcoded base58 addresses in source.
- Outbound hosts: api.x.ai, pumpportal.fun, api.mainnet-beta.solana.com, Jito block engine (config only).

**Quality:** 204 test functions over 4.1k LOC. The README says tests are mocked-HTTP; not run, per the rules.

**README claims:** no performance or profitability claims. A disclaimer says memecoin trading "loses most participants most of their capital".

**Demonstrable vs marketing**
- Demonstrable: scout filter logic, risk manager, scoring vetoes, JSONL log/replay, Alpaca paper path.
- Marketing-ish: "twelve bots", "orchestrated by Grok", "adversarial checker is the safety". The safety rests on LLM judgment over thin data.

### 2.2 zostaff/grokbot-pumpfun
Clone: `<research scratch>/repos/zostaff_grokbot-pumpfun/src`

**Metadata and activity**
- 37 commits by `Maksim <<email redacted>>`: 2026-08-26 15:40 to 17:30 and 2026-08-27 20:02 to 20:31. Commit messages are in Russian. Same owner account as #1, but a different author identity.
- 1 open issue (not inspected).
- MIT LICENSE.

**Architecture** (`README.md:14-80`): nine stages.
1. WS monitor with code filter.
2. Creator-reputation memory from its own log (`src/reputation.py`).
3. Analyzer: 3 parallel REST calls to `https://frontend-api.pump.fun` (`/coins/{mint}`, `/coins/{mint}/holders`, `/trades/all/{mint}`; `src/analyzer.py:84-87`; base URL `src/models.py:318`) with a code risk score and a hard veto on creator ≥25% or top-5 ≥80%.
4. Three Grok agents: auditor, narrative, timing.
5. Weighted scoring matrix: audit·0.30 + narrative·0.25 + timing·0.15 + metrics·0.30.
6. Grok checker (adversarial veto).
7. Risk (`src/risk.py`): 5 limits, background stop-loss, take-profit, trailing stop and max hold.
8. Executor.
9. JSONL log, replay and `scripts/tune.py` weight tuning.

Ops extras: Grok spend limiter, health endpoint, single-instance lock, loss-streak cooldown (`src/ops.py`, `src/state.py`).

**LLM integration**
- Defaults `fast_model="grok-4-fast"` and `checker_model="grok-4"` (`src/models.py:289-290`). Endpoint `https://api.x.ai/v1/chat/completions` (`models.py:288`).
- The LLM never sizes or sends. It supplies scores and the approve/veto.

**Execution and wallet**
- `src/executor.py:1-12, 216-257`: `LiveExecutor.buy/sell` raise `NotImplementedError` (lines 246, 255) with TODO steps.
- `DryRunExecutor` (`executor.py:161-214`) fills with `curve.py` constant-product math including fee, slippage and own-order price impact.
- No latency or priority-fee modelling (grep found none).
- Key: `wallet_private_key: SecretStr` (`src/models.py:308`) from env `GROKBOT_WALLET_PRIVATE_KEY` (`.env.example`). It is never used in code except the TODO comment (`executor.py:231`). SecretStr keeps it out of reprs and logs.

**Webhook alerts**
- `src/alerts.py` posts `text` and `content` to a user-configured `webhook_url` (SecretStr, `models.py:383`). Off by default.
- Payloads are event texts (buy, close, rug and so on). No key material is passed: the Notifier only receives event strings and fields.

**Venue accuracy (stale)**
- `curve.py:31-44` hardcodes initial virtual reserves (30 SOL / 1.073e9 tokens), 85 SOL completion and a 1% fee. Comments and code say graduated tokens go to **Raydium** (`curve.py:40,52`, `executor.py:191-194`).
- pump.fun graduations moved to PumpSwap in 2025, and fees changed to tiered or dynamic. The file itself warns its constants may be outdated (`curve.py:19-22`).

**Dependencies:** websockets, httpx, pydantic, pyyaml, solders, solana. Dev: pytest, pytest-cov, ruff, mypy, types-PyYAML. Nothing suspicious.
- Dockerfile: python:3.12-slim, non-root user, no secrets baked in.
- CI (`.github/workflows/ci.yml`): ruff, mypy and pytest on 3.11-3.13, plus a check that placeholder configs are rejected.

**Security scan:** clean. No eval/exec, base64 blobs, hardcoded addresses or postinstall hooks.

**Quality:** 419 test functions, 6.2k LOC. Better structured than #1.

**README claims:** "отсев ~94%" (the monitor filters out about 94% of launches). Unverified, no data shipped. No profit claims. The README explicitly says dry-run must not show fake profit.

**Reuse:** MIT, so code can be reused with notice. Worth lifting as reference only, re-verified against current pump.fun docs:
- the curve buy/sell quote math (`curve.py`);
- risk-limit structure;
- JSONL replay and funnel stats.

### 2.3 hoodpumps/Grok-The-Trencher-4.5
Clone: `<research scratch>/repos/hoodpumps_Grok-The-Trencher-4.5/src`

**Contents**
- Only `README.md` (225 lines), `architecture.md` and `LICENSE`.
- A `docs.zip` was uploaded and deleted within a minute (commits c780f27 and 635edd2). Listed statically: it contained only `docs/architecture.md` and `docs/world-levels.md`. No code.

**Activity:** 5 "Add files via upload" commits on 2026-07-09 within 13 minutes, by `Grok The Trencher 4.5 <<email redacted>>`. The account has exactly one repo.

**Claims with no code to back them**
- The README describes an Express 5 / React / Postgres monorepo (`artifacts/api-server/src/trader.ts`, `ai-decision.ts`, `pumpportal.ts`) that is **not in the repo**.
- Claims: "fully autonomous", "Grok 4.5 via OpenRouter decides buy/watch/skip", buys through PumpPortal's trading API, Helius RPC.
- `README.md:157-161` instructs `WALLET_PRIVATE_KEY=1,2,3,... # comma-separated byte array`.

**Inconsistencies**
- Moonbase level thresholds differ: README 0.05/0.25/1/5/20 SOL vs world-levels.md in the zip 1/5/10/25/50 SOL.
- The README hero image is hosted on image2url.com.
- The README promotes the X account @Groktrencher.

**Verdict:** Avoid. Nothing demonstrable. If the code is distributed elsewhere (DM, zip, npm), treat it as untrusted. The "paste your private key byte array" plus "fully autonomous AI" pattern is typical of memecoin-promo or drainer funnels. That cannot be confirmed here because no code exists.

### 2.4 swcstudiospace/PumpGrok
Clone: `<research scratch>/repos/swcstudiospace_PumpGrok/src`

**Metadata and activity**
- 133 commits: 126 by `SomeRandmGuyy`, 5 by `Omega Prime <bot-00-omega-prime@localhost>` (an agent identity).
- Activity: 2026-08-26 to 29, a gap, then bursts on 2026-10-23 to 28 and 2026-11-01 to 03 (82 commits in 3 days).
- Org account has 70 repos, mostly AI-agent tooling.
- 4 open issues, 3 of them self-filed "waivers".
- License: AGPL-3.0-only (`pyproject.toml`, `LICENSE`), copyright Spectrum Web Co LLC.

**What it is** (`README.md:5-17`): "agent instructions, 35 skills, a hard security constitution, and Python helpers that never sign or send". It loads into Grok Bot, Claude Code, Cursor or Grok Build (`plugin.json`, `.claude-plugin/`, `.cursor-plugin/`, `.grok-plugin/`). Ten "seats" are defined in `agents/*.md`.

**Python package `src/pumpgrok/`** (38k LOC excluding tests; 1,906 test functions; 270 test files)
- `tools/onchain`: authorities, holders, top_holders, lp_status, priority_fee, deployer_history, curve_layout.
- `tools/market`: Jupiter quote via `https://api.jup.ag/swap/v2/order` (`config/overlays/paper.toml:40`), price, trades, holders_snapshot, ohlcv.
- `ledger` (sqlite), `risk` engine and kill switch, `replay` with a seeded RNG.
- `backtest`: harness, walk-forward, holdout, trial ledger.
- Supervisor and Telegram/webhook notify.
- `engine/`: a screening engine derived from zostaff/grokbot-pumpfun (MIT), provenance in `docs/VENDOR_PIN.md`.
  - Pin `409e74c905…` on branch `docs/english-markdown`. That commit is not in the main-branch clone of #2, so it is unverified.

**Execution**
- `src/pumpgrok/execution/not_yet_implemented.py`: every method raises.
- `execution/live/live_adapter.py:1-7`: every method logs `LiveWriteAttempted` and raises `LiveGateError`. "No transaction is built or submitted… holds no key material".
- No `solders`/`solana` dependency at all (`pyproject.toml`). README:15: paper buy/sell commands "refuse with code `unavailable` until the curve reader is verified".
- Replay paper mode exists: `pumpgrok run --mode paper --once --replay <dir>`.
- Test fixtures are synthetic: `tests/fixtures/replay/smoke-10m/generator.py`, "hand-authored", 41 ticks.

**LLM:** `src/pumpgrok/config/settings.py:95-104` sets `base_url https://api.x.ai/v1`, `strong_model "grok-4.3"`, `fast_model "grok-4.20-0309-non-reasoning"`. The API key is an env-only SecretStr. Human approval by ticket ID is required for any send (`rules/pumpgrok-team.mdc`, README:5).

**Key handling (good)**
- `settings.py:297-312` refuses startup if env names contain `PRIVATE_KEY`, `SECRET_KEY`, `SEED`, `MNEMONIC`, `PHANTOM`, `WALLET_SECRET` or `KEYPAIR`. `.env.example` documents this.
- `logging/redaction.py:24-42` redacts secret-like names and 64-byte JSON keypair arrays.
- Telegram (`tools/notify/telegram.py:1-60`) uses the user's own bot token from `PUMPGROK_TELEGRAM_TOKEN`, sends to an allowlisted chat_id, and sanitizes errors so the token is not leaked.

**Dependencies** (`pyproject.toml` and `uv.lock`, hash-pinned)
- Runtime: pydantic, pydantic-settings, httpx, websockets, pyyaml, tenacity, prometheus-client, defusedxml.
- Dev: ruff, mypy, pytest, pytest-asyncio, pip-audit, types-PyYAML.
- Lock entries `ast-serialize` and `librt` are pulled in by `mypy` only (`uv.lock:325-327`). They look unusual but are dev-only and transitive. Worth a PyPI provenance check before installing; not verified here.

**Security scan**
- Clean: no eval/exec, b64 blobs or postinstall hooks.
- Only hardcoded address: the pump.fun program ID in `scripts/probe_curve_layout.py:54`.
- `.devcontainer.json` is a plain MS python image. `.pre-commit-config.yaml` uses only local hooks.
- CI (`.github/workflows/ci.yml`) runs on self-hosted runners `[self-hosted, linux, x64, omega-ci]` and skips fork PRs.

**Marketing vs code**
- GitHub description: "1-Click Grok Bot 8 Man Crypto Trading Desk as seen on X". `plugin.json`: "Jupiter execution".
- Code reality: ten seats, no execution of any kind. README:15 states "The desk ships no strategies and makes no return claims".

**Quality:** extensive but very process-heavy: `.planning/milestones`, `.receipts`, "seat" bots and "owner approval" receipts. The project is largely agent-authored, and maintainability for humans is questionable.

**Reusable ideas** (AGPL means copying code obliges AGPL for network-served derivatives; prefer reimplementing):
- `backtest/harness.py` docstring: holdout as the final fraction of the time span; tuning on walk-forward folds only; one-shot holdout evaluation; trial counting with eligibility caps.
- Env-name key bans and log redaction.
- Read-only on-chain probes (mint/freeze authority, top holders, LP status).

### 2.5 galleonlabs/hypergrok-trading-desk
Clone: `<research scratch>/repos/galleonlabs_hypergrok-trading-desk/src`

**Metadata and activity**
- 50 commits by Andrew Wilkinson (3 email identities, one `<email redacted>`), steady from 2026-09-04 to 2026-11-01.
- Releases are tagged (v1.7.1) and published to npm as `hypergrok-trading-desk`. The README recommends `npm install --ignore-scripts`.
- `package.json` scripts: only `test` and `prepack`, both `bash scripts/check.sh`. No postinstall.
- MIT license plus `ATTRIBUTION.md`.

**What it is:** 7 role profiles (`agents/*.md`), 17 skills, and stdlib-only Python scripts. All of it targets **Hyperliquid perps**, including HIP-3 markets. No Solana.

**LLM and execution**
- Host LLM agents (Grok Bot and others) follow skills. Execution goes through the official `hyperliquid-python-sdk==0.24.0` (`skills/hyperliquid-setup/SKILL.md:37`) using a **trade-only API (agent) wallet** whose key is stored in the Grok Bot secret store or env `HYPERLIQUID_PRIVATE_KEY`.
- "Never ask them to paste it in chat and never write it to a file under /workspace" (`skills/hyperliquid-setup/SKILL.md:88-147`).
- A human approves each ticket by ID.
- `hooks/exchange_write_gate.py` (117 lines; imports only json, os, re, sys; reads local files only) is a Claude Code PreToolUse hook that asks before exchange-write-looking Bash commands.

**Data tooling worth copying:** `scripts/research_dataset.py` and `docs/RESEARCH_DATASETS.md`.
- Explicit `[start,end)` UTC windows, no open candles.
- Captured request/response `sources.json` and a hashed manifest.
- Explicit `complete` / `incomplete` / `unavailable` status. Never fills gaps synthetically.
- Notes that current metadata cannot remove survivorship bias.

**Security scan:** clean. `base64` appears only in `assets/src/build_graphics.py`, embedding a mascot image into SVG. `subprocess` appears only in test and check scripts.

**Claims:** "no keys" first run, which is verifiable by reading the stdlib scripts. No performance claims; the ETH ticket is labelled "illustrative".

**Verdict:** a good-practice reference for approval gating and data provenance, but the wrong venue for a Solana lab.

---

## 3. Alternatives, reusable components and recommendations

### 3.1 Alternatives evaluated (brief)

| Repo | Stars | Last push | License | Lang | Assessment |
|---|---|---|---|---|---|
| michaelhly/solana-py | 1,443 | 2026-11-01 | MIT | Python | De facto Python RPC client (`solana` on PyPI). **Reuse** for read-only RPC (getSignaturesForAddress, getTransaction, token accounts). |
| kevinheavey/solders | 449 | 2026-10-26 | Apache-2.0 | Rust/Python | Core Solana types (Pubkey, transactions, parsing). **Reuse** for decoding transactions and logs offline. |
| chainstacklabs/pumpfun-bonkfun-bot | 1,049 / 358 forks | 2026-11-01 | Apache-2.0 | Python | Real pump.fun / letsbonk / StonkFun trading bot by an infrastructure vendor. 29 of 30 recent commits by one maintainer, dependabot active. Ships **IDLs** (`idl/pump_fun_idl.json`, `pump_swap_idl.json`, `pump_fees.json`, `raydium_*`). Event parsers decode program logs (`src/platforms/pumpfun/event_parser.py`; base64 use there is legitimate log decoding). Key loaded from config into `Wallet` (`src/core/wallet.py`). Limited tests (39 py files under `tests/regression`). **Reuse parsers/IDLs as reference; do not run its traders.** |
| pump-fun/pump-public-docs | 470 | 2026-10-13 | **none** | TS/docs | Official program docs and IDLs (bonding curve, PumpSwap, fees). **Authoritative reference** for curve and fee math. No license, so read; do not copy. |
| sendaifun/solana-agent-kit | 1,732 / 893 forks | 2026-05-14 | Apache-2.0 | TypeScript | Broad "agent → Solana protocol" toolkit (Jupiter, Raydium, pump.fun, Rugcheck actions). TS, not Python; push cadence slowed. Ideas only for a Python paper lab. |
| hummingbot/hummingbot + hummingbot/gateway | 20,913 / 262 | 2026-10-20 / 2026-11-01 | Apache-2.0 | Python / TS | Mature market-making framework. Solana DEX access (Jupiter, Raydium, Meteora) goes through the TS Gateway. Heavy for discovery research; borrow connector abstraction and paper-exchange ideas only. |
| freqtrade/freqtrade | 56,344 | 2026-11-02 | GPL-3.0 | Python | Best-in-class backtesting discipline: lookahead-analysis and recursive-analysis commands, protections, walk-forward via hyperopt timeranges, fee and slippage settings. CEX-oriented; GPL. **Reimplement ideas, do not copy code.** |
| elizaOS/eliza | 19,755 | 2026-11-02 | MIT | TS | Agent framework; Solana plugins are mostly community and third-party quality. Not needed. |
| outsmartchad/onchain-dex-cli | 581 | 2026-03-07 | MIT | TS | Multi-DEX Solana CLI (18 adapters). Execution-oriented; reference only. |
| joshua-nixon/dexscreener | 156 | 2025-11-13 | MIT | Python | Thin DexScreener API wrapper. The API is simple enough to call directly with httpx; use as a reference for endpoints. |
| dineshpinto/geckoterminal-api / cardosofede/geckoterminal-py | 47 / 15 | 2024-12 / 2026-07 | MIT / GPL-3.0 | Python | GeckoTerminal wrappers (OHLCV per pool). Small; write a 100-line client ourselves. |
| 0xTaoDev/jupiter-python-sdk | 245 | 2024-04-08 | MIT | Python | Stale (2024), unofficial. Jupiter has since moved API versions (PumpGrok uses `api.jup.ag/swap/v2/order`). **Do not use**; call Jupiter's HTTP quote API directly for paper quotes. |
| jup-ag/* (official) | 100-290 | 2026 | mostly **no license** | TS/Rust | Official clients (`jupiter-swap-api-client`, `jupiter-quote-api-node`, `metis-binary`). Use the hosted HTTP API, not the code. |
| ccan23/rugcheck, degenfrends/solana-rugchecker | 11 / 77 | 2025-02 / 2024-06 | MIT / none | Python / TS | Tiny, stale wrappers around the RugCheck API. Note: `@degenfrends/solana-rugchecker` appears as a dependency in the malicious repo below. Implement our own authority, holder and LP checks via RPC; optionally query RugCheck's public API directly. |
| vincentkoc/dexscraper | 119 | 2026-11-02 | GPL-3.0 | Python | Scrapes DexScreener websockets. Possible ToS issues; GPL. Avoid for the core. |

**Malicious example (explicit warning): mortdeus/solana-copy-sniper-mev-trading-bot.** 4,513 stars, BSD-3-Clause, "created 2013-03-01". Clone at `<research scratch>/repos/mortdeus_solana-copy-sniper-mev-trading-bot/src`.
- **Repo takeover or repurpose.** History is an unrelated 2013-2022 C project. Commit `086ec82` (2025-09-18, author `cryptoking1106`) replaced it with Solana bot code (141 files changed). The README advertises `t.me/mortdeus` and a discord invite.
- **Hidden payload dependency.** Every bot's `package.json` declares `"web3-fb": "^1.2.2"`. `package-lock.json:10235-10247` resolves it from **`https://github.com/crypto2001813/solana-sdk/releases/download/1.2.2/web3-fb-1.2.2.tgz`** with `"hasInstallScript": true` and a bin `solana-scan → dist/universal-launcher.cjs`. **It is never imported by any .js file** (grep found no usage).
- **Other suspicious dependencies.** `pump-swap-core-v1` is a self-referencing package (`package-lock.json:8157-8170`). Junk dependencies `fs`, `require` and `util` are also declared.
- This is the standard npm install-time stealer pattern targeting `.env` private keys. **Treat as malicious; never `npm install` it.** It was not executed; the conclusion rests on static indicators.

### 3.2 Recommendations: reuse vs build

**Reuse (libraries / references)**
- `solana` (solana-py, MIT) + `solders` (Apache-2.0): RPC reads and transaction/log decoding. Only libraries with real users and maintenance in Python.
- `pump-fun/pump-public-docs` IDLs and `chainstacklabs/pumpfun-bonkfun-bot/idl/*` (Apache-2.0): decoding pump.fun / PumpSwap events and curve state. Verify against on-chain `global` account values, not hardcoded constants.
- Hosted APIs called directly with `httpx`: Jupiter quote API (paper quotes and price-impact estimates), DexScreener and GeckoTerminal (pairs and OHLCV), optionally the RugCheck API. Wrappers add little and several are stale.
- From zostaff/grokbot-pumpfun (MIT, attribution required): `src/curve.py` quote math and the risk-limit structure, as starting templates only, after correcting the fee and PumpSwap graduation model.

**Build ourselves** (small, critical, and must be survivorship-aware and auditable)
- Event-time data recorder in the hypergrok `research_dataset.py` style:
  - store raw responses, `retrieved_utc`, event block time and slot;
  - mark gaps explicitly; hash a manifest;
  - never fill missing data.
- Cohort builder from **launch events** (all pump.fun creates via RPC, or Coinbase CDP SQL as in `mahad1z/memecoin-survival/sql/01_token_level.sql`), so dead tokens are in the denominator.
- Token-safety checks: mint/freeze authority, top-holder concentration, creator history, LP/pool state. Reads only, via solana-py.
- Paper fill simulator against recorded reserves, with fees, latency, a max-wait and an unfillable state. Launch Tape's README (`<research scratch>/data/launchtape-io_pump-fun-backtest/src/README.md`, "How fills work") is a good spec.
- Backtest protocol, combining freqtrade-style lookahead and recursive checks with the PumpGrok harness rules: walk-forward folds, a held-out final time slice evaluated once, trial counting and closed-only-PnL warnings.
- LLM, if used: advisory classification only, with code vetoes. The zostaff pattern of "pessimistic fallback when the model fails" is sound. Never let an LLM judge facts it is not given (auditor flaw, 2.1).

**Do not reuse:** any repo's signing or execution path (unnecessary for paper-only, and the main risk surface); unvetted npm packages; any "sniper" repo distributed with Telegram contact funnels.

---

## 4. Public historical datasets (survivorship-aware backtests)

All cloned `--depth 1` into `<research scratch>/data/<owner_repo>/src`.

| Dataset | Size (clone) | Format / exact data paths | Time coverage | # tokens | Dead / rugged included? | Timestamps | License | Fit for our use |
|---|---|---|---|---|---|---|---|---|
| **DeFiLabX/SolRPDS** (ACM CODASPY'25 paper) | 116 MB | CSV `dataset/CSV/{2021,2022,2023,Jan_2024-Nov_2024}.csv` (+ same as JSON in `dataset/json/`) | 2021-02-14 to 2024-11-01 | about 67k Raydium pools (861 / 1,886 / 8,324 / 56,004 per file); rows are (pool, mint) pairs | **Yes**: `INACTIVITY_STATUS` Active/Inactive (2021: 90 inactive / 1,612 active; 2024: 19,074 / 76,358) plus liquidity add/remove totals | `YYYY-MM-DD HH:MM:SS.000`, no TZ (likely UTC, unverified). Event-derived (first/last pool activity, last swap) | CC BY 4.0 (README) | Rug labelling and base rates for Raydium pools. **No price series**, pre-pump.fun-PumpSwap era |
| **git-disl/MELT** | 31 MB (raw tx on Google Drive, "very large", not fetched) | `data/memecoin/memecoin_list.jsonl` (46,138 launches), `data/memecoin/metadata.jsonl` (29,557), `data/label/label.csv` (41,470), `data/feat/sol_hourly.txt` | Launches 2024-12-09T22:31:50Z to 2025-02-28T23:59:20Z | 46k pump.fun mints | Partly: labels high/medium/low risk (34,890 high, 4,702 medium, 1,878 low). **Appears limited to tokens that migrated to Raydium** (README splits pre/post-migration tx). Inferred, verify in paper | `time` ISO-8601 Z plus unix `timestamp` (creation event time) | **CC BY-NC 4.0** (non-commercial) | Good for launch-to-migration features; graduation-conditioned; NC licence |
| **ian05012/solana-memecoin-dataset** | 290 MB | Parquet `data/tokens.parquet` (44,460 snapshots, 31,013 mints), `data/trades-000..003.parquet` (9.5M rows), `data/ohlcv.parquet` (272,426 15-min candles, 11,170 mints), `data/sequences.parquet`, `data/wallets.parquet`, `data/wallet_funding.parquet` | Snapshots 2026-06-10T08:36Z to 2026-06-29T20:01Z. OHLCV ts 1781081100 to 1782783900 (2026-06-10 to 06-30). Wallet trades back to 2024-06-04 | 31k | **Partial**: includes tokens that went to zero (max_return 0.0), but the README admits **survivorship bias** (only tokens detected as graduated or trending) | `captured_at` tz-aware UTC = **collection time**. `ohlcv.ts` unix s and `trades.ts` UTC ms = event time | MIT (data scraped from GMGN; source ToS uncertain) | Best ready-made post-graduation panel with forward-return labels; a single 20-day regime |
| **Solid-Sea/pump-gold-dataset** | 80 MB | `pump_gold_dataset_core.csv.gz` (1,306,082 rows, one per token) | Feb 2025 (per `.zenodo.json`; no per-row time) | 1.3M pump.fun tokens | **Yes**, full cohort: 93.4% "dead" (<2x), 0.25% "golden" (≥10x) | **None per row** | CC BY 4.0 (LICENSE); provenance inconsistent (README says Kaggle, zenodo says Dune) | Unconditional base rates only; no time series |
| **willho/fingerprinter-dataset** | 215 MB (pg_dump split `fingerprinter_db.dump.part-aa/ab/ac`, "PGDMP" header confirmed; about 2.1 GB restored) | Postgres tables `fingerprint_snapshots`, `wallet_token_positions`, `tokens`, `creators`, `rotations` | Snapshot taken 2026-06-11; coverage start not stated | about 39k pump.fun launches | **Yes**: `is_deathbed`, drawdown triggers, creator `rug_rate` | `timestamp` unix ms = capture time (event-triggered, irregular) | **No license** (all rights reserved by default) | Rich trajectories but unlicensed and needs pg_restore; not restored here |
| **Smurfetc/solana-memecoin-calls-dataset** | 1.9 MB | `calls.jsonl` (11,384 rows) | 2026-06-30 to 2026-11-02 (synced nightly) | 11,384 called mints | Losers kept (README: about 6/10 never 2x), **but selection = one Telegram channel's calls**; `peak` only, no path | `t` unix s / `utc` ISO Z = call (publish) time; hash anchored via OpenTimestamps | CC0 | Only for evaluating that caller; promotional; peak is not tradeable |
| **hwlongCUHK/Coin-Meme** (WWW'25) | 229 MB (mostly logos) | `data/destription+finance.csv`, `data/comments.csv.zip`, `data/Image/image*.zip` | Jan to Nov 2024 | 3,751 | **No**: only tokens that moved to Raydium | `createdAt`, `bonding_completeTime` as `dd/mm/yyyy, HH:MM:SS`, TZ unstated | MIT | Narrative/multimodal features; survivorship-biased |
| **tomdelcarlo-lgtm/pumpfun-graduation-prediction** | 8.8 MB | `data/pumpfun_training_dataset.parquet` (32,000 × 17) | `launch_month`=1 only (year unstated) | 32,000 launches | **Yes**: 1.82% graduated, the rest not | Only `launch_hour_utc`, `launch_dayofweek`, `launch_month` | MIT | Small first-30-min feature set with graduation label; no prices |
| **inventandchill/data-samples** | 22 MB | `b_prime_training_matrix.csv` (3,231 × 76). `.pkl` is also present: **do not unpickle untrusted pickles** | Tokens migrated 2026-04-02 to 04-17 | 3,231 | No: migrated tokens only | None in matrix | None | Feature ideas only |
| **launchtape-io/pump-fun-backtest** (sample of commercial "Launch Tape") | 2.3 MB | `data/preview.jsonl.gz` (14,496 rows: 5 graduations + all trades + feed `down`/`up` gap markers); provenance `data/provenance.json` (source: `marklavigueur-dev/solana-launch-tape/sample/gradtrades-2026-10-10-1400-1600utc.jsonl.gz`, 66 MB, 105 graduations) | 2026-10-10 14:00 to 16:00 UTC | 5 (sample) / 105 (source sample) | All graduations in the window, unfiltered (graduation-conditioned) | `t` = arrival ms (collection), `bt` = block time s, `slot`; gaps marked | Code MIT; **data: research use, no redistribution** (`DATA_LICENSE.txt`) | Best-specified PumpSwap trade-tape schema with pool reserves and fees; tiny free sample |
| **mahad1z/memecoin-survival** (analysis + SQL) | <1 MB | `data/population_summary.csv` (per launch-day aggregates), `data/daily_context.csv` (2026-08-13 to 10-20 daily launches/graduations), `sql/01..05_*.sql` | Cohort: every launch 2026-10-01 to 10-07 (352,621), followed 7 days | Aggregates only in repo; raw 5% sample must be regenerated | **Yes**: full launch cohort, competing-risk death vs graduation | Query-time UTC ISO | MIT | **Method template** for survivorship-free cohorts (Coinbase CDP SQL on raw `solana.instructions`/`transfers`) |

**Sample rows**

SolRPDS `dataset/CSV/2021.csv`:
```
LIQUIDITY_POOL_ADDRESS,MINT,TOTAL_ADDED_LIQUIDITY,TOTAL_REMOVED_LIQUIDITY,NUM_LIQUIDITY_ADDS,NUM_LIQUIDITY_REMOVES,ADD_TO_REMOVE_RATIO,LAST_POOL_ACTIVITY_TIMESTAMP,FIRST_POOL_ACTIVITY_TIMESTAMP,LAST_SWAP_TIMESTAMP,LAST_SWAP_TX_ID,INACTIVITY_STATUS
821L241gacEqKJiCp9H7E9rqRXB31WoHLBqwhn8NMyWS,EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v,131.42666,37.879524,4,2,3.469596397,2021-12-30 22:26:30.000,2021-12-30 21:55:04.000,2021-12-30 23:59:52.000,3NPujARy…,Active
```

MELT `data/memecoin/memecoin_list.jsonl` and `data/label/label.csv`:
```
{"token_address": "BUnX7py9CgtbCjX5vtdRGfudXcwZykirHPd4CP71pump", "signature": "5gNDGvwo…", "timestamp": "1740787160", "time": "2025-02-28T23:59:20Z", "creator": "39azUYFW…"}
mint_address,min_ratio,manipulated,return_ratio,label
DKGqCGgD3EkzNtPT9c7ePrEnrDDGRujBfJvNDGHBpump,0.10012842088594,unknown,-0.6663484035964322,high
```

ian05012 `data/tokens.parquet` (selected columns), `data/ohlcv.parquet`, `data/trades-000.parquet`:
```
sample_id=128 token_address=7qDSP8y8w58gCb1PJp7wfmAb4TPBDN2zDgjDxtNPW6a9 symbol=SPCX captured_at=2026-06-10 08:36:49.384361+00:00 time_since_launch_sec=275 reaches_2x=False staged_exit_return=0.0 runner_peak_x=1.0
token_address=11SuEtrpMWSCGUgsWiFPfEvKwUaykR3iMUY1Boqpump ts=1782567000 open=0.000004 high=0.000004 low=0.000003 close=0.000003 volume=246.48597
wallet=8vDdFW6hdGWzsFmPAhynnt6DjG7WSAcpsVZTNFGATXUP token=Hrwv56Ut…pump side=buy amount_usd=339.72 ts=2024-06-04 21:40:04+00:00
```

pump-gold `pump_gold_dataset_core.csv.gz`:
```
mint,buy_count,sell_count,unique_wallets,total_quote_lamports,avg_swap_size,pmin,pmax,is_golden,is_dead
112GXdaoTJN4nrL2P2Gc3vXKSkHmTfzvLMBGJukpump,36,25,36,24354126818,399247980.6229508,2.8366e-05,5.3508e-05,0,1
```

Smurfetc `calls.jsonl`:
```
{"mc": 4407, "mint": "8p3MWb9UZsCWLLsc4FSE3vtn5KTHhiynboGpYi88pump", "peak": 6.316, "sym": "TJR", "t": 1782825883, "tg": 94, "utc": "2026-06-30T13:24:43Z"}
```

Coin-Meme `data/destription+finance.csv`:
```
token_name,token_ticker,token_address,description,mCap,createdAt,bonding_completeTime
Elon Musk's nickname,SQUIRTBORN,5njPgyEfBDvGxHtMTN86yQnNr1XTd3zy9D2eb84zpump,We are all born from a squirt,"5,599","13/10/2024, 08:31:20","13/10/2024, 08:33:39"
```

tomdelcarlo `data/pumpfun_training_dataset.parquet` (selected columns):
```
avg_trade_size_30min=75.22 buy_sell_ratio_30min=1.11 graduated=0 launch_dayofweek=3 launch_hour_utc=1 unique_buyers_30min=66 volume_first_30min=5942.31
```

Launch Tape `data/preview.jsonl.gz` (truncated):
```
{"type":"down","t":1791640822088}
{"type":"grad","t":1791640841373,"bt":1791640840,"slot":455281028,"sig":"45ZhqvZw…","mint":"9pAbYE6q9t5F8tDdrJWrVyMKEYhZnPLwB6hNEHPSnkkt","pool":"2uNzE8XX…","poolBase":198060505.73945,"poolQuote":88.783503748,"virtualQuote":17.584505653,"price":5.37048e-7,"capSol":537.05}
{"type":"trade","t":1791640841557,"bt":1791640841,"slot":455281029,"sig":"3yypzsFk…","side":"buy","user":"Hi1XK7kA…","baseAmount":659111.174455,"quoteAmount":0.296442686,"price":4.49761e-7,"poolBase":197401395,"poolQuote":71.496033667,"priceAfter":4.51266e-7,"lpFee":0.000592886,"protocolFee":0.000148222,…}
```

mahad1z `data/daily_context.csv`:
```
day,graduated,graduation_rate,launches
2026-08-13,1004,0.0249515383468363,40238
```

**Dataset conclusions**
- **No single public GitHub dataset offers what a survivorship-aware new-token backtest needs:** all launches in a window, tick- or minute-level prices from launch, including the dead ones, with event timestamps.
- The closest pieces:
  - full-cohort base rates: pump-gold (no time), mahad1z SQL (method), tomdelcarlo (no prices);
  - post-detection price panels: ian05012 (survivorship-biased, but honest about it);
  - trade tapes with reserves and gap markers: Launch Tape (tiny free sample; commercial licence).
- **Recommendation:** build our own recorder from day one, covering all creates plus trades with block time, slot and arrival time. Use the external datasets only for base-rate sanity checks (graduation about 1.8-2.8% per tomdelcarlo and mahad1z; 93% dead per pump-gold) and feature ideas.
- Lead not verified (not on GitHub): "RED-PUMP-2026-v1", 860,213 pump.fun launches with survival outcomes, CC-BY, Zenodo DOI 10.5281/zenodo.20633486 (cited in the Smurfetc README). Worth checking.
- Hugging Face and Kaggle were not searched; only GitHub was.

---

## 5. URLs consulted

**Candidate repos**
- https://github.com/zostaff/grok-trading-desk
- https://github.com/zostaff/grokbot-pumpfun
- https://github.com/hoodpumps/Grok-The-Trencher-4.5
- https://github.com/swcstudiospace/PumpGrok
- https://github.com/galleonlabs/hypergrok-trading-desk

**Alternatives (cloned)**
- https://github.com/chainstacklabs/pumpfun-bonkfun-bot
- https://github.com/mortdeus/solana-copy-sniper-mev-trading-bot

**Alternatives (metadata via search API only)**
- https://github.com/michaelhly/solana-py
- https://github.com/kevinheavey/solders
- https://github.com/pump-fun/pump-public-docs
- https://github.com/sendaifun/solana-agent-kit
- https://github.com/hummingbot/hummingbot
- https://github.com/hummingbot/gateway
- https://github.com/freqtrade/freqtrade
- https://github.com/elizaOS/eliza
- https://github.com/outsmartchad/onchain-dex-cli
- https://github.com/joshua-nixon/dexscreener
- https://github.com/dineshpinto/geckoterminal-api
- https://github.com/cardosofede/geckoterminal-py
- https://github.com/0xTaoDev/jupiter-python-sdk
- https://github.com/jup-ag (org listing: jupiter-swap-api-client, jupiter-quote-api-node, metis-binary)
- https://github.com/ccan23/rugcheck
- https://github.com/degenfrends/solana-rugchecker
- https://github.com/vincentkoc/dexscraper
- Also surfaced in searches but not evaluated: katlogic/solana-arbitrage-bot, YZYLAB/solana-trade-bot, henrytirla/Solana-Trading-Bot, MORNLONG/OpenLucky, nolancacheux/AI-Agent-Grok-Trading-System, L1vsun/GrokBot-Trading-Desk__Setup-Prompt

**Datasets (cloned)**
- https://github.com/DeFiLabX/SolRPDS
- https://github.com/git-disl/MELT
- https://github.com/ian05012/solana-memecoin-dataset
- https://github.com/Solid-Sea/pump-gold-dataset
- https://github.com/willho/fingerprinter-dataset
- https://github.com/Smurfetc/solana-memecoin-calls-dataset
- https://github.com/hwlongCUHK/Coin-Meme
- https://github.com/tomdelcarlo-lgtm/pumpfun-graduation-prediction
- https://github.com/inventandchill/data-samples
- https://github.com/launchtape-io/pump-fun-backtest
- https://github.com/launchtape-io/launchtape
- https://github.com/mahad1z/memecoin-survival

**Referenced in files, not fetched**
- https://github.com/marklavigueur-dev/solana-launch-tape (metadata search returned nothing)
- https://doi.org/10.5281/zenodo.20633486
- MELT Google Drive links in `git-disl/MELT/README.md`

**GitHub API (search endpoints only)**
- `https://api.github.com/search/repositories` (all repo metadata and discovery queries listed in sections 1 and 3)
- `https://api.github.com/search/code` (q=`RED-PUMP-2026`)
- `https://api.github.com/search/issues` (PumpGrok open issues)
