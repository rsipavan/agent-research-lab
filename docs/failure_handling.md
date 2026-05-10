# Failure handling

Every failure mode below has a defined behavior and a place in the code where it's handled. None of them crash the run, none of them silently skip, all of them end in a report that says what happened. "The agent couldn't do X" is information, not an error to swallow.

## The failure matrix

| Failure | Where it surfaces | Behavior | What the user gets |
|---|---|---|---|
| YouTube URL malformed / not a YouTube link | `telegram_bot.py` (input parse) | Reject before any work | Reply: "that doesn't look like a YouTube URL — send a `youtube.com/watch?v=…`, `youtu.be/…`, or Shorts link" |
| No transcript / captions on the video | `transcript.py` | Stop; build a minimal report | Report: `verdict_overall = untestable — no transcript available for this video`; trace records `transcript.fetch ok=false` |
| Transcript fetched but empty / near-empty | `transcript.py` | Treat as no transcript | Same as above, reason `transcript too short to analyze (N words)` |
| Transcript fetched but it's not trading content at all | `thesis.py` (extraction returns zero claims) | Stop after extraction; build report | Report: `untestable — this video contains no trading claims (topic appears to be: <what the LLM saw>)` |
| Video has trading content but no *testable* claims (all opinion/narrative) | `thesis.py` (all claims classified `no`) | Stop after extraction; build report | Report: `untestable — the video makes claims but none are checkable (they are: opinions / predictions-without-mechanism / sales)`; lists the claims it saw and why each isn't testable |
| Claim is `partial` — missing instrument | `thesis.py` → `validate.py` | If no instrument and no `config.defaults.symbol_fallback` (default: none) → can't run | That claim's verdict: `untestable — no instrument named and no fallback configured`; other claims still processed |
| Claim is `partial` — missing timeframe | `thesis.py` → `validate.py` | Use `config.defaults.timeframe`, flag it | Verdict computed normally, caveat: `timeframe not specified in the video; assumed <default>` |
| Claim maps to a disabled test type (e.g. `strategy_backtest`) | `validate.py` | Don't run | Verdict: `untestable — needs a backtest engine (not in v1)` or `untestable — test type disabled in config` |
| `symbol_search` returns no match for the named instrument | `validate.py` | Retry once with a normalized name; if still nothing | Verdict: `untestable — could not resolve "<instrument>" to a tradeable symbol` |
| TradingView MCP call errors (timeout, transport, server down) | `validate.py` | Retry once (`config.validation.mcp_retries`); if still failing | Verdict: `untestable — validation failed: <error>`; trace records the error; **the whole run does not abort** — other claims still get processed |
| MCP returns data but the range has too few occurrences of the trigger (`n < 10`) | `report.py` (verdict rule) | Don't pretend `n=3` is conclusive | Verdict: `partial — only N occurrences in the tested window; observed rate R but the sample is too small to conclude` |
| MCP returns no data for the symbol/timeframe (e.g. new listing, illiquid) | `validate.py` | — | `status: insufficient_data` → verdict `untestable — insufficient market data for <symbol> on <timeframe>` |
| LLM (Anthropic) call errors during extraction | `thesis.py` | Retry once; if still failing | Whole run aborts with a clear message (extraction is load-bearing — without it there's nothing to validate). Reply: "couldn't analyze the transcript right now — try again." Trace records the failure. |
| LLM call errors during report narration | `report.py` | Fall back to a template-rendered report (the verdicts are computed, not LLM-generated, so they're intact) | A plainer report, still correct, with a note: `(narrative synthesis unavailable; verdicts and data are unaffected)` |
| Telegram send fails (user blocked the bot, network) | `telegram_bot.py` | Log it; don't crash the listener | Nothing to the user (they can't receive it); the run + report are still written to `traces/` and recoverable |
| Telegram user not on the allowlist (if `TELEGRAM_ALLOWLIST` is set) | `telegram_bot.py` | Reject before any work | Reply: "this bot is private." |

## Two design rules behind the matrix

1. **One bad claim never kills the run.** Claims are processed independently. If claim 2's symbol doesn't resolve, claim 1 still gets a verdict and the report covers both — claim 1 with its result, claim 2 with `untestable — <reason>`.

2. **The only thing that aborts a run is a load-bearing failure with no useful partial output:** no transcript at all, or extraction itself failing. Everything downstream of "we have testable claims" degrades to per-claim `untestable` verdicts rather than aborting. The user always gets *a* report, and the report always says what the agent could and couldn't do.

## Where this is enforced

- `telegram_bot.py` — input validation, allowlist, send-failure handling
- `transcript.py` — no-transcript / empty-transcript → returns a `Transcript` with `text=""` and the orchestrator short-circuits to a minimal report
- `thesis.py` — extraction LLM retry; zero-claims and all-`no` are normal returns, not exceptions
- `validate.py` — per-claim try/except; MCP retry; symbol-resolution retry; returns `ValidationRun` with `status ∈ {ok, error, insufficient_data}` — never raises past itself
- `report.py` — verdict rules handle every `status`; LLM-narration failure falls back to template
- `orchestrate.py` — wraps the whole thing; the only place a load-bearing failure (no transcript, extraction failure) turns into an aborted run with a user-facing message; logs every step's `ok` flag to the trace
