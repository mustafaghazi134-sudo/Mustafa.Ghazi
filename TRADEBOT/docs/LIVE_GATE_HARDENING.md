# Live-gate hardening: design, threat model and open decisions

Branch `claude/live-gate-hardening`, based on `e15b997`. For independent review; **not merged, not deployed**.
Nothing here enables real-money submission: `MAINNET_SUBMISSION_ENABLED = False` and `tradebot_signer/` are untouched.

> **Scope of this gate.** Operational readiness (trades, span, coverage) **does not establish profitability**. A strategy
> with negative PAPER returns can pass it. Unattended LIVE authorisation additionally requires a **separately approved
> strategy-evidence gate** (design: `docs/LIVE_GATE_REVIEW_2.md` section 1), which does not exist yet and which no number in
> this document defines.

## 1. What changed

| Requirement | Change |
|---|---|
| No production `--force` | `LiveGate.arm(confirm, force)` removed. `arm-live` no longer accepts `--force` or `--confirm`. There is no parameter, flag or environment variable that skips the readiness check. |
| Interactive activation | `arm-live` refuses unless stdin and stdout are a terminal. It prints readiness, then a one-time challenge `ARM-<config digest>-<random>-<hours>H` that the operator types back. The challenge lives in memory only, works once (a wrong attempt burns it) and lasts 120 s. |
| Expiring record | Schema-2 record with `issued_at`, `expires_at`, `ttl_hours`, `nonce`. Default 12 h (`[engine] live_arm_ttl_hours`), hard maximum 24 h **in code** (config cannot raise it). |
| Broader binding | The record binds `config_digest` over `[risk]`, `[live]`, `[execution]`, the strategy list and parameters, the universe, starting capital and the readiness settings. Paths, log level, data-provider URLs and the Claude analyst are excluded (they do not change what an order can be). |
| Fail-closed validation | `validate_record` rejects anything not exactly a valid schema-2 record: wrong type, missing or non-finite fields, `forced`/`force` flags, future `issued_at`, expiry, lifetime over 24 h, ttl/timestamp mismatch, digest mismatch, hash mismatch, short nonce, bad or missing signature when an attestor is configured. It never raises. Old-format files are "not armed". |
| Timestamp-gap coverage | `readiness.operating_report`. Coverage is the time between consecutive equity snapshots no further apart than the tolerance (3 intervals), minus kill-switch time, over the span from the first snapshot to now. A trailing stale period counts as an outage. Snapshot count is not used. The span starts at the first **snapshot**, not the first event, so a backdated `start` event no longer starts the clock. |
| Order-time re-validation | `assert_armed()` (called by the adapter on every order, after the submission switch) re-validates the record and, when the record rests on this machine's own paper ledger, re-runs the readiness check. |
| Audit | Hash-chained `data/LIVE_AUDIT.jsonl` with external anchors, see section 5. Arming is refused if the log cannot be written. |
| Imported evidence | Read-only, distrusted by default, see section 4. |
| Operational vs strategy | Section 3. |

Numeric thresholds are unchanged: **5 trades / 60 days / 0.8 coverage** (pinned by `test_existing_numeric_readiness_thresholds_are_unchanged`).

## 2. Threat model, and what an activation record can and cannot protect

| Tier | Attacker | What stops them today |
|---|---|---|
| 1 | Accident or operator error (wrong config, forgotten arming, stale approval) | Expiry, broad binding, challenge showing the digest and duration, order-time re-check. **Effective.** |
| 2 | A script or AI agent run by the same OS user (e.g. a coding assistant with a shell) | Interactive-terminal requirement and random challenge stop a plain script. A process that can allocate a pseudo-terminal, or can simply write the file, is **not** stopped by anything in the trading process. |
| 3 | Malicious code running as the trading user | **Nothing inside the trading process.** It can read any key the trading user can read, compute `record_hash`, and write a valid-looking record. |

Consequences, stated plainly:

* `record_hash` detects corruption and careless edits. **It is not authentication.** The report and `status` therefore print `authenticated: False` until an independent attestor signs the record.
* A signing key stored under the trading user's account (a key file, or an environment variable) would give Tier 1 hygiene but **no independent security boundary**, because the attacker in Tier 2/3 is that user. No key is generated or stored by this change.
* The real boundary remains the **separate signer account**, which holds the wallet key, requires a TTY-typed activation of at most 24 h, enforces per-transaction and daily caps, and is behind the build switch. This change leaves it untouched.

### What `authenticated` means (corrected after review of commit `12c5dea`)

`status()["authenticated"]` is **True only if a configured, independent verifier validated the record's signature.**

* It is derived in one place, `attestation.check_record`, and only after `attestor.verify(...) is True` with the record's
  scheme equal to the verifier's. Nothing a record contains (a scheme name, an `authenticated` field, a matching
  `record_hash`) can set it.
* With **no** verifier configured (always, in production today), a record must carry exactly the scheme `"none"` and no
  signature. Any other scheme is rejected as unsupported, so a record cannot claim an authentication that nobody checked.
  The earlier code accepted such a record and reported `authenticated=True`; that defect is fixed and pinned by
  `tests/test_live_gate_authentication.py`.
* With a verifier configured, an unsigned record, a record under a different scheme, a bad or empty signature, a verifier
  that returns anything other than the literal `True`, or one that raises, are all rejected and unauthenticated.
* A verifier that declares scheme `""` or `"none"` can neither sign nor authenticate.
* A matching `record_hash` proves only that the file is internally consistent. It is unkeyed, so any process able to write
  the file can produce it.

### How to protect activation independently of the trading engine (options, none implemented)

1. **OS ownership (recommended first step; no cryptography needed).** `LIVE_ARMED` is written by a different account (for example the existing signer account or a third `tradebot-arm` account) and is read-only for the trading user. The trading user can then not create, edit or extend it, and the engine only reads and validates. On Windows this is a file ACL on `data\LIVE_ARMED` plus the audit file. Pair it with the `Attestor` interface below.
2. **Signature by the signer service.** A new signer command (`tradebot-signer attest`, TTY-typed, under the signer account) signs the record body with a key the trading user cannot read. The engine holds only the verification half. This is the `Attestor` interface in `execution/attestation.py` (`sign` / `verify`). It needs a signer change and key generation, which this phase does not do.
3. **Hardware or OS-protected key** (Windows Hello / TPM-backed key, YubiKey): the human gesture is the boundary. Strongest against Tier 3, highest setup cost.
4. **Remote counter-signature** (a service the owner controls approves a digest): independent of the machine, adds an external dependency.

`[engine] require_external_attestation = true` makes the gate refuse every unsigned record. With no attestor installed that means nothing can be armed, which is the safe default for anyone who wants the strict mode early.

## 3. Operational readiness vs strategy evidence

**Operational readiness (enforced, existing thresholds).** The paper engine ran, gap-free, for long enough and closed enough trades to have exercised the order path. This says nothing about profitability.

**Strategy evidence (reported, not enforced).** `readiness.strategy_report` and `tradebot readiness` print: closed trades, net P&L, mean per trade, a deterministic 95% bootstrap interval of the mean, win rate, top-trade share of gains, fees, maximum drawdown, and "out-of-sample: not evaluated". It carries `enforced: false`. No new numeric criterion is enforced anywhere.

Five trades do not demonstrate profitability, and neither does a long period alone. **Six months must not qualify a strategy by itself.**

### Proposed criteria for unattended real-money trading (PROPOSAL, for owner approval; not enforced)

All of these should hold; failing any one means "not demonstrated".

| # | Criterion | Reason |
|---|---|---|
| 1 | Parameters frozen and their digest recorded **before** the evidence window opens; the evidence is entirely after that point | Out-of-sample validity. Evidence gathered while tuning is in-sample |
| 2 | Net expectancy after realised costs: the lower bound of the 95% bootstrap interval of the mean per trade is above zero | A positive mean with an interval spanning zero is indistinguishable from noise |
| 3 | A minimum sample large enough for criterion 2 to be meaningful (suggest at least 30 closed trades **and** at least 90 days after the freeze), plus the trades spread over more than one market regime | Time alone or count alone is not evidence. Low-frequency strategies may need years, and that is the correct answer |
| 4 | Maximum drawdown inside the risk engine's kill threshold with margin | Shows risk limits would not have fired in ordinary operation |
| 5 | No single trade supplies most of the gains (suggest at most 50% of gross gains) | Stops a lucky outlier carrying the result |
| 6 | Realised paper costs not worse than the live cost measurements from the execution test | Paper fills are optimistic |
| 7 | Multiple-testing accounting: if several variants were tried, the count is disclosed and the bar raised | Otherwise the best of many noise runs passes |
| 8 | Operational readiness met and the micro execution test reconciled | Reliability is a precondition, not evidence of edge |

The suggested numbers in 3 and 5 are starting points for the owner's decision. Until they are approved, `strategy_report` stays informational.

## 4. Bringing GitHub PAPER evidence into the local assessment

The paper ledger that matters may live on a GitHub runner; `arm-live` reads the local ledger. A copied file is only a claim, so:

```
runner:  tradebot export-evidence --out bundle.json --provenance source=github_actions repository=... workflow=... run_id=... commit_sha=...
         -> prints content_sha256; the run log / a committed manifest preserves it
machine: tradebot verify-evidence bundle.json --expected-sha256 <hash from the run log>      (read-only)
         tradebot arm-live --evidence bundle.json --evidence-sha256 <hash>
```

A bundle is accepted only if **all** hold (`evidence_import.verify_bundle`):

1. **Structure.** Schema, `mode == paper`, finite and ordered timestamps, bounded sizes, nothing dated after `as_of`, `as_of` not in the future, trades enter before they exit.
2. **Integrity.** Its content hash recomputes **and** equals a hash supplied out-of-band. A hash inside the file proves nothing, because an attacker who edits the data recomputes it.
3. **Correspondence.** Its `evidence_digest` (strategies, universe, risk limits) equals that of the configuration being armed. Evidence from different strategies or limits is rejected.
4. **Provenance.** The repository is on `[engine] evidence_allowed_repositories` (empty list means none), required run metadata is present, and an independent `ProvenanceVerifier` confirms that this exact hash was produced by that run (for example GitHub artifact attestations / Sigstore). **No verifier ships**, so imported evidence is reported as *unauthenticated* and `accepted` is false.

Even when accepted, it is used only if `[engine] accept_imported_paper_evidence = true` (default false). Imported and local evidence are never merged. Operational readiness is evaluated against **now**, so stale evidence shows as an outage. Nothing is written to the local ledger. The running workflows are not modified: producing a bundle on the runner needs a workflow step that has **not** been added.

## 5. Audit trail

The local `LIVE_AUDIT.jsonl` is a hash chain: it detects accidental edits, a deleted or altered line, and truncation of the tail **only if you hold an anchor**. It is **not tamper-proof**: someone who can write the file can rebuild the entire chain. This is tested (`test_whole_file_rewrite_is_only_detected_with_an_external_anchor`).

Independent protection, in order of effort:

1. **External anchors (implemented).** `arm-live` and `disarm-live` print `audit anchor: seq N head H`. Keeping that line where the trading user cannot write (an email to yourself, a note, a committed line in a repository the engine cannot push to) lets `audit-verify --anchor-seq N --anchor-hash H` prove the log still contains exactly that history. A later extension keeps the previous anchor valid.
2. **Sink interface (implemented, no sink shipped).** `AuditSink.emit(record)` receives every entry; a failing sink makes arming fail.
3. **Recommended sinks:** an append-only file owned by the signer/arm account; or the existing GitHub evidence relay committing the head hash from a runner (off-machine); or both.

Entries hold digests, counts and outcomes only. Challenge codes, keys and config values are never logged.

## 6. Files

New: `tradebot/execution/attestation.py`, `readiness.py`, `audit_log.py`, `evidence_import.py`, `tests/gate_helpers.py`, `tests/test_live_gate_hardening.py`, this document.
Changed: `execution/live_gate.py` (rewritten), `cli.py` (arm/disarm/status; new `readiness`, `export-evidence`, `verify-evidence`, `audit-verify`), `config.py` (new `[engine]` keys, `audit_log_file`), `portfolio/ledger.py` (two read-only accessors, no schema change), and four existing test files whose helper arming changed.
Untouched: `submission.py`, `jupiter_adapter.py`, `live_controls.py`, `tradebot_signer/`, `exec_test.py`, workflows, `deploy/`, `.claude/settings.json`.

## 7. Compatibility and risks

* **Existing `LIVE_ARMED` files stop working** (schema 1). Intended: re-arm interactively.
* **Editing `[execution]`, strategies, the universe or any readiness key now disarms.** Two existing tests edited `[execution]` after arming and now re-arm.
* **`arm-live` cannot be scripted.** Any wrapper `.bat` using `--confirm` or `--force` breaks (none exists in the repo).
* **Evidence locality.** Until an authenticated import path is approved and a producer step exists on the runner, the local machine can arm only from its own ledger. With `--force` gone, there is no shortcut.
* **Kill-switch time is not operating time.** A trip with no later `kill_reset` event (for example the file deleted by hand, not via `tradebot resume`) counts as halted until now, which lowers coverage. This errs on the side of refusing. Use `tradebot resume`.
* **Timestamps as evidence.** Coverage rests on the equity table. Someone able to write the ledger can still insert snapshots; only independent provenance (section 4) and the audit anchor reduce that.
* `paper_gap_tolerance_intervals = 3` is a new **measurement parameter** (not a readiness threshold) and needs approval.
* Test cost: the hardening tests build ~17k-row ledgers; the new file takes about a minute.
* The PAPER engine, the Opportunity Lab and all workflows are unaffected (no changes outside the gate, CLI, config and ledger read accessors; paper runs never call `assert_armed`).

## 8. Decisions needed from the owner

1. Approve the **gap tolerance** (3 snapshot intervals) as the definition of an outage, or choose another.
2. Approve **12 h default / 24 h maximum** (default already set; the 24 h cap is a code constant).
3. Choose the **independent protection** for activation records (section 2: OS ownership, signer attestation, hardware token, or a combination), and authorise the signer change that option 2 needs.
4. Approve, amend or reject the **proposed strategy-evidence criteria** (section 3) before any is enforced.
5. Choose the **provenance verifier** for imported evidence (GitHub artifact attestations is the natural fit) and authorise the one workflow step that exports a bundle on the runner.
6. Choose where **audit anchors** are kept and whether a separate sink is built.
7. Decide whether the **maximum-single-outage** and **code-identity** bindings (not implemented) should be added.
8. Decide what to do about `a3880c4` (the 5 / 60 / 0.8 values were changed without your explicit approval). This branch **keeps** them unchanged.
9. Approve **merging** after ChatGPT's independent review.
