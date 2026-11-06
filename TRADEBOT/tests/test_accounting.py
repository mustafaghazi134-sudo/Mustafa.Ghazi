import pytest

from tradebot.models import Fill, Side, Signal
from tradebot.portfolio.accounting import PortfolioBook
from tradebot.portfolio.ledger import Ledger
from tradebot.portfolio.reporting import performance_report


def test_round_trip_pnl_and_persistence(tmp_path, clock):
    ledger = Ledger(tmp_path / "l.sqlite", mode="paper")
    book = PortfolioBook(ledger, 1000.0, clock)
    sig = Signal("SOL", Side.BUY, "momentum", 0.8, "r", 100.0, stop_loss=90.0, take_profit=120.0)
    buy = Fill(order_id="o1", symbol="SOL", side=Side.BUY, qty=0.997, price=100.0, notional_usd=100.0, fee_usd=0.30, ts=clock())
    book.apply_fill(buy, signal=sig)
    assert book.cash == pytest.approx(900.0)
    assert book.positions["SOL"].stop_loss == 90.0
    clock.advance(3600)
    sell = Fill(order_id="o2", symbol="SOL", side=Side.SELL, qty=0.997, price=110.0, notional_usd=0.997 * 110, fee_usd=0.33, ts=clock())
    trade = book.apply_fill(sell, exit_reason="take_profit")
    assert trade is not None and "SOL" not in book.positions
    expected = 0.997 * 110 - 0.33 - 0.997 * 100 - 0.30
    assert trade.pnl_usd == pytest.approx(expected)
    assert book.cash == pytest.approx(900 + 0.997 * 110 - 0.33)

    # reload from the ledger: state survives restarts
    book2 = PortfolioBook(Ledger(tmp_path / "l.sqlite", mode="paper"), 1000.0, clock)
    assert book2.cash == pytest.approx(book.cash) and not book2.positions

    rep = performance_report(ledger, {})
    assert rep.trades == 1 and rep.realized_pnl == pytest.approx(expected)
    assert rep.net_profit == pytest.approx(expected)


def test_api_cost_reduces_net_profit(tmp_path, clock):
    ledger = Ledger(tmp_path / "l.sqlite", mode="paper")
    PortfolioBook(ledger, 1000.0, clock)
    ledger.add_api_cost("m", "research", 10_000, 1_000, 0.06)
    rep = performance_report(ledger, {})
    assert rep.api_cost == pytest.approx(0.06) and rep.net_profit == pytest.approx(-0.06)


def test_day_rollover_and_peak(tmp_path, clock):
    ledger = Ledger(tmp_path / "l.sqlite", mode="paper")
    book = PortfolioBook(ledger, 1000.0, clock)
    book.cash = 1100.0
    book.snapshot({})
    assert book.peak_equity == pytest.approx(1100.0)
    clock.advance(86400)
    book.snapshot({})
    assert book.day_start_equity == pytest.approx(1100.0)
