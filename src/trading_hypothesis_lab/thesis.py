"""Thesis extraction: transcript -> testable claims.

This is the module that decides what is even checkable. It runs an LLM over the
transcript with an explicit rubric (see docs/decision_logic.md), gets back
structured JSON, and applies the config gates (confidence floor, claim cap,
disabled test types).

"Zero claims" and "all claims classified `no`" are NORMAL returns, not errors —
they mean the video had nothing testable, which the report states plainly.

The only thing that raises out of here is the LLM call failing after a retry:
extraction is load-bearing, so the orchestrator turns that into an aborted run
with a user-facing "try again" rather than a half-report.
"""

from __future__ import annotations

import json
import time

from . import knowledge as knowledge_mod
from . import llm
from .config import Config
from .types import Claim, ThesisSet, Transcript, VideoSummary


class ExtractionError(RuntimeError):
    """Raised when the LLM extraction call fails after retrying. Load-bearing failure."""


_SYSTEM_PROMPT = """\
You analyze transcripts of trading / markets YouTube videos and extract the CHECKABLE claims.

Most trading content is opinion, narrative, prediction-without-mechanism, or sales. Your job is to \
honestly separate "this is a claim that could be checked against market data" from "this is a take." \
Err toward classifying a claim as `partial` over `yes`, and toward `no` over `partial`. A false \
"this is testable" produces a misleading report; a false "this isn't testable" just means we say the \
video had no checkable claims — which is often the honest answer, and saying so is useful.

For each candidate claim, classify it:

- "yes"  — names (or strongly implies) an INSTRUMENT, a TIMEFRAME, and a CHECKABLE RELATIONSHIP.
           e.g. "On the daily, when RSI drops below 30 on SPY, price reverses within 3 candles."
- "partial" — a real claim but missing a piece needed to test it.
           e.g. "Oversold always bounces." (no instrument, no timeframe, no threshold)
- "no"   — opinion / prediction-without-mechanism / motivational / sales / pure narrative.
           e.g. "I think Q3 will be bullish." / "This strategy changed my life."

Then assign a test_type for `yes`/`partial` claims:
- "indicator_value_over_range" — "indicator X behaves like Y over timeframe Z" (RSI, MACD, MA cross, BB, etc.)
- "level_zone_hit_rate" — "price respects level/zone L" (support/resistance, POC, VWAP, round numbers, prior high/low)
- "strategy_backtest" — a strategy with defined mechanical entry AND exit rules on a named instrument. If the video gives entry trigger + stop + target (even if the logic involves divergence, swing points, or multi-condition filters), this is the right type. Pine Script can implement any mechanical rule — do not downgrade to "no" just because the logic is complex.
- "none" — a `yes`/`partial` claim that no test type maps to. For `no` claims, use "none".

Also give:
- instrument: the ticker/symbol named or strongly implied, or null
- timeframe: e.g. "1D", "4H", "15m", or null
- confidence: 0..1, how confident you are this is a real, correctly-parsed claim (not how confident you are it's TRUE)
- reason_if_not: for `partial`/`no`, one sentence on what's missing or why it isn't testable. For `yes`, null.
- test_type_justification: one short sentence on why that test type. For `no`/`none`, "".
- statement: the claim restated cleanly in one sentence.

Output ONLY a JSON object: {"claims": [ {...}, ... ]}. If the video has NO trading claims at all, \
return {"claims": []}. Do not wrap in markdown fences. Do not add commentary.
"""

_USER_TEMPLATE = """\
Video: {title} — {channel}
URL: {url}

What this video is (from the summarize step): {content_type} — {topic}
{summary}

Transcript:
\"\"\"
{transcript}
\"\"\"

{prior_patterns}Extract the checkable claims as specified. JSON only."""


def extract(transcript: Transcript, summary: VideoSummary | None, config: Config) -> ThesisSet:
    """Run extraction. Returns a ThesisSet (possibly with zero claims). Raises
    ExtractionError if the LLM call fails after a retry. `summary` (from the
    summarize step) is passed to the model as context so it calibrates — e.g. an
    educational video usually has few/weak claims; a strategy_or_claim video should
    have a real one."""
    if transcript.is_empty:
        # The orchestrator normally short-circuits before calling us, but be safe.
        return ThesisSet(video_id=transcript.video_id, claims=[])

    raw = _call_llm(transcript, summary, config)
    claims = _parse_claims(raw, transcript.video_id)
    claims = _apply_gates(claims, config)
    return ThesisSet(video_id=transcript.video_id, claims=claims)


# ---------------------------------------------------------------------------


def _call_llm(transcript: Transcript, summary: VideoSummary | None, config: Config) -> str:
    # Infer the most likely claim type(s) this video will produce, to pull relevant
    # prior failure traces from the knowledge base before asking the LLM to extract.
    ct = summary.content_type if summary else "unknown"
    likely_type = "strategy_backtest" if ct in ("strategy_or_claim",) else "indicator_value_over_range"
    prior = knowledge_mod.format_prior_patterns_for_prompt(likely_type)
    if prior:
        prior = prior + "\n\n"

    user = _USER_TEMPLATE.format(
        title=transcript.title or "(unknown title)",
        channel=transcript.channel or "(unknown channel)",
        url=transcript.url,
        content_type=ct,
        topic=summary.topic if summary else "(not summarized)",
        summary=summary.summary if summary else "",
        # cap the transcript we send — long videos get truncated; the first ~12k words
        # carry the thesis in practice
        transcript=" ".join(transcript.text.split()[:12000]),
        prior_patterns=prior,
    )

    last_err: Exception | None = None
    for attempt in range(2):  # one retry
        try:
            return llm.complete(_SYSTEM_PROMPT, user, model=config.anthropic_model or None, max_tokens=2000)
        except llm.LlmUnavailable:
            # No backend at all — not worth retrying; surface immediately.
            raise ExtractionError(
                "no LLM backend available — install the `claude` CLI (no key needed), "
                "or set ANTHROPIC_API_KEY / GEMINI_API_KEY (see README)"
            ) from None
        except llm.LlmError as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
    raise ExtractionError(f"LLM extraction failed after retry: {last_err}")


def _parse_claims(raw: str, video_id: str) -> list[Claim]:
    raw = raw.strip()
    # Be tolerant of a stray ```json fence.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # The model didn't return clean JSON. Treat as "no claims" rather than crashing —
        # a degraded-but-honest outcome. (This is rare; if it recurs, tighten the prompt.)
        return []

    out: list[Claim] = []
    for i, c in enumerate(data.get("claims", []), start=1):
        try:
            out.append(
                Claim(
                    id=f"c{i}",
                    statement=str(c.get("statement", "")).strip(),
                    instrument=(c.get("instrument") or None),
                    timeframe=(c.get("timeframe") or None),
                    test_type=_normalize_test_type(c.get("test_type")),
                    testable=_normalize_testability(c.get("testable")),
                    reason_if_not=(c.get("reason_if_not") or None),
                    confidence=_clamp01(c.get("confidence", 0.5)),
                    test_type_justification=str(c.get("test_type_justification", "")).strip(),
                )
            )
        except Exception:  # noqa: BLE001 - skip a malformed claim, keep the rest
            continue
    return out


def _apply_gates(claims: list[Claim], config: Config) -> list[Claim]:
    # 1. Confidence floor: below min_confidence -> downgrade one rung.
    downgraded: list[Claim] = []
    for c in claims:
        if c.confidence < config.min_confidence:
            if c.testable == "yes":
                c.testable = "partial"
                c.reason_if_not = (c.reason_if_not or "") + " (downgraded: low extraction confidence)"
            elif c.testable == "partial":
                c.testable = "no"
                c.reason_if_not = (c.reason_if_not or "") + " (downgraded: low extraction confidence)"
        downgraded.append(c)

    # 1b. Self-contradiction repair: testable=="no" + test_type!="none" + reason_if_not is None
    #     means the LLM assigned a test type and justified it but forgot to flip testable.
    #     Upgrade to "partial" so validation runs rather than silently dropping the claim.
    for c in downgraded:
        if (c.testable == "no"
                and c.test_type != "none"
                and not c.reason_if_not
                and c.test_type_justification):
            c.testable = "partial"
            c.reason_if_not = "testability uncertain — attempting validation"

    # 2. Disabled test types: a yes/partial claim mapping to a disabled type -> still
    #    "testable" in shape, but validate.py will return untestable(test type disabled).
    #    We don't change classification here; the report explains it. (Keeps the trace honest.)

    # 3. Claim cap: keep the most central. The LLM was told to return at most
    #    max_claims_per_video, but enforce it. "Most central" proxy: highest confidence,
    #    ties broken by testable rank (yes > partial > no).
    if len(downgraded) > config.max_claims_per_video:
        rank = {"yes": 2, "partial": 1, "no": 0}
        downgraded.sort(key=lambda c: (c.confidence, rank.get(c.testable, 0)), reverse=True)
        downgraded = downgraded[: config.max_claims_per_video]
        # Re-id so ids are c1..cN in the kept set.
        for i, c in enumerate(downgraded, start=1):
            c.id = f"c{i}"

    return downgraded


# --- small normalizers ---


def _normalize_testability(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in ("yes", "partial", "no") else "no"


def _normalize_test_type(v) -> str:
    v = str(v or "").strip().lower()
    valid = {"indicator_value_over_range", "level_zone_hit_rate", "strategy_backtest", "none"}
    return v if v in valid else "none"


def _clamp01(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))
