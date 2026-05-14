#!/usr/bin/env python3
"""
Direct integration test for the Pine Script synthesis path.

Uses an Opening Range Breakout (ORB) strategy as the test case because it is:
  - Simple enough that the LLM can synthesize clean Pine v5 code in one pass
  - Complete enough (entry, stop, target, time exit) to produce trades in the
    TradingView strategy tester
  - Well-known, so the output is easy to sanity-check

Bypasses the YouTube transcript fetch — constructs a synthetic Claim + Transcript
that describes the ORB rules verbally, then calls pine.run() directly.

Usage:
    python scripts/test_orb.py [--symbol SYMBOL] [--timeframe TF]

Defaults:  --symbol SPY  --timeframe 5
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the src package importable when run from the repo root or scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_research_lab import pine as pine_mod
from agent_research_lab.config import load_config
from agent_research_lab.mcp_client import McpClient
from agent_research_lab.types import Claim, Transcript


# ---------------------------------------------------------------------------
# synthetic ORB strategy transcript
# ---------------------------------------------------------------------------

_ORB_TRANSCRIPT = """\
Today I'm teaching you the Opening Range Breakout (ORB) — one of the most
consistent intraday setups you can trade on any liquid instrument.

THE SETUP
The opening range is the high and low formed during the first 15 minutes after
market open. For US markets that window runs from 09:30 to 09:45 ET.
After the window closes, price often makes a strong directional move as
institutional orders hit the market.

ENTRY RULES
Long setup:
  - Wait for the first 15-minute candle to close.
  - If the next candle closes ABOVE the opening range high with above-average
    volume, go long at the open of the following candle.
Short setup:
  - If the next candle closes BELOW the opening range low with above-average
    volume, go short at the open of the following candle.

EXIT RULES
Stop loss:
  - Longs: stop at the opening range low.
  - Shorts: stop at the opening range high.
Take profit:
  - Target 1.5 times the opening range size measured from the breakout point.
  - Example: range is $2 wide, long entry at range high $102, target $103.
Time exit:
  - Close any open position by 15:00 ET (3 PM). Never hold overnight.

WHY IT WORKS
The first 15 minutes are dominated by overnight orders and news-driven activity.
Once that noise settles, the breakout direction usually reflects institutional
flow. The 1.5R target means you only need a 40% win rate to be profitable.

This strategy works best on SPY, QQQ, and the major index futures on a 5-minute chart.
"""

_ORB_CLAIM_STATEMENT = (
    "Opening Range Breakout (ORB): enter long when price closes above the first-15-minute "
    "high, enter short when price closes below the first-15-minute low. "
    "Stop at the opposite end of the opening range. Target 1.5× range size. "
    "Exit all positions by 3 PM. "
    "Strategy works consistently on SPY / liquid ETFs on a 5-minute chart."
)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _parse_args() -> tuple[str, str]:
    symbol, timeframe = "SPY", "5"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--symbol" and i + 1 < len(args):
            symbol = args[i + 1]; i += 2
        elif args[i] == "--timeframe" and i + 1 < len(args):
            timeframe = args[i + 1]; i += 2
        else:
            i += 1
    return symbol, timeframe


def main() -> int:
    symbol, timeframe = _parse_args()

    claim = Claim(
        id="orb-c1",
        statement=_ORB_CLAIM_STATEMENT,
        instrument=symbol,
        timeframe=timeframe,
        test_type="strategy_backtest",
        testable="yes",
        reason_if_not=None,
        confidence=0.95,
    )
    transcript = Transcript(
        video_id="orb-test",
        url="synthetic://orb-strategy-test",
        title="Opening Range Breakout Strategy",
        channel="agent-research-lab test",
        text=_ORB_TRANSCRIPT,
    )

    config = load_config()
    out_dir = Path("runs/orb-test")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ORB] instrument={symbol}  timeframe={timeframe}")
    print(f"[ORB] output -> {out_dir.resolve()}")
    print()

    with McpClient(config) as mcp:
        vr = pine_mod.run(claim, transcript, None, config, mcp, out_dir=out_dir)

    # ---- report ----
    print("=" * 70)
    print(f"status         : {vr.status}")
    print(f"result         : {vr.result}")
    if vr.data_summary:
        print(f"data_summary   : {vr.data_summary}")
    if vr.pine_script_path:
        print(f"pine file      : {vr.pine_script_path}")
    if vr.strategy_backtest:
        sb = vr.strategy_backtest
        win_pct = f"{sb.win_rate:.1%}"
        print()
        print("  ┌─ Strategy Backtest Results ──────────────────────────┐")
        print(f"  │  total trades  : {sb.total_trades:<6}                         │")
        print(f"  │  winners       : {sb.winning_trades:<6}  ({win_pct})              │")
        print(f"  │  net profit    : {sb.net_profit:+.2f}                        │")
        print(f"  │  max drawdown  : {sb.max_drawdown:.2f}                         │")
        print(f"  │  profit factor : {sb.profit_factor:.2f}                          │")
        print("  └──────────────────────────────────────────────────────┘")
    if vr.caveats:
        print()
        print("caveats:")
        for c in vr.caveats:
            print(f"  • {c}")
    print("=" * 70)

    if vr.pine_script_path:
        p = Path(vr.pine_script_path)
        if p.exists():
            print(f"\n[Pine Script — {p.name}]")
            print("-" * 60)
            print(p.read_text(encoding="utf-8"))
            print("-" * 60)

    return 0 if vr.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
