# Micro-live execution test: proposal for owner approval

Date 2026-11-03. Status: **prepared, not run. No real order has been placed.**

## What this test is, and what it is not

| | Execution test (this document) | Strategy evidence |
|---|---|---|
| Question | Can the system buy, sell and reconcile **real funds** correctly, end to end? | Does a strategy make money after costs? |
| Shown by | One round trip whose chain balances match the booked fills | ≥ 30 forward trades, plus the review in `STRATEGY_AUDIT_GO_NOGO.md` §9 |
| What success enables | Nothing automatically. **The trading engine is never armed.** The approval is single use, so a second order needs a new owner approval. Unattended trading still needs the engine's own live gate (≥ 30 paper trades over 14 days) and a separate decision | — |

## The proposal (exact limits for approval)

| Item | Proposed value |
|---|---|
| Account | A **new dedicated Solana wallet**, created on your PC, funded from funds you already hold. Your main wallet's key is never used or imported |
| Funding moved into it | **8 USDC + 0.04 SOL**. 0.02 SOL stays as the reserve; the rest covers fees |
| Venue | Jupiter Swap API v2 on Solana mainnet. Router Metis or JupiterZ; the transaction is verified and simulated before signing |
| Instrument | **SOL**, native: no wrapper or bridge risk. The pair is USDC → SOL → USDC |
| Order amount | **$6.00 buy**, then sell exactly the SOL received (about 0.065 SOL at $92) |
| Minimum order | Engine and controls: $3 minimum for this test ($5 normally); signer per-transaction cap 10 USDC and 0.08 SOL (defaults, unchanged) |
| Estimated round-trip cost | **Quoted live 2026-11-03 09:04 UTC:** 2–4 bps of swap cost on $10, about $0.002 on $6. **Network:** 2 × (5,000 lamports + priority fee), usually about $0.002–0.02. **Worst case** at the caps (50 bps slippage per leg and 500,000 lamports priority per transaction): about $0.15 |
| Pre-trade cost gate | The test refuses **before signing** if the estimated round trip exceeds **60 bps** (about $0.036) or the maximum loss |
| Exit method | Sell immediately in the same command, seconds after the buy confirms: the exact received quantity at market through Jupiter, up to 3 attempts. **If the position is still held:** `tradebot exec-test --exit-only` within the approval window, or sell it yourself in a wallet app |
| **Maximum intended loss** | **$0.50.** Above that, LiveControls' $5 daily cap applies; the most the wallet can lose in total is its contents, about $9.70 |
| Time limits | Approval valid 1 hour, single use. Signer activation 1 hour |
| What gets reported | Signatures with explorer links, fills read from the chain, the wallet before and after, chain-vs-ledger reconciliation and net result. Written to `data/exec_test_report_*.json` |

## Remaining risks (not removed by the controls)

- **Market move during the seconds between legs.** SOL moves about 0.1% a minute, about $0.006 on $6.
- **Sell leg fails or its outcome is unknown.** For example: Jupiter or the RPC is down, or the price moves past the signer's 0.08 SOL cap. The SOL then stays in the wallet; it is recoverable with `--exit-only` or by hand. Unknown outcomes are settled from chain evidence and are never re-sent blindly.
- **Same-Windows-account signer (recommended simplification for this test only).** Anything that can run commands as you could activate the signer. The exposure is bounded by the wallet's contents, about $9.70. The separate Windows account in `LIVE_EXECUTION_ADAPTER.md` §6 remains required before any larger or unattended use.
- **Software risk on the first real route.** The verifier's program allow-list has never seen a real Metis or JupiterZ transaction. If it rejects the real route, the test stops before signing. The rehearsal below checks this first.
- **Jurisdiction.** A spot swap on a decentralised exchange, from a self-custody wallet. This document does not assess its legal treatment in Jordan.
- **Key handling.** The new wallet's key file sits on your PC. Losing it loses at most the test funds. It must never be pasted into a chat, terminal transcript or repository.

## Steps on your PC (Windows, in `...\Mustafa.Ghazi\TRADEBOT`)

1. Update and test:
   ```bash
   git pull
   ```
   ```bash
   pip install -e .[dev]
   ```
   ```bash
   python -m pytest -q
   ```
2. **Create the dedicated wallet.** The command prints its **public** address only:
   ```bash
   python -c "from solders.keypair import Keypair; import json,os; os.makedirs(os.path.expanduser('~/tradebot-signer'),exist_ok=True); k=Keypair(); open(os.path.expanduser('~/tradebot-signer/test-wallet.json'),'w').write(json.dumps(list(bytes(k)))); print(k.pubkey())"
   ```
3. Send **8 USDC and 0.04 SOL** to that address from your existing wallet.
4. **Read-only rehearsal on GitHub's servers.** Put the **public** address in `deploy/exec_test_wallet.txt` and push it, or give it to Claude. The rehearsal then builds, verifies and simulates the exact buy and sell for your wallet and reports its balances. Nothing is signed.
5. Initialise and start the signer (it keeps the default caps):
   ```bash
   python -m tradebot_signer init --home %USERPROFILE%\tradebot-signer --keypair %USERPROFILE%\tradebot-signer\test-wallet.json
   ```
   ```bash
   python -m tradebot_signer serve --home %USERPROFILE%\tradebot-signer --port 8787
   ```
   In `config.toml`:
   - set `[live] wallet_pubkey` to the public address;
   - set `signer_secret_file` to `%USERPROFILE%\tradebot-signer\client_secret`.
6. Read-only checks:
   ```bash
   tradebot live-preflight
   ```
   ```bash
   tradebot live-dry-run --symbol SOL --side buy --amount 6
   ```
7. **Only after you approve the limits above:**
   1. Edit `tradebot/execution/submission.py` **locally**: `MAINNET_SUBMISSION_ENABLED = True`. Do not commit this.
   2. Run, in the same `cmd` window:
      ```bash
      set TRADEBOT_LIVE_TRADING=I_ACCEPT_THE_RISK
      ```
      ```bash
      python -m tradebot_signer activate --home %USERPROFILE%\tradebot-signer --hours 1
      ```
      ```bash
      tradebot approve-exec-test --symbol SOL --usd 6 --max-loss 0.50 --max-cost-bps 60 --hours 1 --confirm "I APPROVE ONE SOL ROUND TRIP OF 6.00 USD WITH MAX LOSS 0.50 USD"
      ```
      ```bash
      tradebot exec-test
      ```
8. **Immediately afterwards:**
   ```bash
   python -m tradebot_signer deactivate --home %USERPROFILE%\tradebot-signer
   ```
   ```bash
   git checkout -- tradebot/execution/submission.py
   ```
   Close the `cmd` window to drop the environment variable. Share the report file: it contains public data only.
   Claude then checks the signatures and balances independently on the chain.

**Emergency stop at any time:** `tradebot live-halt --reason test`, then
`python -m tradebot_signer halt --home %USERPROFILE%\tradebot-signer`.
