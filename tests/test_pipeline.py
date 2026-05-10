"""Tests for the pure logic — the parts that don't need a live LLM, MCP, or Telegram.

What's covered: URL parsing, claim parsing + the config gates, the verdict thresholds,
the report aggregation, and the validator's counting logic over synthetic bars. The
network-dependent edges (the actual LLM extraction, the actual MCP calls, the Telegram
listener) are exercised manually via the CLI when building examples/ — see the README.

Run: pytest
"""

from __future__ import annotations

import pytest

from agent_research_lab import report as report_mod
from agent_research_lab import thesis as thesis_mod
from agent_research_lab import validate as validate_mod
from agent_research_lab.config import Config
from agent_research_lab.transcript import extract_video_id
from agent_research_lab.types import Claim, ThesisSet, Transcript, ValidationRun


# --------------------------------------------------------------------------- config fixture


def _config(**overrides) -> Config:
    base = dict(
        telegram_bot_token="", anthropic_api_key="", anthropic_model="claude-sonnet-4-6",
        telegram_allowlist=[], tradingview_mcp_url=None,
        test_types={"indicator_value_over_range": True, "level_zone_hit_rate": True, "strategy_backtest": False},
        default_timeframe="1D", default_lookback_days=365, symbol_fallback=None,
        max_claims_per_video=3, min_confidence=0.5, mcp_retries=1, mcp_timeout_seconds=60,
        tracing_enabled=False, tracing_dir="traces",
    )
    base.update(overrides)
    return Config(**base)


# --------------------------------------------------------------------------- URL parsing


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),
    ("not a url", None),
    ("https://www.youtube.com/", None),
])
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


# --------------------------------------------------------------------------- thesis parsing + gates


def test_parse_claims_tolerates_json_fence():
    raw = '```json\n{"claims": [{"statement": "RSI<30 on SPY daily marks reversals", "instrument": "SPY", "timeframe": "1D", "test_type": "indicator_value_over_range", "testable": "yes", "reason_if_not": null, "confidence": 0.8, "test_type_justification": "indicator behavior over a range"}]}\n```'
    claims = thesis_mod._parse_claims(raw, "vid1")
    assert len(claims) == 1
    assert claims[0].id == "c1"
    assert claims[0].instrument == "SPY"
    assert claims[0].testable == "yes"


def test_parse_claims_bad_json_returns_empty():
    assert thesis_mod._parse_claims("the model rambled instead of JSON", "vid1") == []


def test_parse_claims_zero_claims_is_normal():
    assert thesis_mod._parse_claims('{"claims": []}', "vid1") == []


def test_gate_confidence_floor_downgrades():
    claims = [
        Claim("c1", "high-confidence testable", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9),
        Claim("c2", "low-confidence testable", "QQQ", "1D", "indicator_value_over_range", "yes", None, 0.3),
        Claim("c3", "low-confidence partial", None, None, "level_zone_hit_rate", "partial", "no level", 0.2),
    ]
    out = thesis_mod._apply_gates(claims, _config(min_confidence=0.5))
    by_id = {c.statement: c for c in out}
    assert by_id["high-confidence testable"].testable == "yes"
    assert by_id["low-confidence testable"].testable == "partial"   # downgraded
    assert by_id["low-confidence partial"].testable == "no"          # downgraded


def test_gate_claim_cap_keeps_most_central():
    claims = [
        Claim(f"c{i}", f"claim {i}", "SPY", "1D", "indicator_value_over_range", "yes", None, conf)
        for i, conf in enumerate([0.4, 0.95, 0.6, 0.8, 0.7], start=1)
    ]
    out = thesis_mod._apply_gates(claims, _config(max_claims_per_video=3, min_confidence=0.0))
    assert len(out) == 3
    confs = sorted((c.confidence for c in out), reverse=True)
    assert confs == [0.95, 0.8, 0.7]
    assert [c.id for c in out] == ["c1", "c2", "c3"]  # re-ided


# --------------------------------------------------------------------------- verdict thresholds


def _ok_run(claim_id="c1", n=20, r=0.7) -> ValidationRun:
    return ValidationRun(claim_id, "indicator_value_over_range", "ok", "SPY 1D", "summary",
                         occurrences=n, hit_rate=r, result=f"{r:.0%}", caveats=[])


def test_verdict_holds_when_strong_and_enough_n():
    claim = Claim("c1", "x", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9)
    v, _ = report_mod._verdict_for(claim, _ok_run(n=20, r=0.8))
    assert v == "holds"


def test_verdict_fails_when_weak():
    claim = Claim("c1", "x", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9)
    v, _ = report_mod._verdict_for(claim, _ok_run(n=20, r=0.3))
    assert v == "fails"


def test_verdict_partial_when_coinflip():
    claim = Claim("c1", "x", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9)
    v, _ = report_mod._verdict_for(claim, _ok_run(n=20, r=0.55))
    assert v == "partial"


def test_verdict_partial_when_too_few_occurrences():
    claim = Claim("c1", "x", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9)
    v, _ = report_mod._verdict_for(claim, _ok_run(n=4, r=0.9))
    assert v == "partial"  # great rate but n too small


def test_verdict_untestable_for_no_claim():
    claim = Claim("c1", "it's a vibe", None, None, "none", "no", "this is an opinion", 0.6)
    v, reason = report_mod._verdict_for(claim, None)
    assert v == "untestable"
    assert "opinion" in reason


def test_verdict_untestable_on_mcp_error():
    claim = Claim("c1", "x", "FAKE", "1D", "indicator_value_over_range", "yes", None, 0.9)
    run = ValidationRun("c1", "indicator_value_over_range", "error", "", "", None, None,
                        "untestable — could not resolve \"FAKE\"", caveats=[], error="symbol not found")
    v, _ = report_mod._verdict_for(claim, run)
    assert v == "untestable"


# --------------------------------------------------------------------------- report aggregation


def _finding(verdict):
    claim = Claim("c", "x", "SPY", "1D", "indicator_value_over_range", "yes", None, 0.9)
    from agent_research_lab.types import ClaimFinding
    return ClaimFinding(claim=claim, validation=None, verdict=verdict, verdict_reason="")


def test_aggregate_all_untestable():
    t = Transcript("v", "u", None, None, "...")
    v, _ = report_mod._aggregate([_finding("untestable"), _finding("untestable")], t, ThesisSet("v"))
    assert v == "untestable"


def test_aggregate_holds_when_all_hold():
    t = Transcript("v", "u", None, None, "...")
    v, _ = report_mod._aggregate([_finding("holds"), _finding("holds")], t, ThesisSet("v"))
    assert v == "holds"


def test_aggregate_partial_when_mixed():
    t = Transcript("v", "u", None, None, "...")
    v, _ = report_mod._aggregate([_finding("holds"), _finding("fails")], t, ThesisSet("v"))
    assert v == "partial"


def test_aggregate_fails_when_only_fails():
    t = Transcript("v", "u", None, None, "...")
    v, _ = report_mod._aggregate([_finding("fails"), _finding("untestable")], t, ThesisSet("v"))
    assert v == "fails"


# --------------------------------------------------------------------------- validator counting


def _bars(closes, highs=None, lows=None):
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return [{"time": i, "open": closes[i], "high": highs[i], "low": lows[i], "close": closes[i], "volume": 1.0}
            for i in range(len(closes))]


def test_count_level_respect_basic():
    # price bounces off 100 a few times, then breaks through
    closes = [110, 105, 101, 104, 108, 103, 100.5, 106, 109, 102, 99, 95, 92]  # last 3: broke below
    bars = _bars(closes)
    occ, reversed_count = validate_mod._count_level_respect(bars, 100.0)
    assert occ >= 1
    assert 0 <= reversed_count <= occ


def test_count_level_respect_never_tested():
    bars = _bars([200, 205, 210, 215, 220, 225, 230, 235, 240, 245])
    occ, reversed_count = validate_mod._count_level_respect(bars, 100.0)
    assert occ == 0
    assert reversed_count == 0


def test_count_indicator_trigger_with_series():
    # 30 bars; RSI dips below 30 at indices 5, 15, 25; price goes up after each
    closes = [100 + i for i in range(30)]
    bars = _bars(closes)
    rsi = [50.0] * 30
    rsi[5] = rsi[15] = rsi[25] = 25.0
    occ, hits = validate_mod._count_indicator_trigger(bars, rsi, ("rsi", "<", 30.0))
    assert occ == 3
    assert hits == 3  # uptrending closes -> price always "up" after


def test_count_indicator_trigger_no_series_means_zero():
    bars = _bars([100 + i for i in range(30)])
    occ, hits = validate_mod._count_indicator_trigger(bars, None, ("rsi", "<", 30.0))
    assert occ == 0 and hits == 0


# --------------------------------------------------------------------------- minimal report


def test_build_minimal_is_untestable():
    t = Transcript("vid", "https://youtu.be/vid", "Some Vlog", "ChannelX", "")
    r = report_mod.build_minimal(t, "vid-20260510T000000Z", "no transcript available")
    assert r.verdict_overall == "untestable"
    assert "no transcript" in r.overall_reason
    assert "untestable" in r.markdown.lower()
    assert r.json["verdict_overall"] == "untestable"
