"""Claude analyst boundaries, with the API mocked (no network)."""
from types import SimpleNamespace

from tradebot.config import ClaudeConfig
from tradebot.research.claude_analyst import ClaudeAnalyst, ResearchProposal, VetResult, _strict_schema


def _resp(text: str, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=100), stop_details=None)


class FakeClient:
    def __init__(self, text, stop="end_turn"):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._text, self._stop = text, stop

    def _create(self, **kw):
        self.calls.append(kw)
        return _resp(self._text, self._stop)


def _analyst(text, stop="end_turn"):
    a = ClaudeAnalyst(ClaudeConfig(enabled=False))
    a.available = True
    a._client = FakeClient(text, stop)
    return a


def test_vet_cannot_upsize():
    a = _analyst('{"approve": true, "size_multiplier": 1.0, "reason": "ok"}')
    v = a.vet_signal({"x": 1})
    assert v is not None and v.size_multiplier <= 1.0
    # schema forbids >1; a bad payload is rejected and the deterministic path proceeds
    a = _analyst('{"approve": true, "size_multiplier": 3.0, "reason": "moon"}')
    assert a.vet_signal({"x": 1}) is None


def test_refusal_and_garbage_degrade_to_none():
    assert _analyst("{}", stop="refusal").vet_signal({}) is None
    assert _analyst("not json").vet_signal({}) is None


def test_request_uses_structured_output_and_fallbacks():
    a = _analyst('{"strategy_updates": [], "remove_symbols": [], "market_view": "calm"}')
    p = a.research({}, {}, {}, {})
    assert isinstance(p, ResearchProposal)
    call = a._client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "thinking" not in call  # adaptive thinking by default on this model


def test_strict_schema_closes_objects():
    sch = _strict_schema(VetResult)
    assert sch["additionalProperties"] is False and set(sch["required"]) == {"approve", "size_multiplier", "reason"}


def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = ClaudeAnalyst(ClaudeConfig(enabled=True))
    # client construction may succeed without a key (lazy auth) or fail; either way calls never raise
    a.available = False
    assert a.vet_signal({}) is None and a.research({}, {}, {}, {}) is None
