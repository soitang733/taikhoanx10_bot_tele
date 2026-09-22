"""Restore missing raw files from the canonical SQLite baseline.

Recovery only: existing nonempty raw files are never overwritten.  This makes a
complete raw mirror available before the next rebuild, without losing newer
provider responses that may already be present in ``scraper_output``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


TA_COLUMNS = [
    "ticker", "date", "open", "high", "low", "close", "adjusted_open", "adjusted_high",
    "adjusted_low", "adjusted_close", "volume", "trading_value", "trading_value_is_estimated",
    "adjustment_status", "adjustment_source", "source", "fetched_at", "data_version", "row_checksum",
]
FA_COLUMNS = ["ticker", "statement", "report_period", "period_type", "published_date", "item_code", "item_name", "value", "source"]
ACTION_COLUMNS = ["ticker", "ex_date", "action_type", "cash_dividend", "stock_ratio", "split_ratio", "rights_ratio", "rights_price", "source"]
COVERAGE_COLUMNS = ["variable", "available", "source", "periods_available", "notes"]


def write_csv_if_missing(path: Path, data: pd.DataFrame, columns: list[str]) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    data.reindex(columns=columns).to_csv(path, index=False, encoding="utf-8-sig")
    return True


def write_json_if_missing(path: Path, payload: dict[str, Any]) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return True


def query(connection: sqlite3.Connection, statement: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    return pd.read_sql_query(statement, connection, params=params)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing scraper-output files from the current SQLite baseline")
    parser.add_argument("--database", type=Path, default=Path("analysis_data/stocks_analysis.sqlite"))
    parser.add_argument("--raw-dir", type=Path, default=Path("scraper_output"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve()
    raw_dir = args.raw_dir.resolve()
    if not database.exists():
        raise SystemExit(f"Database not found: {database}")

    created = {name: 0 for name in ("metadata", "ta", "annual", "quarterly", "actions", "coverage", "diagnostics")}
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        companies = query(connection, "SELECT * FROM companies ORDER BY ticker")
        if companies.empty:
            raise SystemExit("The baseline database has no companies")
        if not args.dry_run:
            raw_dir.mkdir(parents=True, exist_ok=True)
            companies.to_csv(raw_dir / "summary_metadata.csv", index=False, encoding="utf-8-sig")

        for company in companies.to_dict(orient="records"):
            ticker = str(company["ticker"]).upper()
            ticker_dir = raw_dir / ticker
            prices = query(
                connection,
                "SELECT ticker,date,open,high,low,close,adjusted_open,adjusted_high,adjusted_low,adjusted_close,"
                "volume,trading_value,trading_value_is_estimated,adjustment_status,adjustment_source,"
                "COALESCE(source, 'sqlite_baseline') source,fetched_at,data_version,row_checksum "
                "FROM price_daily WHERE ticker=? ORDER BY date",
                (ticker,),
            )
            annual = query(connection, "SELECT ticker,statement,report_period,period_type,published_date,item_code,item_name,value,source FROM financial_annual WHERE ticker=?", (ticker,))
            quarterly = query(connection, "SELECT ticker,statement,report_period,period_type,published_date,item_code,item_name,value,source FROM financial_quarterly WHERE ticker=?", (ticker,))
            actions = query(connection, "SELECT ticker,ex_date,action_type,cash_dividend,stock_ratio,split_ratio,rights_ratio,rights_price,source FROM corporate_actions WHERE ticker=?", (ticker,))
            coverage = query(connection, "SELECT variable,available,source,periods_available,notes FROM coverage WHERE ticker=?", (ticker,))
            diagnostics = {
                "ticker": ticker,
                "restored_from": "stocks_analysis.sqlite",
                "restored_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "statuses": {
                    "ta_status": "OK" if not prices.empty else "MISSING",
                    "fa_annual_status": "RESTORED",
                    "fa_quarterly_status": "RESTORED",
                    "corporate_actions_status": "RESTORED",
                },
                "quality": {
                    "ta_daily": {
                        "sessions": int(len(prices)),
                        "first_date": None if prices.empty else str(prices["date"].min())[:10],
                        "last_date": None if prices.empty else str(prices["date"].max())[:10],
                    }
                },
            }
            if args.dry_run:
                continue
            created["metadata"] += int(write_json_if_missing(ticker_dir / "metadata.json", company))
            created["ta"] += int(write_csv_if_missing(ticker_dir / "ta_daily.csv", prices, TA_COLUMNS))
            created["annual"] += int(write_csv_if_missing(ticker_dir / "fa_annual.csv", annual, FA_COLUMNS))
            created["quarterly"] += int(write_csv_if_missing(ticker_dir / "fa_quarterly.csv", quarterly, FA_COLUMNS))
            created["actions"] += int(write_csv_if_missing(ticker_dir / "corporate_actions.csv", actions, ACTION_COLUMNS))
            created["coverage"] += int(write_csv_if_missing(ticker_dir / "coverage_report.csv", coverage, COVERAGE_COLUMNS))
            created["diagnostics"] += int(write_json_if_missing(ticker_dir / "diagnostics.json", diagnostics))

    print(json.dumps({"database": str(database), "raw_dir": str(raw_dir), "tickers": int(len(companies)), "created": created, "dry_run": args.dry_run}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
