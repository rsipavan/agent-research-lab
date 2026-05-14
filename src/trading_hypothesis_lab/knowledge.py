"""Pattern-aware validation memory.

Accumulates implementation knowledge about how trading concepts are operationalized,
validated, and translated into executable tests.

What gets stored:
  - How each claim was formalized (instrument, timeframe, trigger, test type)
  - Why operationalization failed, categorized by failure mode
  - What the validation found (hit rate, PF, trade count, compile errors)
  - Whether the inverse signal had edge (for strategy claims)

What does NOT get stored:
  - Directional predictions or "profitable setups"
  - Strategy recommendations
  - Anything framed as alpha or signal quality

How it improves the system:
  - thesis.py gets prior failure traces injected into the extraction prompt —
    the LLM sees "previous ORB implementations failed because: ambiguous exits,
    overfit thresholds" and calibrates confidence lower, adds reason_if_not,
    or asks for clarification on the missing pieces
  - pine.py gets prior compile patterns — synthesis prompts are warned about
    recurring failure modes (e.g., "ORB without explicit stop always fails to
    compile — default to ATR-based stop")
  - report.py detects inverse-edge cases: when a strategy is a consistent loser,
    the report notes that the operationalization has reverse predictive power

Failure modes (categorized, not free-text — allows pattern matching):

  Claim formalization failures:
    no_instrument       — claim names no specific instrument
    no_timeframe        — claim names no specific timeframe
    no_threshold        — no quantitative threshold for the trigger
    vague_outcome       — outcome undefined or circular ("works in all markets")
    framework_only      — teaching a methodology, not making a claim

  Strategy translation failures:
    no_entry_rule       — strategy has no explicit entry condition
    no_exit_rule        — strategy has no explicit exit rule (open-ended)
    no_stop_rule        — strategy has no stop-loss rule
    pine_compile_error  — Pine Script had compilation errors (fixed within retries)
    pine_max_retries    — Pine Script failed to compile after all fix attempts

  Validation execution failures:
    insufficient_data   — market data < 30 bars in the test window
    symbol_not_found    — symbol could not be resolved via MCP
    trigger_never_fired — trigger condition never occurred in the test window

  Result patterns:
    pf_below_threshold  — profit factor < holds_pf threshold (strategy fails)
    hit_rate_below      — hit rate < holds_rate threshold (indicator fails)
    sample_too_small    — fewer than min occurrences/trades
    inverse_edge        — inverse of this strategy cleared the holds threshold
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import repo_root

_STORE_PATH = repo_root() / "knowledge" / "store.jsonl"

# All recognized failure modes — used by thesis.py for pattern matching.
FAILURE_MODES = frozenset({
    "no_instrument", "no_timeframe", "no_threshold", "vague_outcome", "framework_only",
    "no_entry_rule", "no_exit_rule", "no_stop_rule",
    "pine_compile_error", "pine_max_retries",
    "insufficient_data", "symbol_not_found", "trigger_never_fired",
    "pf_below_threshold", "hit_rate_below", "sample_too_small", "inverse_edge",
})


@dataclass
class KnowledgeEntry:
    """One claim's complete operationalization record."""
    tested_at: str                          # ISO timestamp
    video_id: str
    claim_id: str
    claim_statement: str
    claim_type: str                         # test_type value
    instrument: str | None
    timeframe: str | None
    verdict: str                            # holds, partial, fails, untestable
    verdict_reason: str
    failure_modes: list[str] = field(default_factory=list)   # categorized
    operationalization_notes: str = ""      # one-line: how the claim was formalized
    # Indicator metrics
    n_occurrences: int | None = None
    hit_rate: float | None = None
    # Strategy metrics
    profit_factor: float | None = None
    net_profit: float | None = None
    total_trades: int | None = None
    pine_fix_attempts: int = 0
    # Inverse edge (set when reverse of strategy clears holds_pf threshold)
    inverse_edge_pf: float | None = None


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


_ENTRY_DEFAULTS: dict = {
    "failure_modes": list,
    "operationalization_notes": "",
    "n_occurrences": None,
    "hit_rate": None,
    "profit_factor": None,
    "net_profit": None,
    "total_trades": None,
    "pine_fix_attempts": 0,
    "inverse_edge_pf": None,
}


def load() -> list[KnowledgeEntry]:
    """Load all entries from the store, oldest first."""
    if not _STORE_PATH.exists():
        return []
    entries: list[KnowledgeEntry] = []
    for line in _STORE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            kwargs = {}
            for k, default in _ENTRY_DEFAULTS.items():
                kwargs[k] = d.get(k, default() if callable(default) else default)
            for k in ("tested_at", "video_id", "claim_id", "claim_statement",
                      "claim_type", "instrument", "timeframe", "verdict", "verdict_reason"):
                kwargs[k] = d.get(k, "")
            entries.append(KnowledgeEntry(**kwargs))
        except Exception:  # noqa: BLE001
            continue
    return entries


def append(entry: KnowledgeEntry) -> None:
    """Append one entry to the store (creates file and directory if needed)."""
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _STORE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry)) + "\n")


# ---------------------------------------------------------------------------
# Query — for thesis.py and pine.py
# ---------------------------------------------------------------------------


def find_patterns(
    claim_type: str,
    instrument: str | None = None,
    *,
    max_results: int = 5,
) -> list[KnowledgeEntry]:
    """Return prior entries for the same claim_type, most recent first.

    When instrument is given, prefer exact matches but fall back to same-type
    entries if there aren't enough — cross-instrument patterns (e.g., 'ORB on SPY
    also failed on NQ') are still useful context.
    """
    entries = load()
    exact = [
        e for e in entries
        if e.claim_type == claim_type
        and e.instrument is not None
        and instrument is not None
        and e.instrument.upper() == instrument.upper()
    ]
    same_type = [
        e for e in entries
        if e.claim_type == claim_type
        and e not in exact
    ]
    combined = exact + same_type
    combined.sort(key=lambda e: e.tested_at, reverse=True)
    return combined[:max_results]


def find_failure_modes(claim_type: str) -> list[str]:
    """Return the distinct failure modes seen for this claim_type, by frequency."""
    entries = find_patterns(claim_type, max_results=50)
    counts: dict[str, int] = {}
    for e in entries:
        for fm in e.failure_modes:
            counts[fm] = counts.get(fm, 0) + 1
    return sorted(counts, key=counts.get, reverse=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Prompt formatting — for thesis.py
# ---------------------------------------------------------------------------


def format_prior_patterns_for_prompt(claim_type: str, instrument: str | None = None) -> str:
    """Format prior failure traces as a block for the thesis extraction prompt.

    Returns empty string when the knowledge base is empty or has no relevant entries.
    This text is appended to the user prompt so the LLM can calibrate confidence
    and formalization quality based on what has actually failed before.
    """
    entries = find_patterns(claim_type, instrument, max_results=4)
    if not entries:
        return ""

    lines = [
        f"Pattern-aware validation memory — prior operationalization traces for [{claim_type}]:"
    ]
    for e in entries:
        sym = e.instrument or "no instrument"
        tf = e.timeframe or "no timeframe"
        metric = ""
        if e.hit_rate is not None:
            metric = f" | hit rate {e.hit_rate:.0%} of {e.n_occurrences} occurrences"
        elif e.profit_factor is not None:
            metric = f" | PF {e.profit_factor:.2f} over {e.total_trades} trades"
        fm_str = (", ".join(e.failure_modes)) if e.failure_modes else "none"
        notes = f" | {e.operationalization_notes}" if e.operationalization_notes else ""
        lines.append(
            f"  - [{e.tested_at[:10]}] {sym} {tf}: {e.verdict}{metric}"
            f" | failure modes: {fm_str}{notes}"
        )

    # Summarise the most common failure modes across all entries for this type
    top_failures = find_failure_modes(claim_type)[:3]
    if top_failures:
        lines.append(
            f"Most common failure modes for [{claim_type}]: {', '.join(top_failures)}. "
            f"If this claim shows similar gaps, lower confidence and populate reason_if_not."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder — called by orchestrate.py after each run
# ---------------------------------------------------------------------------


def entry_from_finding(
    finding,
    video_id: str,
    *,
    inverse_edge_pf: float | None = None,
    pine_fix_attempts: int = 0,
) -> KnowledgeEntry:
    """Build a KnowledgeEntry from a ClaimFinding (types.ClaimFinding)."""
    claim = finding.claim
    sb_run = next((v for v in finding.validations if v.strategy_backtest is not None), None)
    ind_run = next((v for v in finding.validations if v.hit_rate is not None), None)

    # Infer failure modes from the verdict and available data.
    failure_modes: list[str] = []
    if claim.instrument is None:
        failure_modes.append("no_instrument")
    if claim.timeframe is None and claim.test_type != "strategy_backtest":
        failure_modes.append("no_timeframe")
    if finding.verdict == "untestable":
        reason = (claim.reason_if_not or finding.verdict_reason or "").lower()
        if "framework" in reason or "methodology" in reason or "teaching" in reason:
            failure_modes.append("framework_only")
        elif "vague" in reason or "all markets" in reason or "no criterion" in reason:
            failure_modes.append("vague_outcome")
        elif "no threshold" in reason or "no quantitative" in reason:
            failure_modes.append("no_threshold")
        elif "no instrument" in reason or "no symbol" in reason:
            if "no_instrument" not in failure_modes:
                failure_modes.append("no_instrument")
        elif "no timeframe" in reason or "no tf" in reason:
            if "no_timeframe" not in failure_modes:
                failure_modes.append("no_timeframe")
    if finding.verdict == "fails":
        if sb_run and sb_run.strategy_backtest:
            failure_modes.append("pf_below_threshold")
        elif ind_run and ind_run.hit_rate is not None:
            failure_modes.append("hit_rate_below")
    if finding.verdict == "partial":
        n = (ind_run.occurrences if ind_run else None) or (
            sb_run.strategy_backtest.total_trades if sb_run and sb_run.strategy_backtest else None
        )
        if n is not None and n < 20:
            failure_modes.append("sample_too_small")
    if inverse_edge_pf is not None:
        failure_modes.append("inverse_edge")
    if pine_fix_attempts > 0:
        failure_modes.append("pine_compile_error")

    # Build a one-line operationalization note.
    parts = []
    if claim.test_type == "strategy_backtest" and sb_run and sb_run.strategy_backtest:
        sb = sb_run.strategy_backtest
        parts.append(
            f"strategy compiled; {sb.total_trades} trades, PF {sb.profit_factor:.2f}, "
            f"WR {sb.win_rate:.0%}, net {sb.net_profit:+,.0f}"
        )
    elif ind_run and ind_run.hit_rate is not None:
        parts.append(
            f"indicator test: {ind_run.hit_rate:.0%} of {ind_run.occurrences} occurrences"
        )
    elif claim.testable == "no":
        parts.append(claim.reason_if_not or "classified not testable at extraction")

    return KnowledgeEntry(
        tested_at=datetime.now(timezone.utc).isoformat(),
        video_id=video_id,
        claim_id=claim.id,
        claim_statement=claim.statement,
        claim_type=claim.test_type,
        instrument=claim.instrument,
        timeframe=claim.timeframe,
        verdict=finding.verdict,
        verdict_reason=finding.verdict_reason,
        failure_modes=failure_modes,
        operationalization_notes=" | ".join(parts),
        n_occurrences=ind_run.occurrences if ind_run else None,
        hit_rate=ind_run.hit_rate if ind_run else None,
        profit_factor=sb_run.strategy_backtest.profit_factor if sb_run and sb_run.strategy_backtest else None,
        net_profit=sb_run.strategy_backtest.net_profit if sb_run and sb_run.strategy_backtest else None,
        total_trades=sb_run.strategy_backtest.total_trades if sb_run and sb_run.strategy_backtest else None,
        pine_fix_attempts=pine_fix_attempts,
        inverse_edge_pf=inverse_edge_pf,
    )


