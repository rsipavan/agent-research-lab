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

from .config import Config
from .types import Claim, ThesisSet, Transcript

# anthropic SDK
try:  # pragma: no cover - import guard
    import anthropic

    _HAVE_ANTHROPIC = True
except Exception:  # pragma: no cover
    _HAVE_ANTHROPIC = False


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
- "strategy_backtest" — "strategy S (full entry+exit rules) is profitable" — note: not implemented in v1, but classify it correctly anyway
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

Transcript:
\"\"\"
{transcript}
\"\"\"

Extract the checkable claims as specified. JSON only."""


def extract(transcript: Transcript, config: Config) -> ThesisSet:
    """Run extraction. Returns a ThesisSet (possibly with zero claims). Raises
    ExtractionError if the LLM call fails after a retry."""
    if transcript.is_empty:
        # The orchestrator normally short-circuits before calling us, but be safe.
        return ThesisSet(video_id=transcript.video_id, claims=[])

    raw = _call_llm(transcript, config)
    claims = _parse_claims(raw, transcript.video_id)
    claims = _apply_gates(claims, config)
    return ThesisSet(video_id=transcript.video_id, claims=claims)


# ---------------------------------------------------------------------------


def _call_llm(transcript: Transcript, config: Config) -> str:
    if not _HAVE_ANTHROPIC:  # pragma: no cover
        raise ExtractionError("anthropic SDK not installed")
    if not config.anthropic_api_key:
        raise ExtractionError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    user = _USER_TEMPLATE.format(
        title=transcript.title or "(unknown title)",
        channel=transcript.channel or "(unknown channel)",
        url=transcript.url,
        # cap the transcript we send — long videos get truncated; the first ~12k words
        # carry the thesis in practice
        transcript=" ".join(transcript.text.split()[:12000]),
    )

    last_err: Exception | None = None
    for attempt in range(2):  # one retry
        try:
            resp = client.messages.create(
                model=config.anthropic_model,
                max_tokens=2000,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        except Exception as e:  # noqa: BLE001
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
