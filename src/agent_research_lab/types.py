"""Data contracts between pipeline modules.

These are plain dataclasses. Modules return them; the orchestrator passes them
along and logs summaries. Nothing here knows about tracing, Telegram, or the LLM.

See docs/architecture.md for the contract table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


# ---------------------------------------------------------------------------
# transcript.py output
# ---------------------------------------------------------------------------


@dataclass
class Transcript:
    """A fetched YouTube transcript. `text` is empty if no captions were available
    (the orchestrator short-circuits to a minimal report in that case)."""

    video_id: str
    url: str
    title: str | None
    channel: str | None
    text: str
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def is_empty(self) -> bool:
        # "near-empty" counts as empty — see docs/failure_handling.md
        return self.word_count < 50


# ---------------------------------------------------------------------------
# thesis.py output
# ---------------------------------------------------------------------------

Testability = Literal["yes", "partial", "no"]
TestType = Literal[
    "indicator_value_over_range",
    "level_zone_hit_rate",
    "strategy_backtest",
    "none",  # no test type maps to this claim
]


@dataclass
class Claim:
    """A single candidate claim extracted from a transcript, classified for testability.

    See docs/decision_logic.md for what `testable` and `test_type` mean and how
    they're decided.
    """

    id: str  # "c1", "c2", ...
    statement: str  # the claim, restated cleanly
    instrument: str | None  # ticker / symbol named or implied; None if absent
    timeframe: str | None  # e.g. "1D", "4H", "15m"; None if absent
    test_type: TestType
    testable: Testability
    reason_if_not: str | None  # populated when testable in {"partial", "no"}
    confidence: float  # 0..1, the extractor's confidence this is a real, correctly-parsed claim
    test_type_justification: str = ""  # one line, why this test type — goes into the trace


@dataclass
class ThesisSet:
    video_id: str
    claims: list[Claim] = field(default_factory=list)

    @property
    def testable_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.testable in ("yes", "partial")]

    @property
    def has_any_testable(self) -> bool:
        return len(self.testable_claims) > 0


# ---------------------------------------------------------------------------
# validate.py output
# ---------------------------------------------------------------------------

ValidationStatus = Literal["ok", "error", "insufficient_data"]


@dataclass
class ValidationRun:
    """The result of running one test against real market data via the TradingView MCP.

    The validator reports the *rate* (e.g. "bounced 14 of 22 occurrences"); it does
    NOT assign a verdict. report.py turns this into holds/partial/fails/untestable
    by explicit rules — see docs/validation_logic.md.
    """

    claim_id: str
    test_type: TestType
    status: ValidationStatus
    tradingview_query: str  # human-readable description of what was queried (symbol, timeframe, indicator, range)
    data_summary: str  # what the data showed, in plain language
    occurrences: int | None  # n — how many times the trigger condition occurred; None if N/A
    hit_rate: float | None  # r — fraction of occurrences where the claimed outcome happened; None if N/A
    result: str  # one-line summary of the finding
    caveats: list[str] = field(default_factory=list)
    error: str | None = None  # populated when status == "error"


# ---------------------------------------------------------------------------
# report.py output
# ---------------------------------------------------------------------------

Verdict = Literal["holds", "partial", "fails", "untestable"]


@dataclass
class ClaimFinding:
    claim: Claim
    validation: ValidationRun | None  # None if the claim was never validated (testable == "no")
    verdict: Verdict
    verdict_reason: str


@dataclass
class Report:
    video_id: str
    video_title: str | None
    video_url: str
    channel: str | None
    findings: list[ClaimFinding]
    verdict_overall: Verdict  # the "headline" — see report.py for how it's aggregated
    overall_reason: str
    markdown: str  # the human-readable report (this is what gets sent to Telegram / committed to examples)
    json: dict  # the structured form

    @property
    def run_id(self) -> str:
        # set by the orchestrator; stored on the json for convenience
        return self.json.get("run_id", "")


# ---------------------------------------------------------------------------
# tracing (used by orchestrate.py)
# ---------------------------------------------------------------------------


@dataclass
class TraceEvent:
    step: str
    ok: bool
    summary: str
    ms: int
    extra: dict = field(default_factory=dict)

    def to_jsonl(self) -> dict:
        d = {"step": self.step, "ok": self.ok, "summary": self.summary, "ms": self.ms}
        d.update(self.extra)
        return d
