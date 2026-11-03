# Micro-live execution test ($6 SOL round trip): preparation status

> **PAUSED on 2026-11-03.** The owner's available balance is 0.00663 SOL (about $0.61), which is below what this test
> needs. No funds are to be moved or spent.
>
> **At that balance, checked with the real code:**
>
> - the RiskEngine rejects every order, because the 10% position size is $0.06 against the $5 minimum;
> - LiveControls rejects every live order, because the SOL reserve is 0.02 SOL and the wallet holds 0.00663.
>
> **Smallest test the existing code allows without loosening any control:** a $1 round trip. The wallet would need:
>
> - 1.10 USDC;
> - 0.025 SOL: the 0.020 reserve, which is not spent, plus refundable rent and fees;
> - about 0.00204 SOL of rent for the USDC account.
>
> In total about $3.60 at $92/SOL. The expected amount actually spent is about $0.002–0.005.

Revised 2026-11-03 at about 09:40 UTC. Status: **read-only preparation.**

- Nothing has been funded, activated, signed or submitted.
- The design keeps the **separate Windows signer account** (`LIVE_EXECUTION_ADAPTER.md` §6).

## What this test is, and what it is not

| | Execution test (this document) | Strategy evidence |
|---|---|---|
| Question | Can the system buy, sell and reconcile **real funds** correctly, end to end? | Does a strategy make money after costs? |
| Shown by | One round trip whose chain balances match the booked fills | ≥ 30 forward trades, plus the review in `STRATEGY_AUDIT_GO_NOGO.md` §9 |
| What success enables | Nothing automatically. **The trading engine is never armed**, and the approval is single use. Unattended trading still needs the engine's own live gate and a separate decision | — |

## 1. Re-quoted costs for $6 (live, keyless, 2026-11-03)

Source: `paper_evidence/exec_test/rehearsal_20261103T092029Z.txt` and `..._092236Z.txt`.

### Swap round trip

| Item | Measured |
|---|---|
| Reference SOL price | $92.02–92.09 |
| $6.00 buys | 0.065098–0.065181 SOL |
| Swap cost, six samples | **1.4–5.3 bps**, that is **$0.0008–0.0032** |
| Jupiter platform fee | 2 bps per leg, included in those figures |
| Sell floor at the 50 bps slippage limit | $5.967–5.969 |

### Network, priority fee and rent, per transaction

| Item | Metis route | JupiterZ (RFQ) route |
|---|---|---|
| Signature fee | 5,000 lamports | 0 when gasless (seen on buys); 10,000 when not |
| Priority fee | 17–2,054 lamports observed. Policy cap 500,000 | 0–23 lamports |
| Rent | Native SOL legs use a temporary wrapped-SOL account, created and closed **in the same transaction**. Its ~2,039,280 lamports are refunded, but the wallet must hold them during the transaction | Up to 1,488,440 lamports reported by Jupiter on JupiterZ routes. Whether it is refunded is **not yet verified** |

### Expected total for the test

**About $0.003–0.006**:

- swap cost of about $0.002;
- network fees of about 10,000–14,000 lamports, about $0.001.

### Worst case that the pre-trade gate still allows

**About $0.16**:

- slippage of 50 bps per leg, $0.06;
- priority fees at the cap, 2 × 500,000 lamports = 0.001 SOL, about $0.09;
- base fees.

The test refuses **before signing** if the estimated round trip exceeds 60 bps (about $0.036) or the approved maximum
loss.

## 2. Funding arithmetic (corrected)

The earlier figures (8 USDC + 0.04 SOL, "wallet value $9.70") were wrong.

- $9.70 was the wallet's *exposure* figure, not its value.
- 8 + 0.04 × $92 = **$11.68**.
- 8 USDC was more than needed.

| Need | Amount | Why |
|---|---|---|
| USDC | **6.50 USDC** | The buy spends exactly 6.00 (ExactIn). The 0.50 buffer only lets a second attempt be re-quoted. |
| SOL | **0.030 SOL** | 0.020 is the minimum reserve LiveControls enforces. About 0.0021 is held temporarily as wrapped-SOL rent and refunded in the same transaction. At most about 0.0011 goes on fees at the caps. That leaves about 0.006 SOL of margin. The 0.01 SOL sell-route probe (see 4) is only a simulation and spends nothing. |
| **Total moved** | **6.50 USDC + 0.030 SOL ≈ $9.26** | At SOL $92.05 |
| Paid by your **existing** wallet when sending | USDC token-account rent ≈ 0.00204 SOL (≈ $0.19) + 2 transfer fees ≈ 0.00001 SOL | Phantom or similar creates the new wallet's USDC account. That rent is locked, not lost; it is recoverable only by closing the account later. |

**Exposure during the test:** 0.030 SOL plus $6 of SOL bought, about $8.80. That is under the $25 LiveControls cap.

## 3. Loss: the intended limit is not the maximum possible loss

| Case | What can be lost | Bounded by |
|---|---|---|
| **A. Normal completion** (expected) | About $0.003–0.006, ± about $0.006 market move during the seconds between legs | Measured costs |
| **B. Completion at worst-case fills** | Up to about $0.16 | Slippage and priority caps. The pre-trade gate refuses above $0.036 estimated |
| **The $0.50 "maximum intended loss"** | A **pre-trade approval threshold** on the estimated cost. **It is not a stop-loss**: nothing sells automatically at −$0.50 | — |
| **C. Buy fills, sell fails or stays unknown**: Jupiter, RPC or signer down; route refused; price moves past the signer's 0.08 SOL per-transaction cap | The 0.065 SOL ($6) stays held and moves with the market: −5% ≈ −$0.30, −10% ≈ −$0.60, a −50% crash ≈ −$3. **Theoretical maximum ≈ $6 + fees** | How quickly you exit: `exec-test --exit-only`, or sell by hand |
| **D. Wallet compromise or loss** (key file stolen or deleted) | Everything in the wallet: **≈ $9.26** (+ $0.19 rent in the USDC account) | Wallet contents. The verifier and signer refuse transfers to third parties above 500,000 lamports, but a stolen key bypasses both |
| E. Duplicate submission | Prevented: intents are persisted before submission, and only identical bytes are re-sent | Tested |

## 4. Route verification so far, and what still needs a funded wallet

Read-only results from GitHub's servers, with real Jupiter routes and the real chain:

1. **The buy route (USDC → SOL) passed** verification and simulation for a funded third-party wallet, used only as the
   taker, on a JupiterZ route.
2. **That same simulation exposed a gap in our own check:** the wallet's watched USDC account was not debited, yet
   the check passed. That third-party wallet keeps its USDC outside its standard token account.
   **Fixed:** the simulation must now see the full input leave the wallet's own token account. A regression test was
   added.
3. **The sell route (SOL → USDC) was refused by our verifier** in three samples, on Metis routes:
   - the SOL went through a pre-existing wrapped-SOL account that is not the wallet's standard one;
   - the verifier correctly refuses that today;
   - a **fresh** wallet has no such account, so its route may differ. This cannot be confirmed without funds.
4. **Jupiter builds no transaction at all for an empty wallet**, checked with a fresh random address. So the actual
   route for *your* wallet can be verified and simulated **only after it is funded**.
5. **New safeguard:** before buying, `exec-test` now verifies and simulates a 0.01 SOL sell from the wallet's own
   reserve. If our verifier would refuse the sell route, the test stops **before buying**, so no position can be
   stranded. Tested.

The steps below follow from these findings.

## 5. Steps and approvals

| Step | Who | Money moves? | Needs your approval |
|---|---|---|---|
| **1. Run `windows\setup_signer_account.bat` as Administrator.** It creates the `tradebot-signer` account, the locked `C:\tradebot-signer` folder and a new wallet inside it, then prints only the PUBLIC address | You | No | No (read-only for money) |
| 2. I check the address on chain: it must be empty and new | Claude | No | No |
| 3. Fund the address: 6.50 USDC + 0.030 SOL | You | **Yes** | **Yes**: funding approval |
| 4. Read-only rehearsal on **your** wallet: verify and simulate the real buy, the real sell and the 0.01 SOL sell probe; record exact fees and rent | Claude (GitHub servers) | No | No |
| 5. If step 4 passes: final limits presented for approval | — | — | — |
| 6. Signer set up as `tradebot-signer`, then activation, local build switch, `approve-exec-test`, `exec-test` | You | **Yes** | **Yes**: activation and submission approval |
| 7. Independent check of the signatures and balances on the chain; results and reconciliation reported | Claude | No | — |

**If step 4 shows the sell route is still refused for a fresh wallet:**

- the test does not run;
- the fix is a reviewed verifier change that accepts a wrapped-SOL account created and closed by the wallet within
  the same transaction;
- that change will be shown to you before any activation is requested.
