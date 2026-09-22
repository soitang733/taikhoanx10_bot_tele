"""Read-only current-session stock candle from DNSE OpenAPI."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from typing import Any, Callable
from zoneinfo import ZoneInfo


VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _session_date(value: Any) -> date | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc).astimezone(VIETNAM).date()
        if isinstance(value, str) and value.isdigit():
            return _session_date(int(value))
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=VIETNAM)
        return stamp.astimezone(VIETNAM).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_daily_candle(body: Any, ticker: str, session_date: date) -> dict[str, Any] | None:
    """Accept DNSE's compact OHLC arrays or equivalent row objects; reject other days."""
    if isinstance(body, str):
        body = json.loads(body)
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        body = body["data"]
    rows: list[dict[str, Any]] = []
    if isinstance(body, dict) and isinstance(body.get("t"), list):
        keys = {name: body.get(name, []) for name in ("t", "o", "h", "l", "c", "v")}
        count = min(map(len, keys.values()))
        rows = [{name: keys[name][index] for name in keys} for index in range(count)]
    elif isinstance(body, list):
        rows = [row for row in body if isinstance(row, dict)]
    elif isinstance(body, dict):
        for key in ("bars", "candles", "items"):
            if isinstance(body.get(key), list):
                rows = [row for row in body[key] if isinstance(row, dict)]
                break
    for row in reversed(rows):
        row_date = _session_date(row.get("t", row.get("time", row.get("date"))))
        if row_date != session_date:
            continue
        values = [_number(row.get(short, row.get(long))) for short, long in
                  (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))]
        if any(value is None for value in values):
            continue
        open_, high, low, close, volume = values
        if min(open_, high, low, close) <= 0 or volume < 0:
            continue
        if high < max(open_, close, low) or low > min(open_, close, high):
            continue
        # DNSE stock OHLC prices are quoted in thousands of VND, like latest_trade.
        scale = 1000 if close < 1000 else 1
        return {
            "ticker": ticker, "date": session_date.isoformat(),
            "open": round(open_ * scale, 2), "high": round(high * scale, 2),
            "low": round(low * scale, 2), "close": round(close * scale, 2),
            "volume": volume, "source": "DNSE_SESSION_OHLC",
            "provisional": True,
        }
    return None


def fetch_today_candle(ticker: str, *, now: datetime | None = None,
                       client_factory: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    ticker = ticker.strip().upper()
    if not ticker.isalnum() or len(ticker) > 12:
        raise ValueError("invalid ticker")
    observed = (now or datetime.now(timezone.utc)).astimezone(VIETNAM)
    start = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    key = os.getenv("DNSE_API_KEY", "").strip()
    secret = os.getenv("DNSE_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("DNSE credentials unavailable")
    if client_factory is None:
        from dnse import DNSEClient
        client_factory = DNSEClient
    client = client_factory(api_key=key, api_secret=secret,
                            base_url="https://openapi.dnse.com.vn",
                            api_version="2026-05-07")
    status, body = client.get_ohlc(
        bar_type="STOCK",
        query={"symbol": ticker, "resolution": "1D",
               "from": int(start.timestamp()),
               "to": int((start + timedelta(days=1)).timestamp())},
        dry_run=False,
    )
    if status != 200:
        raise RuntimeError(f"DNSE OHLC HTTP {status}")
    candle = parse_daily_candle(body, ticker, start.date())
    if candle:
        candle["checked_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return candle
