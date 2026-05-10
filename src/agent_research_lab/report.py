"""Report building: Transcript + ThesisSet + ValidationRuns -> Report.

The verdict for each claim (holds / partial / fails / untestable) is COMPUTED here
by explicit rules — it is not asked of an LLM. The LLM is used only to write the
human-readable narrative around the computed verdicts; if that call fails, we fall
back to a template render and note it. The conclusion stays auditable either way.

See docs/validation_logic.md for the verdict thresholds.
"""

from __future__ import annotations

from .config import Config
from .types import (
    ClaimFinding,
    Report,
    ThesisSet,
    Transcript,
    ValidationRun,
    Verdict,
)

try:  # pragma: no cover
    import anthropic

    _HAVE_ANTHROPIC = True
except Exception:  # pragma: no cover
    _HAVE_ANTHROPIC = False

# verdict thresholds — see docs/validation_logic.md
_MIN_N_TO_CONCLUDE = 10
_HOLDS_RATE = 0.65
_FAILS_RATE = 0.45


def build(
    transcript: Transcript,
    thesis: ThesisSet,
    runs: list[ValidationRun],
    config: Config,
    run_id: str,
) -> Report:
    runs_by_claim = {r.claim_id: r for r in runs}
    findings: list[ClaimFinding] = []
    for claim in thesis.claims:
        run = runs_by_claim.get(claim.id)
        verdict, reason = _verdict_for(claim, run)
        findings.append(ClaimFinding(claim=claim, validation=run, verdict=verdict, verdict_reason=reason))

    verdict_overall, overall_reason = _aggregate(findings, transcript, thesis)
    json_doc = _to_json(transcript, findings, verdict_overall, overall_reason, run_id)
    markdown = _to_markdown(transcript, findings, verdict_overall, overall_reason, config)

    return Report(
        video_id=transcript.video_id,
        video_title=transcript.title,
        video_url=transcript.url,
        channel=transcript.channel,
        findings=findings,
        verdict_overall=verdict_overall,
        overall_reason=overall_reason,
        markdown=markdown,
        json=json_doc,
    )


def build_minimal(transcript: Transcript, run_id: str, reason: str) -> Report:
    """For the short-circuit cases: no transcript, or transcript-too-short, or no
    trading content / no testable claims. Produces an honest 'untestable' report."""
    overall_reason = reason
    json_doc = {
        "run_id": run_id,
        "video": {"id": transcript.video_id, "url": transcript.url, "title": transcript.title,
                  "channel": transcript.channel},
        "verdict_overall": "untestable",
        "overall_reason": overall_reason,
        "findings": [],
    }
    md = (
        f"# Research report — {transcript.title or transcript.url}\n\n"
        f"**Verdict: untestable.** {overall_reason}\n\n"
        f"_Source: {transcript.url}_\n"
    )
    return Report(
        video_id=transcript.video_id, video_title=transcript.title, video_url=transcript.url,
        channel=transcript.channel, findings=[], verdict_overall="untestable",
        overall_reason=overall_reason, markdown=md, json=json_doc,
    )


# ---------------------------------------------------------------------------
# verdict computation
# ---------------------------------------------------------------------------


def _verdict_for(claim, run: ValidationRun | None) -> tuple[Verdict, str]:
    if claim.testable == "no":
        return "untestable", claim.reason_if_not or "the video makes this point but it isn't a checkable claim"
    if run is None:
        return "untestable", "claim was not validated"
    if run.status == "error":
        return "untestable", run.result.removeprefix("untestable — ") if run.result.startswith("untestable") else f"validation failed: {run.error}"
    if run.status == "insufficient_data":
        return "untestable", run.result
    # status == ok
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


def _aggregate(findings: list[ClaimFinding], transcript: Transcript, thesis: ThesisSet) -> tuple[Verdict, str]:
    if not findings:
        return "untestable", "no claims were extracted from the video"
    verdicts = [f.verdict for f in findings]
    n_total = len(verdicts)
    n_untestable = verdicts.count("untestable")
    if n_untestable == n_total:
        return "untestable", "none of the video's claims were checkable against market data"
    if "fails" in verdicts and "holds" not in verdicts:
        return "fails", "the video's checkable claim(s) did not hold up against the data"
    if "holds" in verdicts and "fails" not in verdicts and "partial" not in verdicts:
        return "holds", "the video's checkable claim(s) held up against the data, with the usual caveats"
    return "partial", "mixed: some checkable claims held up, others were partial or didn't — see per-claim findings"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _to_json(transcript, findings, verdict_overall, overall_reason, run_id) -> dict:
    return {
        "run_id": run_id,
        "video": {"id": transcript.video_id, "url": transcript.url, "title": transcript.title,
                  "channel": transcript.channel},
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


def _to_markdown(transcript, findings, verdict_overall, overall_reason, config: Config) -> str:
    template = _template_markdown(transcript, findings, verdict_overall, overall_reason)
    # Try to enrich with an LLM-written summary paragraph. The verdicts/data are
    # already in `template`; the LLM only adds a readable opening. If it fails, the
    # template stands on its own.
    if _HAVE_ANTHROPIC and config.anthropic_api_key:
        try:
            intro = _llm_intro(transcript, findings, verdict_overall, overall_reason, config)
            if intro:
                return intro.strip() + "\n\n---\n\n" + template
        except Exception:  # noqa: BLE001
            return template + "\n\n_(narrative synthesis unavailable; verdicts and data are unaffected)_\n"
    return template


def _template_markdown(transcript, findings, verdict_overall, overall_reason) -> str:
    title = transcript.title or transcript.url
    lines = [
        f"# Research report — {title}",
        "",
        f"**Overall verdict: {verdict_overall}.** {overall_reason}",
        "",
        f"_Source: {transcript.url}_" + (f" — {transcript.channel}" if transcript.channel else ""),
        "",
        "## Claims",
        "",
        "| # | Claim | Testable | Test | Verdict |",
        "|---|---|---|---|---|",
    ]
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
    lines += [
        "---",
        "",
        "_Generated by [agent-research-lab](https://github.com/) — verdicts are computed from market "
        "data via the TradingView MCP, not LLM-judged. See the trace for the step-by-step._",
        "",
    ]
    return "\n".join(lines)


def _llm_intro(transcript, findings, verdict_overall, overall_reason, config: Config) -> str:
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    summary = {
        "video": transcript.title or transcript.url,
        "overall": f"{verdict_overall} — {overall_reason}",
        "claims": [
            {"statement": f.claim.statement, "verdict": f.verdict, "reason": f.verdict_reason}
            for f in findings
        ],
    }
    resp = client.messages.create(
        model=config.anthropic_model,
        max_tokens=400,
        system=(
            "You write a 2-3 sentence opening for a research report. You are given the video, "
            "the overall verdict, and each claim's COMPUTED verdict (do not second-guess these — "
            "they came from market data, not from you). Write a tight, honest opening that states "
            "what the video claimed and how it held up. No hype, no hedging boilerplate, no 'it's "
            "important to note'. Plain and direct."
        ),
        messages=[{"role": "user", "content": str(summary)}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()
