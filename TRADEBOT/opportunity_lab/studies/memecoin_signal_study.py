"""Real-data test of the lab's filters, signals and exit rules on a public historical dataset.

Data: github.com/ian05012/solana-memecoin-dataset (MIT): 44,460 detection snapshots of freshly graduated pump.fun
tokens and GMGN-trending tokens, 2026-06-10 .. 06-29, each with features captured AT detection time, plus 15-minute
USD candles for up to 3 days after detection (about 47% of snapshots have candles).

KNOWN BIASES (stated by the dataset and by us): tokens were selected because they graduated or trended
(survivorship / selection), so results are an UPPER bound for a scanner that sees every launch. 15-minute bars hide
intrabar paths: when a bar touches both stop and target, the stop is assumed first. There is no route or sell-failure
data, so a write-off sensitivity is reported instead.

PRE-REGISTERED (written before any result was looked at; not changed afterwards):
  entry   : open of the first 15-min candle starting at or after the detection time (no look-ahead); one entry per token
            (its first qualifying snapshot); candles must start within 20 minutes of detection
  exits   : lab defaults: stop -12% (gap: fill at the bar open if it opens below the stop), target +30%, time 240 min
            (exit at the close of the last bar inside the window); stop checked before target in the same bar
  costs   : lab cost model: DEX fee 30 bps/leg, constant-product impact from liquidity at detection, $0.01 network per
            tx, $0.40 token-account rent, x1 / x2 / x3 stress
  security: liquidity >= L, age >= 30 min, top-10 holders <= 35%, LP burned >= 90% (mint/freeze revoked for all rows)
  variants: L in {$20k, $50k}  x  size in {$25, $100}  x  signal in
            {all (naive basket), momentum_flow: price_change_1h > 0 and 1h buy share >= 0.6,
             strong_flow: price_change_1h >= 20% and 1h buy share >= 0.65}
            -> 12 variants; Bonferroni alpha = 0.05 / 12
  windows : development 06-10..06-19, validation 06-20..06-24, holdout 06-25..06-29 (by detection date)
  promising only if: validation AND holdout expectancy CI lower bound > 0 at x2 costs, n >= 30 in each
Wallet-tag features (smart money, KOL, insider) are NOT used: tags may have been assigned after detection.

usage (from TRADEBOT/): python -m opportunity_lab.studies.memecoin_signal_study --data <dir with tokens.parquet, ohlcv.parquet>
                         --out opportunity_lab/results/real_data_signal_study.json
"""
from __future__ import annotations

import argparse
import json
import random
from bisect import bisect_left

import pandas as pd

from ..config import ExecutionModelConfig
from ..costs import buy_fill, sell_fill

STOP, TARGET, HOLD_SEC, MAX_START_GAP = 0.12, 0.30, 240 * 60, 20 * 60
WINDOWS = {"development": ("2026-06-10", "2026-06-20"), "validation": ("2026-06-20", "2026-06-25"), "holdout": ("2026-06-25", "2026-06-30")}
N_VARIANTS = 12


def simulate(bars: list[tuple], entry_i: int, size: float, liq: float, ex: ExecutionModelConfig) -> tuple[float, str]:
    """-> (net return fraction, exit reason). bars: (ts, open, high, low, close)."""
    ts0, o0 = bars[entry_i][0], bars[entry_i][1]
    if not o0 or o0 <= 0:
        return None, "bad_price"
    qty, _, _ = buy_fill(size, o0, liq, "pumpswap", ex)
    stop, target = o0 * (1 - STOP), o0 * (1 + TARGET)
    exit_px, why = None, "time"
    last = entry_i
    for k in range(entry_i, len(bars)):
        ts, o, h, l, c = bars[k]
        if ts - ts0 >= HOLD_SEC:
            break
        last = k
        if k > entry_i and o <= stop:
            exit_px, why = o, "stop_gap"; break
        if l <= stop:
            exit_px, why = stop, "stop"; break
        if h >= target:
            exit_px, why = target, "target"; break
    if exit_px is None:
        exit_px = bars[last][4]
    proceeds, _ = sell_fill(qty, exit_px, liq, "pumpswap", ex)
    return (proceeds - size) / size, why


def stats(xs: list[float], alpha: float, seed: int = 0) -> dict:
    n = len(xs)
    if n == 0:
        return {"n": 0}
    bps = [x * 1e4 for x in xs]
    out = {"n": n, "expectancy_bps": round(sum(bps) / n, 1), "win_rate": round(sum(1 for x in xs if x > 0) / n, 3)}
    wins, losses = sum(x for x in xs if x > 0), -sum(x for x in xs if x <= 0)
    out["profit_factor"] = round(wins / losses, 2) if losses > 0 else None
    if n >= 10:
        rng = random.Random(seed)
        means = sorted(sum(rng.choice(bps) for _ in bps) / n for _ in range(3000))
        out["ci_bps"] = [round(means[int(3000 * alpha / 2)], 1), round(means[int(3000 * (1 - alpha / 2)) - 1], 1)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="opportunity_lab/results/real_data_signal_study.json")
    a = ap.parse_args()
    t = pd.read_parquet(f"{a.data}/tokens.parquet")
    o = pd.read_parquet(f"{a.data}/ohlcv.parquet").sort_values(["token_address", "ts"])
    bars = {k: list(zip(g.ts.astype(int), g.open.astype(float), g.high.astype(float), g.low.astype(float), g.close.astype(float)))
            for k, g in o.groupby("token_address")}
    t["cap_ts"] = t.captured_at.astype("int64") // 10**9
    t["day"] = t.captured_at.dt.strftime("%Y-%m-%d")
    t["buy_share_1h"] = t.buy_usd_1h / (t.buy_usd_1h + t.sell_usd_1h)
    t = t.sort_values("cap_ts")
    alpha = 0.05 / N_VARIANTS
    signals = {
        "all": lambda r: True,
        "momentum_flow": lambda r: (r.price_change_1h or 0) > 0 and (r.buy_share_1h or 0) >= 0.6,
        "strong_flow": lambda r: (r.price_change_1h or 0) >= 20 and (r.buy_share_1h or 0) >= 0.65,
    }
    # price_change_1h is in PERCENT in this dataset (min -100); checked on the field's distribution before any result
    res: dict = {"dataset": "ian05012/solana-memecoin-dataset", "evidence_class": "HISTORICAL BACKTEST, survivorship-biased (upper bound)",
                 "pre_registered": __doc__.split("PRE-REGISTERED")[1].split("Wallet-tag")[0].strip(), "alpha_bonferroni": alpha,
                 "price_change_1h_quantiles": t.price_change_1h.quantile([0.1, 0.5, 0.9]).round(4).tolist(), "variants": {}}
    coverage = {"snapshots": int(len(t)), "with_candles_after_detection": 0}
    for L in (20_000, 50_000):
        for size in (25.0, 100.0):
            for sname, sig in signals.items():
                key = f"liq>={L // 1000}k|size${size:g}|{sname}"
                per = {w: {1: [], 2: [], 3: []} for w in WINDOWS}
                reasons = {}
                seen = set()
                for r in t.itertuples():
                    if r.token_address in seen:
                        continue
                    if not (r.liquidity_usd and r.liquidity_usd >= L and (r.time_since_launch_sec or 0) >= 1800
                            and (r.top10_holder_pct if r.top10_holder_pct == r.top10_holder_pct else 1) <= 0.35
                            and (r.lp_burned_pct or 0) >= 0.9 and r.mint_disabled and r.freeze_disabled):
                        continue
                    if not sig(r):
                        continue
                    b = bars.get(r.token_address)
                    if not b:
                        continue
                    i = bisect_left([x[0] for x in b], r.cap_ts)
                    if i >= len(b) or b[i][0] - r.cap_ts > MAX_START_GAP:
                        continue
                    seen.add(r.token_address)
                    w = next((k for k, (s, e) in WINDOWS.items() if s <= r.day < e), None)
                    if w is None:
                        continue
                    for m in (1, 2, 3):
                        ex = ExecutionModelConfig(cost_multiplier=float(m))
                        ret, why = simulate(b, i, size, float(r.liquidity_usd), ex)
                        if ret is not None:
                            per[w][m].append(ret)
                            if m == 1:
                                reasons[why] = reasons.get(why, 0) + 1
                v = {w: {f"cost_x{m}": stats(per[w][m], alpha) for m in (1, 2, 3)} for w in WINDOWS}
                allx1 = per["development"][1] + per["validation"][1] + per["holdout"][1]
                if allx1:
                    mean = sum(allx1) / len(allx1)
                    v["breakeven_writeoff_rate_x1"] = round(mean / (1 + mean), 4) if mean > 0 else 0.0
                v["exit_reasons_x1"] = reasons
                ok = all(v[w]["cost_x2"].get("n", 0) >= 30 and (v[w]["cost_x2"].get("ci_bps") or [-1])[0] > 0
                         for w in ("validation", "holdout"))
                v["verdict"] = "PROMISING" if ok else "REJECTED"
                res["variants"][key] = v
                coverage["with_candles_after_detection"] = max(coverage["with_candles_after_detection"], len(seen))
    res["coverage"] = coverage
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1, default=str)
    for k, v in res["variants"].items():
        print(f"{k:<34} " + " | ".join(f"{w[:3]} n={v[w]['cost_x1'].get('n')} x1={v[w]['cost_x1'].get('expectancy_bps')} "
                                         f"x2={v[w]['cost_x2'].get('expectancy_bps')} CI2={v[w]['cost_x2'].get('ci_bps')}" for w in WINDOWS)
              + f" -> {v['verdict']}")


if __name__ == "__main__":
    main()
