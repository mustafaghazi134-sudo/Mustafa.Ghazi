import pytest

from tradebot.config import RiskLimits
from tradebot.risk.engine import RiskEngine
from tradebot.risk.kill_switch import KillSwitch


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "KILL")


@pytest.fixture
def limits():
    return RiskLimits(min_liquidity_usd=10_000, min_volume_24h_usd=10_000, max_position_pct=10, max_total_exposure_pct=50,
                      max_open_positions=3, max_order_notional_usd=500, min_order_notional_usd=5, max_daily_loss_pct=3,
                      max_drawdown_pct=10, max_trades_per_day=5, max_slippage_bps=100, loss_cooldown_sec=600,
                      max_consecutive_losses=3, stale_data_sec=300, approval_ttl_sec=30)


@pytest.fixture
def risk(limits, kill, clock):
    return RiskEngine(limits, kill, clock=clock)


@pytest.fixture
def legacy_switch_on(monkeypatch):
    """TEST-ONLY: turns the build switch ON for the duration of ONE test so the quarantined legacy broker's own logic
    (gate, retries, resend behaviour) can still be exercised with mocked network and a throwaway key. It is restored
    automatically. Tests that prove the lock (tests/test_legacy_broker_quarantine.py) never use it."""
    from tradebot.execution import submission
    monkeypatch.setattr(submission, "MAINNET_SUBMISSION_ENABLED", True)

