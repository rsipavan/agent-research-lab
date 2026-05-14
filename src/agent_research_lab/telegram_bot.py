"""The Telegram edge: listens for YouTube URLs, runs the pipeline, replies with the report.

Agentic flow per request:
  1. Live progress — edits one status message as each pipeline step completes
  2. Chart screenshots — captures TradingView chart for each validated claim, sends as photo
  3. Strategy tester screenshot — captured when claim is a strategy_backtest
  4. Full report — sent as chunked text after all visual assets
  5. Pine Script files — sent as document attachments (drop into TradingView)

Run: python -m agent_research_lab.telegram_bot
Needs TELEGRAM_BOT_TOKEN in .env. Optionally TELEGRAM_ALLOWLIST (comma-separated user IDs).

See docs/failure_handling.md for the input-validation and send-failure behavior.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import Config, load_config
from .mcp_client import McpClient
from .orchestrate import RunAborted, process
from .transcript import extract_video_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent_research_lab.telegram_bot")

try:  # pragma: no cover
    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    _HAVE_PTB = True
except Exception:  # pragma: no cover
    _HAVE_PTB = False


_URL_RE = re.compile(r"https?://\S+")
_HELP = (
    "Send me a YouTube link to a trading video. I'll fetch the transcript, extract the "
    "*checkable* claims, test them against real market data via the TradingView MCP, and "
    "send back a structured report with verdicts, charts, and caveats.\n\n"
    "Most trading videos contain no checkable claims — I'll tell you that plainly when that's the case."
)

_STEP_ICON = {True: "OK", False: "ERR"}


def _find_youtube_url(text: str) -> str | None:
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0)
        if extract_video_id(url):
            return url
    return None


def _build_app(config: Config):
    if not _HAVE_PTB:  # pragma: no cover
        raise RuntimeError("python-telegram-bot not installed — `pip install -e .` first")
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(config.telegram_bot_token).build()

    async def on_message(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        msg = update.effective_message
        if msg is None:
            return
        user_id = update.effective_user.id if update.effective_user else None

        # allowlist gate
        if config.telegram_allowlist and (user_id not in config.telegram_allowlist):
            await msg.reply_text("this bot is private.")
            return

        text = msg.text or ""
        url = _find_youtube_url(text)
        if not url:
            await msg.reply_text(
                "that doesn't look like a YouTube URL — send a youtube.com/watch?v=…, youtu.be/…, "
                "or Shorts link.\n\n" + _HELP
            )
            return

        # ── Live progress tracking ───────────────────────────────────────────
        # Send a status message we'll keep editing as steps complete.
        status = await msg.reply_text("Analyzing video...")
        completed_steps: list[str] = []
        loop = asyncio.get_event_loop()

        def on_step(name: str, detail: str, ok: bool = True) -> None:
            icon = "[OK]" if ok else "[!]"
            completed_steps.append(f"{icon} {detail}")
            body = "\n".join(completed_steps)
            # Fire-and-forget — don't block the pipeline worker thread
            asyncio.run_coroutine_threadsafe(
                _safe_edit(status, body), loop
            )

        # ── Run pipeline in executor (non-blocking) ──────────────────────────
        try:
            report = await loop.run_in_executor(
                None,
                lambda: process(url, config, step_callback=on_step),
            )
        except RunAborted as e:
            await _safe_edit(status, f"[!] {e.user_message}")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed for %s", url)
            await _safe_edit(status, f"[!] Something went wrong: {e}")
            return

        # Mark done
        await _safe_edit(status, "\n".join(completed_steps) + "\n\nSending results...")

        # ── Chart screenshots per validated claim ────────────────────────────
        await _send_claim_charts(msg, report, config)

        # ── Full report text ─────────────────────────────────────────────────
        await _reply_chunked(msg, report.markdown)

        # ── Pine Script attachments ──────────────────────────────────────────
        await _send_pine_files(msg, report)

        # Update final status
        await _safe_edit(status, "\n".join(completed_steps) + "\n\nDone.")

        if report.run_id:
            log.info("done: %s -> %s (trace traces/%s.jsonl)", url, report.verdict_overall, report.run_id)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


async def _safe_edit(msg, text: str) -> None:
    """Edit a message, silently ignoring errors (message not modified, etc.)."""
    try:
        await msg.edit_text(text)
    except Exception:  # noqa: BLE001
        pass


_VERDICT_LABEL = {
    "holds": "CONFIRMED",
    "partial": "PARTIAL SUPPORT",
    "fails": "NOT SUPPORTED",
    "untestable": "UNTESTABLE",
}


async def _send_claim_charts(msg, report, config: Config) -> None:
    """For each validated claim: set TV to the right symbol/timeframe, screenshot, send as photo.
    Strategy backtest claims also get a trade-overlay chart and strategy-tester screenshot.
    """
    claims_to_chart = [
        f for f in report.findings
        if f.claim.testable != "no" and f.validations and f.claim.instrument
    ]
    if not claims_to_chart:
        return

    try:
        with McpClient(config) as mcp:
            for finding in claims_to_chart:
                claim = finding.claim
                sym = claim.instrument
                tf = claim.timeframe or "D"

                mcp.call("chart_set_symbol", {"symbol": sym})
                mcp.call("chart_set_timeframe", {"timeframe": tf})

                # Plain chart screenshot
                result = mcp.call("capture_screenshot", {
                    "region": "chart",
                    "filename": f"tg_chart_{claim.id}",
                })
                chart_path = result.get("file_path") if result.get("success") else None
                if chart_path and Path(chart_path).exists():
                    verdict_label = _VERDICT_LABEL.get(finding.verdict, finding.verdict.upper())
                    caption = (
                        f"{sym} | {tf}\n"
                        f"Verdict: {verdict_label}\n"
                        f"{finding.verdict_reason[:200]}"
                    )
                    await _send_photo(msg, chart_path, caption)

                # For strategy backtests: trade overlay + strategy tester screenshot
                if claim.test_type == "strategy_backtest":
                    await _send_trade_overlay(msg, mcp, claim, finding)

    except Exception:  # noqa: BLE001
        log.warning("chart screenshot step failed — skipping", exc_info=True)


async def _send_trade_overlay(msg, mcp, claim, finding) -> None:
    """Draw trade entry/exit markers on the chart, capture annotated screenshot, send trade table."""
    try:
        trades_result = mcp.call("data_get_trades", {})
        trades = trades_result.get("trades") if isinstance(trades_result, dict) else None
        if not trades:
            # Strategy tester screenshot is still useful even without individual trades
            result = mcp.call("capture_screenshot", {
                "region": "strategy_tester",
                "filename": f"tg_strat_{claim.id}",
            })
            strat_path = result.get("file_path") if result.get("success") else None
            if strat_path and Path(strat_path).exists():
                await _send_photo(msg, strat_path, "Strategy Tester results")
            return

        # Draw entry/exit markers for each trade (up to 20 most recent to avoid clutter)
        recent = trades[-20:] if len(trades) > 20 else trades
        for i, trade in enumerate(recent):
            entry_price = trade.get("entry_price") or trade.get("entryPrice")
            exit_price = trade.get("exit_price") or trade.get("exitPrice")
            direction = (trade.get("type") or trade.get("direction") or "").upper()
            is_long = "LONG" in direction or direction == "BUY"
            color = "#26a69a" if is_long else "#ef5350"  # TV teal/red

            if entry_price:
                mcp.call("draw_shape", {
                    "shape": "horizontal_line",
                    "price": float(entry_price),
                    "color": color,
                    "text": f"E{i+1}",
                })
            if exit_price:
                pnl = trade.get("profit") or trade.get("pnl") or 0
                exit_color = "#26a69a" if float(pnl) >= 0 else "#ef5350"
                mcp.call("draw_shape", {
                    "shape": "horizontal_line",
                    "price": float(exit_price),
                    "color": exit_color,
                    "text": f"X{i+1}",
                })

        # Capture annotated chart
        ann_result = mcp.call("capture_screenshot", {
            "region": "chart",
            "filename": f"tg_trades_{claim.id}",
        })
        ann_path = ann_result.get("file_path") if ann_result.get("success") else None
        if ann_path and Path(ann_path).exists():
            sb = next((v.strategy_backtest for v in finding.validations if v.strategy_backtest), None)
            caption = _trade_overlay_caption(claim, sb, len(trades))
            await _send_photo(msg, ann_path, caption)

        # Clear drawings after capture
        mcp.call("draw_clear", {})

        # Send detailed trade table as text
        table = _trade_table_text(trades, claim)
        if table:
            try:
                await msg.reply_text(table, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                pass

        # Also send strategy tester panel
        st_result = mcp.call("capture_screenshot", {
            "region": "strategy_tester",
            "filename": f"tg_strat_{claim.id}",
        })
        strat_path = st_result.get("file_path") if st_result.get("success") else None
        if strat_path and Path(strat_path).exists():
            await _send_photo(msg, strat_path, "Strategy Tester — full metrics")

    except Exception:  # noqa: BLE001
        log.warning("trade overlay step failed", exc_info=True)


def _trade_overlay_caption(claim, sb, n_trades: int) -> str:
    sym = claim.instrument or "?"
    tf = claim.timeframe or "?"
    lines = [f"Trade Paths | {sym} | {tf}", f"{n_trades} trades annotated"]
    if sb:
        pnl_str = f"{sb.net_profit:+,.2f}" if sb.net_profit is not None else "n/a"
        lines.append(f"Net P&L: {pnl_str}  |  Win rate: {sb.win_rate:.0%}  |  PF: {sb.profit_factor:.2f}")
    return "\n".join(lines)


def _trade_table_text(trades: list, claim) -> str:
    """Format trades as a compact text table for Telegram."""
    if not trades:
        return ""
    sym = claim.instrument or "?"
    tf = claim.timeframe or "?"
    lines = [f"Trade Log | {sym} | {tf}", ""]
    lines.append(f"{'#':<3} {'Dir':<5} {'Entry':>10} {'Exit':>10} {'P&L':>10}")
    lines.append("-" * 42)
    wins = losses = 0
    total_pnl = 0.0
    for i, t in enumerate(trades[:30], 1):
        direction = (t.get("type") or t.get("direction") or "?").upper()
        side = "LONG" if "LONG" in direction or direction == "BUY" else "SHORT"
        entry = t.get("entry_price") or t.get("entryPrice") or 0
        exit_p = t.get("exit_price") or t.get("exitPrice") or 0
        pnl = float(t.get("profit") or t.get("pnl") or 0)
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
            pnl_str = f"+{pnl:,.1f}"
        else:
            losses += 1
            pnl_str = f"{pnl:,.1f}"
        lines.append(f"{i:<3} {side:<5} {float(entry):>10,.1f} {float(exit_p):>10,.1f} {pnl_str:>10}")
    if len(trades) > 30:
        lines.append(f"... (+{len(trades)-30} more trades)")
    lines.append("-" * 42)
    pnl_total_str = f"+{total_pnl:,.1f}" if total_pnl >= 0 else f"{total_pnl:,.1f}"
    lines.append(f"{'TOT':<3} {'':5} {'':>10} {pnl_total_str:>10} ({wins}W/{losses}L)")
    return "\n".join(lines)


async def _send_photo(msg, file_path: str, caption: str) -> None:
    try:
        with open(file_path, "rb") as fh:
            await msg.reply_photo(photo=fh, caption=caption)
    except Exception:  # noqa: BLE001
        log.warning("failed to send photo %s", file_path)


async def _send_pine_files(msg, report) -> None:
    """Send any synthesized .pine files as Telegram document attachments."""
    seen: set[str] = set()
    for finding in report.findings:
        for vr in finding.validations:
            path = vr.pine_script_path
            if not path or path in seen:
                continue
            seen.add(path)
            p = Path(path)
            if not p.exists():
                continue
            try:
                with open(p, "rb") as fh:
                    await msg.reply_document(
                        document=fh,
                        filename=p.name,
                        caption="Pine Script strategy — drop into TradingView Pine editor",
                    )
            except Exception:  # noqa: BLE001
                log.warning("failed to send .pine file attachment %s", p.name)


async def _reply_chunked(msg, text: str, limit: int = 3800) -> None:
    if len(text) <= limit:
        try:
            await msg.reply_text(text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.warning("failed to send reply (user may have blocked the bot)")
        return
    # split on blank lines so we don't cut mid-paragraph
    parts: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) + 2 > limit:
            if buf:
                parts.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        parts.append(buf)
    for i, part in enumerate(parts):
        try:
            await msg.reply_text(
                part + (f"\n\n({i+1}/{len(parts)})" if len(parts) > 1 else ""),
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to send reply chunk %d/%d", i + 1, len(parts))
            break


def main() -> int:
    config = load_config()
    app = _build_app(config)
    log.info(
        "agent-research-lab telegram bot starting%s",
        f" (allowlist: {config.telegram_allowlist})" if config.telegram_allowlist else " (open — no allowlist set)",
    )
    app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
