"""Pure-Python indicators (no NumPy): the live bot must run on locked-down Windows hosts where
Application Control blocks unsigned native extensions such as numpy.random._pcg64.

All functions return lists of floats aligned with the input; undefined values are math.nan.
Series are short (a few hundred to a few thousand bars), so O(n) and O(n*window) loops are fine.
"""
from __future__ import annotations

import math
from collections import deque

from ..models import Candle

NAN = math.nan


def isnan(*xs) -> bool:
    """True if any argument (or any element of a list/tuple argument) is NaN."""
    for x in xs:
        if isinstance(x, (list, tuple)):
            if any(math.isnan(v) for v in x):
                return True
        elif math.isnan(x):
            return True
    return False


def closes(c: list[Candle]) -> list[float]:
    return [x.close for x in c]


def highs(c: list[Candle]) -> list[float]:
    return [x.high for x in c]


def lows(c: list[Candle]) -> list[float]:
    return [x.low for x in c]


def volumes(c: list[Candle]) -> list[float]:
    return [x.volume for x in c]


def sma(x: list[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    if n <= 0 or len(x) < n:
        return out
    s = sum(x[:n])
    out[n - 1] = s / n
    for i in range(n, len(x)):
        s += x[i] - x[i - n]
        out[i] = s / n
    return out


def ema(x: list[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    if n <= 0 or len(x) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(x[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(x)):
        prev = x[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(x: list[float], n: int = 14) -> list[float]:
    out = [NAN] * len(x)
    if len(x) <= n:
        return out
    gains = [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    losses = [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(x)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def atr(c: list[Candle], n: int = 14) -> list[float]:
    out = [NAN] * len(c)
    if len(c) <= n:
        return out
    tr = [c[0].high - c[0].low]
    for i in range(1, len(c)):
        pc = c[i - 1].close
        tr.append(max(c[i].high - c[i].low, abs(c[i].high - pc), abs(c[i].low - pc)))
    prev = sum(tr[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(c)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def bollinger(x: list[float], n: int = 20, k: float = 2.0) -> tuple[list[float], list[float], list[float]]:
    mid = sma(x, n)
    lower = [NAN] * len(x)
    upper = [NAN] * len(x)
    if len(x) >= n:
        s = sum(x[:n]); s2 = sum(v * v for v in x[:n])
        for i in range(n - 1, len(x)):
            if i >= n:
                s += x[i] - x[i - n]
                s2 += x[i] * x[i] - x[i - n] * x[i - n]
            var = max(s2 / n - (s / n) ** 2, 0.0)
            sd = math.sqrt(var)
            lower[i] = mid[i] - k * sd
            upper[i] = mid[i] + k * sd
    return lower, mid, upper


def _rolling_extreme(vals: list[float], n: int, use_max: bool) -> list[float]:
    """Rolling max/min of the PREVIOUS n values (excludes the current index), monotonic deque, O(n)."""
    out = [NAN] * len(vals)
    dq: deque[int] = deque()
    for i in range(len(vals)):
        if i >= n:
            while dq and dq[0] <= i - n - 1:
                dq.popleft()
            out[i] = vals[dq[0]]
        j = i  # push current value for future windows
        while dq and ((vals[dq[-1]] <= vals[j]) if use_max else (vals[dq[-1]] >= vals[j])):
            dq.pop()
        dq.append(j)
    return out


def donchian(c: list[Candle], n: int = 20) -> tuple[list[float], list[float]]:
    """Upper/lower channel using the *previous* n bars (excludes the current bar)."""
    return _rolling_extreme(highs(c), n, True), _rolling_extreme(lows(c), n, False)
