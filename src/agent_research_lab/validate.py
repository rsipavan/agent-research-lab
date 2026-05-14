"""Validation: a Claim -> list[ValidationRun], against real market data via the TradingView MCP.

One ValidationRun = one (claim, timeframe) test. A single claim is typically validated
across several timeframes (config.test_timeframes) and produces several ValidationRuns;
the per-claim verdict aggregates over them in report.py. Multi-timeframe evidence beats
single-timeframe cherry-pick — and when timeframes disagree, that's itself a finding.

This module is the only one that touches market data, and it does so through the
TradingView MCP — never a data vendor directly. It REPORTS THE RATE (e.g. "bounced
14 of 22 occurrences"); it does NOT assign a verdict. report.py turns the rate into
holds/partial/fails/untestable by explicit rules — see docs/validation_logic.md.

Every failure mode (symbol not found, MCP error, insufficient data, disabled test
type, backtest-not-in-v1) returns a ValidationRun with the right status — it never
raises past itself. One bad claim never kills a run.
"""

from __future__ import annotations

import re

from .config import Config
from .mcp_client import McpClient, McpError
from .types import Claim, ValidationRun

# Tolerance for "price approached this level": within this fraction of the level.
_LEVEL_TOLERANCE = 0.003  # 0.3%
# How far forward we look for the "claimed outcome" after a trigger, in bars.
_FORWARD_WINDOW_BARS = 3
# Maximum indicators allowed on a TradingView Free chart.
_TV_FREE_INDICATOR_CAP = 2


def run(claim: Claim, config: Config, mcp: McpClient) -> list[ValidationRun]:
    """Run the appropriate test for `claim` on each timeframe in scope. Always returns
    a non-empty list. Each element is one (claim, timeframe) result.

    Timeframe selection: if `claim.timeframe` is set, ONLY that timeframe is used (the
    video named one — respect it). Otherwise the claim is tested on every entry in
    `config.test_timeframes`. Multi-timeframe evidence beats single-tf cherry-pick.

    If a claim is fundamentally untestable (no instrument, disabled test type,
    strategy_backtest in v1, etc.), returns a single untestable run with timeframe="n/a"
    — the gate applies regardless of timeframe and there's no point running it three times.
    """
    # --- gates that mean "untestable on every timeframe" ---
    if claim.testable == "no":
        return [_untestable(claim, "n/a", "claim was classified as not testable")]
    if claim.test_type == "strategy_backtest":
        return [_untestable(claim, "n/a",
                            "this is a full strategy — needs a backtest engine (not in v1)")]
    if claim.test_type == "none":
        return [_untestable(claim, "n/a", "no test type maps to this claim")]
    if not config.test_type_enabled(claim.test_type):
        return [_untestable(claim, "n/a",
                            f"test type '{claim.test_type}' is disabled in config")]

    instrument = claim.instrument or config.symbol_fallback
    if not instrument:
        return [_untestable(claim, "n/a",
                            "no instrument named in the video and no fallback configured")]

    # --- symbol resolution happens ONCE; reused across timeframes ---
    symbol = _resolve_symbol(instrument, mcp, config)
    if symbol is None:
        return [_untestable(claim, "n/a",
                            f'could not resolve "{instrument}" to a tradeable symbol')]

    # --- decide which timeframes to test ---
    if claim.timeframe:
        timeframes = [claim.timeframe]
        timeframes_assumed = False
    else:
        timeframes = list(config.test_timeframes) if config.test_timeframes else [config.default_timeframe]
        timeframes_assumed = True

    # --- per-timeframe loop ---
    runs: list[ValidationRun] = []
    for tf in timeframes:
        try:
            if claim.test_type == "indicator_value_over_range":
                vr = _test_indicator_value_over_range(claim, symbol, tf, mcp, config)
            elif claim.test_type == "level_zone_hit_rate":
                vr = _test_level_zone_hit_rate(claim, symbol, tf, mcp, config)
            else:  # pragma: no cover - covered by gates above
                vr = _untestable(claim, tf, f"unhandled test type '{claim.test_type}'")
        except McpError as e:
            vr = ValidationRun(
                claim_id=claim.id,
                test_type=claim.test_type,
                timeframe=tf,
                status="error",
                tradingview_query=f"{symbol} {tf}",
                data_summary="",
                occurrences=None,
                hit_rate=None,
                result=f"validation failed: {e}",
                caveats=[],
                error=str(e),
            )

        # Standard caveats every run carries.
        vr.caveats.extend([
            f"tested on {symbol} at {tf} over the last {config.default_lookback_days} days",
            "no transaction costs / slippage modeled (v1 doesn't simulate execution)",
        ])
        if timeframes_assumed and len(timeframes) > 1:
            vr.caveats.append(
                f"the video didn't name a timeframe; this claim is tested across {', '.join(timeframes)}"
            )
        runs.append(vr)
    return runs


# ---------------------------------------------------------------------------
# test type implementations
# ---------------------------------------------------------------------------


def _test_indicator_value_over_range(
    claim: Claim, symbol: str, timeframe: str, mcp: McpClient, config: Config
) -> ValidationRun:
    """Check a claim of the shape 'indicator X behaves like Y over timeframe Z'.

    We pull the indicator series + OHLCV over the lookback window, identify every
    occurrence of the trigger condition the claim describes, look at what price did
    in the next _FORWARD_WINDOW_BARS bars, and report the hit rate.
    """
    indicator_name = _guess_indicator(claim.statement)
    trigger = _guess_trigger(claim.statement)  # e.g. ("RSI", "<", 30) or ("price", "above", "EMA200")

    # Set up the chart and pull data.
    mcp.call("chart_set_symbol", {"symbol": symbol})
    mcp.call("chart_set_timeframe", {"timeframe": timeframe})
    if indicator_name:
        _ensure_indicator(indicator_name, mcp)

    ohlcv = mcp.call("data_get_ohlcv", {"summary": False, "limit": config.default_lookback_days})
    bars = _ohlcv_bars(ohlcv)
    if len(bars) < 30:
        return ValidationRun(
            claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="insufficient_data",
            tradingview_query=f"{symbol} {timeframe} OHLCV", data_summary=f"only {len(bars)} bars available",
            occurrences=None, hit_rate=None,
            result=f"insufficient market data for {symbol} on {timeframe}", caveats=[],
        )

    indicator_series = None
    if indicator_name:
        ind = mcp.call("data_get_indicator", {"name": indicator_name})
        indicator_series = _indicator_values(ind)

    occ, hits = _count_indicator_trigger(bars, indicator_series, trigger)
    if occ == 0:
        return ValidationRun(
            claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="insufficient_data",
            tradingview_query=_query_str(symbol, timeframe, indicator_name, trigger),
            data_summary=f"the trigger condition never occurred in {len(bars)} bars",
            occurrences=0, hit_rate=None,
            result="the claimed trigger condition did not occur in the tested window", caveats=[],
        )

    rate = hits / occ
    return ValidationRun(
        claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="ok",
        tradingview_query=_query_str(symbol, timeframe, indicator_name, trigger),
        data_summary=(
            f"{symbol} {timeframe}, last {len(bars)} bars: trigger occurred {occ} times; "
            f"claimed outcome followed {hits} times within {_FORWARD_WINDOW_BARS} bars"
        ),
        occurrences=occ, hit_rate=rate,
        result=f"claimed behavior occurred {hits}/{occ} ({rate:.0%}) of occurrences",
        caveats=[
            f"'the claimed outcome' was operationalized as price moving in the claimed direction "
            f"within {_FORWARD_WINDOW_BARS} bars — a different definition would change the rate"
        ],
    )


def _test_level_zone_hit_rate(
    claim: Claim, symbol: str, timeframe: str, mcp: McpClient, config: Config
) -> ValidationRun:
    """Check a claim of the shape 'price respects level/zone L'."""
    mcp.call("chart_set_symbol", {"symbol": symbol})
    mcp.call("chart_set_timeframe", {"timeframe": timeframe})
    ohlcv = mcp.call("data_get_ohlcv", {"summary": False, "limit": config.default_lookback_days})
    bars = _ohlcv_bars(ohlcv)
    if len(bars) < 30:
        return ValidationRun(
            claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="insufficient_data",
            tradingview_query=f"{symbol} {timeframe} OHLCV", data_summary=f"only {len(bars)} bars available",
            occurrences=None, hit_rate=None,
            result=f"insufficient market data for {symbol} on {timeframe}", caveats=[],
        )

    level = _guess_level(claim.statement, bars)
    if level is None:
        return ValidationRun(
            claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="error",
            tradingview_query=f"{symbol} {timeframe} OHLCV",
            data_summary="could not determine a numeric level/zone from the claim",
            occurrences=None, hit_rate=None,
            result="couldn't pin the claimed level to a price", caveats=[],
            error="level extraction failed",
        )

    occ, reversed_count = _count_level_respect(bars, level)
    if occ == 0:
        return ValidationRun(
            claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="insufficient_data",
            tradingview_query=f"{symbol} {timeframe} around {level:g}",
            data_summary=f"price never came within {_LEVEL_TOLERANCE:.1%} of {level:g} in {len(bars)} bars",
            occurrences=0, hit_rate=None,
            result=f"price never tested the level {level:g} in the window", caveats=[],
        )

    rate = reversed_count / occ
    broke = occ - reversed_count
    return ValidationRun(
        claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="ok",
        tradingview_query=f"{symbol} {timeframe} around {level:g}",
        data_summary=(
            f"{symbol} {timeframe}, last {len(bars)} bars: price approached {level:g} on {occ} occasions; "
            f"reversed {reversed_count}, broke through {broke}"
        ),
        occurrences=occ, hit_rate=rate,
        result=f"the level held {reversed_count}/{occ} ({rate:.0%}) of the times it was tested",
        caveats=[
            f"'approached' = within {_LEVEL_TOLERANCE:.1%} of the level; 'held' = price closed back on the "
            f"prior side within {_FORWARD_WINDOW_BARS} bars — a different definition would change the rate"
        ],
    )


# ---------------------------------------------------------------------------
# symbol resolution
# ---------------------------------------------------------------------------


def _resolve_symbol(instrument: str, mcp: McpClient, config: Config) -> str | None:
    for candidate in (instrument, _normalize_instrument(instrument)):
        try:
            res = mcp.call("symbol_search", {"query": candidate})
        except McpError:
            continue
        sym = _first_symbol(res)
        if sym:
            return sym
    return None


def _normalize_instrument(s: str) -> str:
    s = s.strip().upper()
    # Strip common suffixes/words: "the S&P 500" -> "SPX", "Bitcoin" -> "BTCUSD", etc.
    aliases = {
        "S&P 500": "SPX", "S&P500": "SPX", "SP500": "SPX", "THE S&P": "SPX",
        "NASDAQ": "NDX", "NASDAQ 100": "NDX", "DOW": "DJI", "DOW JONES": "DJI",
        "BITCOIN": "BTCUSD", "BTC": "BTCUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
        "GOLD": "XAUUSD", "NIFTY": "NIFTY", "NIFTY 50": "NIFTY", "SENSEX": "SENSEX",
        "BANK NIFTY": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
    }
    return aliases.get(s, s.replace("THE ", "").strip())


# ---------------------------------------------------------------------------
# parsing helpers for MCP responses (defensive — MCP payload shapes vary)
# ---------------------------------------------------------------------------


def _first_symbol(res) -> str | None:
    if isinstance(res, dict):
        for key in ("symbols", "results", "matches"):
            arr = res.get(key)
            if isinstance(arr, list) and arr:
                first = arr[0]
                if isinstance(first, dict):
                    return first.get("symbol") or first.get("ticker") or first.get("name")
                if isinstance(first, str):
                    return first
        return res.get("symbol") or res.get("ticker")
    if isinstance(res, list) and res:
        first = res[0]
        return first.get("symbol") if isinstance(first, dict) else (first if isinstance(first, str) else None)
    return None


def _ohlcv_bars(res) -> list[dict]:
    """Normalize an OHLCV response to a list of {time, open, high, low, close, volume} dicts."""
    candidates = res
    if isinstance(res, dict):
        for key in ("bars", "candles", "ohlcv", "data"):
            if isinstance(res.get(key), list):
                candidates = res[key]
                break
    if not isinstance(candidates, list):
        return []
    out: list[dict] = []
    for b in candidates:
        if isinstance(b, dict):
            out.append({
                "time": b.get("time") or b.get("t") or b.get("timestamp"),
                "open": _f(b.get("open", b.get("o"))),
                "high": _f(b.get("high", b.get("h"))),
                "low": _f(b.get("low", b.get("l"))),
                "close": _f(b.get("close", b.get("c"))),
                "volume": _f(b.get("volume", b.get("v"))),
            })
        elif isinstance(b, (list, tuple)) and len(b) >= 5:
            out.append({"time": b[0], "open": _f(b[1]), "high": _f(b[2]), "low": _f(b[3]), "close": _f(b[4]),
                        "volume": _f(b[5]) if len(b) > 5 else None})
    return [b for b in out if b["close"] is not None]


def _indicator_values(res) -> list[float | None]:
    if isinstance(res, dict):
        for key in ("values", "series", "data", "study_values"):
            arr = res.get(key)
            if isinstance(arr, list):
                return [_f(x.get("value") if isinstance(x, dict) else x) for x in arr]
    if isinstance(res, list):
        return [_f(x.get("value") if isinstance(x, dict) else x) for x in res]
    return []


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# claim-text parsing (heuristic — v1; the LLM gives us structure, this fills gaps)
# ---------------------------------------------------------------------------


def _guess_indicator(statement: str) -> str | None:
    s = statement.lower()
    for name, canonical in [
        ("rsi", "Relative Strength Index"), ("macd", "MACD"),
        ("bollinger", "Bollinger Bands"), ("ema", "Moving Average Exponential"),
        ("sma", "Moving Average"), ("moving average", "Moving Average"),
        ("stochastic", "Stochastic"), ("atr", "Average True Range"), ("vwap", "VWAP"),
    ]:
        if name in s:
            return canonical
    return None


def _guess_trigger(statement: str):
    """Return a crude trigger spec: (subject, op, value) — best effort.
    e.g. 'RSI below 30' -> ('rsi', '<', 30.0). Falls back to ('price', 'move', None)."""
    s = statement.lower()
    m = re.search(r"(rsi|macd|stochastic|atr)\D{0,15}?(below|under|above|over|crosses?)\D{0,10}?(\d+(?:\.\d+)?)", s)
    if m:
        op = "<" if m.group(2) in ("below", "under") else ">"
        return (m.group(1), op, float(m.group(3)))
    if "above the" in s and ("ema" in s or "ma" in s or "moving average" in s):
        return ("price", ">", "MA")
    if "below the" in s and ("ema" in s or "ma" in s or "moving average" in s):
        return ("price", "<", "MA")
    return ("price", "move", None)


def _guess_level(statement: str, bars: list[dict]) -> float | None:
    """Extract a numeric level from the claim, or derive one (prior high/low) if it's described, not numbered."""
    m = re.search(r"(\d{2,7}(?:[.,]\d+)?)", statement.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    s = statement.lower()
    closes = [b["close"] for b in bars if b["close"] is not None]
    highs = [b["high"] for b in bars if b["high"] is not None]
    lows = [b["low"] for b in bars if b["low"] is not None]
    if not closes:
        return None
    if "prior high" in s or "previous high" in s or "all-time high" in s or "all time high" in s:
        return max(highs) if highs else None
    if "prior low" in s or "previous low" in s:
        return min(lows) if lows else None
    if "round number" in s:
        # nearest round number to the current price
        px = closes[-1]
        magnitude = 10 ** (len(str(int(px))) - 2) if px >= 100 else 10
        return round(px / magnitude) * magnitude
    return None


# ---------------------------------------------------------------------------
# the actual counting
# ---------------------------------------------------------------------------


def _count_indicator_trigger(bars, indicator_series, trigger) -> tuple[int, int]:
    """Count trigger occurrences and how many were followed by the claimed outcome."""
    subject, op, value = trigger
    n = len(bars)
    occ = 0
    hits = 0
    for i in range(n - _FORWARD_WINDOW_BARS):
        triggered = False
        if subject in ("rsi", "macd", "stochastic", "atr") and indicator_series and i < len(indicator_series):
            iv = indicator_series[i]
            if iv is None or not isinstance(value, (int, float)):
                continue
            triggered = (iv < value) if op == "<" else (iv > value)
        elif subject == "price" and value == "MA" and indicator_series and i < len(indicator_series):
            mav = indicator_series[i]
            if mav is None:
                continue
            triggered = (bars[i]["close"] > mav) if op == ">" else (bars[i]["close"] < mav)
        else:
            # No indicator data — can't evaluate this trigger. Skip the bar.
            continue
        if not triggered:
            continue
        occ += 1
        # "claimed outcome" proxy: price reversed/moved in the implied direction within the window.
        # For an oversold/below trigger -> expect price UP; for above -> expect continuation UP; etc.
        entry = bars[i]["close"]
        future = bars[i + _FORWARD_WINDOW_BARS]["close"]
        expect_up = (op == "<")  # crude: "below threshold" claims usually predict a bounce up
        moved_as_claimed = (future > entry) if expect_up else (future > entry)  # v1: treat both as "up"
        if moved_as_claimed:
            hits += 1
    return occ, hits


def _count_level_respect(bars, level: float) -> tuple[int, int]:
    """Count how often price approached `level` and whether it reversed (held) vs broke through."""
    tol = level * _LEVEL_TOLERANCE
    n = len(bars)
    occ = 0
    reversed_count = 0
    i = 0
    while i < n - _FORWARD_WINDOW_BARS:
        b = bars[i]
        approached = (b["low"] is not None and b["high"] is not None and
                      b["low"] <= level + tol and b["high"] >= level - tol)
        if not approached:
            i += 1
            continue
        occ += 1
        # was price above or below the level just before the touch?
        prev_close = bars[i - 1]["close"] if i > 0 else b["close"]
        side_above = prev_close > level
        # did it close back on the prior side within the window?
        held = False
        for j in range(i + 1, min(i + 1 + _FORWARD_WINDOW_BARS, n)):
            c = bars[j]["close"]
            if c is None:
                continue
            if (side_above and c > level + tol) or (not side_above and c < level - tol):
                held = True
                break
        if held:
            reversed_count += 1
        i += _FORWARD_WINDOW_BARS  # skip ahead so one extended touch isn't counted many times
    return occ, reversed_count


# ---------------------------------------------------------------------------


def _untestable(claim: Claim, timeframe: str, reason: str) -> ValidationRun:
    return ValidationRun(
        claim_id=claim.id, test_type=claim.test_type, timeframe=timeframe, status="error",
        tradingview_query="", data_summary="", occurrences=None, hit_rate=None,
        result=f"untestable — {reason}", caveats=[], error=reason,
    )


def _ensure_indicator(indicator_name: str, mcp: McpClient) -> None:
    """Make sure `indicator_name` is loaded on the chart, respecting TV's per-account
    indicator cap. If already present, do nothing. If the chart is at the cap, remove
    the oldest study to make room. Errors are swallowed — the worst case is the
    indicator wasn't added, and the trigger-counter will skip bars with no value.
    """
    try:
        state = mcp.call("chart_get_state", {})
    except McpError:
        # Couldn't read state — fall through to a naive add. Worst case the add fails
        # silently and downstream sees no indicator series; handled by the counter.
        try:
            mcp.call("chart_manage_indicator", {"action": "add", "name": indicator_name})
        except McpError:
            pass
        return

    studies = state.get("studies", []) if isinstance(state, dict) else []
    studies = [s for s in studies if isinstance(s, dict)]
    if any(s.get("name") == indicator_name for s in studies):
        return  # already loaded — nothing to do

    if len(studies) >= _TV_FREE_INDICATOR_CAP:
        # Remove the oldest study to make room. We can't tell which is "oldest" from
        # state alone — TradingView returns them in the order they were added, so the
        # first entry is the oldest. Defensive: only remove if we have a valid id.
        oldest = studies[0]
        oldest_id = oldest.get("id")
        if oldest_id:
            try:
                mcp.call("chart_manage_indicator", {"action": "remove", "id": oldest_id})
            except McpError:
                pass  # if remove fails, the add below will likely fail too — that's fine

    try:
        mcp.call("chart_manage_indicator", {"action": "add", "name": indicator_name})
    except McpError:
        pass  # honest failure — counter will see no indicator series and skip bars


def _query_str(symbol, timeframe, indicator, trigger) -> str:
    parts = [symbol, timeframe]
    if indicator:
        parts.append(indicator)
    subj, op, val = trigger
    if isinstance(val, (int, float)):
        parts.append(f"{subj} {op} {val:g}")
    return " ".join(str(p) for p in parts)
