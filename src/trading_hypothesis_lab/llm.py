"""Backend-agnostic LLM completion.

One function — `complete(system, prompt) -> str`. Auto-detects what's available,
in this order:

  1. `claude` CLI on PATH (Claude Code) — runs `claude -p` in print mode using your
     existing subscription. No API key needed. This is the default.
  2. ANTHROPIC_API_KEY set — Anthropic Python SDK (`pip install trading-hypothesis-lab[anthropic]`).
  3. GEMINI_API_KEY set — google-generativeai, gemini-2.0-flash, whose free tier
     comfortably covers this workload (`pip install trading-hypothesis-lab[gemini]`).
  4. Nothing available — raise LlmUnavailable with a clear message.

Force a backend with the env var AGENT_RESEARCH_LAB_LLM in {claude_cli, anthropic, gemini}.

The point of this module: the repo runs on whatever AI you already have. No required
paid key. Adding another backend is one function here; the rest of the pipeline never
sees the difference.
"""

from __future__ import annotations

import os
import shutil
import subprocess

_DEFAULT_TIMEOUT = 180  # seconds — extraction over a long transcript can take a bit


class LlmUnavailable(RuntimeError):
    """No usable LLM backend was found / configured."""


class LlmError(RuntimeError):
    """A backend was found but the call failed."""


def available_backend() -> str | None:
    """Return the backend that would be used, or None if none is available.
    Honors the AGENT_RESEARCH_LAB_LLM override."""
    forced = os.getenv("AGENT_RESEARCH_LAB_LLM", "").strip().lower()
    if forced == "claude_cli":
        return "claude_cli" if _claude_cli_path() else None
    if forced == "anthropic":
        return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else None
    if forced == "gemini":
        return "gemini" if os.getenv("GEMINI_API_KEY") else None
    # auto-detect
    if _claude_cli_path():
        return "claude_cli"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return None


def complete(system: str, prompt: str, *, model: str | None = None, max_tokens: int = 2000,
             timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Run a single completion. Returns the model's text output (stripped).

    Raises LlmUnavailable if no backend is configured, LlmError if the call fails.
    """
    backend = available_backend()
    if backend is None:
        raise LlmUnavailable(
            "no LLM backend available. Either:\n"
            "  - install the Claude Code CLI (`claude` on PATH) — no API key needed, or\n"
            "  - set ANTHROPIC_API_KEY (pip install 'trading-hypothesis-lab[anthropic]'), or\n"
            "  - set GEMINI_API_KEY (pip install 'trading-hypothesis-lab[gemini]')"
        )
    if backend == "claude_cli":
        return _complete_claude_cli(system, prompt, model=model, timeout=timeout)
    if backend == "anthropic":
        return _complete_anthropic(system, prompt, model=model, max_tokens=max_tokens)
    if backend == "gemini":
        return _complete_gemini(system, prompt, max_tokens=max_tokens)
    raise LlmUnavailable(f"unknown backend '{backend}'")  # pragma: no cover


# ---------------------------------------------------------------------------
# backend: claude CLI (default — uses the user's Claude Code subscription, $0)
# ---------------------------------------------------------------------------


def _claude_cli_path() -> str | None:
    return shutil.which("claude")


def _complete_claude_cli(system: str, prompt: str, *, model: str | None, timeout: int) -> str:
    claude = _claude_cli_path()
    if not claude:  # pragma: no cover - guarded by available_backend
        raise LlmUnavailable("claude CLI not on PATH")
    # `claude -p` (print mode) reads the prompt from stdin when no positional prompt
    # is given — feed it there to avoid OS arg-length limits on long transcripts.
    full = f"{system}\n\n---\n\n{prompt}" if system else prompt
    cmd = [claude, "-p"]
    if model:
        cmd += ["--model", model]
    # Run the CLI with its own auth (Claude Code subscription). If ANTHROPIC_API_KEY /
    # ANTHROPIC_AUTH_TOKEN are set in our environment, the CLI would try to use them —
    # which fails if they aren't valid raw API keys. We chose the CLI backend on
    # purpose; strip those so it falls back to subscription auth. (If you want the CLI
    # to use an API key, use AGENT_RESEARCH_LAB_LLM=anthropic instead.)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        result = subprocess.run(
            cmd,
            input=full,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as e:  # pragma: no cover
        raise LlmUnavailable(f"claude CLI not runnable: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise LlmError(f"claude CLI timed out after {timeout}s") from e
    out = (result.stdout or "").strip()
    # The CLI sometimes exits 0 but prints an auth/config error to stdout instead of a
    # real answer — catch the obvious ones.
    if out.lower().startswith(("invalid api key", "error:", "authentication")) or "fix external api key" in out.lower():
        raise LlmError(f"claude CLI error: {out[:200]}")
    if result.returncode != 0:
        raise LlmError(f"claude CLI exited {result.returncode}: {(result.stderr or out).strip()[:400]}")
    if not out:
        raise LlmError("claude CLI returned empty output")
    return out


# ---------------------------------------------------------------------------
# backend: Anthropic API
# ---------------------------------------------------------------------------


def _complete_anthropic(system: str, prompt: str, *, model: str | None, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LlmUnavailable("anthropic SDK not installed — pip install 'trading-hypothesis-lab[anthropic]'") from e
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:  # pragma: no cover - guarded
        raise LlmUnavailable("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=max_tokens,
            system=system or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        raise LlmError(f"Anthropic API call failed: {e}") from e
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise LlmError("Anthropic API returned no text")
    return text


# ---------------------------------------------------------------------------
# backend: Gemini API (free tier covers this workload)
# ---------------------------------------------------------------------------


def _complete_gemini(system: str, prompt: str, *, max_tokens: int) -> str:
    try:
        import google.generativeai as genai
    except ImportError as e:  # pragma: no cover
        raise LlmUnavailable("google-generativeai not installed — pip install 'trading-hypothesis-lab[gemini]'") from e
    key = os.getenv("GEMINI_API_KEY")
    if not key:  # pragma: no cover - guarded
        raise LlmUnavailable("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    try:
        model = genai.GenerativeModel(model_name, system_instruction=system or None)
        resp = model.generate_content(prompt, generation_config={"max_output_tokens": max_tokens})
    except Exception as e:  # noqa: BLE001
        raise LlmError(f"Gemini API call failed: {e}") from e
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise LlmError("Gemini API returned no text")
    return text
