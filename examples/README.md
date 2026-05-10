# Examples

Real YouTube trading videos run through the pipeline. Each folder is a complete record
of one run:

- `input.md` — the URL and a one-line note on why this video was picked
- `transcript.txt` — the fetched transcript
- `summary.json` — what the agent decided the video *is* (content_type, topic, summary, has_checkable_claims) — produced by the summarize step before any claim extraction
- `thesis.json` — the claims the agent extracted, each classified for testability
- `report.md` — the human-readable report. It **leads with "What this video is"**, then the claims. This is what the Telegram bot would send back.
- `report.json` — the structured report
- `trace.jsonl` — the step-by-step trace: one line per pipeline step (`transcript.fetch` → `video.summarize` → `thesis.extract` → `validate.run`* → `report.build`), with timing

Read any one `report.md` + its `trace.jsonl` to see exactly what the agent did and decided.

## How these were generated

`python scripts/build_examples.py` from the repo root — it runs `orchestrate.process(url)`
on each video and writes the folder. The LLM backend used was the **`claude` CLI** (Claude
Code, no API key — see the main README). Re-running it reproduces these (modulo any new
videos / changed transcripts).

## What you'll notice: every verdict here is `untestable` — and that's the point

All three videos are "I tested a trading strategy" videos. None of them names the specific
instruments or timeframes the backtest ran on ("the 100 most liquid crypto", "all these
markets", "long time frames"). So the agent classifies their claims as **not independently
checkable** and says so — with a specific reason per claim — rather than manufacturing a
verdict from data that doesn't match what the video described.

That refusal is the most important behavior in the pipeline. The common failure mode of
"AI validates YouTube strategies" tooling is forcing every video into a test and reporting a
number that means nothing. Here, `thesis.py` is allowed to return `no` ("this isn't a checkable
claim") and `validate.py` is allowed to return `untestable` ("needs a backtest engine" /
"no instrument named" / "MCP not configured") — and those are first-class outcomes, not errors.
See [`../docs/decision_logic.md`](../docs/decision_logic.md).

The three examples cover three distinct *untestable* reasons:

| Example | Why untestable |
|---|---|
| `01_rsi_bollinger_tested_2025` | Strategy results can't be reproduced — the instrument universe ("100 most liquid crypto") and capital assumptions are never enumerated |
| `02_rsi_profitable_or_overhyped` | Claims are strategy-shaped (full entry/exit rules + "is it profitable") → map to `strategy_backtest`, which is honestly out of scope in v1 |
| `03_rsi_divergence_xauusd` | The backtest spans unnamed markets and unspecified timeframes; the headline numbers (68–70% win rate, ~37% growth) can't be checked against any single series |

## What's NOT shown here yet

A *validation-complete* run — a video that makes a crisp checkable claim ("on the daily, when
RSI drops below 30 on SPY, price bounces within 3 candles"), the agent resolving the symbol,
pulling the indicator series via the TradingView MCP, counting occurrences, and reporting a
`holds` / `partial` / `fails` verdict with the hit rate.

To produce that, you need (1) a video with such a claim and (2) a TradingView MCP reachable
via `TRADINGVIEW_MCP_URL` (see [`../docs/validation_logic.md`](../docs/validation_logic.md)).
Without the MCP, a testable claim runs through `validate.py` and comes back
`untestable — TradingView MCP not configured` — the failure path being handled, just not the
happy path. Add the MCP, re-run `scripts/build_examples.py` with a suitable video in the list,
and that example appears.
