"""Bias and robustness checks every candidate must pass before it is considered."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import run_backtest
from .strategies import REGISTRY


def lookahead_check(name: str, df: pd.DataFrame, params: dict | None = None, cuts=(0.6, 0.8)) -> dict:
    """Freqtrade-style lookahead test: signals computed on a truncated history must equal the signals
    computed on the full history for every bar before the cut. Any difference means the strategy peeks
    at future bars (centered windows, negative shifts, whole-series normalisation, ...)."""
    fn = REGISTRY[name]["fn"]; p = {**REGISTRY[name]["default"], **(params or {})}
    full_e, full_x, _ = fn(df, p)
    worst = 0
    for cut in cuts:
        n = int(len(df) * cut)
        e, x, _ = fn(df.iloc[:n], p)
        diff = int((e.fillna(False).to_numpy() != full_e.iloc[:n].fillna(False).to_numpy()).sum())
        if x is not None and full_x is not None:
            diff += int((x.fillna(False).to_numpy() != full_x.iloc[:n].fillna(False).to_numpy()).sum())
        worst = max(worst, diff)
    return {"lookahead_signal_mismatches": worst, "passed": worst == 0}


def cost_sensitivity(name: str, df: pd.DataFrame, params: dict, warmup: int, multipliers=(0.0, 1.0, 2.0),
                     fee_bps: float = 10.0, slippage_bps: float = 15.0) -> dict:
    fn = REGISTRY[name]["fn"]; p = {**REGISTRY[name]["default"], **params}
    e, x, spec = fn(df, p); e = e.copy(); e.iloc[:warmup] = False
    out = {}
    for m in multipliers:
        r = run_backtest(df, e, x, spec, fee_bps=fee_bps * m, slippage_bps=slippage_bps * m)
        out[f"cost_x{m:g}"] = {"net_return_pct": r.metrics["net_return_pct"], "profit_factor": r.metrics["profit_factor"], "trades": r.metrics["trades"]}
    return out


def delayed_entry_check(name: str, df: pd.DataFrame, params: dict, warmup: int, fee_bps: float, slippage_bps: float) -> dict:
    """Execute every signal one bar later than modelled. A real edge degrades gracefully; a timing
    artefact (or hidden lookahead) collapses."""
    fn = REGISTRY[name]["fn"]; p = {**REGISTRY[name]["default"], **params}
    e, x, spec = fn(df, p); e = e.copy(); e.iloc[:warmup] = False
    base = run_backtest(df, e, x, spec, fee_bps=fee_bps, slippage_bps=slippage_bps).metrics
    late = run_backtest(df, e.shift(1).fillna(False).astype(bool), x.shift(1).fillna(False).astype(bool) if x is not None else None,
                        spec, fee_bps=fee_bps, slippage_bps=slippage_bps).metrics
    return {"base_return_pct": base["net_return_pct"], "delayed_1bar_return_pct": late["net_return_pct"],
            "retained_fraction": round(late["net_return_pct"] / base["net_return_pct"], 2) if base["net_return_pct"] else None}


def dca_martingale_demo(df: pd.DataFrame, step_pct: float = 3.0, max_adds: int = 5, tp_pct: float = 1.0,
                        fee_bps: float = 10.0, slippage_bps: float = 15.0, initial: float = 10_000.0) -> dict:
    """The classic '95% win rate' construction: buy, double down every -step%, take +tp% on the average
    price, never stop out. Reported to show why win rate alone is meaningless. REJECTED by policy."""
    c = df["close"].to_numpy(); o = df["open"].to_numpy(); lo = df["low"].to_numpy()
    cost = (fee_bps + slippage_bps) / 1e4
    cash = initial; qty = 0.0; avg = 0.0; adds = 0; unit = initial / (2 ** max_adds)  # so the full ladder is fundable
    wins = losses = 0; eq = np.empty(len(c)); worst_dd = 0.0; peak = initial; max_exposure = 0.0; liquidated = False
    for i in range(1, len(c)):
        if qty == 0.0:
            px = o[i] * (1 + cost); q = unit / px; cash -= unit; qty = q; avg = px; adds = 0
        else:
            if lo[i] <= avg * (1 - step_pct / 100) and adds < max_adds:
                px = avg * (1 - step_pct / 100) * (1 + cost); notional = unit * (2 ** (adds + 1))
                if cash >= notional:
                    q = notional / px; avg = (avg * qty + px * q) / (qty + q); qty += q; cash -= notional; adds += 1
            if df["high"].iloc[i] >= avg * (1 + tp_pct / 100):
                px = avg * (1 + tp_pct / 100) * (1 - cost); pnl = (px - avg) * qty; cash += qty * px; qty = 0.0
                wins += 1 if pnl > 0 else 0; losses += 0 if pnl > 0 else 1
        e = cash + qty * c[i]; eq[i] = e; peak = max(peak, e); worst_dd = min(worst_dd, (e - peak) / peak * 100)
        max_exposure = max(max_exposure, qty * c[i] / e if e > 0 else 0)
        if e <= 0:
            liquidated = True; break
    eq[0] = initial
    open_pnl_pct = ((c[-1] - avg) / avg * 100) if qty else 0.0
    return {"closed_trades": wins + losses, "win_rate_pct": round(wins / (wins + losses) * 100, 1) if wins + losses else 0.0,
            "net_return_pct": round((eq[min(i, len(c) - 1)] / initial - 1) * 100, 2), "max_drawdown_pct": round(worst_dd, 2),
            "open_position_unrealised_pct": round(open_pnl_pct, 2), "max_exposure_x_equity": round(max_exposure, 2),
            "liquidated": liquidated, "verdict": "REJECTED: martingale / unlimited averaging down hides catastrophic tail risk"}
