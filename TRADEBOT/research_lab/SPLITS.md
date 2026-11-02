# Data splits and what each may be used for

Three bodies of evidence exist for the deployed strategy. They are kept apart on purpose; a number from one must never
be presented as if it came from another.

| Body | Period / source | Allowed use | Where it lives |
|---|---|---|---|
| **Development** | Binance 1h klines resampled to 4h, **before 2025-01-01**, six public mirrors cross-checked (`DATA_SOURCES.md`) | Strategy design, parameter grids, rolling walk-forward (12-month train / 3-month test). All comparisons between strategies are made here. | `results/results_4h.json`, `results/comparison_4h_holdout2025.json` (`walk_forward_oos`), `RESULTS.md`, `COMPARISON.md` |
| **Holdout** | Same data, **2025-01-01 onward**, parameters frozen from the last development window | One frozen-parameter evaluation per configuration that was fixed *before* the period was opened. Never used for selection or tuning. Every evaluation is appended to `results/holdout_looks.jsonl`. | `results/comparison_4h_holdout2025.json` (`holdout`), `results/holdout_diagnosis_BTCUSDT.json`, `HOLDOUT_DIAGNOSIS.md` |
| **Forward (paper)** | Live Jupiter quotes and Binance candles from the moment `tradebot run` starts on the user's machine | The only independent evidence for anything decided after the holdout was inspected. Parameters are frozen for the whole run (`claude.research_enabled=false`, `claude.auto_apply_overrides=false`). | `data/tradebot.db` (mode `paper`), `tradebot report`, `data/verify_assets.jsonl` |

## Rules enforced in code

- `tradebot apply-profile` sets `[claude] research_enabled=false`, `vet_signals=false`, `daily_report=false`,
  `auto_apply_overrides=false`. The bot then never calls the Anthropic API and never changes strategy parameters on its own.
- If research is switched on without Claude, `research_once` logs **proposals** only (`decisions` kind `research`,
  field `proposals`); nothing is applied unless `auto_apply_overrides=true`, which should stay off during a forward test.
- `research_lab/run_all.py --holdout-start D` develops before `D` and evaluates frozen parameters after `D` once,
  appending a line to `results/holdout_looks.jsonl`. `--dev-only` develops before `D` and refuses to touch the holdout;
  use it for any hypothesis formed after reading holdout results (example: `tsmom_ema_band`).
- Replay sessions (`data.provider = synthetic|csv`) write to ledger mode `replay`, which never counts toward the live
  gate's paper-record requirement. Only mode `paper` does.

## Current look count on the 2025+ holdout

`results/holdout_looks.jsonl` is the authority. As of this commit it records the comparison run (one look per strategy
and symbol, frozen walk-forward parameters) and the diagnosis run (deployed 30/100 and walk-forward-final 30/50, BTC).
Nothing else has been evaluated on it. The 2025+ period is therefore still usable as a *final* check for the current
deployed configuration, but **not** for any variant designed after 2026-11-01; those get the forward record.
