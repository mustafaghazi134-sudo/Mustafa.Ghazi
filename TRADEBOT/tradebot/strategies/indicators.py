"""Vectorised indicators on numpy arrays. All return arrays aligned with input (NaN where undefined)."""
from __future__ import annotations

import numpy as np

from ..models import Candle


def closes(c: list[Candle]) -> np.ndarray:
    return np.array([x.close for x in c], dtype=float)


def highs(c: list[Candle]) -> np.ndarray:
    return np.array([x.high for x in c], dtype=float)


def lows(c: list[Candle]) -> np.ndarray:
    return np.array([x.low for x in c], dtype=float)


def volumes(c: list[Candle]) -> np.ndarray:
    return np.array([x.volume for x in c], dtype=float)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) >= n and n > 0:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < n or n <= 0:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) <= n:
        return out
    d = np.diff(x)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = gain[:n].mean()
    al = loss[:n].mean()
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(x)):
        ag = (ag * (n - 1) + gain[i - 1]) / n
        al = (al * (n - 1) + loss[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def atr(c: list[Candle], n: int = 14) -> np.ndarray:
    h, lo, cl = highs(c), lows(c), closes(c)
    out = np.full_like(cl, np.nan, dtype=float)
    if len(cl) <= n:
        return out
    prev_close = np.roll(cl, 1)
    tr = np.maximum.reduce([h - lo, np.abs(h - prev_close), np.abs(lo - prev_close)])
    tr[0] = h[0] - lo[0]
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(cl)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def bollinger(x: np.ndarray, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(x, n)
    std = np.full_like(x, np.nan, dtype=float)
    if len(x) >= n:
        windows = np.lib.stride_tricks.sliding_window_view(x, n)
        std[n - 1:] = windows.std(axis=1)
    return mid - k * std, mid, mid + k * std


def donchian(c: list[Candle], n: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """Upper/lower channel using the *previous* n bars (excludes the current bar)."""
    h, lo = highs(c), lows(c)
    up = np.full_like(h, np.nan, dtype=float)
    dn = np.full_like(h, np.nan, dtype=float)
    if len(h) > n:
        up[n:] = np.lib.stride_tricks.sliding_window_view(h[:-1], n).max(axis=1)
        dn[n:] = np.lib.stride_tricks.sliding_window_view(lo[:-1], n).min(axis=1)
    return up, dn
