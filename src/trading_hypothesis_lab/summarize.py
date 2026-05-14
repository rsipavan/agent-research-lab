"""Summarize: transcript -> VideoSummary.

The FIRST thing the pipeline does after fetching the transcript. Before extracting
any claims, establish what kind of video this is — a strategy/backtest video, an
educational explainer, market commentary, trader-psychology content, a vlog, a sales
pitch, or a mix. The report leads with this. And if the video is, by nature, not
claim-bearing (psychology, vlog, promo, off-topic), the pipeline short-circuits to a
summary-only report rather than grinding the extractor over content that has nothing
to validate.

See docs/decision_logic.md ("Decision 0: what kind of video is this?").
"""

from __future__ import annotations

import json
import time

from . import llm
from .config import Config
from .types import ContentType, Transcript, VideoSummary


class SummarizeError(RuntimeError):
    """The summarize LLM call failed after a retry. Load-bearing — without knowing
    what the video is, the orchestrator can't decide how to proceed."""


_VALID_TYPES: set[str] = {
    "strategy_or_claim", "educational", "market_commentary",
    "mindset_psychology", "vlog_or_journey", "promotion", "mixed", "other",
}

_SYSTEM_PROMPT = """\
You are given the transcript of a YouTube video about trading / markets. Before anyone
tries to "validate" anything in it, characterize the video honestly.

Classify content_type as exactly one of:
- "strategy_or_claim"   — presents a trading strategy or makes checkable claims about market behavior
                          (e.g. "RSI<30 on SPY daily marks reversals", "this backtested system makes money")
- "educational"         — explains a concept (what RSI is, how order blocks / VWAP / supply-demand work).
                          May contain weak embedded claims.
- "market_commentary"   — opinion or prediction about current markets ("I think Q3 will be bullish",
                          "Bitcoin is going to 100k")
- "mindset_psychology"  — trader psychology, discipline, habits, emotions. No checkable market claims.
- "vlog_or_journey"     — the creator's trading story, lifestyle, day-in-the-life, "how I made $X"
- "promotion"           — primarily a sales pitch for a course / signals service / prop firm / community
- "mixed"               — a genuine blend (e.g. a strategy walkthrough that's also half psychology and pitches a course)
- "other"               — none of the above / off-topic

Then:
- topic: a short phrase (<= 12 words) — what the video is about. e.g. "RSI + Bollinger Bands mean-reversion backtest"
- summary: 2-4 plain sentences — what is actually in the video. No hype, no "in this video the creator...".
- has_checkable_claims: true ONLY if the video contains at least one claim about market behavior that could,
  in principle, be checked against price data (even if under-specified). For mindset/vlog/promotion/educational-only
  content, this is usually false. Be conservative — err toward false.

Output ONLY a JSON object: {"content_type": "...", "topic": "...", "summary": "...", "has_checkable_claims": true|false}.
No markdown fences, no commentary.
"""

_USER_TEMPLATE = """\
Video: {title} — {channel}
URL: {url}

Transcript:
\"\"\"
{transcript}
\"\"\"

Characterize it as specified. JSON only."""


def summarize(transcript: Transcript, config: Config) -> VideoSummary:
    if transcript.is_empty:
        return VideoSummary(
            video_id=transcript.video_id, content_type="other",
            topic="(no transcript)", summary="No transcript was available for this video.",
            has_checkable_claims=False,
        )
    raw = _call_llm(transcript, config)
    return _parse(raw, transcript.video_id)


def _call_llm(transcript: Transcript, config: Config) -> str:
    user = _USER_TEMPLATE.format(
        title=transcript.title or "(unknown title)",
        channel=transcript.channel or "(unknown channel)",
        url=transcript.url,
        transcript=" ".join(transcript.text.split()[:12000]),
    )
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            return llm.complete(_SYSTEM_PROMPT, user, model=config.anthropic_model or None, max_tokens=500)
        except llm.LlmUnavailable:
            raise SummarizeError(
                "no LLM backend available — install the `claude` CLI (no key needed), "
                "or set ANTHROPIC_API_KEY / GEMINI_API_KEY (see README)"
            ) from None
        except llm.LlmError as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
    raise SummarizeError(f"summarize LLM call failed after retry: {last_err}")


def _parse(raw: str, video_id: str) -> VideoSummary:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Degrade: couldn't classify. Treat as "mixed" with checkable claims so the
        # pipeline still runs extraction rather than skipping (a parse failure shouldn't
        # silently turn a strategy video into "nothing to validate"). Rare in practice.
        return VideoSummary(
            video_id=video_id, content_type="mixed",
            topic="(could not classify — model output unparseable)",
            summary="The summarize step did not return a parseable result; proceeding to claim extraction anyway.",
            has_checkable_claims=True,
        )
    ct = str(data.get("content_type", "other")).strip().lower()
    content_type: ContentType = ct if ct in _VALID_TYPES else "other"  # type: ignore[assignment]
    return VideoSummary(
        video_id=video_id,
        content_type=content_type,
        topic=str(data.get("topic", "")).strip() or "(no topic)",
        summary=str(data.get("summary", "")).strip() or "(no summary)",
        has_checkable_claims=bool(data.get("has_checkable_claims", False)),
    )
