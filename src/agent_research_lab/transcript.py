"""YouTube transcript fetch + clean.

Returns a Transcript. If the video has no captions / the transcript is too short,
returns a Transcript with text="" — the orchestrator short-circuits to a minimal
report in that case (see docs/failure_handling.md). This module does not raise on
"no transcript"; that's a normal outcome, not an error.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .types import Transcript

# youtube-transcript-api is the only hard dependency here. Supports the 1.x API
# (instance-based: YouTubeTranscriptApi().fetch / .list).
try:  # pragma: no cover - import guard
    from youtube_transcript_api import (
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
    )

    try:
        from youtube_transcript_api import VideoUnavailable  # 1.x re-exports it
    except ImportError:  # pragma: no cover
        from youtube_transcript_api._errors import VideoUnavailable

    _HAVE_API = True
except Exception:  # pragma: no cover
    _HAVE_API = False


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video id from any common YouTube URL shape:
    watch?v=, youtu.be/, shorts/, embed/. Returns None if it doesn't look like one.
    """
    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS and not host.endswith(".youtube.com"):
        return None

    # youtu.be/<id>
    if host in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if _looks_like_id(vid) else None

    # youtube.com/watch?v=<id>
    qs = parse_qs(parsed.query)
    if "v" in qs and qs["v"]:
        vid = qs["v"][0]
        return vid if _looks_like_id(vid) else None

    # youtube.com/shorts/<id> , /embed/<id> , /live/<id>
    m = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", parsed.path)
    if m:
        return m.group(1)

    return None


def _looks_like_id(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", s or ""))


def fetch(url: str) -> Transcript:
    """Fetch and lightly clean the transcript for `url`.

    Always returns a Transcript. On any "no transcript" condition, returns one with
    text="" (Transcript.is_empty will be True). Raises only on programmer error
    (e.g. youtube-transcript-api not installed) — and even that is degraded to an
    empty transcript so a run never hard-crashes here.
    """
    video_id = extract_video_id(url)
    if not video_id:
        # The caller (telegram_bot / orchestrate) should have validated the URL,
        # but be defensive: an unparseable URL -> empty transcript with a note.
        return Transcript(
            video_id="",
            url=url,
            title=None,
            channel=None,
            text="",
        )

    if not _HAVE_API:  # pragma: no cover - environment issue
        return Transcript(
            video_id=video_id,
            url=url,
            title=None,
            channel=None,
            text="",
        )

    api = YouTubeTranscriptApi()
    langs = ["en", "en-US", "en-GB"]
    fetched = None
    try:
        # Prefer manually-created English; fall back to auto-generated; then any
        # transcript, translated to English if possible.
        listing = api.list(video_id)
        transcript_obj = None
        try:
            transcript_obj = listing.find_manually_created_transcript(langs)
        except Exception:
            try:
                transcript_obj = listing.find_generated_transcript(langs)
            except Exception:
                for t in listing:
                    transcript_obj = t.translate("en") if getattr(t, "is_translatable", False) else t
                    break
        if transcript_obj is None:
            return _empty(video_id, url)
        fetched = transcript_obj.fetch()
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return _empty(video_id, url)
    except Exception:
        # `.list` can fail on some videos / network conditions — try the simpler
        # `.fetch` path before giving up.
        try:
            fetched = api.fetch(video_id, languages=langs)
        except Exception:
            return _empty(video_id, url)

    text = _clean(" ".join(_snippet_text(s) for s in fetched))
    return Transcript(
        video_id=video_id,
        url=url,
        title=None,  # the transcript API doesn't give us title/channel; left None on purpose in v1
        channel=None,
        text=text,
    )


def _snippet_text(snippet) -> str:
    # 1.x yields FetchedTranscriptSnippet objects with .text; older dict-shaped
    # chunks have ["text"]. Be tolerant of both.
    if hasattr(snippet, "text"):
        return snippet.text or ""
    if isinstance(snippet, dict):
        return snippet.get("text", "") or ""
    return str(snippet)


def _empty(video_id: str, url: str) -> Transcript:
    return Transcript(video_id=video_id, url=url, title=None, channel=None, text="")


def _clean(text: str) -> str:
    # Collapse whitespace, drop the "[Music]" / "[Applause]" bracket noise, trim.
    text = re.sub(r"\[(?:music|applause|laughter|inaudible)\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
