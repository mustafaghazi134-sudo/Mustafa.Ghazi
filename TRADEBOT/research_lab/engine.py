"""Event-driven long-only backtest engine on an OHLCV DataFrame with realistic execution.

Execution model (conservative, mirrors Freqtrade's documented assumptions):
  * A signal computed on bar i (its close) is filled at bar i+1 OPEN, plus slippage.
  * Stop-loss is checked against the bar LOW first; if the bar gapped below the stop, fill at the open.
  * Take-profit / ROI targets are checked against the bar HIGH only after the stop check (pessimistic
    ordering: when both could have triggered in one bar, the loss is taken).
  * Fees and slippage are charged on both sides. Position size is a fixed fraction of current equity.
  * No pyramiding and no averaging down: one position per asset. (Martingale/DCA candidates are
    implemented separately and only to demonstrate their hidden tail risk.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class ExitSpec:
    stop_loss_pct: float | None = None          # e.g. 5.0 -> exit if price <= entry * 0.95
    take_profit_pct: float | None = None        # e.g. 3.0
    trailing_stop_pct: float | None = None      # trail from high-water mark
    atr_stop_mult: float | None = None          # stop = entry - mult * ATR(14) at entry
    atr_tp_mult: float | None = None
    minimal_roi: dict[int, float] | None = None # Freqtrade-style: {minutes_held: min_profit_pct}; exit when profit >= threshold
    max_hold_bars: int | None = None


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float            # net of fees and slippage, in quote currency
    ret_pct: float        # net return on the position notional
    reason: str
    bars_held: int


@dataclass
class Result:
    trades: list[Trade]
    equity: pd.Series                      # equity per bar (quote currency)
    metrics: dict = field(default_factory=dict)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def run_backtest(df: pd.DataFrame, entries: pd.Series, exits: pd.Series | None, spec: ExitSpec,
                 fee_bps: float = 10.0, slippage_bps: float = 15.0, initial: float = 10_000.0,
                 size_frac: float = 1.0, bar_minutes: int = 60) -> Result:
    """entries/exits are boolean Series aligned to df (True on the signal bar; acted on at the next open)."""
    o, h, l, c = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    idx = df.index
    n = len(df)
    ent = entries.fillna(False).to_numpy(dtype=bool)
    exi = exits.fillna(False).to_numpy(dtype=bool) if exits is not None else np.zeros(n, dtype=bool)
    atr = _atr(df).to_numpy() if (spec.atr_stop_mult or spec.atr_tp_mult) else None
    cost = (fee_bps + slippage_bps) / 1e4

    cash = initial
    qty = 0.0
    entry_px = entry_i = 0
    stop = tp = None
    hwm = 0.0
    trades: list[Trade] = []
    equity = np.empty(n)
    pending_entry = False
    pending_exit = False
    pending_reason = ""

    def close_pos(i: int, px: float, reason: str) -> None:
        nonlocal cash, qty
        proceeds = qty * px * (1 - cost)
        pnl = proceeds - qty * entry_px * (1 + cost)
        trades.append(Trade(entry_i, i, idx[entry_i], idx[i], entry_px, px, qty, pnl,
                            pnl / (qty * entry_px * (1 + cost)) * 100, reason, i - entry_i))
        cash += proceeds
        qty = 0.0

    for i in range(n):
        if qty == 0.0 and pending_entry:
            px = o[i]
            notional = cash * size_frac
            qty = notional / (px * (1 + cost))
            cash -= notional
            entry_px, entry_i, hwm = px, i, px
            stop = tp = None
            if spec.stop_loss_pct is not None:
                stop = px * (1 - spec.stop_loss_pct / 100)
            if spec.atr_stop_mult is not None and atr is not None and not math.isnan(atr[i - 1]):
                s2 = px - spec.atr_stop_mult * atr[i - 1]
                stop = max(stop, s2) if stop is not None else s2
            if spec.take_profit_pct is not None:
                tp = px * (1 + spec.take_profit_pct / 100)
            if spec.atr_tp_mult is not None and atr is not None and not math.isnan(atr[i - 1]):
                tp = px + spec.atr_tp_mult * atr[i - 1]
            pending_entry = False
            pending_exit = False
        elif qty > 0.0 and pending_exit:
            close_pos(i, o[i], pending_reason)
            pending_exit = False

        if qty > 0.0:
            # trailing stop from high-water mark (updated with this bar's high after the stop check below)
            eff_stop = stop
            if spec.trailing_stop_pct is not None:
                trail = hwm * (1 - spec.trailing_stop_pct / 100)
                eff_stop = max(eff_stop, trail) if eff_stop is not None else trail
            if eff_stop is not None and l[i] <= eff_stop:
                close_pos(i, min(o[i], eff_stop) if o[i] < eff_stop else eff_stop, "stop")
            else:
                hwm = max(hwm, h[i])
                exit_hit = False
                if tp is not None and h[i] >= tp:
                    close_pos(i, max(o[i], tp) if o[i] > tp else tp, "take_profit")
                    exit_hit = True
                if not exit_hit and spec.minimal_roi:
                    held_min = (i - entry_i) * bar_minutes
                    thr = None
                    for m in sorted(spec.minimal_roi):
                        if held_min >= m:
                            thr = spec.minimal_roi[m]
                    if thr is not None and (c[i] / entry_px - 1) * 100 >= thr:
                        close_pos(i, c[i], "roi")
                        exit_hit = True
                if not exit_hit and spec.max_hold_bars is not None and i - entry_i >= spec.max_hold_bars:
                    close_pos(i, c[i], "time")
                    exit_hit = True
                if not exit_hit and exi[i]:
                    pending_exit, pending_reason = True, "signal"
        if qty == 0.0 and ent[i] and i < n - 1:
            pending_entry = True
        equity[i] = cash + qty * c[i]

    if qty > 0.0:
        close_pos(n - 1, c[-1], "end")
        equity[-1] = cash
    res = Result(trades, pd.Series(equity, index=idx))
    res.metrics = compute_metrics(res, initial, bar_minutes)
    return res


def compute_metrics(res: Result, initial: float, bar_minutes: int) -> dict:
    eq = res.equity
    t = res.trades
    final = float(eq.iloc[-1]) if len(eq) else initial
    years = max((eq.index[-1] - eq.index[0]).total_seconds() / (365.25 * 86400), 1e-9) if len(eq) > 1 else 1e-9
    peak = eq.cummax()
    dd = ((eq - peak) / peak).min() * 100 if len(eq) else 0.0
    rets = eq.pct_change().dropna()
    per_year = (365.25 * 24 * 60) / bar_minutes
    sharpe = float(rets.mean() / rets.std() * math.sqrt(per_year)) if len(rets) > 2 and rets.std() > 0 else 0.0
    wins = [x.pnl for x in t if x.pnl > 0]
    losses = [x.pnl for x in t if x.pnl <= 0]
    gw, gl = sum(wins), -sum(losses)
    streak = cur = 0
    for x in t:
        cur = cur + 1 if x.pnl <= 0 else 0
        streak = max(streak, cur)
    exposure = float((eq != eq.shift(1)).mean()) if len(eq) else 0.0
    return {
        "trades": len(t),
        "win_rate_pct": round(len(wins) / len(t) * 100, 1) if t else 0.0,
        "net_profit": round(final - initial, 2),
        "net_return_pct": round((final / initial - 1) * 100, 2),
        "cagr_pct": round(((final / initial) ** (1 / years) - 1) * 100, 2) if final > 0 else -100.0,
        "profit_factor": round(gw / gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0),
        "max_drawdown_pct": round(float(dd), 2),
        "avg_win": round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "avg_win_pct": round(float(np.mean([x.ret_pct for x in t if x.pnl > 0])), 2) if wins else 0.0,
        "avg_loss_pct": round(float(np.mean([x.ret_pct for x in t if x.pnl <= 0])), 2) if losses else 0.0,
        "worst_trade_pct": round(min((x.ret_pct for x in t), default=0.0), 2),
        "expectancy_pct": round(float(np.mean([x.ret_pct for x in t])), 3) if t else 0.0,
        "sharpe": round(sharpe, 2),
        "calmar": round(((final / initial) ** (1 / years) - 1) * 100 / abs(dd), 2) if dd < 0 and final > 0 else 0.0,
        "avg_bars_held": round(float(np.mean([x.bars_held for x in t])), 1) if t else 0.0,
        "longest_losing_streak": streak,
        "avg_win_over_avg_loss": round((gw / len(wins)) / (gl / len(losses)), 2) if wins and losses else 0.0,
        "years": round(years, 2),
    }
