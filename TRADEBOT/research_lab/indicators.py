from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(up.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bbands(s: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(s, n)
    sd = s.rolling(n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def donchian(df: pd.DataFrame, n: int):
    """Channel of the PREVIOUS n bars (shifted by one, so the current bar cannot see itself)."""
    return df["high"].rolling(n).max().shift(1), df["low"].rolling(n).min().shift(1)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(s, fast) - ema(s, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return line, signal, line - signal


def tema(s: pd.Series, n: int) -> pd.Series:
    e1 = ema(s, n); e2 = ema(e1, n); e3 = ema(e2, n)
    return 3 * e1 - 3 * e2 + e3


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = [df["open"].iloc[0]]
    hc = ha["close"].to_numpy()
    for i in range(1, len(df)):
        ha_open.append((ha_open[-1] + hc[i - 1]) / 2)
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha["open"], ha["close"]], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha["open"], ha["close"]], axis=1).min(axis=1)
    return ha


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 (uptrend) / -1 (downtrend) per bar, computed causally."""
    a = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    c = df["close"].to_numpy()
    n = len(df)
    up = hl2 - mult * a
    dn = hl2 + mult * a
    trend = np.ones(n)
    fu, fd = up.copy(), dn.copy()
    for i in range(1, n):
        if np.isnan(a[i]):
            continue
        if np.isnan(fu[i - 1]) or np.isnan(fd[i - 1]):   # first valid bar: initialise the final bands
            fu[i], fd[i] = up[i], dn[i]
            continue
        fu[i] = up[i] if (up[i] > fu[i - 1] or c[i - 1] < fu[i - 1]) else fu[i - 1]
        fd[i] = dn[i] if (dn[i] < fd[i - 1] or c[i - 1] > fd[i - 1]) else fd[i - 1]
        if trend[i - 1] == 1:
            trend[i] = -1 if c[i] < fu[i] else 1
        else:
            trend[i] = 1 if c[i] > fd[i] else -1
    out = pd.Series(trend, index=df.index)
    out[np.isnan(a)] = np.nan
    return out


def cross_up(a: pd.Series, b) -> pd.Series:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a: pd.Series, b) -> pd.Series:
    b = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return (a < b) & (a.shift(1) >= b.shift(1))


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's ADX (causal)."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff(); dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
