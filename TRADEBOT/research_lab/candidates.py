"""Long-only adaptations of the three reviewed repositories (see refs/CANDIDATE_REPOS.md). Each is a NEW strategy:
the originals depend on shorting, leverage, zero costs or repainting filters, so none of their claims carries over.

- pine_ultimate_long : long side of dextergocode/ultimate-crypto-trading-bot (Pine v5), HTF filter made causal.
- tr3_long / hmm_tr3_long : Krishhiv/HMM_TR_Alg TR3 breakout/thrust engine with its full exit stack, without and
  with the 3-state Gaussian-HMM daily regime gate (walk-forward refits, causal decoding). No ECT leverage (1.0x).
- pairs_longleg : long-leg-only reading of sujith-kamme/statistical-arbitrage-crypto's OU/z-score idea on a 2-asset
  spread among BTC/ETH/SOL. The original is long/short cointegration baskets; this is directional and untested.
"""
from __future__ import annotations

import hashlib
import logging
import math

import numpy as np
import pandas as pd

from . import indicators as I
from .engine import ExitSpec, Result, Trade, compute_metrics
from .strategies import register

log = logging.getLogger(__name__)


# =====================================================================================================================
# 1. Pine "ultimate crypto bot", long side only, causal HTF filter
# =====================================================================================================================
def _stoch(df: pd.DataFrame, n: int = 14, d: int = 3):
    lo = df["low"].rolling(n).min(); hi = df["high"].rolling(n).max()
    k = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return k, k.rolling(d).mean()


@register("pine_ultimate_long", {"stop_pct": [3.0], "tp_pct": [6.0]}, {"stop_pct": 3.0, "tp_pct": 6.0},
          "dextergocode/ultimate-crypto-trading-bot (ultimate_crypto_bot.pine), long side only",
          "SMA9>SMA21 & close>SMA200 & 40<RSI<80 & MACD>signal & hist rising & stoch K>D, 20<K<80 & higher-high & higher-low "
          "vs prior 10 bars & volume>1.2xSMA20 & ATR/close<3% & previous completed daily close > daily SMA50 -> enter 10% equity; "
          "stop -3% / take-profit +6% from fill. The script's 'reversal exit' can never trigger (RSI>70 while below SMA200) and is omitted.",
          flags=("adapted_long_only", "author_claim_unverified"),
          author_claim="README: 'Total Trades 150+, Win Rate 65%, Max Drawdown 15%, Profit Factor 1.8, Sharpe 1.2' on BTC/USD 1h over 6 months; no artefact, HTF filter repaints, slippage 0.")
def pine_ultimate_long(df, p):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    sma9, sma21, sma200 = I.sma(c, 9), I.sma(c, 21), I.sma(c, 200)
    r = I.rsi(c, 14)
    macd_line, sig, hist = I.macd(c)
    k, d = _stoch(df)
    vol_ma = I.sma(v, 20)
    atr_pct = I.atr(df, 14) / c * 100
    hh = h > h.shift(1).rolling(10).max(); hl = l > l.shift(1).rolling(10).min()
    lh = h < h.shift(1).rolling(10).max(); ll = l < l.shift(1).rolling(10).min()
    # causal higher-timeframe gate: previous COMPLETED UTC day close > SMA50 of daily closes
    daily = df["close"].resample("1D", label="left", closed="left").last().dropna()
    htf = (daily > daily.rolling(50).mean()).shift(1)                 # shift: only the completed day is known
    htf_bar = htf.reindex(df.index, method="ffill").fillna(False).astype(bool)
    e = ((sma9 > sma21) & (c > sma200) & (r > 40) & (r < 80) & (macd_line > sig) & (hist > hist.shift(1))
         & (k > d) & (k > 20) & (k < 80) & hh & hl & ~(lh & ll) & (v > 1.2 * vol_ma) & (atr_pct < 3.0) & htf_bar)
    return e, None, ExitSpec(stop_loss_pct=p["stop_pct"], take_profit_pct=p["tp_pct"])


# =====================================================================================================================
# 2. TR3 engine (HMM_TR_Alg) with full stateful exit stack; optional HMM regime gate
# =====================================================================================================================
def _bars_per_day(df) -> int:
    step = (df.index[1] - df.index[0]).total_seconds() / 60 if len(df) > 1 else 60
    return max(1, int(round(1440 / step)))


def _tr3_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    c, h, l = df["close"], df["high"], df["low"]
    f["atr"] = I.atr(df, 14)
    f["ema50"] = c.ewm(span=50, adjust=False).mean()
    f["ema200"] = c.ewm(span=200, adjust=False).mean()
    f["donch_up"] = h.rolling(20).max().shift(1)
    f["clv"] = ((c - l) / (h - l).replace(0, np.nan)).fillna(0.5)
    f["adx"] = I.adx(df, 14)
    # R^2 of EMA50 against time over 72 bars (trend quality), rolling
    y = f["ema50"]; n = 72
    t = np.arange(n); tm = t.mean(); st = ((t - tm) ** 2).sum()
    def r2(win):
        if np.isnan(win).any():
            return np.nan
        ym = win.mean(); sy = ((win - ym) ** 2).sum()
        if sy == 0:
            return 0.0
        b = ((t - tm) * (win - ym)).sum() / st
        return (b * b * st) / sy
    f["r2"] = y.rolling(n).apply(r2, raw=True)
    f["thrust"] = (c - c.shift(1)) >= 0.75 * f["atr"]
    f["breakout"] = c > f["donch_up"]
    return f


_HMM_CACHE: dict[str, pd.Series] = {}
_REGIME_STORE: list[tuple[pd.Series, pd.Series]] = []   # (full close series, causal daily regime series)


def prepare_regimes(df_full: pd.DataFrame) -> pd.Series:
    """Compute the causal walk-forward regime series ONCE on the full history of an asset; slices (walk-forward
    windows, holdout) later look it up by date. Because every state is decoded from data strictly before its day,
    using the full-series states inside a slice introduces no lookahead."""
    st = _hmm_regime_series(df_full)
    _REGIME_STORE.append((df_full["close"], st))
    return st


def _regimes_for(df: pd.DataFrame) -> pd.Series | None:
    for closes, st in _REGIME_STORE:
        a, b = df.index[0], df.index[-1]
        if a in closes.index and b in closes.index and abs(closes[a] - df["close"].iloc[0]) < 1e-9 and abs(closes[b] - df["close"].iloc[-1]) < 1e-9:
            return st
    return None


def _hmm_regime_series(df: pd.DataFrame, train_days: int = 730, refit_days: int = 30, window: int = 60, seed: int = 42) -> pd.Series:
    """Daily regime (0 low / 1 middle / 2 high mean return), walk-forward: fit on the trailing `train_days` of daily
    features every `refit_days`, decode causally with a Viterbi window of the last `window` COMPLETED days (T-1 and
    earlier), hold the model fixed in between. Mirrors scripts/run_btc_strategy.py of HMM_TR_Alg. Cached per series."""
    key = hashlib.md5(f"{df.index[0]}{df.index[-1]}{len(df)}{train_days}{refit_days}{window}{seed}".encode()).hexdigest()
    if key in _HMM_CACHE:
        return _HMM_CACHE[key]
    from hmmlearn.hmm import GaussianHMM
    from sklearn.preprocessing import StandardScaler
    d = df.resample("1D", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    f = pd.DataFrame(index=d.index)
    f["logret"] = np.log(d["close"]).diff()
    gk = 0.5 * np.log(d["high"] / d["low"]) ** 2 - (2 * np.log(2) - 1) * np.log(d["close"] / d["open"]) ** 2
    f["gkvol20"] = np.sqrt(gk.rolling(20).mean().clip(lower=0))
    f["volz20"] = (d["volume"] - d["volume"].rolling(20).mean()) / d["volume"].rolling(20).std().replace(0, np.nan)
    ema20 = d["close"].ewm(span=20, adjust=False).mean()
    f["ema20_slope"] = ema20.diff()
    f["body"] = (d["close"] - d["open"]) / d["open"]
    f["cpos"] = ((d["close"] - d["low"]) / (d["high"] - d["low"]).replace(0, np.nan) - 0.5).fillna(0)
    f["c_over_ema20"] = d["close"] / ema20 - 1
    f = f.replace([np.inf, -np.inf], np.nan).dropna()
    states = pd.Series(np.nan, index=f.index)
    model = None
    dates = f.index
    start = 0
    while start < len(dates):
        t0 = dates[start]
        train = f[(f.index >= t0 - pd.Timedelta(days=train_days)) & (f.index < t0)]
        if len(train) >= 200:
            scaler = StandardScaler().fit(train.values)
            X = scaler.transform(train.values)
            m = GaussianHMM(n_components=3, covariance_type="diag", n_iter=300, tol=1e-3, random_state=seed)
            try:
                m.fit(X)
                order = np.argsort(m.means_[:, 0])           # 0 = lowest mean (scaled) log return, 2 = highest
                rank = {s: r for r, s in enumerate(order)}
                model = (m, scaler, rank)
            except Exception as e:  # noqa: BLE001
                log.warning("HMM fit failed at %s: %s (keeping previous model)", t0.date(), e)
        if model is not None:
            m, scaler, rank = model
            for i in range(start, min(start + refit_days, len(dates))):
                hist = f.iloc[max(0, i - window): i]            # completed days only: excludes day i
                if len(hist) >= 10:
                    try:
                        path = m.predict(scaler.transform(hist.values))
                        states.iloc[i] = rank[int(path[-1])]
                    except Exception:  # noqa: BLE001
                        pass
        start += refit_days
    _HMM_CACHE[key] = states
    return states


def _tr3_backtest(df: pd.DataFrame, p: dict, fee_bps: float, slippage_bps: float, size_frac: float, bar_minutes: int,
                  warmup: int, use_hmm: bool) -> Result:
    f = _tr3_features(df)
    bpd = _bars_per_day(df)
    gate = pd.Series(True, index=df.index)
    if use_hmm:
        st = _regimes_for(df)
        if st is None:
            st = prepare_regimes(df)
        gate = st.reindex(df.index, method="ffill").eq(p.get("allowed_state", 1))   # state of the completed day, ffilled
        # states are decoded from days < T, so the daily value is known at the start of day T: no lookahead
    cond = (gate & (f["ema50"] > f["ema200"]) & (df["close"] > f["ema50"]) & (f["breakout"] | f["thrust"])
            & (f["clv"] >= p["clv_min"]) & (f["adx"] >= p["adx_min"]) & (f["r2"] >= p["r2_min"]))
    cond.iloc[:warmup] = False
    o, h, l, c = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    atr, ema50, clv = f["atr"].to_numpy(), f["ema50"].to_numpy(), f["clv"].to_numpy()
    sig = cond.to_numpy()
    cost = (fee_bps + slippage_bps) / 1e4
    cash, qty = 10_000.0, 0.0
    entry_px = entry_i = 0; entry_bar_high = 0.0; hi_since = 0.0; be_armed = False; be_armed_i = -1
    trades: list[Trade] = []; eq = np.empty(len(df))
    idx = df.index
    max_hold = int(p["max_hold_hours"] * 60 / bar_minutes); no_prog = int(12 * 60 / bar_minutes)
    pending = False

    def close_pos(i, px, reason):
        nonlocal cash, qty
        proceeds = qty * px * (1 - cost); pnl = proceeds - qty * entry_px * (1 + cost)
        trades.append(Trade(entry_i, i, idx[entry_i], idx[i], entry_px, px, qty, pnl, pnl / (qty * entry_px * (1 + cost)) * 100, reason, i - entry_i))
        cash += proceeds; qty = 0.0

    for i in range(len(df)):
        if qty == 0.0 and pending:
            px = o[i]; notional = cash * size_frac; qty = notional / (px * (1 + cost)); cash -= notional
            entry_px, entry_i, hi_since, entry_bar_high, be_armed, be_armed_i = px, i, h[i - 1], h[i - 1], False, -1
            pending = False
        if qty > 0.0 and i > entry_i:
            a = atr[i] if not math.isnan(atr[i]) else 1e-9
            hi_since = max(hi_since, h[i])
            adv = (hi_since - entry_px) / a
            reason = None
            if adv < 0.5 and (clv[i] <= 0.35 or c[i] < entry_bar_high - 0.25 * a):
                reason = "fail_to_hold"
            if reason is None:
                if 0.75 <= adv < 1.0 and not be_armed:
                    be_armed, be_armed_i = True, i
                if adv >= 1.0:
                    be_armed = False
                if be_armed and i > be_armed_i and c[i] < entry_px + 0.02 * a:
                    reason = "breakeven"
            if reason is None:
                mult = 2.5 if adv < 1 else (3.5 if adv < 2 else 4.5)
                if c[i] < hi_since - mult * a:
                    reason = "trail_atr"
            if reason is None and not math.isnan(ema50[i]):
                if adv >= 1.0 and c[i] < ema50[i] - 0.25 * a:
                    reason = "ema50_relaxed"
                elif adv < 1.0 and c[i] < ema50[i]:
                    reason = "ema50_break"
            if reason is None and i - entry_i >= no_prog and hi_since - entry_px < 0.5 * a:
                reason = "no_progress"
            if reason is None and i - entry_i >= max_hold:
                reason = "time_stop"
            if reason is not None and i + 1 < len(df):
                close_pos(i + 1, o[i + 1], reason)      # exit at next open (TR3 semantics)
                eq[i] = cash
                if i + 1 < len(df):
                    pass
                continue
        if qty == 0.0 and sig[i] and i < len(df) - 1:
            pending = True
        eq[i] = cash + qty * c[i]
    if qty > 0.0:
        close_pos(len(df) - 1, c[-1], "end"); eq[-1] = cash
    res = Result(trades, pd.Series(eq, index=idx))
    res.metrics = compute_metrics(res, 10_000.0, bar_minutes)
    return res


TR3_DEFAULT = {"adx_min": 14, "r2_min": 0.10, "clv_min": 0.55, "max_hold_hours": 168, "allowed_state": 1}
TR3_GRID = {"adx_min": [14, 25], "r2_min": [0.10, 0.25]}
TR3_RULES = ("EMA50>EMA200 & close>EMA50 & (close>prior 20-bar high OR 1-bar thrust >= 0.75 ATR) & CLV>=0.55 & ADX14>=14 & "
             "R2(EMA50,72)>=0.10 -> enter next open; exits: fail-to-hold (CLV<=0.35 or close<entry-bar high-0.25ATR while <0.5ATR up), "
             "delayed breakeven after 0.75-1.0 ATR advance, ratcheting 2.5/3.5/4.5-ATR trail, EMA50 break (0.25ATR cushion once >=1 ATR up), "
             "12h no-progress, 7-day time stop. Sizing 1.0x (the original's 1.3x equity-curve throttle is leverage and is removed).")
TR3_CLAIM = ("README/METRICS.txt: portfolio 'Total Return 140.83%, CAGR 21.51%, Sharpe 1.461, Max Drawdown -8.52%' 2021-12-31..2026-07-06, "
             "BTC+ETH 50/50 with 1.3x equity-curve throttle, ZERO fees/slippage, -8% per-trade loss floor; trade logs are un-fetched Git LFS pointers.")


@register("tr3_long", TR3_GRID, TR3_DEFAULT, "Krishhiv/HMM_TR_Alg TR3 engine, no regime gate", TR3_RULES + " No HMM gate (isolates the engine).",
          flags=("adapted_long_only", "custom_backtest", "author_claim_unverified"), author_claim=TR3_CLAIM)
def tr3_long(df, p, **kw):
    return _tr3_backtest(df, p, kw["fee_bps"], kw["slippage_bps"], kw["size_frac"], kw["bar_minutes"], kw["warmup"], use_hmm=False)


@register("hmm_tr3_long", TR3_GRID, TR3_DEFAULT, "Krishhiv/HMM_TR_Alg: 3-state Gaussian HMM daily regime gate + TR3",
          TR3_RULES + " Trades only when the walk-forward HMM (7 daily features, 730-day refit every 30 days, causal 60-day Viterbi) is in the middle-return state.",
          flags=("adapted_long_only", "custom_backtest", "needs_regimes", "author_claim_unverified"), author_claim=TR3_CLAIM)
def hmm_tr3_long(df, p, **kw):
    return _tr3_backtest(df, p, kw["fee_bps"], kw["slippage_bps"], kw["size_frac"], kw["bar_minutes"], kw["warmup"], use_hmm=True)


# =====================================================================================================================
# 3. Pairs idea, long leg only (NEW strategy; the original requires shorting)
# =====================================================================================================================
@register("pairs_longleg", {"z_entry": [1.5, 2.0], "z_exit": [0.0, 0.5], "window": [120, 240]}, {"z_entry": 2.0, "z_exit": 0.5, "window": 120, "stop_pct": 8.0},
          "sujith-kamme/statistical-arbitrage-crypto, long-leg-only adaptation (NOT the original)",
          "Spread = log(asset) - beta*log(partner) with rolling OLS beta over `window` bars; z = (spread-mean)/std over the same window. "
          "Long the asset when z < -z_entry (cheap vs partner); exit when z > -z_exit or 8% stop. Partner = the other major with the highest "
          "rolling correlation (BTC/ETH/SOL only). Original is long/short cointegration baskets on 18 Binance.US alts with no stop; its claims do not apply.",
          flags=("adapted_long_only", "author_claim_unverified"), needs_partners=True,
          author_claim="readme: OOS 'Sharpe 1.764 net, Cumulative Return +5.61%, Max Drawdown -1.38%, Win Rate 72.2%' on 9 round trips (author: 'not yet statistically meaningful').")
def pairs_longleg(df, p):
    partners = p.get("_partners") or {}
    e = pd.Series(False, index=df.index)
    if not partners:
        return e, None, ExitSpec(stop_loss_pct=p["stop_pct"])
    la = np.log(df["close"])
    best, best_corr = None, -2
    for k, pdf in partners.items():
        lb = np.log(pdf["close"].reindex(df.index, method="ffill"))
        corr = la.diff().rolling(p["window"]).corr(lb.diff()).iloc[-1]
        if corr is not None and not np.isnan(corr) and corr > best_corr:
            best, best_corr = lb, corr
    if best is None:
        return e, None, ExitSpec(stop_loss_pct=p["stop_pct"])
    w = p["window"]
    cov = la.rolling(w).cov(best); var = best.rolling(w).var()
    beta = (cov / var.replace(0, np.nan)).shift(1)                 # beta from completed window only
    spread = la - beta * best
    z = (spread - spread.rolling(w).mean()) / spread.rolling(w).std().replace(0, np.nan)
    return (z < -p["z_entry"]) & (z.shift(1) >= -p["z_entry"]), z > -p["z_exit"], ExitSpec(stop_loss_pct=p["stop_pct"])
