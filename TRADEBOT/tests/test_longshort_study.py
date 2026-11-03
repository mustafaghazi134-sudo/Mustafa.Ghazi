"""Accounting checks for the pre-registered long/short study (research_lab/longshort_study.py)."""
import numpy as np
import pandas as pd

from research_lab import longshort_study as L


def _df(prices):
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="1D", tz="UTC")
    p = np.asarray(prices, float)
    return pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": 1.0}, index=idx)


def test_short_profits_when_price_falls_and_pays_costs_on_both_sides_of_a_flip():
    df = _df([100, 100, 90, 81, 81, 81])
    sig = pd.Series([-1, -1, -1, 1, 1, 1], index=df.index)
    fb = np.zeros(len(df))
    out = L.simulate(df, sig, fb, cost_bps=10, delay=0, funding_mode="base")
    # held from bar 1: short over open1->open2 (100->90: +10%), open2->open3 (90->81: +10%), flip to long at bar 4
    assert out["held"].tolist() == [0, -1, -1, -1, 1, 1]
    assert abs(out["gross"].iloc[1] - 0.10) < 1e-12 and abs(out["gross"].iloc[2] - 0.10) < 1e-12
    assert abs(out["cost"].iloc[1] + 0.001) < 1e-12           # open short: 1 side
    assert abs(out["cost"].iloc[4] + 0.002) < 1e-12           # flip: close short + open long
    assert abs(out["long"].sum() + out["short"].sum() - out["net"].sum()) < 1e-12


def test_funding_sign_long_pays_short_receives_and_stress_makes_both_pay():
    df = _df([100] * 4)
    fb = np.full(4, 0.001)
    long_ = L.simulate(df, pd.Series([1] * 4, index=df.index), fb, 0, 0, "base")
    short = L.simulate(df, pd.Series([-1] * 4, index=df.index), fb, 0, 0, "base")
    stress = L.simulate(df, pd.Series([-1] * 4, index=df.index), fb, 0, 0, "stress")
    assert long_["funding"].iloc[1] < 0 < short["funding"].iloc[1]
    assert stress["funding"].iloc[1] < 0


def test_delay_shifts_position_by_one_more_bar():
    df = _df([100] * 5)
    sig = pd.Series([1, 0, 0, 0, 0], index=df.index)
    assert L.simulate(df, sig, np.zeros(5), 0, 0, None)["held"].tolist() == [0, 1, 0, 0, 0]
    assert L.simulate(df, sig, np.zeros(5), 0, 1, None)["held"].tolist() == [0, 0, 1, 0, 0]


def test_candidates_use_no_future_data():
    rng = np.random.default_rng(1)
    p = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 900)))
    idx = pd.date_range("2022-01-01", periods=900, freq="1D", tz="UTC")
    df = pd.DataFrame({"open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1.0}, index=idx)
    for name, (fn, _) in L.CANDIDATES.items():
        assert L.lookahead_test(fn, df, rng, n=20)["pass"], name


def test_episodes_and_funding_bucketing():
    assert L.episodes(np.array([0, 1, 1, -1, 0, -1])) == [(1, 3, 1), (3, 4, -1), (5, 6, -1)]
    bars = pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC")
    f = pd.Series([0.1, 0.2, 0.3], index=pd.to_datetime(["2024-01-01 00:00", "2024-01-01 16:00", "2024-01-03 08:00"], utc=True))
    assert L.funding_per_bar(bars, f).tolist() == [0.30000000000000004, 0.0, 0.3]
