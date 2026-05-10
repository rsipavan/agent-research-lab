# agent-research-lab

**Designing reliable autonomous research and validation systems under uncertainty.**

You DM a YouTube URL to a Telegram bot. An agent fetches the transcript, extracts the *testable* trading claim(s), runs a defined validation against real market data via the [TradingView MCP](https://github.com/), produces a structured report — thesis, what was tested, the data, the result, the caveats, a verdict — and replies. Every step is traced. Every failure mode is handled and documented.

This is a small, focused system. It is deliberately small. The interesting part is not "AI watches YouTube videos" — it's the orchestration, the decision logic for *what is even testable*, the validation layer, the failure-aware workflow, and the observability trail. The pattern transfers to any autonomous-research problem; the domain here happens to be trading content.

---

## What it does

```mermaid
flowchart LR
    A[Telegram DM<br/>YouTube URL] --> B[transcript.fetch]
    B --> S[summarize<br/>what kind of video?]
    S --> D0{claim-bearing?}
    D0 -- no<br/>mindset / vlog / promo --> R1[Report:<br/>what this video is<br/>+ nothing to validate]
    D0 -- yes --> C[thesis.extract<br/>LLM → testable claims]
    C --> D{testable?}
    D -- no --> R2[Report:<br/>summary + claims +<br/>why untestable]
    D -- partial / yes --> E[validate.run<br/>TradingView MCP]
    E --> F[report.build<br/>summary + computed verdicts]
    R1 --> G[Telegram reply]
    R2 --> G
    F --> G
    B -.trace.-> T[(traces/run-id.jsonl)]
    S -.trace.-> T
    C -.trace.-> T
    E -.trace.-> T
    F -.trace.-> T
```

1. **Ingest** — `transcript.py` pulls and cleans the YouTube transcript.
2. **Summarize** — `summarize.py` runs first and characterizes the video: strategy/backtest, educational explainer, market commentary, trader psychology, vlog, course pitch, or a mix. It writes a `content_type`, a topic, a 2-4 sentence summary, and a `has_checkable_claims` flag. If the video isn't claim-bearing by nature (psychology / vlog / promo) the pipeline stops here and returns a summary-only report — running a claim extractor over a pep-talk to find zero claims is wasted effort, and saying plainly "this is a mindset video about X" is the honest output.
3. **Extract** — for claim-bearing videos, `thesis.py` extracts *testable claims* — each tagged with instrument, timeframe, test type, and whether it's actually testable (with a reason if not). It gets the summary as context. The agent is allowed — and expected — to say "this is a take, not a checkable claim."
4. **Validate** — `validate.py` calls the TradingView MCP (`symbol_search`, `data_get_ohlcv`, `data_get_indicator`, `data_get_study_values`) to pull real market data and check the claim. v1 supports two test types: indicator-value-over-range checks and level/zone hit-rate checks. Strategy-shaped claims that need a full backtest are honestly marked "untestable in v1 — needs a backtest engine."
5. **Report** — `report.py` builds the report. It **leads with "what this video is"**, then (if there were claims) a structured per-claim result: `{thesis, what_was_tested, data_summary, result, caveats, verdict}` where verdict ∈ {holds, partial, fails, untestable}. Verdicts are *computed* from the validation data, not LLM-judged.
6. **Reply** — `telegram_bot.py` posts the report back.
7. **Trace** — every run writes `traces/<run-id>.jsonl`, one line per step. For the examples in this repo, those traces are committed so you can read the agent's reasoning trail.

## What it deliberately does NOT do (yet)

This is v1. Out of scope on purpose — each of these would be a separate, larger effort:

- ❌ Instagram / TikTok ingestion
- ❌ Video → Pine Script (or any chart-language) code generation
- ❌ A general backtest engine for strategy-shaped claims
- ❌ A standalone replay engine
- ❌ A separate decision-graph orchestration layer (v1 orchestration is sequential and simple, on purpose)
- ❌ Multi-test / batch processing
- ❌ A look-ahead-bias detector (a genuinely good idea — it deserves its own repo)

The point of v1 is one vertical slice that works end-to-end, is observable, and handles failure honestly. Everything else iterates publicly from there.

## Architecture

See [`docs/architecture.md`](docs/architecture.md). In short:

```
src/agent_research_lab/
├── telegram_bot.py    # input/output edge: listens for YouTube URLs, replies with reports
├── transcript.py      # YouTube transcript fetch + clean
├── summarize.py       # transcript → "what kind of video is this?" (runs first; routes the pipeline)
├── thesis.py          # transcript + summary → testable claims (via llm.py)
├── validate.py        # claim → validation run via TradingView MCP (via mcp_client.py)
├── report.py          # summary + validation runs → report (leads with "what this video is"; verdicts computed, not LLM-judged)
├── orchestrate.py     # the sequential pipeline; logs each step to traces/
├── llm.py             # backend-agnostic LLM: claude CLI (default) | Anthropic API | Gemini API
├── mcp_client.py      # thin TradingView MCP client (retries, error → untestable, never crashes a run)
├── config.py          # loads config.yml + .env
└── types.py           # the dataclasses passed between modules
```

Each module has one job, a small typed interface, and can be tested in isolation. The data contract between them is documented in `docs/architecture.md`.

## The interesting docs

- [`docs/decision_logic.md`](docs/decision_logic.md) — how the agent decides what counts as a testable claim, and which test type to run
- [`docs/validation_logic.md`](docs/validation_logic.md) — what the validation actually does, what it can and can't conclude, why
- [`docs/failure_handling.md`](docs/failure_handling.md) — the failure matrix: no transcript, no testable claim, ambiguous claim, MCP error, insufficient data — what happens in each case and why it's handled there

## Run it

```bash
# install
pip install -e .

# one-shot CLI (used to build the examples/)
python -m agent_research_lab.orchestrate "https://www.youtube.com/watch?v=..."

# long-running Telegram listener
python -m agent_research_lab.telegram_bot
```

**LLM backend — no API key required.** The pipeline needs an LLM for thesis extraction
(and an optional report-narration step). It auto-detects, in order:

1. the **`claude` CLI** on your PATH (Claude Code) — uses your existing subscription, no key needed. **This is the default.**
2. `ANTHROPIC_API_KEY` set — Anthropic API (`pip install 'agent-research-lab[anthropic]'`)
3. `GEMINI_API_KEY` set — Gemini API, whose free tier covers this workload (`pip install 'agent-research-lab[gemini]'`)

Force a backend with `AGENT_RESEARCH_LAB_LLM={claude_cli,anthropic,gemini}`. If you have the `claude` CLI, you don't need any API key at all. See `src/agent_research_lab/llm.py`.

**Other config:** copy `.env.example` → `.env`. For the Telegram listener you need `TELEGRAM_BOT_TOKEN`. For validation runs you need a running TradingView MCP — set `TRADINGVIEW_MCP_URL` to its endpoint (or leave empty and validation steps will honestly report "untestable — MCP not configured"; see `docs/validation_logic.md`). `config.yml` controls which test types are enabled.

## Examples

[`examples/`](examples/) contains real YouTube trading videos run through the pipeline — input, transcript, extracted thesis, validation run, final report, and the full trace. These are the artifact: read one to see exactly what the agent did and decided.

## Status

v1. Working end-to-end. Built solo. Iterating publicly.

## Why this exists

I build autonomous research and validation systems — that's the work. This repo is a small, public, end-to-end demonstration of how I approach it: separate the testable from the untestable, validate against real data not vibes, handle every failure mode explicitly, and leave a trace someone else can read. The trading domain is incidental; the engineering pattern is the point.

— [R Sai Pavan](https://www.linkedin.com/in/sai-pavan-86635b23/) · saipavan.pilot1@gmail.com

## License

MIT
