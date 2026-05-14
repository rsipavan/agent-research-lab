"""The pipeline: url -> Report. Owns tracing. Owns the one place a load-bearing
failure (no transcript, extraction failure) becomes an aborted run with a
user-facing message instead of a half-report.

Also the CLI entrypoint (`python -m trading_hypothesis_lab.orchestrate <url>`) — that's
how the examples/ in this repo were built.

See docs/architecture.md and docs/failure_handling.md.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import knowledge as knowledge_mod
from . import pine as pine_mod
from . import report as report_mod
from . import summarize as summarize_mod
from . import thesis as thesis_mod
from . import transcript as transcript_mod
from . import validate as validate_mod
from .config import Config, load_config, repo_root
from .mcp_client import McpClient
from .summarize import SummarizeError
from .thesis import ExtractionError
from .types import Report, ThesisSet, TraceEvent, Transcript, ValidationRun, VideoSummary


class RunAborted(RuntimeError):
    """A load-bearing failure with no useful partial output. Carries a user-facing message."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def process(
    url: str,
    config: Config | None = None,
    *,
    timeframes_override: list[str] | None = None,
    watchlist_name: str | None = None,
    step_callback=None,
) -> Report:
    """Run the full pipeline for `url`. Returns a Report.

    Args:
        timeframes_override: if set, every indicator/level claim is tested on these
            timeframes instead of the claim-extracted or config-default ones. Accepts
            TradingView-style strings: "1", "5", "15", "60", "240", "D", "W", "M".
            Example: ["1D", "4H", "1H"]
        watchlist_name: if set, also run all testable claims against every symbol in
            the named watchlist after the primary run completes. Printed to stdout by
            the CLI; not included in the returned Report.
        step_callback: optional callable(step_name: str, detail: str, ok: bool) -> None
            called after each major pipeline step. Used by telegram_bot for live progress.

    Raises RunAborted only when there's nothing useful to report (no transcript,
    or extraction itself failed). Everything else degrades to per-claim "untestable"
    verdicts and a normal Report — see docs/failure_handling.md.

    Side-effects: writes a streaming trace to traces/<run_id>.jsonl and (if enabled)
    a full artifact bundle to runs/<run_id>/.
    """
    def _cb(name: str, detail: str, ok: bool = True) -> None:
        if step_callback:
            try:
                step_callback(name, detail, ok)
            except Exception:  # noqa: BLE001
                pass  # never let a callback crash the pipeline

    config = config or load_config()
    trace: list[TraceEvent] = []
    summary: VideoSummary | None = None
    thesis: ThesisSet | None = None
    runs: list[ValidationRun] | None = None

    # --- step 1: transcript ---
    t0 = time.perf_counter()
    transcript = transcript_mod.fetch(url)
    run_id = _mint_run_id(transcript.video_id or "unknown")
    if transcript.is_empty:
        trace.append(TraceEvent("transcript.fetch", False,
                                f"no transcript / too short ({transcript.word_count} words)", _ms(t0)))
        _write_trace(config, run_id, trace)
        _cb("transcript.fetch", "No transcript available for this video", False)
        # short-circuit to a minimal report — but DON'T abort: the user gets an honest report.
        report = report_mod.build_minimal(
            transcript, None, run_id,
            reason="no transcript was available for this video (it has no captions, or the captions are too short to analyze)",
        )
        _write_run_artifacts(config, run_id, url, transcript, summary, thesis, runs, report)
        return report
    trace.append(TraceEvent("transcript.fetch", True, f"fetched {transcript.word_count:,} words", _ms(t0)))
    _cb("transcript.fetch", f"Transcript fetched: {transcript.word_count:,} words")

    # --- step 2: summarize — establish what kind of video this is (load-bearing) ---
    t0 = time.perf_counter()
    try:
        summary = summarize_mod.summarize(transcript, config)
    except SummarizeError as e:
        trace.append(TraceEvent("video.summarize", False, f"summarize failed: {e}", _ms(t0)))
        _write_trace(config, run_id, trace)
        raise RunAborted("couldn't analyze the video right now — please try again in a moment.") from e
    trace.append(TraceEvent(
        "video.summarize", True,
        f"{summary.content_type} — {summary.topic} (checkable claims: {'yes' if summary.has_checkable_claims else 'no'})",
        _ms(t0),
    ))
    _cb("video.summarize", f"Video: {summary.content_type} — {summary.topic}")

    # If the video isn't claim-bearing by nature (mindset, vlog, promo, off-topic, or
    # the summarizer flagged no checkable claims), we don't run extraction — the report
    # is the summary plus an honest "nothing to validate, and why".
    if summary.skip_extraction:
        _cb("report.build", f"No checkable claims ({summary.content_type}) — summary only")
        report = report_mod.build_summary_only(transcript, summary, run_id)
        trace.append(TraceEvent("report.build", True,
                                f"summary-only ({summary.content_type}); no extraction run", 0))
        _write_trace(config, run_id, trace)
        _write_run_artifacts(config, run_id, url, transcript, summary, thesis, runs, report)
        return report

    # --- step 3: thesis extraction (load-bearing) ---
    _cb("thesis.extract", "Extracting claims...")
    t0 = time.perf_counter()
    try:
        thesis = thesis_mod.extract(transcript, summary, config)
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
    _cb("thesis.extract",
        f"Claims: {len(thesis.claims)} found, {n_testable} testable")

    # No testable claims (zero claims, or all classified `no`) -> full report that
    # leads with the summary and lists each claim and why it isn't testable.
    if not thesis.has_any_testable:
        _cb("report.build", "No testable claims — building summary report")
        report = report_mod.build(transcript, summary, thesis, [], config, run_id)
        trace.append(TraceEvent("report.build", True,
                                f"verdict_overall={report.verdict_overall} (no testable claims)", 0))
        _write_trace(config, run_id, trace)
        _write_run_artifacts(config, run_id, url, transcript, summary, thesis, runs, report)
        return report

    # --- step 4: validate each testable claim ---
    # Compute run_dir early so pine.py can write .pine files there during the MCP session.
    run_dir: Path | None = None
    if config.runs_enabled:
        run_dir = repo_root() / config.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    # Split claims: strategy_backtest goes through Pine synthesis; everything else
    # goes through the existing indicator/level validator.
    backtest_claims = [c for c in thesis.testable_claims if c.test_type == "strategy_backtest"]
    indicator_claims = [c for c in thesis.testable_claims if c.test_type != "strategy_backtest"]

    runs = []
    with McpClient(config) as mcp:
        for claim in indicator_claims:
            _cb("validate.run",
                f"Checking: {claim.instrument or '?'} {claim.timeframe or ''} ({claim.test_type})")
            t0 = time.perf_counter()
            claim_runs = validate_mod.run(claim, config, mcp, timeframes_override=timeframes_override)
            runs.extend(claim_runs)
            n_ok = sum(1 for r in claim_runs if r.status == "ok")
            trace.append(TraceEvent(
                "validate.run", n_ok > 0,
                f"[{claim.id}] {n_ok}/{len(claim_runs)} timeframe(s) ok",
                _ms(t0),
                extra={"claim_id": claim.id, "n_timeframes": len(claim_runs)},
            ))
            _cb("validate.run",
                f"Validated [{claim.id}]: {n_ok}/{len(claim_runs)} timeframes OK", n_ok > 0)

        for claim in backtest_claims:
            cached = _find_cached_pine_script(transcript.video_id, claim.id, config)
            action = "Reusing cached Pine Script" if cached else "Synthesizing Pine Script"
            _cb("pine.run",
                f"{action} for {claim.instrument or '?'} {claim.timeframe or ''}...")
            t0 = time.perf_counter()
            vr = pine_mod.run(
                claim, transcript, summary, config, mcp,
                out_dir=run_dir, cached_script=cached,
            )
            runs.append(vr)
            trace.append(TraceEvent(
                "pine.run", vr.status == "ok",
                f"[{claim.id}] Pine synthesis: {vr.status} — {vr.result[:80]}",
                _ms(t0),
                extra={"claim_id": claim.id},
            ))
            _cb("pine.run",
                f"Backtest [{claim.id}]: {vr.status} — {vr.result[:60]}", vr.status == "ok")

    # --- step 5: report ---
    t0 = time.perf_counter()
    report = report_mod.build(transcript, summary, thesis, runs, config, run_id)
    trace.append(TraceEvent("report.build", True,
                            f"verdict_overall={report.verdict_overall}; {len(report.findings)} claim(s)", _ms(t0)))
    _cb("report.build", f"Report ready — verdict: {report.verdict_overall}")
    _write_trace(config, run_id, trace)
    _write_run_artifacts(config, run_id, url, transcript, summary, thesis, runs, report)

    # --- step 6: append findings to the pattern-aware validation memory ---
    _append_to_knowledge(report, transcript.video_id or run_id)

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


def _find_cached_pine_script(video_id: str, claim_id: str, config: Config) -> str | None:
    """Return the text of the most recently compiled Pine Script for this claim, or None.

    Scans runs/<run_id>/ directories whose name starts with the video_id. The compiled
    script is `strategy_<claim_id>.pine` — not draft files. Returns the newest match so
    the system always reuses the latest successfully compiled version.
    """
    runs_root = repo_root() / config.runs_dir
    if not runs_root.exists():
        return None
    candidates = sorted(
        runs_root.glob(f"{video_id}-*/strategy_{claim_id}.pine"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return None


def _write_run_artifacts(
    config: Config,
    run_id: str,
    url: str,
    transcript: Transcript,
    summary: VideoSummary | None,
    thesis: ThesisSet | None,
    runs: list[ValidationRun] | None,
    report: Report,
) -> Path | None:
    """Write the full artifact bundle for one run to runs/<run_id>/.

    Mirrors the layout of examples/<slug>/ so anyone cloning the repo and running
    the CLI gets a full disk artifact for every run — not just stdout that's gone
    after the terminal closes. Files written (skipped silently if data is absent):

      input.md         URL + run timestamp
      transcript.txt   the fetched transcript text
      summary.json     the VideoSummary dataclass
      thesis.json      the extracted claims (empty list if extraction was skipped)
      report.md        the human-readable report
      report.json      the structured report
      trace.jsonl      copy of traces/<run_id>.jsonl, for self-contained run bundles
    """
    # `runs` is also a parameter name on this function — keep the param meaning here.
    if not config.runs_enabled:
        return None
    run_dir = repo_root() / config.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "input.md").write_text(
        f"# {run_id}\n\n**URL:** {url}\n\n**Ran:** {datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )
    (run_dir / "transcript.txt").write_text(transcript.text or "", encoding="utf-8")
    if summary is not None:
        summary_dict = {
            "video_id": summary.video_id,
            "content_type": summary.content_type,
            "topic": summary.topic,
            "summary": summary.summary,
            "has_checkable_claims": summary.has_checkable_claims,
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary_dict, indent=2), encoding="utf-8",
        )
    # Write thesis.json unconditionally — empty list when extraction was skipped
    # (summary-only path) so every run bundle has the same file set.
    thesis_dict = (
        [
            {
                "id": c.id,
                "statement": c.statement,
                "instrument": c.instrument,
                "timeframe": c.timeframe,
                "test_type": c.test_type,
                "testable": c.testable,
                "reason_if_not": c.reason_if_not,
                "confidence": c.confidence,
                "test_type_justification": c.test_type_justification,
            }
            for c in thesis.claims
        ]
        if thesis is not None
        else []
    )
    (run_dir / "thesis.json").write_text(
        json.dumps(thesis_dict, indent=2), encoding="utf-8",
    )
    (run_dir / "report.md").write_text(report.markdown, encoding="utf-8")
    (run_dir / "report.json").write_text(
        json.dumps(report.json, indent=2), encoding="utf-8",
    )
    # copy the trace from traces/ (already written) into the run bundle for portability
    src_trace = repo_root() / config.tracing_dir / f"{run_id}.jsonl"
    if src_trace.exists():
        (run_dir / "trace.jsonl").write_text(
            src_trace.read_text(encoding="utf-8"), encoding="utf-8",
        )
    return run_dir


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _append_to_knowledge(report, video_id: str) -> None:
    """Append each claim finding to the pattern-aware validation memory."""
    try:
        for finding in report.findings:
            # Detect inverse edge PF from the verdict reason text, so the knowledge
            # entry is consistent with what the report already computed.
            inverse_edge_pf: float | None = None
            if "inverse signal detected" in finding.verdict_reason:
                import re as _re
                m = _re.search(r"estimated PF ([\d.]+)", finding.verdict_reason)
                if m:
                    try:
                        inverse_edge_pf = float(m.group(1))
                    except ValueError:
                        pass
            entry = knowledge_mod.entry_from_finding(
                finding, video_id, inverse_edge_pf=inverse_edge_pf
            )
            knowledge_mod.append(entry)
    except Exception:  # noqa: BLE001
        # Knowledge base write failure must never affect the report or abort the run.
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_VALID_TIMEFRAMES = ["1", "3", "5", "10", "15", "30", "60", "120", "240", "D", "W", "M"]
_TIMEFRAME_ALIASES = {"1d": "D", "daily": "D", "1w": "W", "weekly": "W", "1m": "M", "monthly": "M",
                      "4h": "240", "1h": "60", "30m": "30", "15m": "15", "5m": "5", "1min": "1"}


def _parse_timeframes(raw: str) -> list[str]:
    """Parse a comma-separated timeframe string into TradingView-style values."""
    out = []
    for part in raw.split(","):
        t = part.strip()
        normalized = _TIMEFRAME_ALIASES.get(t.lower(), t.upper() if t.isalpha() else t)
        out.append(normalized)
    return [t for t in out if t]


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr on Windows (default codepage is cp1252).
    # PYTHONIOENCODING=utf-8:replace is the most reliable approach for piped
    # processes; the reconfigure/buffer-wrap path is kept as an in-process
    # backup for consoles that don't respect the env var.
    import io as _io
    import os as _os
    _os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        # reconfigure is preferred when available (Python 3.7+)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
                continue
            except Exception:  # noqa: BLE001
                pass
        # Fallback: wrap the raw buffer directly
        if hasattr(stream, "buffer"):
            try:
                setattr(sys, stream_name,
                        _io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m trading_hypothesis_lab.orchestrate <youtube-url> [options]")
        print()
        print("  Runs the pipeline on one video, prints the report to stdout,")
        print("  and saves the full artifact bundle under runs/<run_id>/.")
        print()
        print("Options:")
        print("  --watchlist <name>      also scan claims across every symbol in the watchlist")
        print("  --timeframe <tf[,tf]>   override timeframe(s) used for indicator/level tests")
        print()
        print("Watchlists:")
        print("  default      16 cross-market essentials: major indices (SPX NDX DAX NIFTY...), BTC ETH, EURUSD, XAUUSD, USOIL")
        print("  nifty50      50 Nifty 50 constituents (NSE India)")
        print("  sp500        50 S&P 500 large-caps (NYSE/NASDAQ)")
        print("  crypto       10 major crypto pairs (BTCUSD, ETHUSD, ...)")
        print("  forex        10 major FX pairs (EURUSD, GBPUSD, ...)")
        print("  commodities  8 commodities (XAUUSD, USOIL, NGAS, ...)")
        print()
        print("Timeframes (TradingView notation):")
        print("  Minutes : 1  3  5  10  15  30  60  120  240")
        print("  Daily   : D  (alias: 1D, daily)")
        print("  Weekly  : W  (alias: 1W, weekly)")
        print("  Monthly : M  (alias: 1M, monthly)")
        print()
        print("Examples:")
        print("  python -m trading_hypothesis_lab.orchestrate 'https://youtu.be/...'")
        print("  python -m trading_hypothesis_lab.orchestrate 'https://youtu.be/...' --timeframe D,4H,1H")
        print("  python -m trading_hypothesis_lab.orchestrate 'https://youtu.be/...' --watchlist nifty50 --timeframe D")
        return 0

    # Parse flags
    watchlist_name: str | None = None
    timeframes_override: list[str] | None = None

    if "--watchlist" in argv:
        idx = argv.index("--watchlist")
        if idx + 1 < len(argv):
            watchlist_name = argv[idx + 1]
            argv = [a for i, a in enumerate(argv) if i not in (idx, idx + 1)]
        else:
            print("[error] --watchlist requires a name (nifty50, sp500, crypto, forex, commodities)", file=sys.stderr)
            return 1

    if "--timeframe" in argv:
        idx = argv.index("--timeframe")
        if idx + 1 < len(argv):
            timeframes_override = _parse_timeframes(argv[idx + 1])
            argv = [a for i, a in enumerate(argv) if i not in (idx, idx + 1)]
            print(f"[timeframe override] testing on: {', '.join(timeframes_override)}", file=sys.stderr)
        else:
            print("[error] --timeframe requires a value (e.g. D,4H,1H)", file=sys.stderr)
            return 1

    url = argv[0]
    try:
        report = process(url, timeframes_override=timeframes_override, watchlist_name=watchlist_name)
    except RunAborted as e:
        print(f"[aborted] {e.user_message}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort: never crash without saying why
        print(f"[error] unexpected failure: {e}", file=sys.stderr)
        return 2
    # Write through the raw buffer when available so Unicode always works,
    # regardless of whether the stdout TextIOWrapper was successfully reconfigured.
    out = report.markdown + "\n"
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(out)
    if report.run_id:
        print(
            f"\n(saved: runs/{report.run_id}/  ·  trace: traces/{report.run_id}.jsonl)",
            file=sys.stderr,
        )

    if watchlist_name and report.findings:
        _run_watchlist_mode(report, watchlist_name)

    return 0


def _run_watchlist_mode(report, watchlist_name: str) -> None:
    from . import watchlist as wl_mod
    import json as _json

    try:
        symbols = wl_mod.get_watchlist(watchlist_name)
    except KeyError as e:
        print(f"\n[watchlist] {e}", file=sys.stderr)
        return

    config = load_config()
    testable_claims = [f.claim for f in report.findings if f.claim.testable in ("yes", "partial")]
    if not testable_claims:
        print(f"\n[watchlist] no testable claims to run against {watchlist_name}", file=sys.stderr)
        return

    print(f"\n## Watchlist scan: {watchlist_name} ({len(symbols)} symbols)")
    with McpClient(config) as mcp:
        for claim in testable_claims:
            print(f"\n### Claim: {claim.statement}")
            results = wl_mod.run_watchlist(claim, symbols, config, mcp)
            summary = wl_mod.summarize_watchlist_results(results)
            print(_json.dumps(summary, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
