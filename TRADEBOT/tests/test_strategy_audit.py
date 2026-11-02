"""Leakage checks for the strategy-audit families and the evidence collector.

Truncation test (as in Freqtrade's lookahead-analysis): a signal at bar t must be the same whether it is computed on
the full history or on history that ends at t. Any difference means the signal used future bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pandas")

from research_lab.forward_evidence import collect  # noqa: E402
from research_lab.strategy_audit import FAMILIES, run  # noqa: E402


def _bars(n: int, freq: str = "4h", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.004, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.004, n))
    idx = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx)


@pytest.mark.parametrize("family", list(FAMILIES))
def test_signals_do_not_look_ahead(family):
    fn, grid, _ = FAMILIES[family]
    df = _bars(1400)
    for p in grid:
        full_en, full_ex, _ = fn(df, p)
        for cut in (700, 950, 1200, 1399):
            en, ex, _ = fn(df.iloc[: cut + 1], p)
            assert bool(en.iloc[-1]) == bool(full_en.iloc[cut]), (family, p, cut, "entry")
            assert bool(ex.iloc[-1]) == bool(full_ex.iloc[cut]), (family, p, cut, "exit")


def test_fills_happen_after_the_signal_bar():
    fn, grid, _ = FAMILIES["trend_ema"]
    df = _bars(1400)
    res = run(fn, df, grid[0], 26.0, 1, 240, 250)
    en, _, _ = fn(df, grid[0])
    sig_bars = set(np.where(en.fillna(False).to_numpy())[0])
    assert res.trades, "expected trades on a random walk"
    for t in res.trades:
        # signal at bar s (close) + 1 bar of delay -> earliest fill at the open of bar s + 2
        assert any(s + 2 <= t.entry_i for s in sig_bars), t
        assert t.entry_price == pytest.approx(df["open"].iloc[t.entry_i])


def test_higher_cost_never_improves_a_trade():
    fn, grid, _ = FAMILIES["breakout"]
    df = _bars(1400)
    lo = {t.entry_i: t.ret_pct for t in run(fn, df, grid[0], 11.0, 1, 240, 250).trades}
    hi = {t.entry_i: t.ret_pct for t in run(fn, df, grid[0], 51.0, 1, 240, 250).trades}
    common = set(lo) & set(hi)
    assert common
    assert all(hi[i] < lo[i] for i in common)


def test_evidence_collector_reports_no_data_honestly(tmp_path):
    out = collect(tmp_path)
    assert out["evidence_class"] == "NO DATA"
    assert out["closed_trades"] == 0 and "expectancy_usd" not in out
