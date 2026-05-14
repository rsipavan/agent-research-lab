"""Pine Script synthesis: strategy_backtest claims → .pine file → compiled → backtested.

The public entry point is `run()`. It:
  1. Detects any Pine Script code in the transcript text
  2. Synthesizes a complete Pine v5 strategy via the LLM (from extracted snippets
     OR from the verbal description when no code was shown)
  3. Compiles the script via the TradingView MCP; if it has errors, asks the LLM to fix
     them (up to _MAX_COMPILE_RETRIES attempts)
  4. Runs the TradingView strategy tester and reads the metrics
  5. Returns a ValidationRun with the results and (optionally) writes the .pine file to disk

Every failure mode returns a ValidationRun — it never raises past `run()`.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import llm as llm_mod
from .config import Config
from .mcp_client import McpClient, McpError
from .types import Claim, StrategyBacktestMetrics, Transcript, ValidationRun, VideoSummary

_MAX_COMPILE_RETRIES = 3

# Patterns that indicate Pine Script code is present in a transcript.
_PINE_MARKERS = re.compile(
    r"//@version=|strategy\s*\(|indicator\s*\(|plot\s*\(|ta\.rsi\(|ta\.ema\(|ta\.sma\(|"
    r"alertcondition\s*\(|bgcolor\s*\(|barcolor\s*\(|syminfo\.",
    re.IGNORECASE,
)


class PineSynthesisError(RuntimeError):
    """Raised when LLM synthesis itself fails — caught inside run() and converted to ValidationRun."""


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def run(
    claim: Claim,
    transcript: Transcript,
    summary: VideoSummary | None,
    config: Config,
    mcp: McpClient,
    *,
    out_dir: Path | None = None,
) -> ValidationRun:
    """Synthesize, compile, and backtest a Pine Script strategy for `claim`.

    `out_dir`: if provided, the .pine file is written there and `vr.pine_script_path`
    is the absolute path. If None, the script is processed in-memory only (useful in
    tests that don't need a real file).
    """
    instrument = claim.instrument or "SPY"
    timeframe = claim.timeframe or "1D"

    # --- step 1: synthesize ---
    try:
        script = _synthesize(claim, transcript, summary, config)
    except PineSynthesisError as e:
        return _failed(claim, f"could not synthesize Pine Script — {e}")
    except llm_mod.LlmUnavailable:
        return _failed(claim, "could not synthesize Pine Script — LLM unavailable")

    # Save the raw synthesis output immediately — before any compile attempts.
    # This is the key crash-safety artifact: if the compile loop fails or the process
    # dies mid-repair, draft_synthesis_<id>.pine always shows what the LLM produced.
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"draft_synthesis_{claim.id}.pine").write_text(script, encoding="utf-8")

    # --- step 2: compile (with self-repair loop) ---
    script, errors = _compile_and_fix(script, mcp, config, draft_dir=out_dir, claim_id=claim.id)

    # Write the final script to disk regardless of compile result.
    pine_path: str | None = None
    if out_dir is not None:
        file_path = out_dir / f"strategy_{claim.id}.pine"
        file_path.write_text(script, encoding="utf-8")
        pine_path = str(file_path.resolve())

    if errors:
        msg = f"script written but does not compile after {_MAX_COMPILE_RETRIES} fix attempts"
        if pine_path:
            msg += f" — see {Path(pine_path).name}"
        return ValidationRun(
            claim_id=claim.id,
            test_type="strategy_backtest",
            timeframe=timeframe,
            status="insufficient_data",
            tradingview_query=f"{instrument} {timeframe} Pine Strategy",
            data_summary=f"compile errors: {'; '.join(errors[:3])}",
            occurrences=None,
            hit_rate=None,
            result=msg,
            caveats=["script has compilation errors — open in TradingView Pine editor for details"],
            pine_script_path=pine_path,
        )

    # --- step 3: resolve symbol and run backtest ---
    try:
        metrics = _run_backtest(instrument, timeframe, mcp)
    except McpError as e:
        return ValidationRun(
            claim_id=claim.id,
            test_type="strategy_backtest",
            timeframe=timeframe,
            status="error",
            tradingview_query=f"{instrument} {timeframe} Pine Strategy",
            data_summary="",
            occurrences=None,
            hit_rate=None,
            result=f"strategy compiled but backtest failed: {e}",
            caveats=[],
            pine_script_path=pine_path,
        )

    if metrics is None:
        return ValidationRun(
            claim_id=claim.id,
            test_type="strategy_backtest",
            timeframe=timeframe,
            status="insufficient_data",
            tradingview_query=f"{instrument} {timeframe} Pine Strategy",
            data_summary="strategy compiled but produced no trades",
            occurrences=0,
            hit_rate=None,
            result=f"strategy compiled but produced no trades on {instrument} {timeframe} — adjust the date range or instrument",
            caveats=["no trades means the entry conditions never fired in the tested window"],
            pine_script_path=pine_path,
        )

    if pine_path:
        metrics = StrategyBacktestMetrics(
            net_profit=metrics.net_profit,
            gross_profit=metrics.gross_profit,
            total_trades=metrics.total_trades,
            winning_trades=metrics.winning_trades,
            win_rate=metrics.win_rate,
            max_drawdown=metrics.max_drawdown,
            profit_factor=metrics.profit_factor,
            pine_script_path=pine_path,
        )

    win_rate_pct = f"{metrics.win_rate:.0%}"
    return ValidationRun(
        claim_id=claim.id,
        test_type="strategy_backtest",
        timeframe=timeframe,
        status="ok",
        tradingview_query=f"{instrument} {timeframe} Pine Strategy (strategy tester)",
        data_summary=(
            f"{instrument} {timeframe}: {metrics.total_trades} trades, "
            f"{win_rate_pct} win rate, net {metrics.net_profit:+.2f}, "
            f"max drawdown {metrics.max_drawdown:.2f}, PF {metrics.profit_factor:.2f}"
        ),
        occurrences=metrics.total_trades,
        hit_rate=metrics.win_rate,
        result=(
            f"{metrics.winning_trades}/{metrics.total_trades} trades won ({win_rate_pct}); "
            f"net profit {metrics.net_profit:+.2f}; profit factor {metrics.profit_factor:.2f}"
        ),
        caveats=[
            "strategy tester results depend on the instrument, date range, and default capital settings in TradingView",
            "no slippage or commission modeled unless the Pine script includes them",
            "past performance does not predict future results",
        ],
        strategy_backtest=metrics,
        pine_script_path=pine_path,
    )


# ---------------------------------------------------------------------------
# synthesis
# ---------------------------------------------------------------------------


def _detect_pine_code(text: str) -> list[str]:
    """Return any Pine Script code snippets found in `text`. Empty list if none."""
    if not _PINE_MARKERS.search(text):
        return []
    # Heuristic: split on blank lines, keep chunks that contain Pine markers.
    chunks = re.split(r"\n{2,}", text)
    return [c.strip() for c in chunks if _PINE_MARKERS.search(c)]


def _synthesize(
    claim: Claim,
    transcript: Transcript,
    summary: VideoSummary | None,
    config: Config,
) -> str:
    """Ask the LLM to produce one complete Pine v5 strategy script."""
    snippets = _detect_pine_code(transcript.text)
    topic = summary.topic if summary else claim.statement
    text_excerpt = transcript.text[:6000]  # keep prompt manageable

    if snippets:
        combined = "\n\n---\n\n".join(snippets[:5])
        prompt = (
            f"Topic: {topic}\n"
            f"Strategy claim: {claim.statement}\n\n"
            f"The video transcript contains these Pine Script snippets:\n\n"
            f"```pine\n{combined}\n```\n\n"
            f"Extract, merge, and complete these into ONE complete, compilable Pine Script v5 "
            f"strategy (not indicator). Requirements:\n"
            f"- Start with //@version=5 and strategy() call\n"
            f"- Include all entry and exit logic from the snippets\n"
            f"- Fill any gaps (missing stop-loss, position sizing, etc.) with sensible defaults "
            f"documented in comments\n"
            f"- Output ONLY the Pine Script code, no explanation\n"
        )
    else:
        prompt = (
            f"Topic: {topic}\n"
            f"Strategy claim: {claim.statement}\n\n"
            f"Video transcript (excerpt):\n{text_excerpt}\n\n"
            f"Write a complete, compilable Pine Script v5 STRATEGY (not indicator) that implements "
            f"the trading strategy described above. Requirements:\n"
            f"- Start with //@version=5 and strategy() call\n"
            f"- Implement the entry and exit rules described in the video\n"
            f"- Where the video is ambiguous or silent, add a comment: // TODO: [what was unclear]\n"
            f"- Use sensible defaults for stop-loss, take-profit, and position sizing\n"
            f"- Output ONLY the Pine Script code, no explanation\n"
        )

    system = (
        "You are a Pine Script v5 expert. You write clean, compilable TradingView strategy "
        "scripts from trading strategy descriptions. You never write indicator scripts — always "
        "strategy scripts with strategy() and strategy.entry()/strategy.close() calls. You output "
        "ONLY the Pine Script code block, nothing else."
    )

    try:
        result = llm_mod.complete(system, prompt, max_tokens=4000, timeout=600)
    except llm_mod.LlmError as e:
        raise PineSynthesisError(str(e)) from e

    # Strip markdown fences if the LLM wrapped the code
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)
    return result.strip()


# ---------------------------------------------------------------------------
# compilation with self-repair
# ---------------------------------------------------------------------------


def _compile_and_fix(
    script: str,
    mcp: McpClient,
    config: Config,
    max_retries: int = _MAX_COMPILE_RETRIES,
    *,
    draft_dir: Path | None = None,
    claim_id: str = "c0",
) -> tuple[str, list[str]]:
    """Compile `script` via MCP, fix errors with LLM up to `max_retries` times.

    Returns (final_script, errors). If errors is empty, the script compiled cleanly.
    If draft_dir is provided, each failed fix attempt is saved as
    draft_fix_{claim_id}_{n}.pine so the full repair trajectory is inspectable.
    """
    for attempt in range(max_retries + 1):
        errors = _compile_once(script, mcp)
        if not errors:
            return script, []
        if attempt == max_retries:
            break
        # Ask the LLM to fix the errors
        try:
            script = _fix_script(script, errors, config)
        except (llm_mod.LlmUnavailable, llm_mod.LlmError):
            break
        # Save the post-fix draft so the repair trajectory is visible on disk.
        if draft_dir is not None:
            (draft_dir / f"draft_fix_{claim_id}_{attempt + 1}.pine").write_text(
                script, encoding="utf-8"
            )
    return script, errors


def _compile_once(script: str, mcp: McpClient) -> list[str]:
    """Inject script, compile, and return a list of error strings (empty = success)."""
    try:
        mcp.call("pine_set_source", {"source": script})
        mcp.call("pine_smart_compile", {})
        errors_res = mcp.call("pine_get_errors", {})
        return _parse_compile_errors(errors_res)
    except McpError:
        # MCP doesn't support Pine tools — treat as a non-recoverable error upstream
        raise


def _parse_compile_errors(res) -> list[str]:
    """Normalize the pine_get_errors response into a flat list of error strings."""
    if isinstance(res, dict):
        errors = res.get("errors") or res.get("error") or res.get("messages") or []
        if isinstance(errors, str):
            return [errors] if errors else []
        if isinstance(errors, list):
            out = []
            for e in errors:
                if isinstance(e, dict):
                    msg = e.get("message") or e.get("text") or e.get("error") or str(e)
                    out.append(str(msg))
                elif isinstance(e, str):
                    out.append(e)
            return out
    if isinstance(res, list):
        out = []
        for e in res:
            if isinstance(e, dict):
                msg = e.get("message") or e.get("text") or e.get("error") or str(e)
                out.append(str(msg))
            elif e:
                out.append(str(e))
        return out
    if isinstance(res, str) and res:
        return [res]
    return []


def _fix_script(script: str, errors: list[str], config: Config) -> str:
    """Ask the LLM to fix compilation errors in `script`."""
    error_text = "\n".join(f"- {e}" for e in errors[:10])
    prompt = (
        f"This Pine Script v5 strategy has compilation errors. Fix ALL of them.\n\n"
        f"Errors:\n{error_text}\n\n"
        f"Current script:\n```pine\n{script}\n```\n\n"
        f"Output ONLY the corrected Pine Script code, no explanation."
    )
    system = (
        "You are a Pine Script v5 expert fixing compilation errors. "
        "Output ONLY the corrected Pine Script code block, nothing else."
    )
    result = llm_mod.complete(system, prompt, max_tokens=4000, timeout=180)
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```\w*\n?", "", result)
        result = re.sub(r"\n?```$", "", result)
    return result.strip()


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def _run_backtest(instrument: str, timeframe: str, mcp: McpClient) -> StrategyBacktestMetrics | None:
    """Set the chart to instrument/timeframe and read strategy tester results.

    Returns None if the strategy produced no trades or the results couldn't be parsed.
    """
    mcp.call("chart_set_symbol", {"symbol": instrument})
    mcp.call("chart_set_timeframe", {"timeframe": timeframe})
    res = mcp.call("data_get_strategy_results", {})
    return _parse_strategy_results(res)


def _parse_strategy_results(res) -> StrategyBacktestMetrics | None:
    """Normalize data_get_strategy_results response into StrategyBacktestMetrics."""
    if not isinstance(res, dict):
        return None

    # TradingView MCP may return the data directly or nested under a key
    data = res
    for key in ("result", "data", "strategy", "performance"):
        if isinstance(res.get(key), dict):
            data = res[key]
            break

    def _fv(key, *aliases):
        for k in (key,) + aliases:
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _iv(key, *aliases):
        return int(_fv(key, *aliases))

    total = _iv("total_trades", "totalTrades", "total_closed_trades", "closedTrades")
    if total == 0:
        return None

    winning = _iv("winning_trades", "winningTrades", "profitable_trades", "profitableTrades")
    win_rate = winning / total if total > 0 else 0.0
    gross_profit = _fv("gross_profit", "grossProfit")
    gross_loss = abs(_fv("gross_loss", "grossLoss"))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return StrategyBacktestMetrics(
        net_profit=_fv("net_profit", "netProfit", "net_profit_percent", "netProfitPercent"),
        gross_profit=gross_profit,
        total_trades=total,
        winning_trades=winning,
        win_rate=win_rate,
        max_drawdown=abs(_fv("max_drawdown", "maxDrawdown", "max_drawdown_percent")),
        profit_factor=profit_factor,
        pine_script_path="",  # set by caller after writing the file
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _failed(claim: Claim, reason: str) -> ValidationRun:
    return ValidationRun(
        claim_id=claim.id,
        test_type="strategy_backtest",
        timeframe="n/a",
        status="error",
        tradingview_query="",
        data_summary="",
        occurrences=None,
        hit_rate=None,
        result=f"untestable — {reason}",
        caveats=[],
        error=reason,
    )
