"""The pipeline: url -> Report. Owns tracing. Owns the one place a load-bearing
failure (no transcript, extraction failure) becomes an aborted run with a
user-facing message instead of a half-report.

Also the CLI entrypoint (`python -m agent_research_lab.orchestrate <url>`) — that's
how the examples/ in this repo were built.

See docs/architecture.md and docs/failure_handling.md.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import report as report_mod
from . import thesis as thesis_mod
from . import transcript as transcript_mod
from . import validate as validate_mod
from .config import Config, load_config, repo_root
from .mcp_client import McpClient
from .thesis import ExtractionError
from .types import Report, TraceEvent


class RunAborted(RuntimeError):
    """A load-bearing failure with no useful partial output. Carries a user-facing message."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def process(url: str, config: Config | None = None) -> Report:
    """Run the full pipeline for `url`. Returns a Report.

    Raises RunAborted only when there's nothing useful to report (no transcript,
    or extraction itself failed). Everything else degrades to per-claim "untestable"
    verdicts and a normal Report — see docs/failure_handling.md.
    """
    config = config or load_config()
    trace: list[TraceEvent] = []

    # --- step 1: transcript ---
    t0 = time.perf_counter()
    transcript = transcript_mod.fetch(url)
    run_id = _mint_run_id(transcript.video_id or "unknown")
    if transcript.is_empty:
        trace.append(TraceEvent("transcript.fetch", False,
                                f"no transcript / too short ({transcript.word_count} words)",
                                _ms(t0)))
        _write_trace(config, run_id, trace)
        # short-circuit to a minimal report — but DON'T abort: the user gets an honest report.
        return report_mod.build_minimal(
            transcript, run_id,
            reason="no transcript was available for this video (it has no captions, or the captions are too short to analyze)",
        )
    trace.append(TraceEvent("transcript.fetch", True,
                            f"fetched {transcript.word_count:,} words", _ms(t0)))

    # --- step 2: thesis extraction (load-bearing) ---
    t0 = time.perf_counter()
    try:
        thesis = thesis_mod.extract(transcript, config)
    except ExtractionError as e:
        trace.append(TraceEvent("thesis.extract", False, f"extraction failed: {e}", _ms(t0)))
        _write_trace(config, run_id, trace)
        raise RunAborted("couldn't analyze the transcript right now — please try again in a moment.") from e

    n_testable = len(thesis.testable_claims)
    trace.append(TraceEvent(
        "thesis.extract", True,
        f"{len(thesis.claims)} claim(s): {n_testable} testable, {len(thesis.claims) - n_testable} not",
        _ms(t0),
    ))

    if not thesis.claims:
        _write_trace(config, run_id, trace)
        return report_mod.build_minimal(
            transcript, run_id,
            reason="this video contains no trading claims (the agent found nothing checkable in the transcript)",
        )
    if not thesis.has_any_testable:
        # We have claims but they're all `no` — build the full report (it lists each claim
        # and why it isn't testable), not the minimal one.
        report = report_mod.build(transcript, thesis, [], config, run_id)
        trace.append(TraceEvent("report.build", True,
                                f"verdict_overall={report.verdict_overall} (no testable claims)", 0))
        _write_trace(config, run_id, trace)
        report.json["run_id"] = run_id
        return report

    # --- step 3: validate each testable claim ---
    runs = []
    with McpClient(config) as mcp:
        for claim in thesis.testable_claims:
            t0 = time.perf_counter()
            vr = validate_mod.run(claim, config, mcp)
            runs.append(vr)
            ok = vr.status == "ok"
            trace.append(TraceEvent(
                "validate.run", ok,
                f"[{claim.id}] {vr.result}" + ("" if ok else f" (status={vr.status})"),
                _ms(t0), extra={"claim_id": claim.id},
            ))

    # --- step 4: report ---
    t0 = time.perf_counter()
    report = report_mod.build(transcript, thesis, runs, config, run_id)
    report.json["run_id"] = run_id
    trace.append(TraceEvent("report.build", True,
                            f"verdict_overall={report.verdict_overall}; {len(report.findings)} claim(s)",
                            _ms(t0)))

    _write_trace(config, run_id, trace)
    return report


# ---------------------------------------------------------------------------
# tracing
# ---------------------------------------------------------------------------


def _mint_run_id(video_id: str) -> str:
    return f"{video_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _write_trace(config: Config, run_id: str, trace: list[TraceEvent]) -> Path | None:
    if not config.tracing_enabled:
        return None
    trace_dir = repo_root() / config.tracing_dir
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for ev in trace:
            fh.write(json.dumps(ev.to_jsonl()) + "\n")
    return path


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m agent_research_lab.orchestrate <youtube-url>")
        print("       runs the pipeline on one video and prints the report (also writes a trace to traces/).")
        return 0
    url = argv[0]
    try:
        report = process(url)
    except RunAborted as e:
        print(f"[aborted] {e.user_message}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort: never crash without saying why
        print(f"[error] unexpected failure: {e}", file=sys.stderr)
        return 2
    print(report.markdown)
    if report.run_id:
        print(f"\n(trace: traces/{report.run_id}.jsonl)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
