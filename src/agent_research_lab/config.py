"""Loads config.yml and .env. One place that reads configuration; everything else
takes plain values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config.yml"

load_dotenv(_REPO_ROOT / ".env")


@dataclass
class Config:
    # secrets / env
    telegram_bot_token: str
    anthropic_api_key: str
    anthropic_model: str
    telegram_allowlist: list[int]
    tradingview_mcp_url: str | None    # HTTP/SSE endpoint
    tradingview_mcp_cmd: str | None    # stdio: command that launches the MCP server

    # config.yml
    test_types: dict[str, bool]
    default_timeframe: str
    test_timeframes: list[str]  # claim is validated on each — multi-tf evidence beats single-tf cherry-pick
    default_lookback_days: int
    symbol_fallback: str | None
    max_claims_per_video: int
    min_confidence: float
    mcp_retries: int
    mcp_timeout_seconds: int
    tracing_enabled: bool
    tracing_dir: str
    runs_enabled: bool
    runs_dir: str

    def test_type_enabled(self, test_type: str) -> bool:
        return bool(self.test_types.get(test_type, False))


def load_config(config_path: Path | None = None) -> Config:
    path = config_path or _CONFIG_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}

    allowlist_raw = os.getenv("TELEGRAM_ALLOWLIST", "").strip()
    allowlist = [int(x) for x in allowlist_raw.split(",") if x.strip()] if allowlist_raw else []

    defaults = raw.get("defaults", {})
    extraction = raw.get("extraction", {})
    validation = raw.get("validation", {})
    tracing = raw.get("tracing", {})
    outputs = raw.get("outputs", {})

    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        telegram_allowlist=allowlist,
        tradingview_mcp_url=os.getenv("TRADINGVIEW_MCP_URL") or None,
        tradingview_mcp_cmd=os.getenv("TRADINGVIEW_MCP_CMD") or None,
        test_types=raw.get("test_types", {
            "indicator_value_over_range": True,
            "level_zone_hit_rate": True,
            "strategy_backtest": False,
        }),
        default_timeframe=defaults.get("timeframe", "1D"),
        test_timeframes=list(defaults.get("test_timeframes", ["1D", "4H", "1H"])),
        default_lookback_days=int(defaults.get("lookback_days", 365)),
        symbol_fallback=defaults.get("symbol_fallback") or None,
        max_claims_per_video=int(extraction.get("max_claims_per_video", 3)),
        min_confidence=float(extraction.get("min_confidence", 0.5)),
        mcp_retries=int(validation.get("mcp_retries", 1)),
        mcp_timeout_seconds=int(validation.get("mcp_timeout_seconds", 60)),
        tracing_enabled=bool(tracing.get("enabled", True)),
        tracing_dir=tracing.get("dir", "traces"),
        runs_enabled=bool(outputs.get("save_runs", True)),
        runs_dir=outputs.get("runs_dir", "runs"),
    )


def repo_root() -> Path:
    return _REPO_ROOT
