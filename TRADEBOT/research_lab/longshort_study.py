"""Pre-registered long/short study (docs/LONGSHORT_PREREGISTRATION.md). Six fixed-parameter candidates, run once.

Model (identical for every candidate):
  - signal at the close of bar t -> position held from the open of bar t+1 (returns are open-to-open);
    sensitivity: one extra bar of delay;
  - each asset holds +1, 0 or -1 times one third of capital (constant notional per bar; no leverage beyond 1x gross);
  - costs per side on notional traded; perpetual funding charged at each funding time inside the bar
    (base: longs pay the rate, shorts receive it; stress: both sides pay |rate|);
  - long-only comparison: same signal clipped at 0, spot Jupiter costs, no funding.
Statistics on daily portfolio returns: stationary block bootstrap, Bonferroni over 12 configurations,
direction-randomised null on holding episodes, lookahead truncation test.

usage (from TRADEBOT/):
  PYTHONPATH=$PWD python -m research_lab.longshort_study --root-1m <data_1m> --raw-dirs <data_raw>/* \
      --funding-dir research_lab/data_ext --cache <scratch>/ls_cache.pkl --out research_lab/results/longshort_study.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import pickle
import time

import numpy as np
import pandas as pd

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
E_START, E_END = pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2026-11-01", tz="UTC")
P_END = E_START
COSTS = {"perp": {"base": 8.0, "stress": 16.0}, "spot": {"base": 26.0, "stress": 51.0}}
N_CONFIGS = 12
ALPHA = 0.05
FALLBACK_FUNDING_8H = 0.0001
WARMUP_DAYS = 150
SEED = 20261103


# =========================================================================== data
def load_hourly(root_1m: str, raw_dirs: list[str], cache: str | None) -> dict[str, pd.DataFrame]:
    if cache and os.path.exists(cache):
        with open(cache, "rb") as f:
            return pickle.load(f)
    from .data import discover, load_symbol
    from .data import resample as resample_h
    from .intraday import load_1m
    files = discover(raw_dirs)
    out = {}
    for sym in SYMS:
        parts = []
        f1m = sorted(glob.glob(f"{root_1m}/parshant231234-eng_{sym}-Historical-Data/1m/*/*/*.csv"))
        if f1m:
            m = load_1m(f1m)
            parts.append(resample_h(m, "1h"))
        if sym in files:
            raw, _ = load_symbol(sym, files[sym], prefer=["nawr1_RL_trading", "changhaowuwu_LSTM-live-cripto-forecast",
                                                           "7huahua_RedditDataset", "el-frontend_frontbot"])
            parts.append(raw[["open", "high", "low", "close", "volume"]])
        df = pd.concat(parts).sort_index()
        df = df[~df.index.duplicated(keep="first")]           # 1m-verified data first
        df = df[df.index < E_END]
        out[sym] = df
        print(f"{sym}: {len(df)} hourly bars {df.index[0]} .. {df.index[-1]}", flush=True)
    if cache:
        with open(cache, "wb") as f:
            pickle.dump(out, f)
    return out


def blocks(df: pd.DataFrame, max_gap_h: int = 72) -> list[pd.DataFrame]:
    d = df.index.to_series().diff()
    cuts = [0] + [df.index.get_loc(t) for t, g in d.items() if g > pd.Timedelta(hours=max_gap_h)] + [len(df)]
    return [df.iloc[a:b] for a, b in zip(cuts, cuts[1:]) if b - a > 24 * 200]


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if rule == "1h":
        return df
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def load_funding(fdir: str, sym: str) -> tuple[pd.Series, dict]:
    """Funding rate per event (8h on Binance), indexed by event time. Missing periods -> fallback assumption."""
    path = os.path.join(fdir, f"funding_binance_{sym}.csv")
    info = {"source": "fallback 0.01%/8h"}
    actual = pd.Series(dtype=float)
    if os.path.exists(path):
        f = pd.read_csv(path)
        if len(f):
            actual = pd.Series(f["rate"].values, index=pd.to_datetime(f["time_ms"], unit="ms", utc=True)).sort_index()
            actual.index = actual.index.floor("min")
            actual = actual[~actual.index.duplicated()]
            info = {"source": "binance data.binance.vision", "first": str(actual.index[0]), "last": str(actual.index[-1]),
                    "events": int(len(actual)), "mean_per_8h_bps": round(float(actual.mean()) * 1e4, 3)}
    grid = pd.date_range(pd.Timestamp("2019-01-01", tz="UTC"), E_END, freq="8h", inclusive="left")
    s = pd.Series(FALLBACK_FUNDING_8H, index=grid)
    if len(actual):
        s = s[(s.index < actual.index[0] - pd.Timedelta(hours=4)) | (s.index > actual.index[-1] + pd.Timedelta(hours=12))]
        s = pd.concat([s, actual]).sort_index()
    return s, info


def funding_per_bar(bar_index: pd.DatetimeIndex, funding: pd.Series) -> np.ndarray:
    """Sum of funding events with time in [open_t, open_{t+1})."""
    pos = np.searchsorted(bar_index.values, funding.index.values, side="right") - 1
    out = np.zeros(len(bar_index))
    ok = pos >= 0
    np.add.at(out, pos[ok], funding.values[ok])
    return out


# =========================================================================== candidates: df -> desired position after the close of each bar
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def c1_tsmom_ls_1d(df):
    c = df["close"]
    mom, e = c / c.shift(30) - 1, ema(c, 100)
    pos = np.where((mom > 0) & (c > e), 1, np.where((mom < 0) & (c < e), -1, 0))
    pos[: 100] = 0
    return pd.Series(pos, index=df.index)


def c2_ema_ls_4h(df):
    c = df["close"]
    f, s, t = ema(c, 12), ema(c, 26), ema(c, 100)
    pos = np.where((f > s) & (c > t), 1, np.where((f < s) & (c < t), -1, 0))
    pos[: 100] = 0
    return pd.Series(pos, index=df.index)


def c3_donchian_ls_1d(df):
    c = df["close"].values
    hi20 = df["high"].rolling(20).max().shift(1).values
    lo20 = df["low"].rolling(20).min().shift(1).values
    hi10 = df["high"].rolling(10).max().shift(1).values
    lo10 = df["low"].rolling(10).min().shift(1).values
    pos = np.zeros(len(c), dtype=int)
    p = 0
    for i in range(len(c)):
        if np.isnan(hi20[i]):
            pos[i] = 0; continue
        if p == 1 and c[i] < lo10[i]:
            p = 0
        elif p == -1 and c[i] > hi10[i]:
            p = 0
        if p == 0:
            if c[i] > hi20[i]:
                p = 1
            elif c[i] < lo20[i]:
                p = -1
        pos[i] = p
    return pd.Series(pos, index=df.index)


def c4_ewmac_ls_1d(df):
    c = df["close"]
    vol = c.pct_change().rolling(25).std()
    fc = [((ema(c, a) - ema(c, b)) / (c * vol)).clip(-2, 2) for a, b in ((8, 32), (16, 64), (32, 128))]
    avg = (sum(fc) / 3).values
    pos = np.zeros(len(c), dtype=int)
    p = 0
    for i in range(len(c)):
        a = avg[i]
        if i < 128 or np.isnan(a):
            pos[i] = 0; continue
        if p != 0 and p * a < 0.25:
            p = 0
        if p == 0 and abs(a) >= 0.5:
            p = 1 if a > 0 else -1
        pos[i] = p
    return pd.Series(pos, index=df.index)


def c5_ema_ls_1h(df):
    c = df["close"]
    f, s, t = ema(c, 24), ema(c, 96), ema(c, 480)
    pos = np.where((f > s) & (c > t), 1, np.where((f < s) & (c < t), -1, 0))
    pos[: 480] = 0
    return pd.Series(pos, index=df.index)


def c6_meanrev_ls_1h(df):
    r = df["close"].pct_change()
    z = ((r - r.rolling(60).mean()) / r.rolling(60).std()).values
    pos = np.zeros(len(z), dtype=int)
    p, left = 0, 0
    for i in range(len(z)):
        if p != 0:
            left -= 1
            if left <= 0:
                p = 0
        if p == 0 and not np.isnan(z[i]):
            if z[i] < -2.5:
                p, left = 1, 6
            elif z[i] > 2.5:
                p, left = -1, 6
        pos[i] = p
    return pd.Series(pos, index=df.index)


CANDIDATES = {
    "C1_tsmom_ls_1d": (c1_tsmom_ls_1d, "1D"),
    "C2_ema_ls_4h": (c2_ema_ls_4h, "4h"),
    "C3_donchian_ls_1d": (c3_donchian_ls_1d, "1D"),
    "C4_ewmac_ls_1d": (c4_ewmac_ls_1d, "1D"),
    "C5_ema_ls_1h": (c5_ema_ls_1h, "1h"),
    "C6_meanrev_ls_1h": (c6_meanrev_ls_1h, "1h"),
}


# =========================================================================== simulation
def simulate(df: pd.DataFrame, sig: pd.Series, fund_bar: np.ndarray, cost_bps: float, delay: int,
             funding_mode: str | None) -> pd.DataFrame:
    """Per-bar returns (fraction of the asset's capital). funding_mode: None (spot), 'base', 'stress'."""
    o = df["open"].values
    r = np.empty(len(o)); r[:-1] = o[1:] / o[:-1] - 1; r[-1] = 0.0          # open t -> open t+1
    held = sig.shift(1 + delay).fillna(0).values.astype(float)
    prev = np.concatenate([[0.0], held[:-1]])
    c = cost_bps / 1e4
    cost_long = c * np.abs(np.maximum(held, 0) - np.maximum(prev, 0))
    cost_short = c * np.abs(np.minimum(held, 0) - np.minimum(prev, 0))
    if funding_mode == "base":
        fund = -held * fund_bar
    elif funding_mode == "stress":
        fund = -np.abs(held) * np.abs(fund_bar)
    else:
        fund = np.zeros(len(o))
    gross = held * r
    out = pd.DataFrame({"held": held, "gross": gross, "cost": -(cost_long + cost_short), "funding": fund,
                        "long": np.where(held > 0, gross + fund, 0) - cost_long,
                        "short": np.where(held < 0, gross + fund, 0) - cost_short}, index=df.index)
    out["net"] = out["gross"] + out["cost"] + out["funding"]
    out["r_asset"] = r
    out["fund_bar"] = fund_bar
    return out


def episodes(held: np.ndarray) -> list[tuple[int, int, int]]:
    """(start, end_exclusive, sign) of contiguous same-sign non-zero holding runs."""
    eps, i, n = [], 0, len(held)
    while i < n:
        if held[i] == 0:
            i += 1; continue
        j = i
        while j < n and held[j] == held[i]:
            j += 1
        eps.append((i, j, int(np.sign(held[i]))))
        i = j
    return eps


# =========================================================================== statistics
def block_bootstrap_p(x: np.ndarray, rng, n_boot: int = 5000, mean_block: int = 10) -> float:
    """One-sided p-value for mean(x) > 0, stationary bootstrap of the centred series."""
    n = len(x)
    if n < 30:
        return float("nan")
    xc = x - x.mean()
    p_new = 1.0 / mean_block
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_boot)
    jumps = rng.random((n_boot, n)) < p_new
    fresh = rng.integers(0, n, (n_boot, n))
    for t in range(1, n):
        idx[:, t] = np.where(jumps[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    means = xc[idx].mean(axis=1)
    return float((means >= x.mean()).mean())


def summarise(daily: pd.Series) -> dict:
    if len(daily) < 2:
        return {"days": int(len(daily))}
    eq = (1 + daily).cumprod()
    years = len(daily) / 365
    ann = float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else -1.0
    vol = float(daily.std() * math.sqrt(365))
    return {"days": int(len(daily)), "total_return_pct": round(float(eq.iloc[-1] - 1) * 100, 1),
            "ann_return_pct": round(ann * 100, 1), "ann_vol_pct": round(vol * 100, 1),
            "sharpe": round(float(daily.mean() * 365 / (daily.std() * math.sqrt(365))), 2) if daily.std() > 0 else None,
            "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1)}


# =========================================================================== lookahead truncation test
def lookahead_test(fn, df: pd.DataFrame, rng, n: int = 50) -> dict:
    full = fn(df)
    lo = min(len(df) - 2, 600)
    cuts = rng.integers(lo, len(df) - 1, n)
    bad = [int(t) for t in cuts if int(fn(df.iloc[: t + 1]).iloc[-1]) != int(full.iloc[t])]
    return {"cuts": n, "mismatches": len(bad), "pass": not bad}


# =========================================================================== study
def run_candidate(name, fn, rule, data, funding, rng) -> dict:
    per = {}         # sym -> dict of scenario -> per-bar frame (all blocks)
    look = {}
    for sym, h in data.items():
        frames: dict[str, list] = {}
        for bi, blk in enumerate(blocks(h)):
            df = resample(blk, rule)
            sig = fn(df)
            if bi == 0 or sym not in look:
                look[sym] = lookahead_test(fn, df[df.index >= E_START - pd.Timedelta(days=400)] if df.index[-1] > E_START else df, rng)
            fb = funding_per_bar(df.index, funding[sym])
            warm = df.index[0] + pd.Timedelta(days=WARMUP_DAYS)
            scen = {
                "ls_base": simulate(df, sig, fb, COSTS["perp"]["base"], 0, "base"),
                "ls_stress": simulate(df, sig, fb, COSTS["perp"]["stress"], 0, "stress"),
                "ls_delay1": simulate(df, sig, fb, COSTS["perp"]["base"], 1, "base"),
                "ls_zero_cost": simulate(df, sig, fb, 0.0, 0, None),
                "lo_base": simulate(df, sig.clip(lower=0), fb, COSTS["spot"]["base"], 0, None),
                "lo_stress": simulate(df, sig.clip(lower=0), fb, COSTS["spot"]["stress"], 0, None),
                "lo_delay1": simulate(df, sig.clip(lower=0), fb, COSTS["spot"]["base"], 1, None),
            }
            for k, v in scen.items():
                frames.setdefault(k, []).append(v[v.index >= warm])
        per[sym] = {k: pd.concat(v).sort_index() for k, v in frames.items()}

    def window(frame, w):
        return frame[(frame.index >= E_START) & (frame.index < E_END)] if w == "E" else frame[frame.index < P_END]

    def daily_port(scen, w, col="net"):
        cols = {s: window(per[s][scen], w)[col].resample("1D").sum() for s in per if len(window(per[s][scen], w))}
        if not cols:
            return pd.Series(dtype=float)
        return pd.DataFrame(cols).mean(axis=1, skipna=True).dropna()

    res = {"bar": rule, "lookahead": look, "configs": {}}
    for side in ("ls", "lo"):
        cfg = {}
        for w in ("E", "P"):
            d = daily_port(f"{side}_base", w)
            cfg[w] = summarise(d)
            cfg[w]["p_boot"] = round(block_bootstrap_p(d.values, rng), 4) if len(d) > 30 else None
            cfg[w]["stress"] = summarise(daily_port(f"{side}_stress", w))
            cfg[w]["delay1"] = summarise(daily_port(f"{side}_delay1", w))
            if side == "ls":
                cfg[w]["gross_no_cost"] = summarise(daily_port("ls_zero_cost", w))
            # components (sum of daily per-asset averages, % of capital)
            comp = {}
            for col in ("gross", "cost", "funding", "long", "short"):
                comp[col] = round(float(daily_port(f"{side}_base", w, col).sum()) * 100, 1)
            cfg[w]["components_pct"] = comp
            # per asset, trades
            pa, rts, changes = {}, 0, 0
            for s in per:
                fr = window(per[s][f"{side}_base"], w)
                if not len(fr):
                    continue
                eps = episodes(fr["held"].values)
                rts += len(eps)
                changes += int((np.abs(np.diff(np.concatenate([[0], fr["held"].values]))) > 0).sum())
                pa[s] = {"net_pct": round(float(fr["net"].sum()) * 100, 1), "round_trips": len(eps),
                         "long_eps": sum(1 for e in eps if e[2] > 0), "short_eps": sum(1 for e in eps if e[2] < 0),
                         "time_in_market_pct": round(float((fr["held"] != 0).mean()) * 100, 1)}
            cfg[w]["per_asset"] = pa
            cfg[w]["round_trips"] = rts
            ndays = max(1, cfg[w].get("days", 1))
            cfg[w]["round_trips_per_day"] = round(rts / ndays, 3)
            cfg[w]["fills_per_day"] = round(changes / ndays, 3)
            # direction-randomised null (episodes pooled over assets; costs kept as actual)
            if side == "ls":
                g, sgn, cost_total = [], [], 0.0
                for s in per:
                    fr = window(per[s]["ls_base"], w)
                    if not len(fr):
                        continue
                    ra, fb, h = fr["r_asset"].values, fr["fund_bar"].values, fr["held"].values
                    for a, b, sg in episodes(h):
                        g.append(float((ra[a:b] - fb[a:b]).sum())); sgn.append(sg)
                    cost_total += float(fr["cost"].sum())
                g, sgn = np.array(g), np.array(sgn)
                if len(g):
                    actual = float((sgn * g).sum()) + cost_total
                    rnd = rng.choice([-1, 1], size=(1000, len(g)))
                    null = (rnd * g).sum(axis=1) + cost_total
                    cfg[w]["direction_null_percentile"] = round(float((null < actual).mean()) * 100, 1)
        cfg["acceptance"] = acceptance(cfg, side, look)
        res["configs"]["long_short_perp" if side == "ls" else "long_only_spot"] = cfg
    return res


def acceptance(cfg: dict, side: str, look: dict) -> dict:
    E, P = cfg["E"], cfg["P"]
    thr = ALPHA / N_CONFIGS
    pa = E.get("per_asset", {})
    c = {
        "1_ann_ret_pos": E.get("ann_return_pct", -1) > 0,
        "1_p_bonferroni": (E.get("p_boot") is not None and E["p_boot"] < thr),
        "2_stress_pos": E["stress"].get("total_return_pct", -1) > 0,
        "3_delay1_pos": E["delay1"].get("total_return_pct", -1) > 0,
        "4_P_pos": P.get("total_return_pct", -1) > 0,
        "5_two_of_three_assets": sum(1 for v in pa.values() if v["net_pct"] > 0) >= 2,
        "7_min_30_round_trips": E.get("round_trips", 0) >= 30,
        "9_lookahead_pass": all(v["pass"] for v in look.values()),
    }
    if side == "ls":
        c["6_beats_direction_null_p95"] = E.get("direction_null_percentile", 0) > 95
        c["8_short_leg_pos"] = E["components_pct"]["short"] > 0
    tier_a = all(c.values())
    others = {k: v for k, v in c.items() if k != "1_p_bonferroni"}
    tier_b = all(others.values()) and E.get("p_boot") is not None and E["p_boot"] < ALPHA
    return {"criteria": c, "bonferroni_alpha": round(thr, 5),
            "verdict": "TIER_A" if tier_a else ("TIER_B_forward_paper_only" if tier_b else "REJECTED")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root-1m", required=True)
    ap.add_argument("--raw-dirs", nargs="*", default=[])
    ap.add_argument("--funding-dir", default="research_lab/data_ext")
    ap.add_argument("--cache")
    ap.add_argument("--out", default="research_lab/results/longshort_study.json")
    a = ap.parse_args()
    rng = np.random.default_rng(SEED)
    t0 = time.time()
    data = load_hourly(a.root_1m, a.raw_dirs, a.cache)
    funding, finfo = {}, {}
    for s in SYMS:
        funding[s], finfo[s] = load_funding(a.funding_dir, s)
    out = {"preregistration": "docs/LONGSHORT_PREREGISTRATION.md", "seed": SEED, "costs_bps_per_side": COSTS,
           "windows": {"E": [str(E_START)[:10], str(E_END)[:10]], "P_end": str(P_END)[:10]},
           "data": {s: {"first": str(d.index[0]), "last": str(d.index[-1]), "hourly_bars": len(d),
                        "blocks": [[str(b.index[0])[:10], str(b.index[-1])[:10]] for b in blocks(d)]} for s, d in data.items()},
           "funding": finfo, "candidates": {}}
    # buy-and-hold reference per window (equal weight, spot, no costs)
    bh = {}
    for w in ("E", "P"):
        cols = {}
        for s, h in data.items():
            d = h["close"].resample("1D").last().pct_change().dropna()
            d = d[(d.index >= E_START) & (d.index < E_END)] if w == "E" else d[d.index < P_END]
            cols[s] = d
        bh[w] = summarise(pd.DataFrame(cols).mean(axis=1, skipna=True).dropna())
    out["buy_and_hold_equal_weight"] = bh
    for name, (fn, rule) in CANDIDATES.items():
        t1 = time.time()
        out["candidates"][name] = run_candidate(name, fn, rule, data, funding, rng)
        r = out["candidates"][name]["configs"]
        print(f"{name}: LS {r['long_short_perp']['acceptance']['verdict']} E ann {r['long_short_perp']['E'].get('ann_return_pct')}% "
              f"p {r['long_short_perp']['E'].get('p_boot')} | LO {r['long_only_spot']['acceptance']['verdict']} "
              f"E ann {r['long_only_spot']['E'].get('ann_return_pct')}%  ({time.time() - t1:.0f}s)", flush=True)
    out["runtime_sec"] = round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
