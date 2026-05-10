"""Report building: Transcript + VideoSummary + ThesisSet + ValidationRuns -> Report.

The report LEADS with "what this video is" (from the summarize step), then — if there
was anything to validate — the per-claim verdicts.

The verdict for each claim (holds / partial / fails / untestable) is COMPUTED here by
explicit rules — it is not asked of an LLM. The LLM is used only to write the
human-readable narrative around the computed verdicts; if that call fails, we fall back
to a template render and note it. The conclusion stays auditable either way.

See docs/decision_logic.md (Decision 0: what kind of video is this?) and
docs/validation_logic.md (the verdict thresholds).
"""

from __future__ import annotations

from . import llm
from .config import Config
from .types import (
    ClaimFinding,
    Report,
    ThesisSet,
    Transcript,
    ValidationRun,
    Verdict,
    VideoSummary,
)

# verdict thresholds — see docs/validation_logic.md
_MIN_N_TO_CONCLUDE = 10
_HOLDS_RATE = 0.65
_FAILS_RATE = 0.45


# ---------------------------------------------------------------------------
# public builders
# ---------------------------------------------------------------------------


def build(
    transcript: Transcript,
    video_summary: VideoSummary | None,
    thesis: ThesisSet,
    runs: list[ValidationRun],
    config: Config,
    run_id: str,
) -> Report:
    """Full report: leads with the video summary, then the per-claim findings."""
    runs_by_claim = {r.claim_id: r for r in runs}
    findings: list[ClaimFinding] = []
    for claim in thesis.claims:
        run = runs_by_claim.get(claim.id)
        verdict, reason = _verdict_for(claim, run)
        findings.append(ClaimFinding(claim=claim, validation=run, verdict=verdict, verdict_reason=reason))

    verdict_overall, overall_reason = _aggregate(findings)
    json_doc = _to_json(transcript, video_summary, findings, verdict_overall, overall_reason, run_id)
    markdown = _to_markdown(transcript, video_summary, findings, verdict_overall, overall_reason, config)
    return _report(transcript, video_summary, findings, verdict_overall, overall_reason, markdown, json_doc)


def build_summary_only(transcript: Transcript, video_summary: VideoSummary, run_id: str) -> Report:
    """For videos that are, by nature, not claim-bearing (mindset/psychology, vlog,
    promotion, off-topic, or anything the summarize step flagged as having no checkable
    claims). We don't run claim extraction — the report is the summary plus an honest
    'nothing to validate here, and why'."""
    ct = video_summary.content_type
    reason = {
        "mindset_psychology": "this is a trader-psychology / mindset video — it makes no claims about market behavior that can be checked against data",
        "vlog_or_journey": "this is a vlog / trading-journey video — it tells the creator's story rather than making checkable market claims",
        "promotion": "this is primarily a promotional video (course / signals / community / prop firm) — there's no checkable market claim to validate",
        "other": "this video isn't about a checkable trading claim — there's nothing here to validate against market data",
    }.get(ct, "the summarize step found no checkable market claim in this video — there's nothing to validate")
    overall_reason = reason
    json_doc = {
        "run_id": run_id,
        "video": _video_json(transcript),
        "video_summary": _summary_json(video_summary),
        "verdict_overall": "untestable",
        "overall_reason": overall_reason,
        "findings": [],
    }
    md = _summary_section_md(transcript, video_summary, verdict_overall="untestable", overall_reason=overall_reason)
    md += "\n" + _footer_md()
    return _report(transcript, video_summary, [], "untestable", overall_reason, md, json_doc)


def build_minimal(transcript: Transcript, video_summary: VideoSummary | None, run_id: str, reason: str) -> Report:
    """For the no-transcript / transcript-too-short cases. Honest 'untestable' report."""
    overall_reason = reason
    json_doc = {
        "run_id": run_id,
        "video": _video_json(transcript),
        "video_summary": _summary_json(video_summary) if video_summary else None,
        "verdict_overall": "untestable",
        "overall_reason": overall_reason,
        "findings": [],
    }
    md = (
        f"# Research report — {transcript.title or transcript.url}\n\n"
        f"**Overall verdict: untestable.** {overall_reason}\n\n"
        f"_Source: {transcript.url}_\n\n" + _footer_md()
    )
    return _report(transcript, video_summary, [], "untestable", overall_reason, md, json_doc)


# ---------------------------------------------------------------------------
# verdict computation
# ---------------------------------------------------------------------------


def _verdict_for(claim, run: ValidationRun | None) -> tuple[Verdict, str]:
    if claim.testable == "no":
        return "untestable", claim.reason_if_not or "the video makes this point but it isn't a checkable claim"
    if run is None:
        return "untestable", "claim was not validated"
    if run.status == "error":
        return ("untestable",
                run.result.removeprefix("untestable — ") if run.result.startswith("untestable")
                else f"validation failed: {run.error}")
    if run.status == "insufficient_data":
        return "untestable", run.result
    n = run.occurrences or 0
    r = run.hit_rate
    if r is None:
        return "untestable", "validation produced no rate"
    if n < _MIN_N_TO_CONCLUDE:
        return "partial", f"only {n} occurrence(s) in the tested window — observed rate {r:.0%}, but the sample is too small to conclude"
    if r >= _HOLDS_RATE:
        return "holds", f"the claimed behavior occurred {r:.0%} of the time over {n} occurrences"
    if r < _FAILS_RATE:
        return "fails", f"the claimed behavior occurred only {r:.0%} of the time over {n} occurrences"
    return "partial", f"the claimed behavior occurred {r:.0%} of the time over {n} occurrences — roughly coin-flip; the claim overstates it"


def _aggregate(findings: list[ClaimFinding]) -> tuple[Verdict, str]:
    if not findings:
        return "untestable", "no checkable claims were found in this video"
    verdicts = [f.verdict for f in findings]
    if verdicts.count("untestable") == len(verdicts):
        return "untestable", "none of the video's claims were checkable against market data"
    if "fails" in verdicts and "holds" not in verdicts:
        return "fails", "the video's checkable claim(s) did not hold up against the data"
    if "holds" in verdicts and "fails" not in verdicts and "partial" not in verdicts:
        return "holds", "the video's checkable claim(s) held up against the data, with the usual caveats"
    return "partial", "mixed: some checkable claims held up, others were partial or didn't — see per-claim findings"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _report(transcript, video_summary, findings, verdict_overall, overall_reason, markdown, json_doc) -> Report:
    return Report(
        video_id=transcript.video_id,
        video_title=transcript.title,
        video_url=transcript.url,
        channel=transcript.channel,
        video_summary=video_summary,
        findings=findings,
        verdict_overall=verdict_overall,
        overall_reason=overall_reason,
        markdown=markdown,
        json=json_doc,
    )


def _video_json(transcript) -> dict:
    return {"id": transcript.video_id, "url": transcript.url, "title": transcript.title, "channel": transcript.channel}


def _summary_json(s: VideoSummary) -> dict:
    return {"content_type": s.content_type, "topic": s.topic, "summary": s.summary,
            "has_checkable_claims": s.has_checkable_claims}


def _to_json(transcript, video_summary, findings, verdict_overall, overall_reason, run_id) -> dict:
    return {
        "run_id": run_id,
        "video": _video_json(transcript),
        "video_summary": _summary_json(video_summary) if video_summary else None,
        "verdict_overall": verdict_overall,
        "overall_reason": overall_reason,
        "findings": [
            {
                "claim_id": f.claim.id,
                "statement": f.claim.statement,
                "instrument": f.claim.instrument,
                "timeframe": f.claim.timeframe,
                "testable": f.claim.testable,
                "test_type": f.claim.test_type,
                "verdict": f.verdict,
                "verdict_reason": f.verdict_reason,
                "validation": None if f.validation is None else {
                    "status": f.validation.status,
                    "tradingview_query": f.validation.tradingview_query,
                    "data_summary": f.validation.data_summary,
                    "occurrences": f.validation.occurrences,
                    "hit_rate": f.validation.hit_rate,
                    "result": f.validation.result,
                    "caveats": f.validation.caveats,
                    "error": f.validation.error,
                },
            }
            for f in findings
        ],
    }


def _summary_section_md(transcript, video_summary: VideoSummary | None, *, verdict_overall, overall_reason) -> str:
    title = transcript.title or transcript.url
    lines = [
        f"# Research report — {title}",
        "",
        f"**Overall verdict: {verdict_overall}.** {overall_reason}",
        "",
        f"_Source: {transcript.url}_" + (f" — {transcript.channel}" if transcript.channel else ""),
        "",
        "## What this video is",
        "",
    ]
    if video_summary:
        lines.append(f"- **Type:** `{video_summary.content_type}`")
        lines.append(f"- **Topic:** {video_summary.topic}")
        lines.append(f"- **Summary:** {video_summary.summary}")
        lines.append(f"- **Has checkable market claims:** {'yes' if video_summary.has_checkable_claims else 'no'}")
    else:
        lines.append("- (not summarized — transcript was unavailable)")
    lines.append("")
    return "\n".join(lines)


def _to_markdown(transcript, video_summary, findings, verdict_overall, overall_reason, config: Config) -> str:
    head = _summary_section_md(transcript, video_summary, verdict_overall=verdict_overall, overall_reason=overall_reason)
    body = _claims_section_md(findings)
    template = head + "\n" + body + _footer_md()
    # Enrich with an LLM-written opening, around the COMPUTED verdicts. If no backend
    # is available or the call fails, the template stands — verdicts are computed, not
    # LLM-generated, so they're unaffected.
    if llm.available_backend() is None:
        return template
    try:
        intro = _llm_intro(transcript, video_summary, findings, verdict_overall, overall_reason, config)
        if intro:
            return intro.strip() + "\n\n---\n\n" + template
    except Exception:  # noqa: BLE001
        return template + "\n_(narrative synthesis unavailable; verdicts and data are unaffected)_\n"
    return template


def _claims_section_md(findings: list[ClaimFinding]) -> str:
    if not findings:
        return "## Claims\n\nNo checkable market claims were found in this video — nothing to validate.\n\n"
    lines = ["## Claims", "", "| # | Claim | Testable | Test | Verdict |", "|---|---|---|---|---|"]
    for f in findings:
        lines.append(
            f"| {f.claim.id} | {_md_escape(f.claim.statement)} | {f.claim.testable} | "
            f"{f.claim.test_type} | **{f.verdict}** |"
        )
    lines += ["", "## Findings", ""]
    for f in findings:
        lines.append(f"### {f.claim.id} — {_md_escape(f.claim.statement)}")
        lines.append("")
        lines.append(f"- **Verdict:** {f.verdict} — {f.verdict_reason}")
        if f.claim.instrument or f.claim.timeframe:
            lines.append(f"- **Scope:** {f.claim.instrument or '?'} · {f.claim.timeframe or 'timeframe unspecified'}")
        if f.validation is not None:
            v = f.validation
            if v.tradingview_query:
                lines.append(f"- **Tested:** {v.tradingview_query}")
            if v.data_summary:
                lines.append(f"- **Data:** {v.data_summary}")
            if v.caveats:
                lines.append("- **Caveats:**")
                for c in v.caveats:
                    lines.append(f"  - {c}")
        elif f.claim.reason_if_not:
            lines.append(f"- **Why not testable:** {f.claim.reason_if_not}")
        lines.append("")
    return "\n".join(lines)


def _footer_md() -> str:
    return (
        "---\n\n"
        "_Generated by [agent-research-lab](https://github.com/rsipavan/agent-research-lab) — the video "
        "is characterized first, then any checkable claims are validated against market data via the "
        "TradingView MCP. Verdicts are computed from the data, not LLM-judged. See the trace for the "
        "step-by-step._\n"
    )


def _llm_intro(transcript, video_summary, findings, verdict_overall, overall_reason, config: Config) -> str:
    payload = {
        "video": transcript.title or transcript.url,
        "what_it_is": None if not video_summary else
            {"type": video_summary.content_type, "topic": video_summary.topic, "summary": video_summary.summary},
        "overall": f"{verdict_overall} — {overall_reason}",
        "claims": [
            {"statement": f.claim.statement, "verdict": f.verdict, "reason": f.verdict_reason}
            for f in findings
        ],
    }
    system = (
        "You write a 2-3 sentence opening for a research report on a trading YouTube video. You're "
        "given what kind of video it is, the overall verdict, and each claim's COMPUTED verdict (do "
        "not second-guess these — they came from market data, not from you). Open by saying what the "
        "video is and what it claims, then how it held up. No hype, no hedging boilerplate, no 'it's "
        "important to note'. Plain and direct."
    )
    return llm.complete(system, str(payload), model=config.anthropic_model or None, max_tokens=400)


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()
