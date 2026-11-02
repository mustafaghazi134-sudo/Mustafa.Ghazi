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
    research_interval_hours: float = 168.0      # weekly: research is periodic, monitoring/execution are deterministic
    vet_signals: bool = True
    daily_report: bool = True
    fail_open: bool = Field(True, description="If Claude is unreachable, proceed on deterministic strategies")
    # When Claude is unavailable the deterministic research pass may only LOG candidate parameters. Setting this true
    # lets it apply them automatically, which re-optimises the running strategy on recent data and makes the paper
    # record a tuned-in-sample result instead of an independent forward test. Keep false during forward testing.
    auto_apply_overrides: bool = False
    max_tokens: int = 8000
    # rough pricing for net-profitability accounting (USD per 1M tokens)
    input_price_per_mtok: float = 4.0
    output_price_per_mtok: float = 20.0


class DataConfig(BaseModel):
    provider: Literal["live", "synthetic", "csv"] = "live"
    candle_interval: str = "4h"
    candle_history: int = 1000
    csv_dir: str = "data/csv"
    synthetic_seed: int = 42
    synthetic_symbols: list[str] = Field(default_factory=lambda: ["SOL", "JUP", "RAY", "BONK", "WIF"])
    # Jupiter REST (api.jup.ag) requires an x-api-key from https://developers.jup.ag (free tier available), per the
    # official `integrating-jupiter` skill. Set [data].jupiter_api_key or env JUPITER_API_KEY. jupiter_base_url is an
    # override for proxies/testing only.
    jupiter_base_url: str = ""
    jupiter_api_key: str = ""
    jupiter_quote_timeout_sec: float = 5.0      # skill: 5s for quotes
    jupiter_execute_timeout_sec: float = 30.0   # skill: 30s for executions
    binance_base_url: str = "https://api.binance.com"
    # Signals come from Binance candles (BTCUSDT...) while execution is on Jupiter (cbBTC, WETH, SOL). Every cycle the
    # bot compares the Jupiter price with the Binance spot price of the same symbol; if the gap exceeds max_basis_bps
    # the signal no longer describes the asset we would buy, so new entries for that symbol are blocked that cycle.
    reference_price_check: bool = True
    max_basis_bps: float = 100.0
    dexscreener_base_url: str = "https://api.dexscreener.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    request_timeout_sec: float = 15.0


class UniverseConfig(BaseModel):
    # Research-validated exposure (research_lab/RESULTS.md): BTC and ETH price exposure, traded on Solana via
    # wrapped assets (cbBTC by Coinbase, WETH by Wormhole), plus SOL. auto_scan stays off so untested tokens are
    # never added automatically; enable it deliberately if you want the bot to pick up other verified tokens.
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    auto_scan: bool = False
    max_symbols: int = 12
    quote_symbol: str = "USDC"
    # Known mints for live trading; auto-scan may add verified Jupiter tokens.
    mints: dict[str, str] = Field(default_factory=lambda: {
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "SOL": "So11111111111111111111111111111111111111112",
        "BTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",   # cbBTC (Coinbase Wrapped BTC), 8 decimals
        "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # WETH (Wormhole, Ethereum), 8 decimals
        "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
        "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    })
    # Candle/price-history symbol per traded symbol (Binance spot klines drive the signals; the wrapped token's
    # Jupiter price drives fills). Defaults to <symbol>USDT.
    candle_symbols: dict[str, str] = Field(default_factory=lambda: {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"})
    decimals: dict[str, int] = Field(default_factory=lambda: {
        "USDC": 6, "SOL": 9, "BTC": 8, "ETH": 8, "JUP": 6, "RAY": 6, "BONK": 5, "WIF": 6, "PYTH": 6, "JTO": 9,
    })


class ExecutionConfig(BaseModel):
    paper_fee_bps: float = 30.0            # Jupiter route + Solana fees approximated
    paper_slippage_bps: float = 15.0
    live_slippage_bps: int = 50
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    # Execution uses Swap API v2 order -> sign -> execute (Jupiter submits and confirms; no RPC needed for swaps).
    jupiter_swap_base_url: str = Field("", description="override for the Swap API base (default: <jupiter base>/swap/v2)")
    execute_max_retries: int = Field(2, ge=0, description="re-quote + retry on retryable /execute codes (-1, -1000, -1001, -1004, -2000..-2004)")
    intent_stale_warn_sec: float = Field(600.0, gt=0, description="log CRITICAL while an intent stays UNKNOWN longer than this (it never auto-expires on time alone)")
    reconcile_on_start: bool = Field(True, description="live: compare ledger with on-chain balances at startup; mismatch trips the kill switch")
    balance_tolerance_pct: float = Field(2.0, ge=0, description="allowed shortfall of wallet vs ledger before tripping")


class JupiterEndpoints(BaseModel):
    """Current Jupiter API surface (official jup-ag/agent-skills `integrating-jupiter`): Price v3, Tokens v2, Swap v2."""
    base: str
    headers: dict[str, str] = Field(default_factory=dict)
    swap_override: str = ""

    @property
    def has_key(self) -> bool:
        return "x-api-key" in self.headers

    @property
    def price(self) -> str:
        return f"{self.base}/price/v3"

    @property
    def tokens(self) -> str:
        return f"{self.base}/tokens/v2"

    @property
    def swap(self) -> str:
        return self.swap_override or f"{self.base}/swap/v2"


def jupiter_endpoints(data: "DataConfig", execution: "ExecutionConfig | None" = None) -> JupiterEndpoints:
    key = data.jupiter_api_key or os.environ.get("JUPITER_API_KEY", "")
    base = data.jupiter_base_url.rstrip("/") or "https://api.jup.ag"
    return JupiterEndpoints(base=base, headers={"x-api-key": key} if key else {},
                            swap_override=(execution.jupiter_swap_base_url.rstrip("/") if execution else ""))


class EngineConfig(BaseModel):
    scan_interval_sec: float = 60.0
    equity_snapshot_interval_sec: float = 300.0
    flatten_on_kill: bool = True
    flatten_on_daily_halt: bool = True
    min_paper_trades_before_live: int = 30
    min_paper_days_before_live: float = 14.0
    live_confirm_phrase: str = "I UNDERSTAND THIS TRADES REAL MONEY WITHOUT APPROVAL"


class ScannerConfig(BaseModel):
    """Read-only real-time opportunity scanner (`tradebot scan`). Separate process and database from the 4h engine.
    It never trades: no wallet, no signing, no `taker` on any Jupiter request."""
    poll_sec: float = 5.0
    size_usd: float = 50.0                      # hypothetical position size used to price every candidate
    min_net_edge_bps: float = 20.0              # expected move minus WORST-CASE round trip minus adverse buffer
    adverse_buffer_bps: float = 10.0            # the quote you can act on is the one nobody else took
    max_round_trip_bps: float = 150.0
    min_organic_score: float = 50.0
    require_verified: bool = True
    max_price_age_sec: float = 10.0
    max_price_slots_behind: int = 25            # Price v3 blockId vs current slot (~0.4 s per slot)
    check_slot: bool = True
    slot_interval_sec: float = 30.0
    facts_interval_sec: float = 600.0
    probe_interval_sec: float = 600.0
    probe_sizes_usd: list[float] = Field(default_factory=lambda: [5.0, 50.0, 100.0, 1000.0])
    probe_symbols: list[str] = Field(default_factory=lambda: ["SOL", "BTC", "ETH"])
    default_round_trip_bps: float = 60.0        # used to mark rejected candidates when no quote/probe exists
    extra_mints: list[str] = Field(default_factory=list)
    discover_category: str = ""                 # "" = off; "toptraded" widens the universe from Jupiter Tokens v2
    discover_interval: str = "1h"
    discover_interval_sec: float = 1800.0
    max_universe: int = 25
    max_requests_per_min: int = 50              # stay inside the free-tier budget; probes/quotes are skipped first


class FastConfig(BaseModel):
    """Fast engine (`tradebot fast`): scanner detection wired to the risk engine and the PAPER broker, own ledger
    data/fast.db, shares the kill switch file with the 4h engine. Paper only; a live broker is not constructible here."""
    initial_capital_usd: float = 1000.0
    size_usd: float = 50.0                      # per position, also capped by risk.max_position_pct of fast equity
    stop_pct: float = 1.0                       # hard stop below the worst-case entry price
    take_profit_frac: float = 1.0               # take profit at expected_move x this (never below cost + min_take_profit_bps)
    min_take_profit_bps: float = 10.0
    trailing_pct: float = 0.0                   # 0 = off
    max_hold_mult: float = 2.0                  # exit at signal horizon x this if neither stop nor target hit
    flatten_on_kill: bool = True
    ws_reference: bool = False                  # Binance WebSocket for reference prices (needs `pip install websockets`)


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    fast: FastConfig = Field(default_factory=FastConfig)
    initial_capital_usd: float = 1000.0
    data_dir: str = "data"
    log_level: str = "INFO"
    risk: RiskLimits = Field(default_factory=RiskLimits)
    # Defaults follow research_lab/RESULTS.md: TSMOM with a 100-day EMA regime filter (primary) and the EMA-cross
    # trend strategy (secondary) on 4h candles. mean_reversion and breakout lost money out of sample after costs.
    strategies: list[StrategyConfig] = Field(default_factory=lambda: [
        StrategyConfig(name="tsmom", params={"lookback_days": 30, "ema_filter_days": 100, "stop_pct": 25.0}),
        StrategyConfig(name="momentum", params={"fast": 12, "slow": 26, "trend": 100, "atr_mult": 2.5, "tp_mult": 0.0, "trailing": False}),
        StrategyConfig(name="mean_reversion", enabled=False),
        StrategyConfig(name="breakout", enabled=False),
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

# Strategy selection follows research_lab/RESULTS.md (walk-forward validated on BTC/ETH/SOL, 2019-2026).
[[strategies]]                 # PRIMARY: daily-evaluated time-series momentum with a 100-day EMA regime filter
name = "tsmom"
enabled = true
[strategies.params]
lookback_days = 30
ema_filter_days = 100
stop_pct = 25.0               # disaster stop between daily checks
trail_pct = 0.0               # 20.0 enables the "tsmom_trail" variant

[[strategies]]                 # SECONDARY: EMA 12/26 cross above EMA 100, 2.5-ATR stop, no take-profit (validated form)
name = "momentum"
enabled = true
[strategies.params]
fast = 12
slow = 26
trend = 100
atr_mult = 2.5
tp_mult = 0.0
trailing = false

[[strategies]]                 # REJECTED in research (loses after costs); kept for experimentation only
name = "mean_reversion"
enabled = false

[[strategies]]                 # REJECTED on 1h/4h; the daily Donchian variant looked good but on too few trades
name = "breakout"
enabled = false
[strategies.params]
channel = 55
exit_channel = 20
volume_mult = 0
atr_mult = 2.0
tp_mult = 0.0
trailing = false

[claude]
enabled = false              # Anthropic API calls OFF: deterministic strategies only. Set true (and ANTHROPIC_API_KEY) to enable.
model = "claude-opus-5-5"
effort = "medium"
research_enabled = false     # forward test runs FROZEN parameters; no re-optimisation of the live strategy
research_interval_hours = 168
vet_signals = false
daily_report = false
fail_open = true
auto_apply_overrides = false # deterministic research may only log proposals, never apply them

[data]
provider = "live"            # "synthetic" or "csv" for offline testing
candle_interval = "4h"       # the validated timeframe; 1h churns away the edge at realistic costs
candle_history = 1000        # >= 30d lookback (180 bars) + 100d EMA (600 bars) at 4h
jupiter_api_key = ""         # REQUIRED for live market data and quotes: free key from https://developers.jup.ag (or env JUPITER_API_KEY)
reference_price_check = true # each cycle: Jupiter price vs Binance spot of the signal symbol; entries blocked if the gap exceeds
max_basis_bps = 100

[universe]                   # validated exposure: BTC (as cbBTC), ETH (as Wormhole WETH), SOL. See `tradebot verify-assets`.
symbols = ["BTC", "ETH", "SOL"]
auto_scan = false            # true lets the bot add other verified Jupiter tokens (untested exposure)
max_symbols = 12

[execution]
paper_fee_bps = 30
paper_slippage_bps = 15
live_slippage_bps = 50
rpc_url = "https://api.mainnet-beta.solana.com"   # balances only; swaps execute via Jupiter Swap API v2

[engine]
scan_interval_sec = 60
flatten_on_kill = true
flatten_on_daily_halt = true
min_paper_trades_before_live = 30
min_paper_days_before_live = 14
"""
