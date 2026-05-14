"""Regenerate the examples/ folder by running the pipeline on a fixed set of videos.

Each example folder gets:
  input.md        — the URL + a one-line note on why this video was chosen
  transcript.txt  — the fetched transcript (already written by the run, kept)
  thesis.json     — the extracted, classified claims
  report.md       — the human-readable report
  report.json     — the structured report
  trace.jsonl     — the step-by-step trace, copied from traces/<run-id>.jsonl

Run from the repo root:  python scripts/build_examples.py
Requires an LLM backend (the `claude` CLI by default — see README) and, for
validation-complete runs, a TradingView MCP reachable via TRADINGVIEW_MCP_URL.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_research_lab.config import repo_root  # noqa: E402
from agent_research_lab.orchestrate import RunAborted, process  # noqa: E402

# (folder slug, youtube url, why-this-video note)
EXAMPLES = [
    (
        "01_rsi_bollinger_tested_2025",
        "https://www.youtube.com/watch?v=j2ESnjhT2no",
        "A 'we tested this strategy' video whose results can't be independently checked — "
        "it never enumerates the instrument universe ('100 most liquid crypto') or the capital "
        "assumptions. Shows the agent correctly refusing to manufacture a verdict.",
    ),
    (
        "02_rsi_profitable_or_overhyped",
        "https://www.youtube.com/watch?v=ualY_K-TPe0",
        "A backtest-results video ('4,000+ trades'). Strategy-shaped claims map to "
        "strategy_backtest, which is honestly out of scope in v1 — shows the 'needs a "
        "backtest engine' boundary.",
    ),
    (
        "03_rsi_divergence_xauusd",
        "https://www.youtube.com/watch?v=aHeWIR8kM9o",
        "An RSI-divergence + EMA-200 strategy video. Claims map to strategy_backtest "
        "(full entry+exit rules with a TradingView backtest) and a math-verification, "
        "neither of which v1 can test — shows the 'strategy_backtest not in v1' boundary "
        "even for content that mentions specific instruments and timeframes.",
    ),
    (
        "04_alphainsider_promo_walkthrough",
        "https://youtu.be/RzJIhCWj3rQ",
        "A walkthrough promoting AlphaInsider + Alpaca for auto-trading. Tests the "
        "`summary.skip_extraction` path for `promotion` content_type — the pipeline correctly "
        "skips claim extraction rather than fabricating trading claims out of a software demo.",
    ),
    (
        "05_orb_acceptance_short_no_claims",
        "https://youtu.be/IuvsvMUon5k",
        "A 99-word YouTube Short giving directional ORB-trading advice with no instrument, "
        "no timeframe, no quantified threshold. Tests the `educational, has_checkable_claims=false` "
        "short-circuit — the agent refuses to treat hand-wavy strategy talk as a testable claim.",
    ),
]


def main() -> int:
    root = repo_root()
    examples_dir = root / "examples"
    traces_dir = root / "traces"

    for slug, url, why in EXAMPLES:
        out = examples_dir / slug
        out.mkdir(parents=True, exist_ok=True)
        print(f"=== {slug} : {url}")
        (out / "input.md").write_text(
            f"# {slug}\n\n**URL:** {url}\n\n**Why this video:** {why}\n", encoding="utf-8"
        )
        try:
            report = process(url)
        except RunAborted as e:
            (out / "report.md").write_text(f"# (run aborted)\n\n{e.user_message}\n", encoding="utf-8")
            print(f"  aborted: {e.user_message}")
            continue
        except Exception as e:  # noqa: BLE001
            (out / "report.md").write_text(f"# (run failed)\n\n{e}\n", encoding="utf-8")
            print(f"  failed: {e}")
            continue

        # transcript.txt: re-write from the run's transcript so it always matches
        # (the run fetched it; we don't have the Transcript object back, but the
        # report.json has the video id — and transcript.fetch is cheap. Simpler: the
        # earlier fetch already wrote transcript.txt; leave it. If missing, note it.)
        if not (out / "transcript.txt").exists():
            (out / "transcript.txt").write_text("(transcript not captured — re-run to populate)\n",
                                                encoding="utf-8")

        (out / "report.md").write_text(report.markdown, encoding="utf-8")
        (out / "report.json").write_text(json.dumps(report.json, indent=2), encoding="utf-8")
        vs = report.json.get("video_summary")
        if vs:
            (out / "summary.json").write_text(json.dumps(vs, indent=2), encoding="utf-8")
        (out / "thesis.json").write_text(
            json.dumps([f["statement"] and {
                "claim_id": f["claim_id"], "statement": f["statement"],
                "instrument": f["instrument"], "timeframe": f["timeframe"],
                "testable": f["testable"], "test_type": f["test_type"],
                "verdict": f["verdict"], "verdict_reason": f["verdict_reason"],
            } for f in report.json.get("findings", [])],
            indent=2),
            encoding="utf-8",
        )
        # copy the trace
        run_id = report.run_id
        src_trace = traces_dir / f"{run_id}.jsonl"
        if src_trace.exists():
            shutil.copyfile(src_trace, out / "trace.jsonl")
        print(f"  verdict_overall={report.json.get('verdict_overall')}  run_id={run_id}")

    print("\ndone. examples/ updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
