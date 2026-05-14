"""Tests for pine.py — the pure logic that doesn't need LLM or MCP.

Covers: Pine code detection, compile error parsing, strategy results parsing,
and the _failed() helper. The full synthesis+compile+backtest loop requires a
live MCP and LLM; those are exercised manually via the CLI.
"""

from __future__ import annotations

import pytest

from agent_research_lab import pine as pine_mod
from agent_research_lab.types import Claim, StrategyBacktestMetrics


def _claim(**kwargs) -> Claim:
    base = dict(
        id="c1", statement="RSI below 30 and price bounces",
        instrument="SPY", timeframe="1D",
        test_type="strategy_backtest", testable="yes",
        reason_if_not=None, confidence=0.9,
    )
    base.update(kwargs)
    return Claim(**base)


# --------------------------------------------------------------------------- detection


def test_detect_pine_code_finds_version_marker():
    text = "So the script starts with //@version=5\nstrategy('My Strategy', overlay=true)"
    snippets = pine_mod._detect_pine_code(text)
    assert len(snippets) >= 1
    assert any("//@version" in s or "strategy" in s for s in snippets)


def test_detect_pine_code_finds_strategy_call():
    text = "I add this: strategy('RSI Bounce', overlay=true, default_qty_type=strategy.percent_of_equity)"
    snippets = pine_mod._detect_pine_code(text)
    assert snippets  # at least one snippet found


def test_detect_pine_code_returns_empty_for_clean_transcript():
    text = "Today I want to talk about the RSI indicator and how it can help traders. Let me show you."
    snippets = pine_mod._detect_pine_code(text)
    assert snippets == []


def test_detect_pine_code_returns_empty_for_empty_text():
    assert pine_mod._detect_pine_code("") == []


# --------------------------------------------------------------------------- compile error parsing


def test_parse_compile_errors_list_of_dicts():
    res = {"errors": [{"message": "Undeclared identifier 'foo'"}, {"message": "Expected 'end of line'"}]}
    errors = pine_mod._parse_compile_errors(res)
    assert errors == ["Undeclared identifier 'foo'", "Expected 'end of line'"]


def test_parse_compile_errors_list_of_strings():
    res = {"errors": ["line 5: syntax error", "line 12: undefined variable"]}
    errors = pine_mod._parse_compile_errors(res)
    assert errors == ["line 5: syntax error", "line 12: undefined variable"]


def test_parse_compile_errors_empty_means_success():
    assert pine_mod._parse_compile_errors({"errors": []}) == []
    assert pine_mod._parse_compile_errors({}) == []
    assert pine_mod._parse_compile_errors(None) == []


def test_parse_compile_errors_plain_string():
    res = "Syntax error on line 3"
    errors = pine_mod._parse_compile_errors(res)
    assert errors == ["Syntax error on line 3"]


def test_parse_compile_errors_list_directly():
    res = [{"message": "type mismatch"}, "another error"]
    errors = pine_mod._parse_compile_errors(res)
    assert "type mismatch" in errors
    assert "another error" in errors


# --------------------------------------------------------------------------- strategy results parsing


def test_parse_strategy_results_extracts_metrics():
    res = {
        "total_trades": 42,
        "winning_trades": 30,
        "net_profit": 5200.0,
        "gross_profit": 8000.0,
        "gross_loss": -2800.0,
        "max_drawdown": 1200.0,
    }
    m = pine_mod._parse_strategy_results(res)
    assert m is not None
    assert m.total_trades == 42
    assert m.winning_trades == 30
    assert abs(m.win_rate - 30 / 42) < 0.001
    assert m.net_profit == 5200.0
    assert m.profit_factor == pytest.approx(8000.0 / 2800.0, rel=0.01)


def test_parse_strategy_results_handles_zero_trades():
    res = {"total_trades": 0, "winning_trades": 0, "net_profit": 0.0}
    assert pine_mod._parse_strategy_results(res) is None


def test_parse_strategy_results_handles_none():
    assert pine_mod._parse_strategy_results(None) is None
    assert pine_mod._parse_strategy_results("not a dict") is None
    assert pine_mod._parse_strategy_results({}) is None


def test_parse_strategy_results_camel_case_keys():
    res = {
        "totalTrades": 10,
        "winningTrades": 7,
        "netProfit": 1000.0,
        "grossProfit": 1500.0,
        "grossLoss": -500.0,
        "maxDrawdown": 300.0,
    }
    m = pine_mod._parse_strategy_results(res)
    assert m is not None
    assert m.total_trades == 10
    assert m.winning_trades == 7


def test_parse_strategy_results_nested_under_result_key():
    res = {
        "result": {
            "total_trades": 5,
            "winning_trades": 3,
            "net_profit": 200.0,
            "gross_profit": 300.0,
            "gross_loss": -100.0,
            "max_drawdown": 50.0,
        }
    }
    m = pine_mod._parse_strategy_results(res)
    assert m is not None
    assert m.total_trades == 5


# --------------------------------------------------------------------------- _failed helper


def test_failed_returns_valid_validation_run():
    claim = _claim()
    vr = pine_mod._failed(claim, "LLM unavailable")
    assert vr.claim_id == "c1"
    assert vr.status == "error"
    assert "LLM unavailable" in vr.result
    assert vr.strategy_backtest is None
    assert vr.pine_script_path is None


# --------------------------------------------------------------------------- StrategyBacktestMetrics in types


def test_strategy_backtest_metrics_dataclass():
    m = StrategyBacktestMetrics(
        net_profit=1000.0, gross_profit=1500.0, total_trades=20,
        winning_trades=14, win_rate=0.7, max_drawdown=300.0,
        profit_factor=2.5, pine_script_path="/some/path.pine",
    )
    assert m.win_rate == 0.7
    assert m.pine_script_path == "/some/path.pine"
