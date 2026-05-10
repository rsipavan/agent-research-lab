"""The Telegram edge: listens for YouTube URLs, runs the pipeline, replies with the report.

The only stateful, long-running component. Everything else is pure-ish functions over
data — you can run the whole pipeline from the CLI (orchestrate.main) with no Telegram
involved, which is how examples/ were built.

Run: python -m agent_research_lab.telegram_bot
Needs TELEGRAM_BOT_TOKEN in .env. Optionally TELEGRAM_ALLOWLIST (comma-separated user IDs).

See docs/failure_handling.md for the input-validation and send-failure behavior.
"""

from __future__ import annotations

import logging
import re

from .config import Config, load_config
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
    "send back a structured report with verdicts and caveats.\n\n"
    "Most trading videos contain no checkable claims — I'll tell you that plainly when that's the case."
)


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

        await msg.reply_text("on it — fetching transcript, extracting claims, validating… (this takes a minute)")
        try:
            report = process(url, config)
        except RunAborted as e:
            await msg.reply_text(e.user_message)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed for %s", url)
            await msg.reply_text(f"something went wrong analyzing that video — try again. ({e})")
            return

        # Telegram message limit is ~4096 chars; chunk if needed.
        await _reply_chunked(msg, report.markdown)
        if report.run_id:
            log.info("done: %s -> %s (trace traces/%s.jsonl)", url, report.verdict_overall, report.run_id)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


async def _reply_chunked(msg, text: str, limit: int = 3800) -> None:
    if len(text) <= limit:
        try:
            await msg.reply_text(text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001 - user blocked the bot, etc. — don't crash the listener
            log.warning("failed to send reply (user may have blocked the bot)")
        return
    # split on blank lines so we don't cut mid-table
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
            await msg.reply_text(part + (f"\n\n_({i+1}/{len(parts)})_" if len(parts) > 1 else ""),
                                 disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.warning("failed to send reply chunk %d/%d", i + 1, len(parts))
            break


def main() -> int:
    config = load_config()
    app = _build_app(config)
    log.info("agent-research-lab telegram bot starting%s",
             f" (allowlist: {config.telegram_allowlist})" if config.telegram_allowlist else " (open — no allowlist set)")
    app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
