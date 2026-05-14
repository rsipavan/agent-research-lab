"""Report building: Transcript + VideoSummary + ThesisSet + ValidationRuns -> Report.

The report LEADS with "what this video is" (from the summarize step), then — if there
was anything to validate — per-claim findings structured around five questions:
  1. What did the video claim?
  2. What was actually testable?
  3. What data was checked?
  4. What happened?
  5. Why did the system conclude this?

The verdict for each claim (holds / partial / fails / untestable) is COMPUTED here by
explicit rules — it is not asked of an LLM. Verdicts stay auditable even if the LLM
backend is unavailable.

See docs/decision_logic.md and docs/validation_logic.md.
"""

from __future__ import annotations

from .config import Config
from .types import (
    ClaimFinding,
    Report,
    StrategyBacktestMetrics,
    ThesisSet,
    Transcript,
    ValidationRun,
    Verdict,
    VideoSummary,
)

# Fallback thresholds used when config is unavailable (tests, build_minimal).
_DEFAULT_MIN_N = 10
_DEFAULT_HOLDS_RATE = 0.65
_DEFAULT_FAILS_RATE = 0.45
_DEFAULT_MIN_TRADES = 20
_DEFAULT_HOLDS_PF = 1.5
_DEFAULT_FAILS_PF = 1.0

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class _Thresholds:
    min_n: int
    holds_rate: float
    fails_rate: float
    min_trades: int
    holds_pf: float
    fails_pf: float

    @classmethod
    def from_config(cls, config: Config | None) -> "_Thresholds":
        if config is None:
            return cls(_DEFAULT_MIN_N, _DEFAULT_HOLDS_RATE, _DEFAULT_FAILS_RATE,
                       _DEFAULT_MIN_TRADES, _DEFAULT_HOLDS_PF, _DEFAULT_FAILS_PF)
        return cls(
            min_n=config.indicator_min_occurrences,
            holds_rate=config.indicator_holds_rate,
            fails_rate=config.indicator_fails_rate,
            min_trades=config.strategy_min_trades,
            holds_pf=config.strategy_holds_profit_factor,
            fails_pf=config.strategy_fails_profit_factor,
        )

    @classmethod
    def defaults(cls) -> "_Thresholds":
        return cls(_DEFAULT_MIN_N, _DEFAULT_HOLDS_RATE, _DEFAULT_FAILS_RATE,
                   _DEFAULT_MIN_TRADES, _DEFAULT_HOLDS_PF, _DEFAULT_FAILS_PF)


_VERDICT_LABEL: dict[str, str] = {
    "holds": "CONFIRMED",
    "partial": "PARTIAL SUPPORT",
    "fails": "NOT SUPPORTED",
    "untestable": "UNTESTABLE",
}


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
    thresholds = _Thresholds.from_config(config)
    runs_by_claim: dict[str, list[ValidationRun]] = {}
    for r in runs:
        runs_by_claim.setdefault(r.claim_id, []).append(r)
    findings: list[ClaimFinding] = []
    for claim in thesis.claims:
        claim_runs = runs_by_claim.get(claim.id, [])
        verdict, reason = _verdict_for(claim, claim_runs, thresholds)
        findings.append(ClaimFinding(claim=claim, validations=claim_runs,
                                     verdict=verdict, verdict_reason=reason))

    verdict_overall, overall_reason = _aggregate(findings)
    json_doc = _to_json(transcript, video_summary, findings, verdict_overall, overall_reason, run_id)
    markdown = _to_markdown(transcript, video_summary, findings, verdict_overall, overall_reason, thresholds)
    return _report(transcript, video_summary, findings, verdict_overall, overall_reason, markdown, json_doc)


def build_summary_only(transcript: Transcript, video_summary: VideoSummary, run_id: str) -> Report:
    """For videos that are, by nature, not claim-bearing (mindset/psychology, vlog,
    promotion, off-topic). Report is the summary plus an honest 'nothing to validate here'."""
    ct = video_summary.content_type
    reason = {
        "mindset_psychology": "this is a trader-psychology / mindset video — it makes no claims about market behavior that can be checked against data",
        "vlog_or_journey": "this is a vlog / trading-journey video — it tells the creator's story rather than making checkable market claims",
        "promotion": "this is primarily a promotional video (course / signals / community / prop firm) — there's no checkable market claim to validate",
        "other": "this video isn't about a checkable trading claim — there's nothing here to validate against market data",
    }.get(ct, "the summarize step found no checkable market claim in this video — there's nothing to validate")
    json_doc = {
        "run_id": run_id,
        "video": _video_json(transcript),
        "video_summary": _summary_json(video_summary),
        "verdict_overall": "untestable",
        "overall_reason": reason,
        "findings": [],
    }
    md = _summary_only_md(transcript, video_summary, reason)
    return _report(transcript, video_summary, [], "untestable", reason, md, json_doc)


def build_minimal(transcript: Transcript, video_summary: VideoSummary | None, run_id: str, reason: str) -> Report:
    """For the no-transcript / transcript-too-short cases."""
    json_doc = {
        "run_id": run_id,
        "video": _video_json(transcript),
        "video_summary": _summary_json(video_summary) if video_summary else None,
        "verdict_overall": "untestable",
        "overall_reason": reason,
        "findings": [],
    }
    title = transcript.title or transcript.url
    md = "\n".join([
        f"# {title}",
        "",
        f"*{transcript.url}*",
        "",
        f"**Overall: UNTESTABLE** — {reason}",
        "",
        "---",
        "",
        _pipeline_md(),
        _footer_md(),
    ])
    return _report(transcript, video_summary, [], "untestable", reason, md, json_doc)


# ---------------------------------------------------------------------------
# verdict computation
# ---------------------------------------------------------------------------


def _verdict_for_one(claim, run: ValidationRun, t: _Thresholds) -> tuple[Verdict, str]:
    if run.status == "error":
        return ("untestable",
                run.result.removeprefix("untestable — ") if run.result.startswith("untestable")
                else f"validation failed: {run.error}")
    if run.status == "insufficient_data":
        return "untestable", run.result

    # Strategy backtest: judge on profit_factor and net_profit, not win_rate.
    # Win rate alone is meaningless without knowing the R:R — a 40% WR on a 2R system is fine.
    if run.strategy_backtest is not None:
        sb = run.strategy_backtest
        if sb.total_trades < t.min_trades:
            return ("partial",
                    f"only {sb.total_trades} trades — too few to draw a reliable conclusion")
        if sb.profit_factor >= t.holds_pf and sb.net_profit > 0:
            return ("holds",
                    f"positive edge: profit factor {sb.profit_factor:.2f}, "
                    f"win rate {sb.win_rate:.0%}, net profit {sb.net_profit:+,.2f} "
                    f"over {sb.total_trades} trades")
        if sb.profit_factor < t.fails_pf or sb.net_profit < 0:
            # Check if the inverse of this strategy clears the holds threshold.
            # A consistent loser is also informative: the operationalization has
            # real predictive power, just in the wrong direction.
            inverse_note = ""
            if sb.profit_factor > 0:
                inverse_pf = 1.0 / sb.profit_factor
                if inverse_pf >= t.holds_pf:
                    inverse_note = (
                        f" | inverse signal detected: reversing all entries and exits "
                        f"gives estimated PF {inverse_pf:.2f} — above the {t.holds_pf} holds threshold; "
                        f"the strategy has consistent predictive power but in the wrong direction"
                    )
            return ("fails",
                    f"no edge: profit factor {sb.profit_factor:.2f}, "
                    f"net profit {sb.net_profit:+,.2f} over {sb.total_trades} trades"
                    + inverse_note)
        return ("partial",
                f"marginal edge: profit factor {sb.profit_factor:.2f}, "
                f"win rate {sb.win_rate:.0%}, net profit {sb.net_profit:+,.2f} "
                f"over {sb.total_trades} trades")

    n = run.occurrences or 0
    r = run.hit_rate
    if r is None:
        return "untestable", "validation produced no rate"
    if n < t.min_n:
        return "partial", f"only {n} occurrence(s) — rate {r:.0%}, sample too small to conclude"
    if r >= t.holds_rate:
        return "holds", f"the claimed behavior occurred {r:.0%} of the time across {n} occurrences"
    if r < t.fails_rate:
        return "fails", f"the claimed behavior occurred only {r:.0%} of the time across {n} occurrences"
    return "partial", f"the claimed behavior occurred {r:.0%} of the time across {n} occurrences — roughly coin-flip; the claim overstates it"


def _verdict_for(claim, runs: list[ValidationRun], t: _Thresholds | None = None) -> tuple[Verdict, str]:
    t = t or _Thresholds.defaults()
    """Aggregate per-timeframe verdicts into a single per-claim verdict."""
    if claim.testable == "no":
        return ("untestable",
                claim.reason_if_not or "the video makes this point but it isn't a checkable claim")
    if not runs:
        return "untestable", "claim was not validated"

    per_tf = [(_verdict_for_one(claim, r, t), r) for r in runs]
    verdicts = [v for (v, _), _ in per_tf]

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
    return "partial", "mixed results — some claims held up, others were partial or didn't; see per-claim findings"


# ---------------------------------------------------------------------------
# JSON serialization (unchanged contract)
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
    return {"id": transcript.video_id, "url": transcript.url,
            "title": transcript.title, "channel": transcript.channel}


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
                        "strategy_backtest": _strategy_backtest_json(v.strategy_backtest),
                        "pine_script_path": v.pine_script_path,
                    }
                    for v in f.validations
                ],
            }
            for f in findings
        ],
    }


def _strategy_backtest_json(sb: StrategyBacktestMetrics | None) -> dict | None:
    if sb is None:
        return None
    return {
        "net_profit": sb.net_profit,
        "gross_profit": sb.gross_profit,
        "total_trades": sb.total_trades,
        "winning_trades": sb.winning_trades,
        "win_rate": round(sb.win_rate, 3),
        "max_drawdown": sb.max_drawdown,
        "profit_factor": round(sb.profit_factor, 2),
        "pine_script_file": sb.pine_script_path,
    }


# ---------------------------------------------------------------------------
# Markdown rendering — document-style, structured around 5 questions
# ---------------------------------------------------------------------------


def _to_markdown(
    transcript: Transcript,
    video_summary: VideoSummary | None,
    findings: list[ClaimFinding],
    verdict_overall: Verdict,
    overall_reason: str,
    thresholds: _Thresholds | None = None,
) -> str:
    t = thresholds or _Thresholds.defaults()
    return "".join([
        _header_md(transcript, video_summary, verdict_overall, overall_reason),
        _video_section_md(video_summary, findings),
        _claims_section_md(findings, t),
        _overall_verdict_md(verdict_overall, overall_reason),
        _pipeline_md(),
        _footer_md(),
    ])


def _header_md(
    transcript: Transcript,
    video_summary: VideoSummary | None,
    verdict_overall: Verdict,
    overall_reason: str,
) -> str:
    title = transcript.title or transcript.url
    channel_bit = f" | {transcript.channel}" if transcript.channel else ""
    vtype = video_summary.content_type if video_summary else "video"
    label = _VERDICT_LABEL.get(verdict_overall, verdict_overall.upper())
    return (
        f"# {title}\n"
        f"\n"
        f"*{vtype}{channel_bit}*\n"
        f"*{transcript.url}*\n"
        f"\n"
        f"**Overall: {label}** | {overall_reason}\n"
        f"\n"
        f"---\n"
        f"\n"
    )


def _video_section_md(
    video_summary: VideoSummary | None,
    findings: list[ClaimFinding],
) -> str:
    lines = ["## What This Video Is", ""]
    if video_summary:
        lines.append(video_summary.summary)
        lines.append("")
        n_total = len(findings)
        n_testable = sum(1 for f in findings if f.claim.testable != "no")
        if n_total:
            claim_word = "claim" if n_total == 1 else "claims"
            lines.append(f"**{n_total} {claim_word} found** | {n_testable} testable")
        else:
            lines.append("**No checkable market claims found in this video.**")
    else:
        lines.append("*(Video could not be characterized — transcript was unavailable.)*")
    lines += ["", "---", ""]
    return "\n".join(lines) + "\n"


def _claims_section_md(findings: list[ClaimFinding], t: _Thresholds | None = None) -> str:
    if not findings:
        return "## Claims\n\nNo checkable market claims were found in this video.\n\n---\n\n"
    t = t or _Thresholds.defaults()
    total = len(findings)
    parts = ["## Claims\n"]
    for i, f in enumerate(findings, 1):
        parts.append(_claim_card_md(f, i, total, t))
    return "\n".join(parts)


def _claim_card_md(finding: ClaimFinding, idx: int, total: int, t: _Thresholds | None = None) -> str:
    t = t or _Thresholds.defaults()
    claim = finding.claim
    lines: list[str] = []

    # Heading
    untestable_tag = " | UNTESTABLE" if claim.testable == "no" else ""
    lines += [f"### Claim {idx} of {total}{untestable_tag}", "", f"> {claim.statement}", ""]

    # Short-circuit: explicitly untestable
    if claim.testable == "no":
        reason = claim.reason_if_not or finding.verdict_reason or "not operationalizable as a mechanical test"
        lines += [
            "**Why this couldn't be tested:**",
            reason,
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    valid_runs = [v for v in finding.validations if v.timeframe != "n/a"]
    na_runs = [v for v in finding.validations if v.timeframe == "n/a"]

    # Gate failure — no valid runs at all (e.g. no instrument, MCP down)
    if not valid_runs and na_runs:
        v = na_runs[0]
        reason = (v.result or finding.verdict_reason or "").removeprefix("untestable — ")
        label = _VERDICT_LABEL.get(finding.verdict, finding.verdict.upper())
        lines += [
            "**Why this couldn't be tested:**",
            reason,
            "",
            f"**Verdict: {label}**",
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    sym = claim.instrument or "?"
    tf_list = ", ".join(v.timeframe for v in valid_runs)

    # ── Q3: What data was checked? ───────────────────────────────────────────
    lines.append("**What was checked:**")
    if claim.test_type == "strategy_backtest":
        tf = claim.timeframe or "?"
        lines.append(
            f"Full strategy backtest | {sym} | {tf} | "
            f"Pine Script v6, TradingView strategy tester"
        )
    elif len(valid_runs) == 1:
        v = valid_runs[0]
        occ_bit = f" | {v.occurrences:,} occurrences" if v.occurrences else ""
        lines.append(f"{sym} | {v.timeframe}{occ_bit}")
    else:
        lines.append(f"{sym} | {len(valid_runs)} timeframes tested ({tf_list})")
    lines.append("")

    # ── Q4: What happened? ───────────────────────────────────────────────────
    lines.append("**What happened:**")
    if claim.test_type == "strategy_backtest":
        sb_run = next((v for v in valid_runs if v.strategy_backtest is not None), None)
        if sb_run and sb_run.strategy_backtest:
            sb = sb_run.strategy_backtest
            losing = sb.total_trades - sb.winning_trades
            profit_str = f"{sb.net_profit:+,.2f}" if sb.net_profit is not None else "n/a"
            lines.append(
                f"{sb.total_trades} trades executed | "
                f"Win rate: {sb.win_rate:.0%} ({sb.winning_trades} wins, {losing} losses) | "
                f"Net profit: {profit_str} | "
                f"Profit factor: {sb.profit_factor:.2f} | "
                f"Max drawdown: {sb.max_drawdown:.2f}"
            )
            if sb.pine_script_path:
                from pathlib import Path as _Path
                fname = _Path(sb.pine_script_path).name
                lines += ["", f"*Script: `{fname}` — load into TradingView Pine editor*"]
        else:
            v0 = valid_runs[0] if valid_runs else (na_runs[0] if na_runs else None)
            lines.append(v0.result if v0 else "strategy synthesis did not produce results")
    elif len(valid_runs) == 1:
        v = valid_runs[0]
        if v.hit_rate is not None and v.occurrences is not None:
            lines.append(
                f"{v.hit_rate:.0%} of {v.occurrences:,} occurrences showed the claimed behavior"
            )
        else:
            lines.append(v.result or "validation ran but produced no rate")
    else:
        for v in valid_runs:
            if v.hit_rate is not None and v.occurrences is not None:
                n, r = v.occurrences, v.hit_rate
                if n < t.min_n:
                    conclusion = "sample too small"
                elif r >= t.holds_rate:
                    conclusion = "confirmed"
                elif r < t.fails_rate:
                    conclusion = "not supported"
                else:
                    conclusion = "partial"
                lines.append(f"- **{v.timeframe}:** {r:.0%} of {n:,} occurrences ({conclusion})")
            else:
                lines.append(f"- **{v.timeframe}:** {v.result}")
    lines.append("")

    # ── Q5: Why did the system conclude this? ────────────────────────────────
    label = _VERDICT_LABEL.get(finding.verdict, finding.verdict.upper())
    lines += [
        f"**Verdict: {label}**",
        finding.verdict_reason,
        "",
    ]

    # Inverse edge block — rendered when a strategy lost consistently enough
    # that reversing every entry and exit would have cleared the holds threshold.
    if finding.verdict == "fails" and "inverse signal detected" in finding.verdict_reason:
        lines += [
            "> **Inverse Signal Detected**",
            "> This strategy lost consistently. Reversing every entry and exit — selling",
            "> where it buys, buying where it sells — had an estimated profit factor above",
            "> the holds threshold over the same period. This is an operationalization",
            "> finding: the strategy's rules have real predictive power, but in the direction",
            "> opposite to what was claimed.",
            "",
        ]

    # Caveats
    seen: set[str] = set()
    all_caveats: list[str] = []
    for v in valid_runs or na_runs:
        for c in v.caveats:
            if c not in seen:
                seen.add(c)
                all_caveats.append(c)
    if all_caveats:
        lines.append("**Caveats:**")
        for c in all_caveats:
            lines.append(f"- {c}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _overall_verdict_md(verdict_overall: Verdict, overall_reason: str) -> str:
    label = _VERDICT_LABEL.get(verdict_overall, verdict_overall.upper())
    return (
        f"## Overall: {label}\n"
        f"\n"
        f"{overall_reason}\n"
        f"\n"
        f"---\n"
        f"\n"
    )


def _pipeline_md() -> str:
    return (
        "## How This Was Checked\n"
        "\n"
        "1. **Transcript fetched** from the video\n"
        "2. **Video characterized** — content type, topic, whether it makes checkable claims\n"
        "3. **Claims extracted** — each tagged with instrument, timeframe, and test type\n"
        "4. **Validated** against real market data via TradingView MCP\n"
        "5. **Verdicts computed** from the data — not LLM-judged\n"
        "\n"
        "---\n"
        "\n"
    )


def _footer_md() -> str:
    return (
        "*Generated by [agent-research-lab](https://github.com/rsipavan/agent-research-lab). "
        "Verdicts are computed from market data, not LLM-judged. "
        "See the trace file for step-by-step reasoning.*\n"
    )


def _summary_only_md(transcript: Transcript, video_summary: VideoSummary, reason: str) -> str:
    title = transcript.title or transcript.url
    channel_bit = f" | {transcript.channel}" if transcript.channel else ""
    ct = video_summary.content_type
    return (
        f"# {title}\n"
        f"\n"
        f"*{ct}{channel_bit}*\n"
        f"*{transcript.url}*\n"
        f"\n"
        f"**Overall: UNTESTABLE** | {reason}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"## What This Video Is\n"
        f"\n"
        f"{video_summary.summary}\n"
        f"\n"
        f"**Content type:** {ct}\n"
        f"**Topic:** {video_summary.topic}\n"
        f"\n"
        f"This video doesn't contain checkable market claims — there's nothing to validate against market data.\n"
        f"\n"
        f"---\n"
        f"\n"
        + _pipeline_md()
        + _footer_md()
    )


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()
