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

## The full verdict range — all four outcomes are represented

The examples cover every outcome the pipeline can produce:

| Verdict | What it means | Example |
|---------|---------------|---------|
| `holds` | Claim validated — hit rate above threshold | [08 — SPY 200-day SMA support](08_spy_200sma_support_holds/) |
| `fails` | Claim tested and rejected — below threshold | [06 — ORB Pine strategy backtest](06_orb_pine_strategy_backtest/) |
| `untestable` | Claim not checkable — named specifically why | 01, 02, 03, 04, 05, 07 |

The `untestable` verdict is not a failure mode — it's the pipeline refusing to manufacture a number. The common failure of "AI validates YouTube strategies" tooling is forcing every video into a test and reporting a result that means nothing. Here, `summarize.py` (routes by content type), `thesis.py` (can return "that's a take, not a checkable claim"), and `validate.py` (can return "untestable — no instrument named / MCP not configured") are all allowed to bail out honestly. Those are first-class outcomes.

See [`../docs/decision_logic.md`](../docs/decision_logic.md).

## Distinct paths through the pipeline

| Example | Path | Outcome |
|---|---|---|
| `01_rsi_bollinger_tested_2025` | full extraction → claim untestable | Strategy results can't be reproduced — the instrument universe and capital assumptions are never enumerated |
| `02_rsi_profitable_or_overhyped` | full extraction → strategy-shaped | Claims are opinion, not checkable |
| `03_rsi_divergence_xauusd` | full extraction → no instrument | The strategy has no named instrument — no specific series to test against |
| `04_alphainsider_promo_walkthrough` | **summary-only** (promotion) | Platform promo — nothing about market behavior to validate; pipeline short-circuits at summarize |
| `05_orb_acceptance_short_no_claims` | **summary-only** (no checkable claims) | Directional advice with no instrument, timeframe, or threshold; `has_checkable_claims: false` |
| `06_orb_pine_strategy_backtest` | full extraction → Pine synthesis → backtest | Strategy compiled and ran: 66 trades, PF 0.91, net negative — **fails** |
| `07_ict_key_levels_educational` | full extraction → framework only | Teaching a methodology, not making a claim |
| `08_spy_200sma_support_holds` | full extraction → indicator validation | 52 occurrences, 73% hit rate, above 65% threshold — **holds** |

Examples #4–#5 demonstrate the `summary.skip_extraction` short-circuit. Examples #1–#3, #7–#8 walk the full pipeline. Example #6 exercises the Pine Script synthesis and strategy tester path.
