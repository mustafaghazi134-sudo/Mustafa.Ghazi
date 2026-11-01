"""Claude as a bounded research analyst.

Capabilities (all advisory):
  * research()         -> propose strategy parameter/enable changes chosen from backtested candidates
  * vet_signal()       -> approve, veto, or shrink a signal (size_multiplier <= 1; never enlarges)
  * daily_narrative()  -> plain-language summary of performance and events

Hard boundaries enforced in code, not by prompt: Claude cannot create orders, change risk limits,
touch the kill switch or live gate, or pick parameters that were not backtested. Every call is
wrapped so an API failure degrades to the deterministic path.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from ..config import ClaudeConfig
from ..portfolio.ledger import Ledger

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the research analyst inside an automated Solana spot-trading system.

Your role is advisory and reduce-only. A deterministic risk engine, loss limits and a kill switch
sit between you and any money; you cannot change them. You can only: pick among already-backtested
strategy parameter candidates, veto or shrink individual trade signals, and write summaries.

Principles:
- Prefer out-of-sample evidence over in-sample fit. Treat a large gap between in-sample and
  out-of-sample returns as overfitting and reject it.
- Prefer fewer, higher-quality trades. Fees and slippage are real; churn loses money.
- Memecoins and low-liquidity tokens carry rug/pull and manipulation risk; be conservative.
- When uncertain, choose the conservative option (keep current parameters, shrink or veto).
- Be concise and specific. Numbers over adjectives.
"""


class VetResult(BaseModel):
    approve: bool = Field(description="False vetoes the trade")
    size_multiplier: float = Field(ge=0.0, le=1.0, description="Scale the proposed size; 1.0 keeps it, never above 1.0")
    reason: str = Field(max_length=400)


class StrategyUpdate(BaseModel):
    name: str
    enabled: bool
    candidate_index: int | None = Field(None, description="Index into the provided candidates list for this strategy, or null to keep current params")
    rationale: str = Field(max_length=400)


class ResearchProposal(BaseModel):
    strategy_updates: list[StrategyUpdate]
    remove_symbols: list[str] = Field(default_factory=list, description="Symbols to stop trading (too risky / illiquid)")
    market_view: str = Field(max_length=800)


class ClaudeAnalyst:
    def __init__(self, cfg: ClaudeConfig, ledger: Ledger | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self._client = None
        self.available = cfg.enabled
        if cfg.enabled:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except Exception as e:  # noqa: BLE001 - missing key / import issues disable Claude, nothing else
                log.warning("Claude disabled: %s", e)
                self.available = False

    # ------------------------------------------------------------------ plumbing
    def _record_cost(self, resp: Any, purpose: str) -> None:
        try:
            u = resp.usage
            cost = (u.input_tokens * self.cfg.input_price_per_mtok + u.output_tokens * self.cfg.output_price_per_mtok) / 1e6
            if self.ledger:
                self.ledger.add_api_cost(self.cfg.model, purpose, u.input_tokens, u.output_tokens, cost)
        except Exception:  # noqa: BLE001
            log.debug("could not record api cost", exc_info=True)

    def _ask(self, purpose: str, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        if not self.available or self._client is None:
            return None
        import anthropic
        try:
            resp = self._client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.cfg.effort,
                               "format": {"type": "json_schema", "schema": _strict_schema(schema)}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            log.warning("Claude rate limited (%s): %s", purpose, e)
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API error (%s): %s %s", purpose, e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.warning("Claude connection error (%s): %s", purpose, e)
            return None
        self._record_cost(resp, purpose)
        if resp.stop_reason == "refusal":
            log.warning("Claude refused (%s): %s", purpose, getattr(resp, "stop_details", None))
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            return schema.model_validate_json(text)
        except ValueError as e:
            log.warning("Claude returned invalid %s: %s", schema.__name__, e)
            return None

    # ------------------------------------------------------------------ capabilities
    def vet_signal(self, signal_ctx: dict[str, Any]) -> VetResult | None:
        prompt = ("Vet this trade signal. Approve, veto, or shrink it (size_multiplier <= 1). Consider liquidity, "
                  "24h change (chasing?), recent strategy performance, and open exposure.\n\n"
                  f"{json.dumps(signal_ctx, indent=2, default=str)}")
        res = self._ask("vet_signal", prompt, VetResult)
        if res is not None:
            res.size_multiplier = min(1.0, max(0.0, res.size_multiplier))
        return res

    def research(self, market_summary: dict[str, Any], candidates: dict[str, list[dict]], current: dict[str, dict],
                 recent_performance: dict[str, Any]) -> ResearchProposal | None:
        prompt = (
            "Review the backtest candidates below and propose parameter updates. For each strategy choose either a "
            "candidate_index (only from its own candidates list, all of which were already backtested walk-forward) or null "
            "to keep current params, and whether it should stay enabled. Reject candidates whose in-sample vs out-of-sample "
            "gap suggests overfitting. Suggest symbols to remove if their liquidity/volume/volatility profile is dangerous.\n\n"
            f"MARKET SUMMARY:\n{json.dumps(market_summary, indent=1, default=str)}\n\n"
            f"CURRENT STRATEGY PARAMS:\n{json.dumps(current, indent=1, default=str)}\n\n"
            f"RECENT LIVE/PAPER PERFORMANCE:\n{json.dumps(recent_performance, indent=1, default=str)}\n\n"
            f"CANDIDATES (per strategy, ranked by out-of-sample score; index = position in list):\n"
            f"{json.dumps(candidates, indent=1, default=str)}"
        )
        return self._ask("research", prompt, ResearchProposal)

    def daily_narrative(self, report_text: str, events: list[str]) -> str | None:
        class Narrative(BaseModel):
            summary: str = Field(max_length=1500)
        res = self._ask("daily_report", "Write a short, factual daily summary for the owner of this bot (what happened, "
                        "P&L, risks worth attention). No hype.\n\nREPORT:\n" + report_text + "\n\nEVENTS:\n" + "\n".join(events), Narrative)
        return res.summary if res else None


def _strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with additionalProperties=false everywhere (required by the API)."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
                props = node.get("properties", {})
                node["required"] = list(props.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return schema
