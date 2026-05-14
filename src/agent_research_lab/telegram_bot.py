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


async def _send_claim_charts(msg, report, config: Config) -> None:
    """For each validated claim: set TV to the right symbol/timeframe, screenshot, send as photo.
    Strategy backtest claims also get a strategy-tester screenshot.
    """
    claims_to_chart = [
        f for f in report.findings
        if f.claim.testable != "no" and f.validations and f.claim.instrument
    ]
    if not claims_to_chart:
        return

    _VERDICT_LABEL = {
        "holds": "CONFIRMED",
        "partial": "PARTIAL SUPPORT",
        "fails": "NOT SUPPORTED",
        "untestable": "UNTESTABLE",
    }

    try:
        with McpClient(config) as mcp:
            for finding in claims_to_chart:
                claim = finding.claim
                sym = claim.instrument
                tf = claim.timeframe or "D"

                # Navigate chart to the right symbol and timeframe
                mcp.call("chart_set_symbol", {"symbol": sym})
                mcp.call("chart_set_timeframe", {"timeframe": tf})

                # Capture chart region
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

                # For strategy backtests: also send the strategy tester panel
                if claim.test_type == "strategy_backtest":
                    result2 = mcp.call("capture_screenshot", {
                        "region": "strategy_tester",
                        "filename": f"tg_strat_{claim.id}",
                    })
                    strat_path = result2.get("file_path") if result2.get("success") else None
                    if strat_path and Path(strat_path).exists():
                        await _send_photo(msg, strat_path, "Strategy Tester results")

    except Exception:  # noqa: BLE001
        log.warning("chart screenshot step failed — skipping", exc_info=True)


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
