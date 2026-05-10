# Roadmap

v1 (current) is one vertical slice that works end-to-end and is observable: a YouTube
trading video → characterized → checkable claims extracted → validated against real
market data via the TradingView MCP → a traced, structured report. See the README for
exactly what v1 does and deliberately doesn't.

Everything below is the iterate-publicly backlog. Order is rough; each item is its own
PR (or its own repo, where noted), built after v1 is public — not before.

## v1.x — robustness, no new surface

- **TradingView account-tier awareness.** Most TradingView accounts are free, and the
  free tier has real limits: fewer indicators per chart, capped historical bars, no
  second-based timeframes, fewer simultaneous symbols. `mcp_client.py` (or a probe at
  startup) should detect free vs. paid and `validate.py` should adjust — e.g. fall back
  to a shorter lookback, or mark a claim `untestable — needs more history than this
  TradingView tier provides` instead of silently returning thin data. Right now the
  pipeline assumes whatever the MCP returns is enough; it should know the ceiling.
- **stdio transport for the TradingView MCP** in `mcp_client.py` (currently HTTP/SSE
  only; stdio is a TODO).
- **Tighter `reason_if_not` on `no` claims** — the extractor sometimes leaves it null;
  the report then falls back to a generic line. Make it required in the prompt.
- **Title/channel metadata** on `Transcript` (the transcript API doesn't give it; pull
  it from oEmbed or yt-dlp so reports/examples read better).

## v2 — the idea book (accumulated knowledge)

A persistent, append-only **knowledge book** — like the one in the author's private
research stack: every processed video adds an entry, and the book tracks *which kinds of
claims held vs. failed across all videos seen*. Concretely:

- `knowledge/INDEX.md` — one line per video processed (date, channel, content_type,
  overall verdict, link to the report)
- `knowledge/claims.jsonl` — append-only: every claim ever extracted, its verdict, the
  data behind it
- `knowledge/patterns.md` — periodically synthesized: "RSI-oversold-bounce claims have
  held in 3/9 cases tested, all on indices, none on alts" — accumulated wisdom, with the
  evidence
- The Telegram bot gains a `/knowledge` command: "what have we learned about <topic>?"

This is the "it improves itself" part — the system gets more useful the more videos run
through it.

## v2 — Pine Script synthesis

For `strategy_or_claim` videos that describe a full strategy:

- **If the video shows/quotes Pine Script** (many do — creators screen-share their
  indicator/strategy code): extract those snippets, and if there are several pieces,
  **merge them into one coherent strategy script**.
- **If the video describes the strategy but shows no code**: **write the Pine Script from
  scratch** from the described entry/exit rules.
- Either way: output a `.pine` file alongside the report, ready to drop into TradingView,
  plus a note on what was assumed where the video was vague.
- This pairs with the backtest engine below — once you can synthesize the strategy as
  Pine, you can run TradingView's own strategy tester on it via the MCP and report the
  results, which is the missing `strategy_backtest` test type.

## v2 — a real backtest engine (the `strategy_backtest` test type)

v1 honestly marks strategy-shaped claims `untestable — needs a backtest engine`. v2 adds
one: take a synthesized Pine strategy, run TradingView's strategy tester on it via the
MCP over a defined period and symbol, and report the strategy-tester metrics — with the
usual caveats (single symbol, single period, no slippage modeling unless TV's tester
includes it). Done carefully, with out-of-sample handling. Done badly, it's worse than
not doing it — so it waits until it can be done right.

## v2 — more ingestion sources

- Instagram / TikTok trading content (Reels, Shorts) — same pipeline, different
  transcript source. Lower-quality data; lower priority.
- Newsletters / Substack / X threads — text-native, no transcript step.

## v2 — more test types in `validate.py`

- Pattern claims ("head and shoulders predicts a drop") — pattern detection + forward-return stats.
- Seasonality claims ("Mondays are bullish on SPX") — day-of-week / calendar stats.
- Correlation claims ("when DXY drops, gold rises") — rolling correlation over a window.

## v2 — multi-test / batch

Process a list of videos at once (a channel, a playlist) and produce a comparative
report. Wire it to a scheduled scan if you want a standing "what did the trading-YouTube
ecosystem claim this week, and which of it holds" feed.

## Separate repo — look-ahead-bias detector

A standalone tool: given a backtest spec or strategy code, detect look-ahead leakage
(centered windows on pivot detection, using future bars in a signal, repainting
indicators, survivorship in the universe). Look-ahead bias is the #1 backtesting sin; a
tool that catches it is squarely "validation infrastructure." Deserves its own repo, not
a corner of this one.

---

**Contributions welcome on any of the above.** Each item is sized to be one focused PR
against a working v1 — which is the point of shipping v1 small.
