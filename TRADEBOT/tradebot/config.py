"""Configuration: TOML file + environment overrides, validated with pydantic."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RiskLimits(BaseModel):
    """Hard limits enforced by the independent RiskEngine. Claude cannot change these."""

    max_position_pct: float = Field(10.0, gt=0, le=100, description="Max single position, % of equity")
    max_total_exposure_pct: float = Field(50.0, gt=0, le=100, description="Max sum of positions, % of equity")
    max_open_positions: int = Field(5, ge=1)
    max_order_notional_usd: float = Field(500.0, gt=0)
    min_order_notional_usd: float = Field(5.0, gt=0)
    max_daily_loss_pct: float = Field(3.0, gt=0, le=100, description="Daily loss that halts new entries (see daily_loss_action)")
    daily_loss_action: Literal["halt_day", "kill"] = Field("halt_day", description="halt_day: flatten and pause until next UTC day; kill: trip the kill switch")
    max_drawdown_pct: float = Field(10.0, gt=0, le=100, description="Peak-to-trough drawdown that trips the kill switch")
    max_trades_per_day: int = Field(30, ge=1)
    max_slippage_bps: int = Field(100, ge=1)
    min_liquidity_usd: float = Field(250_000.0, ge=0)
    min_volume_24h_usd: float = Field(500_000.0, ge=0)
    max_consecutive_losses: int = Field(5, ge=1, description="Pause new entries after this many losses in a row")
    loss_cooldown_sec: float = Field(1800.0, ge=0, description="No new entries for this long after any loss")
    loss_streak_pause_sec: float = Field(14400.0, ge=0, description="After max_consecutive_losses, pause entries this long after the last loss")
    stale_data_sec: float = Field(300.0, gt=0, description="Trip kill switch if no fresh data for this long")
    max_consecutive_errors: int = Field(5, ge=1)
    allowlist: list[str] = Field(default_factory=list, description="If non-empty, only these symbols may be traded")
    denylist: list[str] = Field(default_factory=list)
    approval_ttl_sec: float = Field(30.0, gt=0)


class StrategyConfig(BaseModel):
    name: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class ClaudeConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    research_enabled: bool = True
    research_interval_hours: float = 6.0
    vet_signals: bool = True
    daily_report: bool = True
    fail_open: bool = Field(True, description="If Claude is unreachable, proceed on deterministic strategies")
    max_tokens: int = 8000
    # rough pricing for net-profitability accounting (USD per 1M tokens)
    input_price_per_mtok: float = 4.0
    output_price_per_mtok: float = 20.0


class DataConfig(BaseModel):
    provider: Literal["live", "synthetic", "csv"] = "live"
    candle_interval: str = "1h"
    candle_history: int = 400
    csv_dir: str = "data/csv"
    synthetic_seed: int = 42
    synthetic_symbols: list[str] = Field(default_factory=lambda: ["SOL", "JUP", "RAY", "BONK", "WIF"])
    jupiter_base_url: str = "https://lite-api.jup.ag"
    jupiter_api_key: str = ""
    binance_base_url: str = "https://api.binance.com"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    request_timeout_sec: float = 15.0


class UniverseConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["SOL", "JUP", "RAY", "BONK", "WIF", "PYTH", "JTO"])
    auto_scan: bool = True
    max_symbols: int = 12
    quote_symbol: str = "USDC"
    # Known mints for live trading; auto-scan may add verified Jupiter tokens.
    mints: dict[str, str] = Field(default_factory=lambda: {
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "SOL": "So11111111111111111111111111111111111111112",
        "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
        "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    })
    decimals: dict[str, int] = Field(default_factory=lambda: {
        "USDC": 6, "SOL": 9, "JUP": 6, "RAY": 6, "BONK": 5, "WIF": 6, "PYTH": 6, "JTO": 9,
    })


class ExecutionConfig(BaseModel):
    paper_fee_bps: float = 30.0            # Jupiter route + Solana fees approximated
    paper_slippage_bps: float = 15.0
    live_slippage_bps: int = 50
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_swap_base_url: str = "https://lite-api.jup.ag/swap/v1"
    confirm_timeout_sec: float = 60.0
    priority_fee_lamports: int = 100_000


class EngineConfig(BaseModel):
    scan_interval_sec: float = 60.0
    equity_snapshot_interval_sec: float = 300.0
    flatten_on_kill: bool = True
    flatten_on_daily_halt: bool = True
    min_paper_trades_before_live: int = 30
    min_paper_days_before_live: float = 14.0
    live_confirm_phrase: str = "I UNDERSTAND THIS TRADES REAL MONEY WITHOUT APPROVAL"


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    initial_capital_usd: float = 1000.0
    data_dir: str = "data"
    log_level: str = "INFO"
    risk: RiskLimits = Field(default_factory=RiskLimits)
    strategies: list[StrategyConfig] = Field(default_factory=lambda: [
        StrategyConfig(name="momentum"),
        StrategyConfig(name="mean_reversion"),
        StrategyConfig(name="breakout"),
    ])
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)

    @field_validator("mode", mode="before")
    @classmethod
    def _env_mode_override(cls, v: Any) -> Any:
        # Environment can force paper, never force live.
        if os.environ.get("TRADEBOT_FORCE_PAPER") == "1":
            return "paper"
        return v

    # ---- derived paths -------------------------------------------------
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "ledger.sqlite"

    @property
    def kill_file(self) -> Path:
        return self.data_path / "KILL"

    @property
    def armed_file(self) -> Path:
        return self.data_path / "LIVE_ARMED"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from a TOML file (if given/exists) and apply env overrides."""
    raw: dict[str, Any] = {}
    cfg_path = Path(path) if path else Path(os.environ.get("TRADEBOT_CONFIG", "config.toml"))
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    settings = Settings.model_validate(raw)
    if os.environ.get("TRADEBOT_DATA_DIR"):
        settings.data_dir = os.environ["TRADEBOT_DATA_DIR"]
    settings.data_path.mkdir(parents=True, exist_ok=True)
    return settings


EXAMPLE_CONFIG = """# tradebot configuration. Paper trading is the default and the safe choice.
mode = "paper"               # "live" requires arming; see README "Going live"
initial_capital_usd = 1000.0
data_dir = "data"
log_level = "INFO"

[risk]                       # Hard limits. Enforced by the independent risk engine.
max_position_pct = 10.0
max_total_exposure_pct = 50.0
max_open_positions = 5
max_order_notional_usd = 500.0
min_order_notional_usd = 5.0
max_daily_loss_pct = 3.0      # flatten + no new entries until next UTC day ("halt_day") or "kill"
daily_loss_action = "halt_day"
max_drawdown_pct = 10.0       # trips the kill switch
max_trades_per_day = 30
max_slippage_bps = 100
min_liquidity_usd = 250000.0
min_volume_24h_usd = 500000.0
max_consecutive_losses = 5
loss_cooldown_sec = 1800
loss_streak_pause_sec = 14400  # 4h pause after a losing streak, then entries resume
stale_data_sec = 300
max_consecutive_errors = 5
allowlist = []
denylist = []

[[strategies]]
name = "momentum"
enabled = true
[strategies.params]
fast = 12
slow = 26
trend = 100
atr_mult = 2.5

[[strategies]]
name = "mean_reversion"
enabled = true
[strategies.params]
rsi_period = 14
rsi_buy = 30
rsi_exit = 55
bb_period = 20
bb_std = 2.0

[[strategies]]
name = "breakout"
enabled = true
[strategies.params]
channel = 20
volume_mult = 1.5
atr_mult = 2.0

[claude]
enabled = true
model = "claude-opus-5-5"
effort = "medium"
research_enabled = true
research_interval_hours = 6
vet_signals = true
daily_report = true
fail_open = true

[data]
provider = "live"            # "synthetic" or "csv" for offline testing
candle_interval = "1h"
candle_history = 400

[universe]
symbols = ["SOL", "JUP", "RAY", "BONK", "WIF", "PYTH", "JTO"]
auto_scan = true
max_symbols = 12

[execution]
paper_fee_bps = 30
paper_slippage_bps = 15
live_slippage_bps = 50
rpc_url = "https://api.mainnet-beta.solana.com"

[engine]
scan_interval_sec = 60
flatten_on_kill = true
flatten_on_daily_halt = true
min_paper_trades_before_live = 30
min_paper_days_before_live = 14
"""
