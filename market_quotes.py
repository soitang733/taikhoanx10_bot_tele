"""Rate-conscious quote snapshots for the local BUY board."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
import time
from typing import Any, Callable

from stock_ai_reply import is_fresh_trade, latest_trade


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = Lock()
_CACHE_SECONDS = 75


def quote_snapshot(ticker: str, eod: dict[str, Any] | None = None,
                   fetch: Callable[[str], dict[str, Any]] = latest_trade) -> dict[str, Any]:
    """Only label a DNSE trade live when its timestamp is verifiably fresh."""
    ticker = ticker.upper()
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(ticker)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    result: dict[str, Any] = {"ticker": ticker, "price_vnd": None, "source": "UNAVAILABLE",
                              "trade_time": None, "fresh": False,
                              "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        trade = fetch(ticker)
        raw = float(trade.get("matchPrice"))
        if raw > 0 and is_fresh_trade(trade):
            result.update(price_vnd=raw * 1000, source="DNSE_REALTIME",
                          trade_time=trade.get("time"), fresh=True)
    except Exception:
        pass
    if not result["fresh"] and eod:
        try:
            close = float(eod.get("close"))
            if close > 0:
                result.update(price_vnd=close, source="EOD_FALLBACK", eod_date=eod.get("date"))
        except (TypeError, ValueError):
            pass
    with _LOCK:
        _CACHE[ticker] = (now, result)
    return result


def quote_board(tickers: list[str], eod_by_ticker: dict[str, dict[str, Any]],
                fetch: Callable[[str], dict[str, Any]] = latest_trade) -> list[dict[str, Any]]:
    if len(tickers) > 40:
        raise ValueError("too many tickers")
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda ticker: quote_snapshot(ticker, eod_by_ticker.get(ticker), fetch), tickers))
