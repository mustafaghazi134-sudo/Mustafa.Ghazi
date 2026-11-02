"""Opportunity Lab configuration (`lab.toml`, all sections optional). Independent of the main bot's config.toml."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    """Keyless public sources are on by default; keyed or unverified ones are off until the owner enables them."""
    dexscreener: bool = True
    geckoterminal: bool = True
    jupiter_tokens: bool = True          # Tokens v2 (audit fields); JUPITER_API_KEY if set, else keyless (0.5 req/s)
    jupiter_quotes: bool = True          # read-only /swap/v2/order WITHOUT taker: route and round-trip cost checks
    solana_rpc: bool = True              # mint/freeze authority, Token-2022 extensions, largest holders
    rugcheck: bool = False
    goplus: bool = False
    birdeye: bool = False                # needs an API key (env BIRDEYE_API_KEY)
    helius: bool = False                 # needs an API key (env HELIUS_API_KEY); used as the RPC URL when set
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    request_timeout_sec: float = 10.0
    user_agent: str = "tradebot-opportunity-lab/0.1 (paper research)"


class ScanConfig(BaseModel):
    poll_sec: float = 30.0
    max_tracked_tokens: int = 150
    history_points: int = 120                       # snapshots kept in memory per token
    new_token_max_age_min: float = 360.0
    min_liquidity_usd: float = 20_000.0             # discovery floor; the security agent applies stricter rules
    volume_surge_ratio: float = 3.0                 # m5 volume vs average 5-min volume of the last hour
    liquidity_change_pct: float = 30.0
    momentum_pct_m5: float = 8.0
    breakout_lookback_points: int = 20
    buy_pressure_ratio: float = 0.65                # buys / (buys + sells), m5
    min_txns_m5: int = 20


class SecurityConfig(BaseModel):
    min_liquidity_usd: float = 50_000.0
    min_age_min: float = 30.0                       # newer tokens are rejected (bundled launches, snipers, instant rugs)
    max_top10_holder_pct: float = 35.0              # excluding known pool / burn accounts when identifiable
    max_single_holder_pct: float = 12.0
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    max_round_trip_loss_bps: float = 250.0          # buy then sell the received amount, quoted (no taker)
    max_price_impact_bps: float = 150.0
    max_liquidity_drop_pct: float = 25.0            # within the tracked history
    max_transfer_fee_bps: float = 0.0               # Token-2022 transfer fees are rejected by default
    reject_token2022_extensions: list[str] = Field(default_factory=lambda: [
        "transferHook", "permanentDelegate", "nonTransferable", "defaultAccountState", "pausable", "pausableConfig", "confidentialTransferMint",
        "transferFeeConfig"])
    reject_jupiter_sus: bool = True
    min_organic_score: float = 0.0                  # Jupiter organicScore; 0 disables
    max_assessment_age_sec: float = 300.0


class AnalystConfig(BaseModel):
    strategies: list[str] = Field(default_factory=lambda: ["momentum_volume", "breakout", "buy_pressure", "migration_momentum"])
    max_total_cost_bps: float = 300.0               # all-in round trip (fees, impact, network, Token-2022, adverse)
    adverse_selection_bps: float = 50.0
    min_reward_risk: float = 1.5
    require_calibrated_edge: bool = False           # paper exploration: trade uncalibrated signals to collect evidence
    calibration_min_trades: int = 30
    max_size_pct_of_liquidity: float = 0.5


class ExitConfig(BaseModel):
    stop_loss_pct: float = 12.0
    take_profit_pct: float = 30.0
    trailing_stop_pct: float = 10.0                 # from the high-water mark, active once in profit
    trailing_activation_pct: float = 8.0
    vol_stop_mult: float = 3.0                      # stop at entry - k * stdev of recent per-snapshot returns (if tighter)
    max_hold_min: float = 240.0
    momentum_reversal_pct: float = 6.0              # exit when the m5 change falls below -X% while in the trade
    liquidity_drop_exit_pct: float = 30.0           # exit when pool liquidity falls by X% from entry
    unsellable_writeoff_min: float = 60.0           # no sell route for this long: written off at zero


class ExecutionModelConfig(BaseModel):
    entry_latency_sec: float = 5.0                  # decision -> fill; the fill uses the first observation after the delay
    max_quote_age_sec: float = 60.0
    slippage_tolerance_bps: float = 300.0           # a fill worse than the decision price by more than this fails
    network_fee_usd: float = 0.01                   # base + priority fee per transaction (Solana)
    ata_rent_usd: float = 0.40                      # new token account rent on first buy (refundable, treated as cost)
    default_dex_fee_bps: float = 30.0
    dex_fee_bps: dict[str, float] = Field(default_factory=lambda: {
        "pumpfun": 100.0, "pumpswap": 25.0, "raydium": 25.0, "raydium-clmm": 25.0, "orca": 30.0, "meteora": 30.0,
        "meteoradbc": 100.0})
    cost_multiplier: float = 1.0                    # stress tests use 2.0 and 3.0
    fail_entry_without_route: bool = True


class LabRisk(BaseModel):
    paper_capital_usd: float = 1_000.0
    position_usd: float = 25.0
    max_open_positions: int = 4
    max_exposure_usd: float = 100.0
    max_daily_loss_usd: float = 50.0
    max_drawdown_usd: float = 150.0
    max_entries_per_day: int = 20
    token_cooldown_min: float = 120.0
    loss_streak_pause: int = 4
    loss_streak_pause_min: float = 240.0


class AIConfig(BaseModel):
    """Optional, OFF by default. AI scores are recorded in SHADOW mode only: they never gate, size or execute trades.
    Evaluation compares the AI-approved subset with the deterministic baseline on the same paper trades."""
    claude_enabled: bool = False
    claude_model: str = "claude-sonnet-5-5"
    grok_enabled: bool = False
    grok_model: str = "grok-4.3"                 # cheapest current model per docs.x.ai search results, 2026-11-02
    grok_base_url: str = "https://api.x.ai/v1"
    max_calls_per_day: int = 100
    max_spend_usd_per_day: float = 1.0
    # USD per million tokens, used only for the budget guard. Defaults are deliberately HIGH placeholders (the guard
    # then stops early); set them from the provider's current price page (docs/GROK_STYLE_RESEARCH.md).
    grok_input_per_mtok: float = 1.25            # grok-4.3 list price (docs.x.ai via search, unverified live)
    grok_output_per_mtok: float = 2.50
    claude_input_per_mtok: float = 5.0
    claude_output_per_mtok: float = 25.0


class LabConfig(BaseModel):
    mode: Literal["paper"] = "paper"                # there is no other mode
    data_dir: str = "data/lab"
    main_kill_file: str = "data/KILL"               # the main bot's emergency stop, honoured read-only
    sources: SourceConfig = Field(default_factory=SourceConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    analyst: AnalystConfig = Field(default_factory=AnalystConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    execution: ExecutionModelConfig = Field(default_factory=ExecutionModelConfig)
    risk: LabRisk = Field(default_factory=LabRisk)
    ai: AIConfig = Field(default_factory=AIConfig)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "lab.sqlite"

    @property
    def halt_file(self) -> Path:
        return self.data_path / "LAB_HALT"

    @property
    def events_file(self) -> Path:
        return self.data_path / "lab_events.jsonl"


def load_config(path: str | Path | None = None) -> LabConfig:
    p = Path(path or os.environ.get("LAB_CONFIG", "lab.toml"))
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    cfg = LabConfig.model_validate(raw)
    if os.environ.get("LAB_DATA_DIR"):
        cfg.data_dir = os.environ["LAB_DATA_DIR"]
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    return cfg
