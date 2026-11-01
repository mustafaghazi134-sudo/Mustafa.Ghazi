"""Candidate strategies. Each returns (entries, exits, ExitSpec); signals on bar i are acted on at bar i+1 open.

Sources: Freqtrade sample/community strategies (freqtrade/freqtrade-strategies), vectorbt and LEAN
textbook examples, classic literature (Turtle/Donchian, Connors RSI2, time-series momentum), plus the
strategies already in tradebot. "High win rate" candidates are included deliberately so their hidden
tail risk can be measured rather than assumed.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from . import indicators as I
from .engine import ExitSpec

Strat = Callable[[pd.DataFrame, dict], tuple[pd.Series, pd.Series | None, ExitSpec]]
REGISTRY: dict[str, dict] = {}


def register(name: str, grid: dict, default: dict, source: str, note: str = "", flags: tuple[str, ...] = ()):
    def deco(fn: Strat):
        REGISTRY[name] = {"fn": fn, "grid": grid, "default": default, "source": source, "note": note, "flags": flags}
        return fn
    return deco


# ---------------------------------------------------------------- benchmarks / controls
@register("buy_hold", {}, {}, "benchmark", "Buy on first bar, hold to the end. Every candidate must beat this on risk-adjusted terms.")
def buy_hold(df, p):
    # entry signal on every bar: the engine opens one position at the first tradable bar and never exits
    return pd.Series(True, index=df.index), None, ExitSpec()


@register("random_entry_atr_exits", {"seed": [1, 2, 3]}, {"seed": 1}, "control",
          "Random entries (~2% of bars) with the Donchian strategy's ATR stop/TP. If a strategy barely beats this, its edge is the exit logic or luck.")
def random_entry(df, p):
    rng = np.random.default_rng(p["seed"])
    e = pd.Series(rng.random(len(df)) < 0.02, index=df.index)
    return e, None, ExitSpec(atr_stop_mult=2.0, atr_tp_mult=4.0)


# ---------------------------------------------------------------- trend following
@register("ema_cross_trend", {"fast": [12, 20], "slow": [26, 50], "trend": [100, 200], "atr_mult": [2.5, 3.0]},
          {"fast": 12, "slow": 26, "trend": 100, "atr_mult": 2.5}, "tradebot momentum / vectorbt dual-MA example",
          "EMA fast>slow cross while close>EMA trend; exit on cross down; ATR trailing stop.")
def ema_cross_trend(df, p):
    c = df["close"]; f, s, t = I.ema(c, p["fast"]), I.ema(c, p["slow"]), I.ema(c, p["trend"])
    return I.cross_up(f, s) & (c > t), I.cross_down(f, s), ExitSpec(atr_stop_mult=p["atr_mult"], trailing_stop_pct=None)


@register("donchian_turtle", {"entry": [20, 55], "exit": [10, 20], "atr_mult": [2.0, 3.0]},
          {"entry": 55, "exit": 20, "atr_mult": 2.0}, "Turtle rules / tradebot breakout",
          "Enter on close above previous N-bar high; exit on close below previous M-bar low; 2-ATR stop.")
def donchian_turtle(df, p):
    up, _ = I.donchian(df, p["entry"]); _, dn = I.donchian(df, p["exit"])
    return df["close"] > up, df["close"] < dn, ExitSpec(atr_stop_mult=p["atr_mult"])


@register("supertrend_consensus", {"mult": [2.0, 3.0], "stop_pct": [5.0, 10.0]}, {"mult": 3.0, "stop_pct": 10.0},
          "freqtrade-strategies/Supertrend.py", "Three Supertrends (periods 7/10/14) all up -> enter; all down -> exit. Original uses stoploss -10%.")
def supertrend_consensus(df, p):
    s = [I.supertrend(df, n, p["mult"]) for n in (7, 10, 14)]
    up = (s[0] == 1) & (s[1] == 1) & (s[2] == 1)
    dn = (s[0] == -1) & (s[1] == -1) & (s[2] == -1)
    return up & ~up.shift(1).fillna(False).astype(bool), dn, ExitSpec(stop_loss_pct=p["stop_pct"])


@register("macd_trend", {"trend": [100, 200], "atr_mult": [2.0, 3.0]}, {"trend": 200, "atr_mult": 3.0},
          "LEAN/vectorbt MACD example", "MACD line crosses above signal while close>EMA trend; exit cross below; ATR stop.")
def macd_trend(df, p):
    line, sig, _ = I.macd(df["close"]); t = I.ema(df["close"], p["trend"])
    return I.cross_up(line, sig) & (df["close"] > t), I.cross_down(line, sig), ExitSpec(atr_stop_mult=p["atr_mult"])


def _bars_per_day(df) -> int:
    step = (df.index[1] - df.index[0]).total_seconds() / 60 if len(df) > 1 else 60
    return max(1, int(round(1440 / step)))


@register("tsmom_30d", {"lookback_days": [20, 30, 60], "vol_stop_pct": [15.0, 25.0]}, {"lookback_days": 30, "vol_stop_pct": 25.0},
          "time-series momentum (Moskowitz, Ooi, Pedersen 2012), daily-evaluated",
          "Long when the trailing N-day return is positive, evaluated once a day at 00:00 UTC; exit when negative. Wide disaster stop only.")
def tsmom(df, p):
    c = df["close"]; bpd = _bars_per_day(df); lb = p["lookback_days"] * bpd
    mom = c / c.shift(lb) - 1
    daily = pd.Series(df.index.hour == 0, index=df.index) if bpd > 1 else pd.Series(True, index=df.index)
    return (mom > 0) & daily, (mom < 0) & daily, ExitSpec(stop_loss_pct=p["vol_stop_pct"])


# ---------------------------------------------------------------- mean reversion (incl. "high win rate" claims)
@register("bb_rsi_meanrev_nostop", {"rsi_buy": [25, 30, 35], "tp_pct": [1.0, 2.0]}, {"rsi_buy": 30, "tp_pct": 1.0},
          "Freqtrade BBRSI / ClucMay72018 family (claimed 80-95% win rates)",
          "Close below lower BB(20,2) and RSI<thr -> enter; take profit +tp%; NO stop loss. Measures the hidden tail risk behind high-win-rate claims.",
          flags=("no_stop_loss",))
def bb_rsi_nostop(df, p):
    lo, mid, _ = I.bbands(df["close"]); r = I.rsi(df["close"])
    return (df["close"] < lo) & (r < p["rsi_buy"]), None, ExitSpec(take_profit_pct=p["tp_pct"])


@register("bb_rsi_meanrev_stop", {"rsi_buy": [25, 30, 35], "stop_pct": [4.0, 6.0, 8.0], "max_hold": [48, 96]},
          {"rsi_buy": 30, "stop_pct": 6.0, "max_hold": 48}, "tradebot mean_reversion",
          "RSI<thr and re-entry above lower BB -> enter; exit RSI>55 or BB mid; stop and time stop.")
def bb_rsi_stop(df, p):
    c = df["close"]; lo, mid, _ = I.bbands(c); r = I.rsi(c)
    e = (r < p["rsi_buy"]) & (c.shift(1) < lo.shift(1)) & (c >= lo)
    x = (r > 55) | (c > mid)
    return e, x, ExitSpec(stop_loss_pct=p["stop_pct"], max_hold_bars=p["max_hold"])


@register("connors_rsi2", {"rsi_thr": [5, 10], "sma_trend": [200], "stop_pct": [5.0, 8.0]}, {"rsi_thr": 10, "sma_trend": 200, "stop_pct": 8.0},
          "Connors & Alvarez RSI(2) (equities literature)", "RSI(2)<thr with close>SMA200 -> enter; exit close>SMA5. Stop added (original has none).")
def connors_rsi2(df, p):
    c = df["close"]; r2 = I.rsi(c, 2)
    return (r2 < p["rsi_thr"]) & (c > I.sma(c, p["sma_trend"])), c > I.sma(c, 5), ExitSpec(stop_loss_pct=p["stop_pct"])


# ---------------------------------------------------------------- Freqtrade community strategies (ported)
_ROI_SCALE = 12   # originals are 5m strategies; their minute-based ROI tables are scaled to 1h bars


@register("ft_sample_strategy", {"stop_pct": [10.0], "roi_scale": [1, 12]}, {"stop_pct": 10.0, "roi_scale": 12},
          "freqtrade/templates/sample_strategy.py",
          "RSI crosses up 30 & TEMA<=BB mid & TEMA rising -> enter; RSI crosses down 70 & TEMA>BB mid & TEMA falling -> exit; "
          "minimal_roi {0:4%,20m:2%,30m:1%} (scaled), stoploss -10%.")
def ft_sample(df, p):
    c = df["close"]; r = I.rsi(c); lo, mid, _ = I.bbands(c); t = I.tema(c, 9)
    e = I.cross_up(r, 30) & (t <= mid) & (t > t.shift(1)) & (df["volume"] > 0)
    x = I.cross_down(r, 70) & (t > mid) & (t < t.shift(1))
    k = p["roi_scale"]
    return e, x, ExitSpec(stop_loss_pct=p["stop_pct"], minimal_roi={0: 4.0, 20 * k: 2.0, 30 * k: 1.0})


@register("ft_strategy001", {"stop_pct": [10.0], "roi_scale": [1, 12]}, {"stop_pct": 10.0, "roi_scale": 12},
          "freqtrade-strategies/Strategy001.py",
          "EMA20 crosses above EMA50 & HA close>EMA20 & HA open<HA close -> enter; EMA50 crosses below EMA100 & HA close<EMA20 & HA open>HA close -> exit; "
          "minimal_roi {60m:1%,30m:3%,20m:4%,0:5%} (scaled), stoploss -10%.")
def ft_strategy001(df, p):
    c = df["close"]; ha = I.heikin_ashi(df)
    e20, e50, e100 = I.ema(c, 20), I.ema(c, 50), I.ema(c, 100)
    e = I.cross_up(e20, e50) & (ha["close"] > e20) & (ha["open"] < ha["close"])
    x = I.cross_down(e50, e100) & (ha["close"] < e20) & (ha["open"] > ha["close"])
    k = p["roi_scale"]
    return e, x, ExitSpec(stop_loss_pct=p["stop_pct"], minimal_roi={0: 5.0, 20 * k: 4.0, 30 * k: 3.0, 60 * k: 1.0})


@register("ft_strategy003", {"stop_pct": [10.0], "roi_scale": [12]}, {"stop_pct": 10.0, "roi_scale": 12},
          "freqtrade-strategies/Strategy003.py (simplified port)",
          "RSI<28 & RSI>0 & close<SMA(fast) & Fisher-RSI<-0.94 & MFI<16.0 & EMA50>EMA100 -> enter; SAR>close & Fisher-RSI>0.3 -> exit (SAR replaced by EMA10 cross); minimal_roi {60m:1%,30m:2%,20m:3%,0:4%}, stoploss -10%. Simplified.",
          flags=("simplified_port",))
def ft_strategy003(df, p):
    c = df["close"]; r = I.rsi(c)
    rsi_norm = 0.1 * (r - 50); fisher = (np.exp(2 * rsi_norm) - 1) / (np.exp(2 * rsi_norm) + 1)
    tp = (df["high"] + df["low"] + c) / 3; mf = tp * df["volume"]; pos = mf.where(tp > tp.shift(1), 0.0).rolling(14).sum(); neg = mf.where(tp < tp.shift(1), 0.0).rolling(14).sum()
    mfi = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    e = (r < 28) & (r > 0) & (c < I.sma(c, 20)) & (fisher < -0.94) & (mfi < 16.0) & (I.ema(c, 50) > I.ema(c, 100))
    x = (c < I.ema(c, 10)) & (fisher > 0.3)
    k = p["roi_scale"]
    return e, x, ExitSpec(stop_loss_pct=p["stop_pct"], minimal_roi={0: 4.0, 20 * k: 3.0, 30 * k: 2.0, 60 * k: 1.0})


# ---------------------------------------------------------------- Freqtrade community ports from the catalog shortlist
@register("ft_bbandrsi", {"stop_pct": [25.0], "roi_pct": [10.0]}, {"stop_pct": 25.0, "roi_pct": 10.0},
          "freqtrade-strategies/berlinguyinca/BbandRsi.py (1h)",
          "RSI(14)<30 & close<BB(20,2) lower -> enter; exit RSI>70; minimal_roi 10%; stoploss -25%. ROI-vs-stop asymmetry = high win rate, hidden tail.",
          flags=("roi_tail_profile",))
def ft_bbandrsi(df, p):
    c = df["close"]; r = I.rsi(c); lo, _, _ = I.bbands(c)
    return (r < 30) & (c < lo), r > 70, ExitSpec(stop_loss_pct=p["stop_pct"], take_profit_pct=p["roi_pct"])


@register("ft_adx_smas", {"adx_thr": [25, 30], "stop_pct": [10.0, 25.0]}, {"adx_thr": 25, "stop_pct": 25.0},
          "freqtrade-strategies/berlinguyinca/AdxSmas.py (1h)",
          "ADX>25 & SMA3 crosses above SMA6 -> enter; ADX<25 & SMA6 crosses above SMA3 -> exit; stoploss -25%, minimal_roi 10%.")
def ft_adx_smas(df, p):
    c = df["close"]; a = I.adx(df); s3, s6 = I.sma(c, 3), I.sma(c, 6)
    return (a > p["adx_thr"]) & I.cross_up(s3, s6), (a < p["adx_thr"]) & I.cross_up(s6, s3), ExitSpec(stop_loss_pct=p["stop_pct"], take_profit_pct=10.0)


@register("ft_adx_momentum", {"stop_pct": [25.0], "roi_pct": [1.0]}, {"stop_pct": 25.0, "roi_pct": 1.0},
          "freqtrade-strategies/berlinguyinca/ADXMomentum.py (1h)",
          "ADX>25 & +DI>25 & +DI>-DI & MOM>0 -> enter; exit ADX>25 & -DI>25 & -DI>+DI & MOM<0; minimal_roi 1%; stoploss -25%. "
          "The purest 'take 1%, risk 25%' construction: expect a very high win rate and a ruinous tail.",
          flags=("roi_tail_profile",))
def ft_adx_momentum(df, p):
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff(); dn = -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0); mdm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_
    mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_
    a = I.adx(df); mom = c.diff(14)
    e = (a > 25) & (pdi > 25) & (pdi > mdi) & (mom > 0)
    x = (a > 25) & (mdi > 25) & (mdi > pdi) & (mom < 0)
    return e, x, ExitSpec(stop_loss_pct=p["stop_pct"], take_profit_pct=p["roi_pct"])


# ---------------------------------------------------------------- time-series momentum variants (added after the first pass; all reported)
@register("tsmom_vote", {"lookbacks": [(20, 60), (30, 90), (20, 60, 120)], "vol_stop_pct": [20.0, 30.0]},
          {"lookbacks": (30, 90), "vol_stop_pct": 25.0}, "TSMOM multi-horizon vote (Moskowitz et al.; Hurst, Ooi, Pedersen 2017)",
          "Long when the trailing return is positive on ALL listed horizons (days), evaluated once a day; exit when any turns negative. Disaster stop only.")
def tsmom_vote(df, p):
    c = df["close"]; bpd = _bars_per_day(df)
    moms = [c / c.shift(d * bpd) - 1 for d in p["lookbacks"]]
    daily = pd.Series(df.index.hour == 0, index=df.index) if bpd > 1 else pd.Series(True, index=df.index)
    all_up = pd.concat([m > 0 for m in moms], axis=1).all(axis=1)
    any_dn = pd.concat([m < 0 for m in moms], axis=1).any(axis=1)
    return all_up & daily, any_dn & daily, ExitSpec(stop_loss_pct=p["vol_stop_pct"])


@register("tsmom_trail", {"lookback_days": [30, 60], "trail_pct": [15.0, 25.0]}, {"lookback_days": 30, "trail_pct": 20.0},
          "TSMOM + trailing stop", "Daily TSMOM entry/exit plus a trailing stop from the high-water mark to cap give-back.")
def tsmom_trail(df, p):
    c = df["close"]; bpd = _bars_per_day(df); lb = p["lookback_days"] * bpd
    mom = c / c.shift(lb) - 1
    daily = pd.Series(df.index.hour == 0, index=df.index) if bpd > 1 else pd.Series(True, index=df.index)
    return (mom > 0) & daily, (mom < 0) & daily, ExitSpec(trailing_stop_pct=p["trail_pct"])


@register("tsmom_ema_filter", {"lookback_days": [30, 60], "ema_days": [50, 100], "vol_stop_pct": [25.0]},
          {"lookback_days": 30, "ema_days": 100, "vol_stop_pct": 25.0}, "TSMOM with long-term EMA regime filter",
          "Daily TSMOM, but only long while close is above a long EMA (regime filter); exit when momentum turns negative or close drops below the EMA.")
def tsmom_ema(df, p):
    c = df["close"]; bpd = _bars_per_day(df); lb = p["lookback_days"] * bpd
    mom = c / c.shift(lb) - 1; e = I.ema(c, p["ema_days"] * bpd)
    daily = pd.Series(df.index.hour == 0, index=df.index) if bpd > 1 else pd.Series(True, index=df.index)
    return (mom > 0) & (c > e) & daily, ((mom < 0) | (c < e)) & daily, ExitSpec(stop_loss_pct=p["vol_stop_pct"])
