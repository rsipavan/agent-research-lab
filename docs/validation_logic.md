# Validation logic

`validate.py` takes a `Claim` and produces a `ValidationRun`. It is the only module that talks to market data, and it does so through the TradingView MCP — `symbol_search`, `data_get_ohlcv`, `data_get_indicator`, `data_get_study_values`, `quote_get`. It never holds API keys for a data vendor directly; the MCP owns that.

## What each test type actually does

### `indicator_value_over_range`

The claim names an indicator, a timeframe, and a behavior (e.g. "RSI under 30 on the daily marks reversals," "price above the 200 EMA means uptrend," "MACD cross precedes a move").

1. `symbol_search` → resolve the instrument to a TradingView symbol.
2. `chart_set_symbol` + `chart_set_timeframe` → set the chart to the claimed timeframe.
3. `chart_manage_indicator` → ensure the named indicator is on the chart.
4. `data_get_indicator` / `data_get_study_values` → pull the indicator series over `config.defaults.lookback_days`.
5. Compute the claimed relationship against the actual series. Concretely: identify every occurrence of the trigger condition (e.g. RSI crossing below 30), look at what price did in the claimed window after each, and report **how often the claimed outcome occurred** — `bounced 14 of 22 occurrences (64%)`.

It does **not** conclude "the claim is true" or "false." It reports the rate. The verdict (`holds` / `partial` / `fails`) is assigned in `report.py` by explicit thresholds (see below), not by the validator.

### `level_zone_hit_rate`

The claim names a level or zone (support/resistance, POC, VWAP, prior high/low, a round number).

1. `symbol_search` + `chart_set_timeframe`.
2. `data_get_ohlcv` (with `summary=true` unless individual bars are needed).
3. Identify every bar where price came within a tolerance of the level. For each, classify: **reversed** (price moved away in the expected direction) or **broke through** (price closed beyond the level).
4. Report the hit rate — `price approached 24,550 on 9 occasions; reversed 6, broke 3 (67%)`.

Same discipline: it reports the rate, `report.py` assigns the verdict.

### `strategy_backtest` — not in v1

Strategy-shaped claims (full entry + exit rules, "this system makes money") are marked `untestable — needs a backtest engine`. v1 does not have one and will not fake one. The report says so plainly. (A real backtest engine — with proper out-of-sample handling, slippage stress, and look-ahead-bias checks — is a separate project. Doing it badly is worse than not doing it.)

## How the verdict is computed (in `report.py`, not here)

Given a `ValidationRun` with a hit rate `r` and the number of occurrences `n`:

| Condition | Verdict |
|---|---|
| `status != ok` | `untestable` (with the reason: insufficient data / MCP error / disabled / no symbol) |
| `n < 10` | `partial` — "too few occurrences (`n`) to conclude; observed rate `r`, but the sample is small" |
| `n ≥ 10` and `r ≥ 0.65` | `holds` — "claimed behavior occurred `r` of the time over `n` occurrences" |
| `n ≥ 10` and `0.45 ≤ r < 0.65` | `partial` — "occurred `r` of the time — roughly coin-flip; the claim overstates it" |
| `n ≥ 10` and `r < 0.45` | `fails` — "occurred only `r` of the time over `n` occurrences" |

These thresholds are deliberately blunt and stated openly. They are not the last word on whether a strategy is good — they're an honest first-pass sanity check against the *strength of the claim as stated*. A claim that says "always" and tests at 64% is overstated; the report says exactly that.

## Caveats the validator always attaches

Every `ValidationRun` carries a `caveats` list. Standard entries:

- Survivorship / single-symbol: "tested on one instrument over one period; not a cross-market or cross-regime check."
- Definition sensitivity: "'reversal' / 'bounce' was operationalized as [specific definition]; a different definition would change the rate."
- Lookback window: "tested over the last `N` days; a different window may give a different rate."
- Timeframe assumed (for `partial` claims): "the video didn't specify a timeframe; used `config.defaults.timeframe`."
- No transaction costs / slippage modeled (always — v1 doesn't simulate execution).

The report surfaces these next to the verdict. The whole point is that a reader sees not just "64%" but "64%, on one symbol, over one year, with reversal defined as X, and the claim said 'always.'"

## MCP setup

The TradingView MCP server must be running and reachable. If you run it over stdio (the default), `TRADINGVIEW_MCP_URL` in `.env` stays empty and the pipeline launches/attaches to it locally. If you run it over HTTP/SSE, set `TRADINGVIEW_MCP_URL`. On any MCP call failure, `validate.py` retries once (`config.validation.mcp_retries`) then returns `status: error` with the message — which becomes an `untestable` verdict in the report, never a silent skip.
