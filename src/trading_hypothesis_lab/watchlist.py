"""Predefined symbol lists and a multi-symbol claim runner.

Usage
-----
From the CLI (see orchestrate.py --watchlist):

    python -m trading_hypothesis_lab.orchestrate <youtube-url> --watchlist nifty50

Programmatically:

    from trading_hypothesis_lab.watchlist import get_watchlist, run_watchlist
    symbols = get_watchlist("nifty50")
    results = run_watchlist(claim, symbols, config, mcp)
    # results: {symbol: list[ValidationRun]}

Market-cap filtering is not available in v1 (TradingView MCP does not expose
constituent market caps in real time). Use predefined lists instead.
"""

from __future__ import annotations

from .config import Config
from .mcp_client import McpClient
from .types import Claim, ValidationRun
from . import validate as validate_mod

# ---------------------------------------------------------------------------
# Predefined watchlists
# ---------------------------------------------------------------------------

NIFTY_50: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK",
    "SBIN", "LT", "INFY", "KOTAKBANK", "BAJFINANCE",
    "HINDUNILVR", "MARUTI", "NTPC", "ITC", "TITAN",
    "AXISBANK", "ADANIENT", "BAJAJ-AUTO", "SUNPHARMA", "TATAMOTORS",
    "M&M", "WIPRO", "ADANIPORTS", "ULTRACEMCO", "NESTLEIND",
    "TATASTEEL", "HCLTECH", "POWERGRID", "ASIANPAINT", "COALINDIA",
    "BEL", "JSWSTEEL", "CIPLA", "HEROMOTOCO", "TRENT",
    "SHRIRAMFIN", "DRREDDY", "HINDALCO", "ONGC", "SBILIFE",
    "EICHERMOT", "TATACONSUM", "BRITANNIA", "BAJAJFINSV", "ZOMATO",
    "LTIM", "HDFCLIFE", "GRASIM", "APOLLOHOSP", "BPCL",
]

SP500_LARGE_CAP: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AVGO", "BRK.B", "JPM",
    "UNH", "V", "XOM", "MA", "JNJ",
    "HD", "PG", "COST", "MRK", "ABBV",
    "CVX", "BAC", "LLY", "KO", "NFLX",
    "AMD", "TMO", "ORCL", "CRM", "CSCO",
    "WMT", "DIS", "MCD", "ACN", "ADBE",
    "PEP", "ABT", "CAT", "GS", "MS",
    "AMGN", "GE", "AXP", "VZ", "TXN",
    "INTU", "RTX", "SPGI", "BLK", "ISRG",
]

CRYPTO_MAJOR: list[str] = [
    "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD",
    "ADAUSD", "AVAXUSD", "DOTUSD", "MATICUSD", "LINKUSD",
]

FOREX_MAJOR: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
    "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
]

COMMODITIES: list[str] = [
    "XAUUSD", "XAGUSD", "USOIL", "UKOIL", "NGAS",
    "COPPER", "PLATINUM", "PALLADIUM",
]

# Default cross-market watchlist — the essential instruments that appear in the
# vast majority of trading YouTube content regardless of asset class. Used when
# --watchlist default is passed and as the fallback for broad claim scans.
DEFAULT: list[str] = [
    # Major indices
    "SPX",       # S&P 500
    "NDX",       # NASDAQ 100
    "DJI",       # Dow Jones
    "DAX",       # Germany DAX 40
    "FTSE",      # UK FTSE 100
    "NI225",     # Japan Nikkei 225
    "HSI",       # Hong Kong Hang Seng
    "NIFTY",     # India Nifty 50
    # Crypto
    "BTCUSD",
    "ETHUSD",
    # Forex
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    # Commodities
    "XAUUSD",    # Gold
    "USOIL",     # WTI Crude
]

_LISTS: dict[str, list[str]] = {
    "default": DEFAULT,
    "nifty50": NIFTY_50,
    "sp500": SP500_LARGE_CAP,
    "crypto": CRYPTO_MAJOR,
    "forex": FOREX_MAJOR,
    "commodities": COMMODITIES,
}


def get_watchlist(name: str) -> list[str]:
    """Return a predefined symbol list by name (case-insensitive). Raises KeyError if unknown."""
    key = name.lower().replace(" ", "").replace("_", "").replace("-", "")
    matches = {k.replace("_", "").replace("-", ""): v for k, v in _LISTS.items()}
    if key not in matches:
        available = ", ".join(sorted(_LISTS))
        raise KeyError(f"unknown watchlist '{name}'. Available: {available}")
    return list(matches[key])


def available_watchlists() -> list[str]:
    return sorted(_LISTS)


# ---------------------------------------------------------------------------
# Multi-symbol runner
# ---------------------------------------------------------------------------


def run_watchlist(
    claim: Claim,
    symbols: list[str],
    config: Config,
    mcp: McpClient,
    *,
    max_symbols: int = 50,
) -> dict[str, list[ValidationRun]]:
    """Run `claim` against each symbol in `symbols`. Returns {symbol: [ValidationRun]}.

    Symbols that fail to resolve are recorded as an untestable ValidationRun under their
    name — they don't abort the rest of the batch.

    `max_symbols` caps the batch to avoid excessively long runs (default 50).
    """
    results: dict[str, list[ValidationRun]] = {}
    for sym in symbols[:max_symbols]:
        # Temporarily override the claim's instrument for this symbol
        patched = Claim(
            id=claim.id,
            statement=claim.statement,
            instrument=sym,
            timeframe=claim.timeframe,
            test_type=claim.test_type,
            testable=claim.testable,
            reason_if_not=claim.reason_if_not,
            confidence=claim.confidence,
            test_type_justification=claim.test_type_justification,
        )
        runs = validate_mod.run(patched, config, mcp)
        results[sym] = runs
    return results


def summarize_watchlist_results(results: dict[str, list[ValidationRun]]) -> dict:
    """Aggregate watchlist results into a summary dict."""
    total = len(results)
    ok_symbols = []
    fail_symbols = []
    untestable_symbols = []

    for sym, runs in results.items():
        ok_runs = [r for r in runs if r.status == "ok"]
        if not ok_runs:
            untestable_symbols.append(sym)
            continue
        rates = [r.hit_rate for r in ok_runs if r.hit_rate is not None]
        if not rates:
            untestable_symbols.append(sym)
            continue
        avg_rate = sum(rates) / len(rates)
        if avg_rate >= 0.6:
            ok_symbols.append((sym, avg_rate))
        else:
            fail_symbols.append((sym, avg_rate))

    ok_symbols.sort(key=lambda x: -x[1])
    fail_symbols.sort(key=lambda x: x[1])

    return {
        "total_symbols": total,
        "holds_count": len(ok_symbols),
        "fails_count": len(fail_symbols),
        "untestable_count": len(untestable_symbols),
        "top_holds": [{"symbol": s, "hit_rate": round(r, 3)} for s, r in ok_symbols[:10]],
        "top_fails": [{"symbol": s, "hit_rate": round(r, 3)} for s, r in fail_symbols[:10]],
        "untestable": untestable_symbols,
    }
