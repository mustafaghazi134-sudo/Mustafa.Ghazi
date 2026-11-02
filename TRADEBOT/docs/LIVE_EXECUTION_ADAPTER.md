# Jupiter live-execution adapter: design, operations, readiness

Date 2026-11-02. **Status: built, tested, NOT activated.** Real transaction submission is disabled in this build by
a code constant (`tradebot/execution/submission.py: MAINNET_SUBMISSION_ENABLED = False`). No private key was used,
requested or created for a real wallet, and nothing was sent on chain. PAPER remains the default mode, and the risk
limits are unchanged; the new controls only add restrictions.

## 1. Jupiter API facts this adapter relies on

Source: the official documentation repository `jup-ag/docs`, commit `acbe702` of 2026-10-28. The relevant files are
`swap/order-and-execute.mdx`, `swap/advanced/transaction-versions.mdx` and `openapi-spec/swap/v2/swap.yaml` (v2.0.0).

| Fact | Consequence in the adapter |
|---|---|
| `/swap/v2/order` + `/swap/v2/execute` (Meta-Aggregator) is the recommended path; `taker` is required to get a transaction | Live and dry-run requests set `taker` to the wallet's public key. `quote()` omits it and is read-only. |
| `maxSupportedTransactionVersion` defaults to `"0"` and is a ceiling. v1 comes only from Metis and only on request. v1 needs `@solana/kit` v8, and the compute budget moves to the message config. | **v0 only.** The adapter sends `maxSupportedTransactionVersion=0` and checks the response's `transactionVersion`. The Python signer (solders 0.29) refuses to parse v1 (verified), so it fails closed. |
| Routers: metis, jupiterz, dflow, okx. `excludeRouters` can remove them. | DFlow and OKX use their own programs, which this verifier cannot vouch for, so they are excluded. Metis uses the Jupiter v6 program `JUP6Lkb…TaV4`. JupiterZ uses the Order Engine `61DFfeT…TpYH`. Both IDs were taken from the docs. |
| JupiterZ (RFQ) requires a second signature from the market maker, added at `/execute`. Its validity is set by `expireAt`. | The signer signs **partially**, filling only the wallet's slot. The RFQ maker must be the only co-signer, and `expireAt` must leave at least 5 s. |
| Aggregator transactions expire at `lastValidBlockHeight` | At least 30 blocks must remain when the transaction is verified. The block height is sent with `/execute`. |
| `priceImpact` is in percentage points; `priceImpactPct` is deprecated | `priceImpact` is used, with `priceImpactPct` only as a fallback. |
| `/execute` codes: 0 success; −1, −2, −3; −1000 to −1005 (−1005 means the blockhash expired before landing); −2000 to −2004 | **Any** non-success after signing is treated as uncertain. Chain evidence decides, and no code triggers a new signature. |
| `transaction` is `""` with `errorCode` when the router can price a swap but cannot build it | The order is refused before signing. |

## 2. Architecture

```
 strategy -> RiskEngine (single-use HMAC approval, limits, kill) -> TradingBot._execute_with_intent
               intent "created" (SQLite) ----------------------------------------+
                                                                                 v
 JupiterExecutionAdapter (Broker interface, same as PaperBroker)                 |
   1 build switch -> 2 LiveGate (mode=live + env + armed file, hash of [risk]+[live])
   3 signer health (wallet, activation, halt) -> 4 LiveControls (halt, serial, caps, exposure, daily loss, SOL reserve)
   5 GET /order (taker, v0 only, dflow/okx excluded) -> 6 tx_verify (order fields + every instruction + signers)
   7 simulateTransaction on our RPC: wallet balance changes within the order
   8 SignerClient --HMAC, loopback--> tradebot_signer (separate OS user, holds the key)
        halt? build switch? activation? idempotency? tx_verify again? blockhash valid? own simulation? daily caps?
        -> partial signature at the wallet's slot, logged per client order id
   9 signed message == verified message, signature verifies
  10 presubmit hook: signature + signed bytes persisted (intent "signed") ---------+
  11 POST /execute (identical bytes re-sent on timeout/429/5xx until expiry)
  12 getSignatureStatuses -> getTransaction: fill = wallet balance changes on chain
       anything else after step 10 -> SubmittedUnknownError -> intent "submitted" -> reconciliation
```

The trading process holds only the wallet's **public** key and a client secret that lets it *ask* the signer. The
private key exists only in the signer's process. It is never logged, returned, persisted or sent over the network.
`tests/test_signer_service.py` checks the logs, errors, the object's text form, the signer's health output and the
signing log for key material.

## 3. What is verified before signing (both processes)

| Area | Check (fails closed) | File |
|---|---|---|
| Order | taker is the wallet; input and output mints equal the request and are allow-listed; `inAmount` equals the request; ExactIn only; router allowed; v0; `otherAmountThreshold` ≤ `outAmount` and ≥ **our** floor (reference price × (1 − max total cost)); slippage, fee (`feeBps`) and impact caps; `feeMint` is one of the two legs; priority-fee and rent caps when the wallet pays; quote age ≤ 8 s; ≥ 30 blocks left (RFQ: ≥ 5 s before `expireAt`, maker present); `requestId` present; empty transaction refused | `tx_verify.verify_order` |
| Transaction | base64, ≤ 1232 bytes, v0 (legacy and v1 refused); not durable-nonce; every lookup table resolves; at most 2 signers, wallet among them; the only allowed co-signer is the RFQ maker or a gasless sponsor acting as fee payer; programs allow-listed (System, Token, Token-2022, ATA, ComputeBudget, Jupiter v6, Order Engine); System: Transfer only, and wallet-to-third-party lamports ≤ 0.0005 SOL (tips); Token: SyncNative on the wallet's wSOL account, CloseAccount refunds to the wallet, nothing else (no Approve, Transfer or SetAuthority); ATA create: allow-listed mint, canonical address, wallet-paid accounts owned by the wallet; compute budget: priority fee ≤ cap; at least one Jupiter swap instruction | `tx_verify.inspect_transaction` |
| Effect | simulation (no signature needed): the wallet spends ≤ the order's input, receives ≥ our minimum output, SOL for fees, tips and rent ≤ 0.005 SOL, and no other allow-listed balance decreases | `simulation.check_deltas` |
| Signer only | owner activation (HMAC, ≤ 24 h, bound to wallet and policy hash); per-transaction input cap per mint; daily input cap per mint; ≤ 10 signatures/day; blockhash still valid; one signature per client order id | `tradebot_signer/core.py` |

## 4. Outcomes and how each is handled

| Outcome | Detection | Action | Can it duplicate? |
|---|---|---|---|
| Any gate fails | before `/order` with taker | intent `failed`, nothing signed | no |
| Verification or simulation fails | before signing | intent `failed`, nothing signed | no |
| Signer refuses | HTTP 403 | intent `failed` | no |
| Signer does not answer | timeout | intent `failed`; the trading process has nothing to submit | no: only the trading process submits |
| Signed message differs from the verified one, or the signature is invalid | byte comparison and verification | intent `failed`, **not submitted** | no |
| Crash after signing, before persisting | intent left `created` | on restart the signer's log is asked: nothing → `abandoned`; signature found → `signed`, then chain evidence; signer unreachable → stays pending | no |
| `/execute` timeout, 429 or 5xx | transport | identical bytes re-sent until 30 s or block-height expiry, then `SubmittedUnknownError` → `submitted` | no: same signature, so it lands once at most |
| `/execute` `Failed` (any code, including −1005) | response | `submitted`; chain decides | no new signature is ever made |
| `/execute` `Success` | response | wait for chain confirmation; fill = **on-chain** balance changes from `getTransaction` | — |
| Success but failed on chain | signature status has `err` | reconciliation → `failed`, nothing booked (fees lost) | no |
| Landed with less output (still ≥ minimum) | chain amounts | booked as the chain recorded it | no |
| Chain output below the order minimum | comparison | booked from chain **and** `LIVE_HALT` written | no |
| Any unexpected exception after the signature was persisted | adapter and bot | `submitted` (never `failed`); chain decides | no |
| RPC unavailable during reconciliation | exception | stays pending, new live orders blocked | no |
| Not found on chain | status lookup | stays pending while the blockhash is valid at **finalized** commitment; `expired` only once it is not (checked before the signature lookup) | no |
| Confirmed but transaction body unreadable | `getTransaction` empty | stays pending, CRITICAL log | no |

"Partial" execution does not occur within a single Solana swap: a transaction applies entirely or not at all. What can
differ is the amount: output below the quote but above the minimum, fees, and rent. These are booked from chain balances.

## 5. Live controls (defaults in `[live]`; they only add restrictions)

Per order ≤ $10. Wallet exposure (non-USDC holdings including native SOL, plus this buy) ≤ $25. Daily loss ≤ $5,
measured as realised loss or a fall in wallet value since the UTC day's first snapshot; it halts buys and writes
`LIVE_HALT`. Daily limits: ≤ 10 orders and ≤ $50 notional. SOL reserve ≥ 0.02. Fee ≤ 25 bps, impact ≤ 30 bps,
slippage ≤ 50 bps, total cost vs reference ≤ 100 bps. Priority fee ≤ 0.0005 SOL. Execution is strictly serial: any
unresolved intent blocks every new live order. `LIVE_HALT` blocks everything. `KILL` blocks buys and still allows
reduce-only sells. The signer has its own caps in `policy.json`: 10 USDC, 0.08 SOL, 0.00012 cbBTC or 0.004 WETH per
transaction, five times that per day, and 10 signatures per day. The trading process cannot change these.

## 6. Deploy (Windows, supervised micro-live, future; do not do this until section 9 is complete)

The owner does every step. Claude sessions in this repository are denied access to `tradebot_signer`, the signer's
secrets, the activation file and the build switch (`.claude/settings.json`).

1. Create a separate local Windows account, for example `tradebot-signer`, without administrator rights. The signer runs
   only as this account. Do not disable Device Guard, Defender or any other protection; nothing here needs it.
2. Signed in as that account, create its home, for example `C:\Users\tradebot-signer\signer`. Then restrict it so that
   only that account and SYSTEM can access it:
   ```bash
   icacls C:\Users\tradebot-signer\signer /inheritance:r /grant:r tradebot-signer:(OI)(CI)F SYSTEM:(OI)(CI)F
   ```
3. Create a **new, dedicated** wallet key file in that directory. Do not reuse a wallet that holds anything else, and
   never paste a key into a chat, a terminal transcript or the repository:
   ```bash
   python -c "from solders.keypair import Keypair; import json; k=Keypair(); open('bot-wallet.json','w').write(json.dumps(list(bytes(k)))); print(k.pubkey())"
   ```
4. Initialise the signer. This reads the key file only to learn the public key and writes `policy.json`,
   `activation_secret` and `client_secret`:
   ```bash
   python -m tradebot_signer init --home C:\Users\tradebot-signer\signer --keypair C:\Users\tradebot-signer\signer\bot-wallet.json --rpc-url <your RPC>
   ```
5. Review `policy.json`: per-transaction and daily caps at current prices, and the RPC URL (use a reputable paid RPC).
   Any edit changes the policy hash and deactivates the signer.
6. Copy `client_secret`, and only that file, to a location readable by the trading account. Set
   `[live] signer_secret_file` to that path. Set `[live] wallet_pubkey` to the printed public key.
7. Fund the wallet with no more than the micro-live budget: about 0.05 SOL for fees and $20–25 of USDC. The wallet
   must hold no other tokens, because simulation watches only allow-listed balances.
8. Install the package for the signer account (`pip install -e <path to TRADEBOT>`), then start the signer as that
   account:
   ```bash
   python -m tradebot_signer serve --home C:\Users\tradebot-signer\signer --port 8787
   ```
9. As the trading account, run the read-only checks:
   ```bash
   tradebot live-preflight
   ```
   ```bash
   tradebot live-dry-run --symbol SOL --side buy --amount 5
   ```

## 7. Monitor

- `tradebot live-status` shows the build switch, gate, halt, kill and every unresolved intent with its signature and age.
- `tradebot live-preflight` shows quotes, router, cost against reference, RPC, balances, signer health and signatures used today.
- `tradebot status` and `tradebot report --mode live` show the book and net results.
- `data/tradebot.log` and the ledger `events` table record `live_halt`, `execution_unknown`, `reconciled`,
  `balance_mismatch` and `intent_manual_resolution`. Watch for any line containing `LIVE HALT`, `UNKNOWN for`,
  `below the order minimum`, `REFUSED` (signer log) or `mismatch`.
- On the signer side, `python -m tradebot_signer status --home …` shows activation and halt. Its `signing_log.sqlite`
  lists every signature and every refusal with its reason.

## 8. Stop and recover

**Stop, fastest first.** Each step is independent and safe to repeat.

1. `tradebot live-halt --reason "<why>"`: refuses every live order.
2. `python -m tradebot_signer halt --home …`: refuses every signature.
3. `python -m tradebot_signer deactivate --home …`: removes the activation.
4. `tradebot disarm-live` and remove `TRADEBOT_LIVE_TRADING`.
5. Stop the bot process, then the signer process.

None of these cancels a transaction that has already been signed: a transaction already sent may still land until its
blockhash expires, which takes about a minute.

**Recover.**

1. `tradebot live-status`: list unresolved intents.
2. Restart the bot. It reconciles every pending intent on each cycle: confirmed is booked from the chain, failed is
   closed, not found with an expired blockhash is expired, and anything else stays pending.
3. If an intent stays pending past `intent_stale_warn_sec`, look up its signature in a block explorer. After verifying
   it yourself, resolve it explicitly with a note:
   ```bash
   tradebot resolve-intent <id> expired|failed|confirmed --note "<what you checked>"
   ```
4. A balance mismatch trips `KILL`. Compare the wallet with `tradebot status` and correct the cause before
   `tradebot resume`.
5. `tradebot live-resume` is refused while any intent is unresolved.

## 9. Readiness checklist for a future supervised micro-live deployment

| # | Item | State |
|---|---|---|
| 1 | Common Broker/ExecutionAdapter interface shared by paper and live | **Done** (`execution/base.py`) |
| 2 | Order and transaction verification before signing (section 3) | **Done**, 44 adversarial tests |
| 3 | Separate signer service, owner activation (≤ 24 h, interactive), its own caps and simulation, idempotent per order | **Done**, 15 tests |
| 4 | Intents persisted before submission; identical-bytes resubmission; chain-derived fills; uncertain outcomes never marked failed | **Done**, 27 integration tests plus crash tests |
| 5 | Live halt, serial execution, exposure, daily loss and daily caps, SOL reserve | **Done** |
| 6 | Read-only preflight and dry-run commands | **Done**; network checks unverified (no route from the build environment) |
| 7 | Static guarantees: the trading process loads no key and has no LLM in the execution path | **Done** (test) |
| 8 | Positive forward-paper evidence for the strategy to be traded | **Not met**. `docs/STRATEGY_AUDIT_GO_NOGO.md` is NO-GO for live trading. |
| 9 | `live-preflight` passes on the owner's PC: Jupiter key, quotes for SOL/BTC/ETH, RPC, signer health | **Not run** |
| 10 | `live-dry-run` passes on the PC for buy and sell of each symbol, with the simulation verified against real routes; the program allow-list confirmed against real Metis and JupiterZ transactions | **Not run** |
| 11 | Signer deployed as a separate Windows account with an NTFS-restricted home; key file created by the owner | **Not done** (owner only) |
| 12 | Signer HTTP and permissions behaviour verified on Windows; the container tests ran on Linux | **Not done** |
| 13 | A dedicated wallet funded with the micro-live budget only, holding no other tokens | **Not done** (owner only) |
| 14 | Written owner authorisation to set `MAINNET_SUBMISSION_ENABLED = True`, as a reviewed commit; the pinned test updated in the same commit; the deny rule on `submission.py` removed deliberately | **Not given** |
| 15 | `tradebot arm-live` with the confirmation phrase, `TRADEBOT_LIVE_TRADING` set, `mode = "live"` | **Not done** (owner only) |
| 16 | `python -m tradebot_signer activate --hours N` typed by the owner for a short, supervised window | **Not done** (owner only) |
| 17 | First live order supervised at minimum size, then reconciled and inspected in an explorer before any second order | **Not done** |

## 10. Known limitations and residual risks

- **Same-user compromise.** If the signer runs as the same OS user as the bot, anything able to run commands as that
  user can activate it. Separation relies on the separate Windows account in section 6.
- **Swap internals.** The Jupiter swap instruction is not decoded. The signer's simulation, which measures wallet
  balance changes, is what bounds a malicious or wrong route, and it trusts the RPC provider's simulation.
- **Watched balances.** Simulation watches the wallet's SOL and the token accounts of allow-listed mints. The wallet
  must hold nothing else.
- **Not supported:** v1 transactions, DFlow and OKX routes, and gasless orders whose sponsor is not the fee payer.
  Token-2022 mints are handled by the ATA check but untested against real routes; none are in the default universe.
- **Price assumptions.** USDC is assumed to be worth $1 in the minimum-output floor. The reference price comes from
  Jupiter Price v3 with a 30 s staleness limit.
- **Not implemented:** hardware wallet or Windows DPAPI key storage. The key file is protected by NTFS permissions only.
- **Untested in reality:** live network latency, real 429 rates and real `/execute` behaviour. The build environment
  has no route to Jupiter or Solana.

## 11. Test evidence (this build)

| Suite | Result |
|---|---|
| Full test suite | **267 passed** (was 181 before this work) |
| `tests/test_tx_verify.py` (order, instruction, signer, version, lookup-table and simulation attacks) | 44 passed |
| `tests/test_signer_service.py` (activation, caps, idempotency, partial signing, HMAC API on loopback, key hygiene) | 15 passed |
| `tests/test_live_adapter.py` (gates, outcomes, crash recovery, controls, static guarantees; through the real bot path) | 27 passed |
| Mutation checks: no persist before submit; abandon without asking the signer; ignore SOL drains; accept a different signed message | all caught (4/4) |
| `tradebot selftest` | OPERATIONAL 10/10 |
| `tradebot live-preflight` / `live-dry-run` in the build environment | run read-only; network checks FAIL as expected (403 from proxy); nothing signed or submitted |
