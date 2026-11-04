# Live-gate hardening: answers to the second review

Branch `claude/live-gate-hardening` (base `e15b997`). Not merged, not deployed. No thresholds changed, no key generated,
signer untouched, real-money submission still disabled (`MAINNET_SUBMISSION_ENABLED = False`).
Test output for this exact tree: `docs/live_gate_test_output.txt` (542 passed; baseline before this work: 320).

What this round changed in code (small): one optional setting, `[engine] max_single_outage_hours`, default **None = not
enforced** (section 4); it is part of the activation binding. Everything else is tests and this document.

---

## 1. Can a strategy with negative PAPER returns arm the engine?

**Yes.** Exactly what `readiness.evaluate` checks (`tradebot/execution/readiness.py`), and nothing else:

| Check | Input | Looks at profit? |
|---|---|---|
| `closed trades >= 5` | `len(trades)`: a row count | **No.** Five losing trades count the same as five winners |
| `span >= 60 days` | first to last equity snapshot | No |
| `coverage >= 0.8` | gap-free snapshot time, minus kill-switch time | No |
| `longest outage <= cap` | only if `max_single_outage_hours` is set (default unset) | No |

`strategy_report` (net P&L, bootstrap interval, drawdown, concentration) is computed and printed, and stored in the
activation record, but `evaluate` never reads it (`enforced: false`). Pinned by
`test_negative_paper_returns_can_still_pass_every_operational_condition_and_arm`: 61 days, equity falling 20% (twice the 10%
drawdown limit), 8 losing trades (mean about -$26, interval entirely below zero), one kill-switch trip of one day. All
operational conditions pass and `begin_arm`/`complete_arm` succeed.

A losing strategy is penalised only indirectly: a kill-switch trip subtracts its halted time from coverage, which matters
only if more than about 20% of the record (12 days of 60) was halted.

What still stands between an armed engine and real money, none of it profit-related: the build switch (`False`), the
separate signer (owner-typed activation of at most 24 h, per-transaction and daily caps) and the `[live]` caps
($10 per order, $25 exposure, $5 daily loss).

### Proposed separate, mandatory strategy-validation gate (for approval; nothing enforced or numeric here)

Two gates, both required, neither able to substitute for the other:

| Gate | Question | Status |
|---|---|---|
| Operational readiness | Did the system run, without gaps, long enough to exercise every path? | Implemented, existing thresholds |
| **Strategy validation** | Is there independent evidence of positive net expectancy after costs? | **Proposed** |

Design:

1. **Pre-registration manifest** `STRATEGY_VALIDATION.json`, written and approved by the owner *before* the evidence window:
   the strategy-set digest (`evidence_digest`), the freeze timestamp, the evidence-window start, the cost model, and the
   criteria *with values the owner chooses*. The code ships no default values.
2. **Evaluator** `strategy_validation.evaluate(manifest, evidence)` returns one of `PASS`, `FAIL`, `NOT_APPROVED`
   (criterion has no value) or `NOT_EVALUABLE` (evidence missing) per criterion. The gate passes only if **every**
   criterion is approved *and* passes. An unconfigured gate is a failed gate (fail-closed).
3. **Criteria categories** (values left blank on purpose): configuration frozen and unchanged since the freeze; evidence
   entirely after the freeze (out-of-sample); lower bound of the net-expectancy interval above zero at an owner-chosen
   confidence; minimum sample size and minimum time after the freeze; maximum drawdown; maximum single-trade share of
   gains; paper costs not more optimistic than measured live costs; disclosed number of variants tried and the correction
   applied; more than one market regime represented; execution test reconciled.
4. **Enforcement point:** `begin_arm` (a failed validation refuses to arm the unattended engine) and a copy of the verdict
   in the activation record. The micro execution test (`exec-test`) stays outside this gate, as designed: it never arms the
   engine.
5. **Evidence integrity** needs the same provenance work as section 6, because trade lists are as forgeable as snapshots.

Owner approval needed for: the criteria values, the confidence level, the sample bars, and whether "fail-closed when
unconfigured" applies immediately.

---

## 2. Activation security: how records are authenticated

**Today they are not authenticated.** Exact path:

1. `LiveGate.assert_armed()` -> `status()` -> `_read_record()` -> `attestation.validate_record(data, now, expected_digest, require_external, attestor)`.
2. `attestor` is `LiveGate.__init__(..., attestor=None)`. The only production construction is
   `tradebot/engine/bot.py:613`, `LiveGate(settings, ledger)`, so **`attestor` is always `None`**. A test
   (`test_no_attestor_is_wired_into_production_code_paths`) pins this.
3. With no attestor the record is checked for shape, expiry, lifetime, digest and `record_hash`. `record_hash` is
   `sha256` of the record's own canonical JSON: **unkeyed**. Anyone can compute it.
4. **Where verification keys come from: nowhere.** None exists, none was generated, there is no configuration field for one.
   `status()["authenticated"]` is `False` for every record.

**Who can issue a valid activation today:** (a) the intended path: an owner at a terminal running `arm-live`;
(b) **any process that can write `data/LIVE_ARMED`** and compute a SHA-256, which includes anything running under the bot's
Windows account.

**Could a process under the bot's account forge its own authorization? Yes.** Demonstrated by
`test_KNOWN_LIMITATION_a_process_with_write_access_can_forge_an_unsigned_activation`: with the ledger empty (readiness
nowhere near met) a forged 24-hour record makes `is_armed()` true, with no challenge, no readiness check and **no audit
entry**. The same process can also edit `config.toml` (mode) and set the environment variable, so the engine's own three
conditions are all within its reach.

What the forgery does *not* give it:

* **Signer.** Separate account, own HMAC activation typed by the owner at a terminal (at most 24 h), per-transaction and
  daily caps, policy hash. The signer never consults the engine's `LIVE_ARMED`.
* **Build switch.** Read at call time by both the adapter and the signer.

**New finding (deployment, not code): the signer loads its safety code from the trading account's tree.**
`docs/LIVE_EXECUTION_ADAPTER.md` step 8 says to install the signer with `pip install -e <path to TRADEBOT>`. The signer
imports `tradebot.execution.submission`, `tx_verify`, `simulation` and `signer_protocol` from that path
(`tradebot_signer/core.py:32-35`, `policy.py:18`, `server.py:8`). An editable install points at the trading account's own
working tree, so a process under the bot's account could edit `submission.py` or `tx_verify.py` and change what the
signer enforces at its next start. A running signer is unaffected until restart; the key and the owner's activation remain
protected. The signer is not deployed yet (checklist item 11, "owner only"), so this is cheap to fix before it is. I have
not changed the signer or its docs. **This is a blocker for real money** until the signer runs from a copy the trading
account cannot write (a non-editable install in the signer account's own environment, or a read-only code directory).

**Why verification inside the engine cannot, by itself, fix this.** If the bot account both runs the verifier and can edit
the file holding the verification key (or the config that points at it), it can replace the key. A public key in
`config.toml` is not an independent boundary. Options, strongest to weakest:

1. **Enforce at the signer** (the bot account cannot reach it): the owner's TTY activation at the signer also covers the
   readiness evidence digest, and the signer refuses to sign if the engine presents a different configuration or code
   digest. Requires a signer change (not made).
2. **OS ownership:** `LIVE_ARMED`, the audit log and the code tree owned by another account and read-only to the bot
   account (NTFS ACLs, like the signer home). The engine only reads. No cryptography needed; needs a second writer account.
3. **Hardware or OS-protected key** whose use needs a human gesture.
4. **Remote counter-signature** by a service the owner controls.

---

## 3. Code identity (proposal; not implemented)

Goal: an activation dies if executable trading code changes after arming; PAPER runs are never affected.

* `code_digest = sha256` over sorted `(posix relative path, sha256(bytes with CRLF normalised to LF))` for
  `tradebot/**/*.py`, `tradebot_signer/**/*.py` and `pyproject.toml`. Excludes `tests/`, `docs/`, `research_lab/`,
  `opportunity_lab/`, `__pycache__`. Measured on this tree: **77 files, 588 KB, 7.6 ms**.
* Stored in the activation record at arm time, compared in `assert_armed()` and in `build_bot` at startup. Paper runs never
  call `assert_armed()` (it is on the live branch only), so they are unaffected. `status` and `selftest` may print it
  informationally.
* Per-order cost is negligible; a stat-based fast path (path, size, mtime) with a full rehash on change keeps it flat.
* A running Python process keeps the code it already loaded; edits take effect at the next start. The order-time check
  therefore blocks orders from a process whose files changed underneath it (conservative).
* Why file hashes and not a git commit id: a dirty working tree has the same commit id.
* Merging this very branch changes `tradebot/`, so every future activation will need re-arming after a code change. That
  is the intent.
* Limits: bytecode (`.pyc`) tampering is not covered unless bytecode writing is disabled; a same-account attacker who can
  edit code can edit an unsigned record to match (section 2). The signer's own code is covered only if the signer reads
  the digest (also section 2).

Decisions: the exact file set; whether the signer code is included; dependency versions in the digest.

---

## 4. Coverage reliability

**Actual snapshot frequency.** `bot.py:119` writes a snapshot when `now - last >= 300 s`, checked once per scan cycle
(60 s). The latest GitHub checkpoint shows `stop: after 296 cycles` for a 300-minute run, about 60.8 s per cycle, so
snapshots land every 5 cycles, **about 304 s** apart. The tolerance of 3 intervals is 900 s: **one missed snapshot (608 s)
is tolerated; two consecutive missed snapshots (912 s) are an outage.** That is conservative and deliberate; a fluke costs
only its own duration. Pinned by `test_one_missed_snapshot_is_tolerated_two_consecutive_misses_are_an_outage`.

**Restart gaps.** The GitHub chain runs about 5-hour jobs; the `start` events on 2026-11-03 were 13:22:31, 18:23:48 and
23:25:01, about 5 h 01 min apart, so roughly 1-2 minutes between jobs, inside the tolerance.
**I could not compute a real coverage figure:** the evidence relay carries text checkpoints, not equity timestamps, and the
local `data/ledger.sqlite` in this container has zero equity rows. The table below is a *simulation* with the branch's own
`operating_report` (60 days, 5-hour jobs, 304 s spacing; "old" is the replaced snapshot-count formula):

| Scenario | old | tol 2x | tol 3x (current) | tol 6x |
|---|---|---|---|---|
| restart gap 2 min | 0.993 | 1.000 | 1.000 | 1.000 |
| restart gap 10 min | 0.968 | 0.964 (278 outages) | 1.000 | 1.000 |
| restart gap 20 min | 0.938 | 0.934 | 0.934 (270 outages) | 1.000 |
| restart gap 45 min | 0.870 | 0.867 | 0.867 | 0.867 |
| 2 min restarts + 20-min stall every 3 h | 0.927 | 0.917 | 0.917 (286 outages) | 1.000 |
| 2 min restarts + 12-min stall every 3 h | 0.944 | 0.944 | 0.944 (286 outages) | 1.000 |

Raw output of this simulation is appended at the end of `docs/live_gate_test_output.txt`.

**Can the result mislead?**

| Case | Effect | Verdict |
|---|---|---|
| Long outage | **Yes, in the permissive direction.** 60 days at 0.8 allows about 12 days of nothing in one block; a measured example (11-day hole) passes today (`test_a_single_eleven_day_outage_passes_today...`) | See recommendation |
| Repeated or flooded snapshots | No. Coverage is time between distinct timestamps; 50,000 duplicates and 5,000 sub-millisecond rows add nothing (`test_repeated_and_flooded_snapshots_add_nothing`) | Safe |
| Kill-switch periods | Snapshots **continue while halted** (the snapshot runs before the kill-switch return in `bot.py`), so counting snapshots alone would credit halted time. The verifier subtracts `kill_switch` to `kill_reset`. A trip never reset counts as halted until now. A KILL file deleted by hand (not `tradebot resume`) leaves no reset event and keeps counting as halted: wrong in the **safe** direction | Safe, but use `tradebot resume` |
| Daily-loss halts, loss-streak pauses | Not subtracted: the engine was running. Arguably correct; stated so it is not a surprise | Acceptable |
| Restart gaps above 15 min | Each costs its duration; many small ones lower coverage but are never hidden | Conservative |
| A writer of the ledger | Can insert regularly spaced fake snapshots (section 6) | **Not detectable locally** |
| Tolerance set by whoever arms | `evaluate` uses the *current* `equity_snapshot_interval_sec` and `paper_gap_tolerance_intervals`. Setting either very large before arming makes a gappy record look continuous. They are in the activation binding (so visible in the record and the digest) but **nothing in code floors them** | **New weakness**, same class as the editable `min_paper_*` thresholds (W6) |

**Recommendation for maximum individual outages.** Add a per-outage cap in addition to the coverage fraction, because the
fraction alone cannot see how the missing time is arranged. Mechanism implemented, **value not set**:
`[engine] max_single_outage_hours` (default `None` = off; any number is bound into the activation and, when set, enforced
by `evaluate`; boundary tested). A value needs Mustafa's approval; I suggest deciding it after seeing real outage
durations from a bundle export, and expressing it relative to the longest restart gap seen. I also recommend **code
floors** (constants, not config) for the readiness minimums and the tolerance, so editing `config.toml` cannot lower them.
That again introduces numbers, so it needs approval.

---

## 5. Regression coverage: invalidation without re-arming

Previously the review was right that some existing tests re-armed after editing `[execution]`
(`test_execute_retryable_code...`, two `test_safety_audit` cases). Those are about retry behaviour, not gating, and still
re-arm because they need to run an order after the edit. The invalidation itself is now tested separately, **never
re-arming between the edit and the assertion**:

| Where | What | Count |
|---|---|---|
| `test_live_gate_review.py::test_editing_a_setting_after_arming_blocks_the_next_order_check` | Arm once through the real `begin_arm`/`complete_arm` flow on a 61-day ledger; copy it; apply **one** edit; assert `assert_armed()` raises "configuration changed" and a fresh `LiveGate` also refuses. Covers 6 risk fields, 5 live fields, 4 execution fields, 7 strategy edits (parameter changed/added, enabled, disabled, removed, reordered, added), 2 universe fields, starting capital, and all 9 readiness settings | 34 |
| `...::test_editing_the_config_file_after_arming_disarms_on_next_load` | Same, through the real route: edit `config.toml`, `load_settings`, build a new gate | 12 |
| `...::test_reverting_the_edit_restores_validity_because_binding_is_exact` | Binding is exact, not a time-of-edit flag | 4 |
| `...::test_every_bound_section_is_covered_by_an_edit_above` | Fails if a new section or readiness key enters the binding without a test | 1 |
| `...::test_unrelated_config_changes_do_not_disarm` | Log level and scan interval do not | 1 |
| `test_live_adapter.py::test_every_gate_blocks_before_anything_is_signed` | Adapter level: the edit is made, the next order is refused, and **nothing is signed and no `/execute` is sent**. 9 new cases (risk, live, execution, strategies, universe, two readiness keys, outage cap, expiry) | 16 total |
| `test_live_gate_hardening.py::test_editing_any_bound_setting_disarms` | Earlier version, kept | 19 |

One trap found while writing these: a test that "changes" a value to what it already is proves nothing. Two of my own
first edits did that (`universe.mints` JUP, and `universe.symbols` where the fixture already used `["SOL"]`); both were
caught because the test failed to disarm, and were fixed.

---

## 6. Evidence trust

**Confirmed: imported GitHub evidence stays disabled until a trusted provenance verifier exists.**

* `accept_imported_paper_evidence` defaults to `false`, and is itself part of the activation binding.
* `verify_bundle` reports `accepted` only if a `ProvenanceVerifier` confirms the hash. No class implements the interface
  anywhere in `tradebot/` (checked by AST in `test_no_provenance_verifier_exists_in_production_code`), and the CLI passes
  none.
* `test_cli_cannot_arm_from_imported_evidence_even_with_everything_enabled`: a structurally perfect, correctly pinned
  bundle from an allow-listed repository, with import switched on, is refused before any challenge is shown.

**What a locally editable PAPER ledger can and cannot prove.** Anyone who can write `data/ledger.sqlite` can fabricate 61
days of snapshots and five trades that satisfy every operational condition; nothing local distinguishes them
(`test_KNOWN_LIMITATION_a_hand_built_ledger_passes_the_local_readiness_check`). The gap-based method defeats *accidental*
distortion and naive manipulation (duplicates, backdated events, floods), not deliberate fabrication. Real protection needs
either independent provenance (the verifier, with a trusted producer step on the runner) or a ledger the bot account cannot
write.

**What the hash-chained audit log can and cannot prove.** It detects edits, deletions and tail truncation *when you hold
an anchor*. A whole-file rewrite by someone who can write the file verifies cleanly without one
(`test_KNOWN_LIMITATION_audit_log_rewritten_wholesale_verifies_without_an_anchor`). A forged activation record also writes
**no** audit entry, so the log proves what the CLI did, not that nothing else happened.

---

## 7. Security acceptance summary

**Implemented**

* No override flag; interactive TTY-only arming with a one-time, in-memory challenge.
* Expiring activation (12 h default, 24 h hard cap in code), bound to risk, live, execution, strategies, universe, capital
  and readiness settings; fail-closed validation.
* Gap-based, kill-switch-aware coverage; backdated events cannot start the clock; order-time re-check.
* Optional per-outage cap (unset); report-only strategy statistics.
* Imported evidence: read-only, off by default, never accepted without a verifier.
* Hash-chained audit with anchors and a sink interface.
* Submission switch, signer, adapter order and live caps untouched.

**Unresolved**

* Activation is not independently authenticated (forgeable by the bot's account).
* No strategy-validation gate; negative-return paper strategies can arm.
* No code-identity binding.
* No code floors under editable thresholds and the gap tolerance.
* Outage cap value, gap tolerance value, and strategy criteria values need approval.
* No provenance verifier, no producer step on the runner, no external audit sink or anchor policy.
* `a3880c4` (thresholds changed without explicit approval) still undecided; this branch keeps its values.

**What would block a real-money activation (any one is sufficient)**

1. `MAINNET_SUBMISSION_ENABLED = False`: needs a reviewed code change and your written authorisation.
2. Signer not deployed, not funded, not activated; the execution test not run (your steps).
3. Signer would load its safety code from a tree the trading account can write (section 2).
4. Activation not independently authenticated (section 2).
5. No approved and passing strategy validation; the current GitHub paper chain has recorded **0 closed trades** so far
   (latest checkpoint), so there is no performance evidence at all.
6. Operational evidence rests on a locally forgeable ledger until provenance exists.
7. Your explicit approval of the exact limits, and ChatGPT's independent review of the final diff.
