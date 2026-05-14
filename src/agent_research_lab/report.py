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
    # group runs by claim — each claim can have multiple ValidationRuns (one per timeframe)
    runs_by_claim: dict[str, list[ValidationRun]] = {}
    for r in runs:
        runs_by_claim.setdefault(r.claim_id, []).append(r)
    findings: list[ClaimFinding] = []
    for claim in thesis.claims:
        claim_runs = runs_by_claim.get(claim.id, [])
        verdict, reason = _verdict_for(claim, claim_runs)
        findings.append(ClaimFinding(claim=claim, validations=claim_runs,
                                     verdict=verdict, verdict_reason=reason))

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


def _verdict_for_one(claim, run: ValidationRun) -> tuple[Verdict, str]:
    """Verdict for ONE (claim, timeframe). The per-claim verdict aggregates across these."""
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


def _verdict_for(claim, runs: list[ValidationRun]) -> tuple[Verdict, str]:
    """Aggregate per-timeframe verdicts into a single per-claim verdict.

    The aggregation rule: if EVERY tested timeframe lands in the same bucket
    (holds / fails / partial), the claim gets that verdict. If timeframes disagree —
    e.g. holds on 1D but fails on 1H — the verdict is "partial" with a reason that
    surfaces the disagreement. Disagreement is itself a finding worth reporting.
    """
    if claim.testable == "no":
        return ("untestable",
                claim.reason_if_not or "the video makes this point but it isn't a checkable claim")
    if not runs:
        return "untestable", "claim was not validated"

    # per-timeframe verdicts
    per_tf = [(_verdict_for_one(claim, r), r) for r in runs]
    verdicts = [v for (v, _), _ in per_tf]

    # all timeframes untestable
    if all(v == "untestable" for v in verdicts):
        reasons = {reason for (_, reason), _ in per_tf}
        if len(reasons) == 1:
            return "untestable", reasons.pop()
        return "untestable", "the test couldn't run cleanly on any timeframe: " + "; ".join(
            f"{r.timeframe}: {reason}" for (_, reason), r in per_tf
        )

    valid = [((v, reason), r) for (v, reason), r in per_tf if v != "untestable"]

    def _tf_brief(r: ValidationRun) -> str:
        if r.hit_rate is not None and r.occurrences is not None:
            return f"{r.timeframe}: {r.hit_rate:.0%} of {r.occurrences}"
        return f"{r.timeframe}: {r.result}"

    if all(v == "holds" for (v, _), _ in valid):
        return "holds", "held across " + ", ".join(_tf_brief(r) for _, r in valid)
    if all(v == "fails" for (v, _), _ in valid):
        return "fails", "failed across " + ", ".join(_tf_brief(r) for _, r in valid)
    if all(v == "partial" for (v, _), _ in valid):
        return "partial", "partial across " + ", ".join(_tf_brief(r) for _, r in valid)
    # timeframes disagree — surface that explicitly
    return "partial", "timeframes disagree — " + ", ".join(
        f"{r.timeframe}: {v} ({r.hit_rate:.0%} of {r.occurrences})"
        if r.hit_rate is not None and r.occurrences is not None
        else f"{r.timeframe}: {v}"
        for (v, _), r in valid
    )


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
                "validations": [
                    {
                        "timeframe": v.timeframe,
                        "status": v.status,
                        "tradingview_query": v.tradingview_query,
                        "data_summary": v.data_summary,
                        "occurrences": v.occurrences,
                        "hit_rate": v.hit_rate,
                        "result": v.result,
                        "caveats": v.caveats,
                        "error": v.error,
                    }
                    for v in f.validations
                ],
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
        # Multi-timeframe results table
        valid_runs = [v for v in f.validations if v.timeframe != "n/a"]
        if valid_runs:
            lines += ["", "**Per-timeframe results:**", "", "| Timeframe | Status | Occurrences | Hit rate | Result |", "|---|---|---|---|---|"]
            for v in valid_runs:
                occ = "—" if v.occurrences is None else str(v.occurrences)
                rate = "—" if v.hit_rate is None else f"{v.hit_rate:.0%}"
                lines.append(
                    f"| {v.timeframe} | {v.status} | {occ} | {rate} | {_md_escape(v.result)} |"
                )
            # consolidated caveats from all timeframes (deduped)
            all_caveats: list[str] = []
            seen: set[str] = set()
            for v in valid_runs:
                for c in v.caveats:
                    if c not in seen:
                        seen.add(c)
                        all_caveats.append(c)
            if all_caveats:
                lines.append("")
                lines.append("**Caveats:**")
                for c in all_caveats:
                    lines.append(f"  - {c}")
        elif f.validations:
            # all "n/a" timeframe — fundamental untestable (gate failure)
            v = f.validations[0]
            if v.result:
                lines.append(f"- **Why:** {v.result.removeprefix('untestable — ')}")
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
