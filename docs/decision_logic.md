# Decision logic

Two decisions matter in this pipeline. Both live in `thesis.py`. Getting them right is most of the value.

## Decision 1 — Is this a testable claim?

A trading video says a lot of things. Almost none of them are checkable. The agent classifies each candidate claim into one of:

| Class | Meaning | Example | Outcome |
|---|---|---|---|
| `yes` — testable | Names (or strongly implies) an instrument, a timeframe, and a checkable relationship | "On the daily, when RSI drops below 30 on SPY, price reverses within 3 candles" | Goes to `validate.run` |
| `partial` — under-specified | A real claim but missing a piece needed to test it | "Oversold always bounces" (no instrument, no timeframe, no threshold) | Goes to `validate.run` with best-effort defaults from `config.yml`; report flags exactly what was assumed |
| `no` — not a claim | Opinion, narrative, prediction-without-mechanism, motivational content, sales pitch | "I really think Q3 is going to be bullish" / "This strategy changed my life" | Skipped; report says *why* it isn't testable |

The classifier is an LLM prompt with the rubric above, run over the transcript, returning structured JSON. It is told: **err toward `partial` over `yes`, and toward `no` over `partial`.** A false "this is testable" wastes a validation run and produces a misleading report. A false "this isn't testable" just means the report says "this video had no checkable claims" — which is often the honest answer for trading content, and saying so plainly is itself useful.

Confidence: each claim carries the LLM's confidence (0–1). Claims rated below `config.extraction.min_confidence` are downgraded one rung (`yes`→`partial`, `partial`→`no`).

Cap: `config.extraction.max_claims_per_video` (default 3). If a video has more candidate claims, the agent keeps the ones most central to the video's argument, not the first three it found.

## Decision 2 — Which test type?

For a `yes` or `partial` claim, the agent picks a test type:

| Claim shape | Test type | What runs |
|---|---|---|
| "Indicator X behaves like Y over timeframe Z" (RSI, MACD, MA cross, BB, etc.) | `indicator_value_over_range` | Pull the indicator's values over `lookback_days` via the MCP; compute whether the claimed behavior holds, and how often |
| "Price respects level/zone L" (support, resistance, POC, VWAP, round numbers, prior high/low) | `level_zone_hit_rate` | Pull OHLCV; count how often price approached L and reversed vs. broke through; report the hit rate |
| "Strategy S (entry + exit rules) is profitable" | `strategy_backtest` | **v1: not implemented.** Marked `untestable — needs a backtest engine`. Honest boundary. |
| Anything else | — | Marked `untestable — no test type maps to this claim` |

The mapping is, again, an LLM step that returns one of the enum values with a one-line justification — which goes into the trace and the report. If `config.test_types.<type>` is `false`, claims mapping to it get `untestable (test type disabled)`.

## Why the agent is allowed to say "I can't test this"

The most common failure mode of "AI validates YouTube strategies" tooling is forcing every video into a test and reporting a number that means nothing. This pipeline treats "untestable" as a normal, frequent, correct outcome. A report that says *"this video made no checkable claims — it was a narrative about the creator's trading journey"* is doing its job. The discipline of refusing to manufacture a verdict is the point of `thesis.py` existing as its own module with `no` as a first-class return value.
