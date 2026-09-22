"""Fetch current DNSE financial ratios used by the FA scoring engine."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

import vn_stock_scraper_complete as scraper


FIELD_MAP = {
    "marketShare": "market_share",
    "totalAssets": "total_assets",
    "eps": "eps_ttm",
    "pe": "pe",
    "ps": "ps",
    "pb": "pb",
    "beta": "beta",
    "profitGrowth": "profit_growth_qoq",
    "roe": "roe_ttm",
    "roa": "roa_ttm",
    "grossMargin": "gross_margin_ttm",
    "debtEquityRatio": "debt_equity_ratio",
    "inventoryGrowth": "inventory_growth_qoq",
    "freeFloatRatio": "free_float_ratio",
    "dividendYield": "dividend_yield",
    "bookValue": "book_value_per_share",
    "revenue": "revenue_ttm",
    "profit": "net_income_ttm",
    "capitalization": "market_cap",
}


def scalar_value(value: Any) -> Any:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str) and value.strip().endswith("%"):
        try:
            return float(value.strip().rstrip("%")) / 100.0
        except ValueError:
            return None
    return scraper.safe_float(value)


def fetch_one(symbol: str) -> dict[str, Any]:
    raw = scraper.http_get_json(
        "https://api-bo.dnse.com.vn/senses-api/v5/financial-info/financial-index",
        headers=scraper.get_dnse_auth_headers(),
        params={"symbol": symbol},
    )
    payload = raw if isinstance(raw, dict) else {}
    indexes = payload.get("indexes", {}) if isinstance(payload.get("indexes"), dict) else {}
    row: dict[str, Any] = {
        "ticker": symbol,
        "as_of_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "company_type": payload.get("companyType"),
        "status": "OK" if indexes else "EMPTY",
        "error": None,
    }
    for source, target in FIELD_MAP.items():
        row[target] = scalar_value(indexes.get(source))
    return row


def refresh(symbols: Iterable[str], output_path: Path, workers: int = 8) -> dict[str, Any]:
    symbols = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
    if not symbols:
        # Preserve the last good snapshot; an empty selection is not a refresh.
        return {
            "requested": 0,
            "ok": 0,
            "failed": 0,
            "empty": 0,
            "skipped": "no symbols selected",
            "output": str(output_path.resolve()),
        }
    if not scraper.DNSE_API_KEY or not scraper.DNSE_API_SECRET:
        raise RuntimeError("DNSE_API_KEY and DNSE_API_SECRET are required for financial snapshots")
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_one, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append(
                    {
                        "ticker": symbol,
                        "as_of_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                        "status": "FAIL",
                        "error": str(exc),
                    }
                )
    frame = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    os.replace(temp, output_path)
    return {
        "requested": len(symbols),
        "ok": int(frame["status"].eq("OK").sum()),
        "failed": int(frame["status"].eq("FAIL").sum()),
        "empty": int(frame["status"].eq("EMPTY").sum()),
        "output": str(output_path.resolve()),
    }
