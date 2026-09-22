"""Build analysis-ready stock datasets from the scraper's per-ticker outputs.

The raw layer is never modified. This script is idempotent and can be rerun while
the scraper is active: only ticker folders containing diagnostics.json are read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import warnings
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


SCHEMA_VERSION = "1.2.0"


def replace_sqlite_database(staged: Path, destination: Path, retries: int = 6) -> None:
    """Publish a complete SQLite build, with a recoverable Windows rename fallback."""
    with closing(sqlite3.connect(f"file:{staged.as_posix()}?mode=ro", uri=True)) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Staged SQLite failed integrity check: {staged}")
    for attempt in range(retries):
        try:
            os.replace(staged, destination)
            return
        except PermissionError:
            if attempt + 1 < retries:
                time.sleep(attempt + 1)
    backup = destination.with_name(destination.name + f".swap-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.bak")
    if backup.exists():
        raise FileExistsError(f"Refusing to overwrite SQLite swap backup: {backup}")
    os.rename(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        os.rename(backup, destination)
        raise
    try:
        backup.unlink()
    except OSError as exc:
        warnings.warn(f"New SQLite is published; old swap backup remains at {backup}: {exc}")


SOURCE_PRIORITY = {
    "DNSE": 0,
    "vnfinancialdata": 10,
    "vnstock KBS": 20,
    "KBS": 20,
    "vnstock VCI": 30,
    "VCI": 30,
}


def repair_text(value: object) -> object:
    if not isinstance(value, str) or not any(mark in value for mark in ("Ã", "Â", "Æ")):
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
        return value if "�" in repaired else repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def clean_text_columns(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        frame[column] = frame[column].map(repair_text)
        frame[column] = frame[column].replace(r"^\s*$", pd.NA, regex=True)
    return frame


def normalize_ticker(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().str.upper()


def read_csv_if_present(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return clean_text_columns(pd.read_csv(path, encoding="utf-8-sig", low_memory=False))


def read_sqlite_if_present(path: Path, table: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        with closing(sqlite3.connect(path)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return pd.read_sql_query(f'SELECT * FROM "{table}"', connection) if exists else pd.DataFrame()
    except (sqlite3.Error, OSError):
        return pd.DataFrame()


def _checksum_number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return "" if not math.isfinite(number) else (format(number, ".8f").rstrip("0").rstrip(".") or "0")


def price_row_checksum(row: pd.Series) -> str:
    values = [str(row.get("ticker", "")).strip().upper(), str(row.get("date", ""))[:10]]
    values.extend(_checksum_number(row.get(column)) for column in ("open", "high", "low", "close", "volume"))
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def concat_ticker_files(ticker_dirs: Iterable[Path], filename: str, add_ticker: bool = False) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for ticker_dir in ticker_dirs:
        frame = read_csv_if_present(ticker_dir / filename)
        if frame.empty:
            continue
        if add_ticker and "ticker" not in frame.columns:
            frame.insert(0, "ticker", ticker_dir.name.upper())
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def source_priority(source: object) -> int:
    text = str(source or "")
    for prefix, priority in SOURCE_PRIORITY.items():
        if text.casefold().startswith(prefix.casefold()):
            return priority
    return 99


def parse_period(frame: pd.DataFrame) -> pd.DataFrame:
    period = frame["report_period"].astype("string").str.strip()
    annual = period.str.fullmatch(r"\d{4}", na=False)
    quarterly = period.str.extract(r"^(\d{4})-Q([1-4])$")
    frame["fiscal_year"] = pd.to_numeric(period.where(annual, quarterly[0]), errors="coerce").astype("Int64")
    frame["fiscal_quarter"] = pd.to_numeric(quarterly[1], errors="coerce").astype("Int64")
    frame["period_end"] = pd.NaT
    frame.loc[annual, "period_end"] = pd.to_datetime(period[annual] + "-12-31", errors="coerce")
    qmask = quarterly[0].notna()
    if qmask.any():
        qperiod = pd.PeriodIndex(
            quarterly.loc[qmask, 0] + "Q" + quarterly.loc[qmask, 1], freq="Q-DEC"
        )
        frame.loc[qmask, "period_end"] = qperiod.to_timestamp(how="end").normalize().to_numpy()
    return frame


def prepare_companies(ticker_dirs: list[Path]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for ticker_dir in ticker_dirs:
        path = ticker_dir / "metadata.json"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            row = {key: repair_text(value) for key, value in json.load(handle).items()}
        row["ticker"] = ticker_dir.name.upper()
        rows.append(row)
    frame = clean_text_columns(pd.DataFrame(rows))
    if frame.empty:
        return frame
    frame["ticker"] = normalize_ticker(frame["ticker"])
    if "listing_date" in frame:
        listing_dates = frame["listing_date"].astype("string").str.strip().str.slice(0, 10)
        frame["listing_date"] = pd.to_datetime(listing_dates, format="%Y-%m-%d", errors="coerce")
    for column in ("shares_outstanding", "market_cap"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    return frame.drop_duplicates("ticker", keep="last").sort_values("ticker").reset_index(drop=True)


def prepare_financials(frame: pd.DataFrame, expected_period_type: str) -> tuple[pd.DataFrame, int]:
    if frame.empty:
        return frame, 0
    frame = clean_text_columns(frame.copy())
    frame["ticker"] = normalize_ticker(frame["ticker"])
    for column in ("statement", "period_type", "item_code"):
        frame[column] = frame[column].astype("string").str.strip().str.lower()
    frame["report_period"] = frame["report_period"].astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce").astype("Float64")
    frame["published_date"] = pd.to_datetime(frame.get("published_date"), errors="coerce")
    frame["source_priority"] = frame["source"].map(source_priority).astype("Int64")
    frame["unit"] = np.where(frame["item_code"].eq("eps"), "VND/share", "VND")
    frame["point_in_time_ready"] = frame["published_date"].notna()
    frame["row_valid"] = (
        frame["ticker"].notna()
        & frame["report_period"].notna()
        & frame["item_code"].notna()
        & frame["value"].notna()
        & frame["period_type"].eq(expected_period_type)
    )
    frame = parse_period(frame)
    key = ["ticker", "statement", "report_period", "item_code"]
    before = len(frame)
    frame = (
        frame.sort_values(key + ["source_priority"], na_position="last")
        .drop_duplicates(key, keep="first")
        .sort_values(["ticker", "period_end", "statement", "item_code"], na_position="last")
        .reset_index(drop=True)
    )
    return frame, before - len(frame)


def prepare_prices(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame.empty:
        return frame, 0
    frame = clean_text_columns(frame.copy())
    frame["ticker"] = normalize_ticker(frame["ticker"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if "fetched_at" not in frame:
        frame["fetched_at"] = pd.NaT
    if "data_version" not in frame:
        frame["data_version"] = 1
    if "row_checksum" not in frame:
        frame["row_checksum"] = pd.NA
    if "dnse_checksum" not in frame:
        frame["dnse_checksum"] = pd.NA
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], errors="coerce", utc=True)
    frame["fetched_at"] = frame["fetched_at"].fillna(pd.Timestamp.now(tz="UTC").floor("s"))
    frame["data_version"] = pd.to_numeric(frame["data_version"], errors="coerce").fillna(1).astype("Int64")
    numeric = [
        "open", "high", "low", "close", "adjusted_open", "adjusted_high",
        "adjusted_low", "adjusted_close", "volume", "trading_value",
    ]
    for column in numeric:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    frame["source_priority"] = frame["source"].map(source_priority).astype("Int64")
    before = len(frame)
    frame = (
        frame.sort_values(["ticker", "date", "source_priority"], na_position="last")
        .drop_duplicates(["ticker", "date"], keep="first")
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )
    checksum_dates = frame["date"].dt.strftime("%Y-%m-%d")
    checksum_frame = frame.copy()
    checksum_frame["date"] = checksum_dates
    frame["row_checksum"] = frame["row_checksum"].astype("string")
    missing_checksum = frame["row_checksum"].isna() | frame["row_checksum"].str.strip().eq("")
    frame.loc[missing_checksum, "row_checksum"] = checksum_frame.loc[missing_checksum].apply(price_row_checksum, axis=1)
    frame["dnse_checksum"] = frame["dnse_checksum"].astype("string")
    missing_dnse = frame["dnse_checksum"].isna() | frame["dnse_checksum"].str.strip().eq("")
    dnse_rows = frame["source"].astype("string").eq("DNSE").fillna(False)
    frame.loc[missing_dnse & dnse_rows, "dnse_checksum"] = frame.loc[missing_dnse & dnse_rows, "row_checksum"]
    nonnegative = frame[["open", "high", "low", "close"]].ge(0).all(axis=1)
    ohlc_order = (
        frame["high"].ge(frame[["open", "close", "low"]].max(axis=1))
        & frame["low"].le(frame[["open", "close", "high"]].min(axis=1))
    )
    frame["ohlc_valid"] = frame[["open", "high", "low", "close"]].notna().all(axis=1) & nonnegative & ohlc_order
    frame["volume_valid"] = frame["volume"].isna() | frame["volume"].ge(0)
    frame["analysis_ready"] = frame["ticker"].notna() & frame["date"].notna() & frame["ohlc_valid"] & frame["volume_valid"]
    # Returns use adjacent valid observations, not necessarily adjacent calendar
    # days (a symbol can be suspended or illiquid). Invalid bars never leak into
    # either side of a return calculation.
    frame["previous_price_date"] = pd.NaT
    frame["return_gap_calendar_days"] = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    frame["return_prev_session"] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
    frame["log_return_prev_session"] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
    valid_return_rows = (
        frame["analysis_ready"]
        & frame["adjusted_close"].gt(0)
        & frame["volume"].fillna(0).gt(0)
    )
    valid_prices = frame.loc[valid_return_rows, ["ticker", "date", "adjusted_close"]].copy()
    previous_date = valid_prices.groupby("ticker", observed=True)["date"].shift(1)
    previous_close = valid_prices.groupby("ticker", observed=True)["adjusted_close"].shift(1)
    frame.loc[valid_return_rows, "previous_price_date"] = previous_date
    frame.loc[valid_return_rows, "return_gap_calendar_days"] = (
        valid_prices["date"] - previous_date
    ).dt.days.astype("Int64")
    frame.loc[valid_return_rows, "return_prev_session"] = valid_prices["adjusted_close"] / previous_close - 1.0
    frame.loc[valid_return_rows, "log_return_prev_session"] = np.log(valid_prices["adjusted_close"] / previous_close)
    frame["return_gap_trading_sessions"] = pd.Series(pd.NA, index=frame.index, dtype="Int64")
    # These strict one-market-session returns are populated once the VNINDEX
    # trading calendar is available in build().
    frame["return_1d"] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
    frame["log_return_1d"] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
    frame["estimated_trading_value"] = frame["close"] * frame["volume"]
    frame["price_unit"] = "VND/share"
    frame["volume_unit"] = "shares"
    frame["trading_value_unit"] = "VND"
    return frame, before - len(frame)


def apply_trading_calendar_returns(frame: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """Separate prior-observation returns from strict consecutive-session returns."""
    result = frame.copy()
    market_dates = pd.Series(pd.to_datetime(benchmark["date"], errors="coerce").dropna().unique()).sort_values()
    session_number = {date: index for index, date in enumerate(market_dates, start=1)}
    current_session = result["date"].map(session_number)
    previous_session = result["previous_price_date"].map(session_number)
    gap = current_session - previous_session
    result["return_gap_trading_sessions"] = pd.to_numeric(gap, errors="coerce").astype("Int64")
    consecutive = result["return_gap_trading_sessions"].eq(1)
    result["return_1d"] = result["return_prev_session"].where(consecutive)
    result["log_return_1d"] = result["log_return_prev_session"].where(consecutive)
    return result


def prepare_actions(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame.empty:
        return frame, 0
    frame = clean_text_columns(frame.copy())
    frame["ticker"] = normalize_ticker(frame["ticker"])
    # Provider files mix YYYY-MM-DD and YYYY-MM-DD HH:MM:SS. Pandas infers one
    # format for an entire Series and silently coerces the other to NaT.
    ex_dates = frame["ex_date"].astype("string").str.strip().str.slice(0, 10)
    frame["ex_date"] = pd.to_datetime(ex_dates, format="%Y-%m-%d", errors="coerce")
    for column in ("cash_dividend", "stock_ratio", "split_ratio", "rights_ratio", "rights_price"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    key = [column for column in frame.columns if column != "source"]
    before = len(frame)
    frame = frame.drop_duplicates(key, keep="first").sort_values(["ticker", "ex_date"]).reset_index(drop=True)
    frame["row_valid"] = frame["ticker"].notna() & frame["ex_date"].notna() & frame["action_type"].notna()
    return frame, before - len(frame)


def prepare_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = clean_text_columns(frame.copy())
    frame["ticker"] = normalize_ticker(frame["ticker"])
    frame["available"] = frame["available"].astype("string").str.lower().map({"true": True, "false": False}).astype("boolean")
    frame["periods_available"] = pd.to_numeric(frame["periods_available"], errors="coerce").astype("Int64")
    return frame.sort_values(["ticker", "variable"]).reset_index(drop=True)


def financial_wide(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    valid = frame.loc[frame["row_valid"]].copy()
    valid = (
        valid.sort_values(["ticker", "report_period", "item_code", "source_priority"])
        .drop_duplicates(["ticker", "report_period", "item_code"], keep="first")
    )
    period_keys = valid[["ticker", "report_period", "fiscal_year", "fiscal_quarter", "period_end"]].drop_duplicates()
    wide = valid.pivot(index=["ticker", "report_period"], columns="item_code", values="value").reset_index()
    wide = period_keys.merge(wide, on=["ticker", "report_period"], how="inner", validate="one_to_one")
    wide.columns.name = None
    return wide.sort_values(["ticker", "period_end"]).reset_index(drop=True)


def make_quality_report(
    companies: pd.DataFrame,
    annual: pd.DataFrame,
    quarterly: pd.DataFrame,
    prices: pd.DataFrame,
    actions: pd.DataFrame,
    duplicate_counts: dict[str, int],
) -> pd.DataFrame:
    checks = [
        ("companies", "rows", len(companies)),
        ("companies", "missing_company_name", int(companies.get("company_name", pd.Series(dtype="object")).isna().sum())),
        ("financial_annual", "rows", len(annual)),
        ("financial_annual", "duplicates_removed", duplicate_counts["financial_annual"]),
        ("financial_annual", "missing_published_date", int(annual["published_date"].isna().sum()) if not annual.empty else 0),
        ("financial_quarterly", "rows", len(quarterly)),
        ("financial_quarterly", "duplicates_removed", duplicate_counts["financial_quarterly"]),
        ("financial_quarterly", "missing_published_date", int(quarterly["published_date"].isna().sum()) if not quarterly.empty else 0),
        ("price_daily", "rows", len(prices)),
        ("price_daily", "duplicates_removed", duplicate_counts["price_daily"]),
        ("price_daily", "invalid_ohlc_rows", int((~prices["ohlc_valid"]).sum()) if not prices.empty else 0),
        ("price_daily", "invalid_volume_rows", int((~prices["volume_valid"]).sum()) if not prices.empty else 0),
        ("corporate_actions", "rows", len(actions)),
        ("corporate_actions", "duplicates_removed", duplicate_counts["corporate_actions"]),
    ]
    return pd.DataFrame(checks, columns=["dataset", "check", "value"])


def sqlite_safe(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if isinstance(result[column].dtype, pd.DatetimeTZDtype):
            result[column] = result[column].dt.tz_convert(None)
        if pd.api.types.is_datetime64_any_dtype(result[column]):
            result[column] = result[column].dt.strftime("%Y-%m-%d %H:%M:%S").where(result[column].notna(), None)
        elif str(result[column].dtype) in {"boolean", "bool"}:
            result[column] = result[column].astype("Int64")
    return result.replace({pd.NA: None, np.nan: None})


def write_dataset(frame: pd.DataFrame, name: str, output_dir: Path, connection: sqlite3.Connection) -> None:
    parquet_path = output_dir / f"{name}.parquet"
    parquet_temp = output_dir / f"{name}.parquet.tmp"
    frame.to_parquet(parquet_temp, index=False, compression="zstd")
    os.replace(parquet_temp, parquet_path)
    sqlite_safe(frame).to_sql(name, connection, if_exists="replace", index=False, chunksize=10_000)


def build(input_dir: Path, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ticker_dirs = sorted(
        path for path in input_dir.iterdir()
        if path.is_dir() and (path / "diagnostics.json").exists()
    )
    universe_path = input_dir / "summary_metadata.csv"
    if universe_path.exists():
        universe = read_csv_if_present(universe_path)
        if "ticker" in universe.columns:
            active_tickers = set(normalize_ticker(universe["ticker"]).dropna())
            ticker_dirs = [path for path in ticker_dirs if path.name.upper() in active_tickers]
    if not ticker_dirs:
        raise RuntimeError(f"No completed ticker folders found in {input_dir}")

    database_path = output_dir / "stocks_analysis.sqlite"
    companies = prepare_companies(ticker_dirs)
    annual, annual_dupes = prepare_financials(concat_ticker_files(ticker_dirs, "fa_annual.csv"), "annual")
    quarterly, quarterly_dupes = prepare_financials(concat_ticker_files(ticker_dirs, "fa_quarterly.csv"), "quarterly")
    prices, price_dupes = prepare_prices(concat_ticker_files(ticker_dirs, "ta_daily.csv"))
    actions, action_dupes = prepare_actions(concat_ticker_files(ticker_dirs, "corporate_actions.csv"))
    coverage = prepare_coverage(concat_ticker_files(ticker_dirs, "coverage_report.csv", add_ticker=True))
    financial_snapshot = read_csv_if_present(input_dir / "financial_snapshot.csv")
    revision_columns = [
        "sync_run_id", "ticker", "date", "detected_at", "reason", "change_type",
        "old_open", "old_high", "old_low", "old_close", "old_volume", "old_source", "old_checksum", "old_dnse_checksum",
        "new_open", "new_high", "new_low", "new_close", "new_volume", "new_source", "new_checksum", "new_dnse_checksum",
    ]
    price_revisions = pd.concat(
        [read_sqlite_if_present(database_path, "price_revisions"), read_csv_if_present(input_dir / "price_revisions.csv")],
        ignore_index=True,
    ).reindex(columns=revision_columns)
    if not price_revisions.empty:
        price_revisions = price_revisions.drop_duplicates(
            ["sync_run_id", "ticker", "date", "old_checksum", "new_checksum"], keep="last"
        )
    sync_columns = [
        "sync_run_id", "started_at", "finished_at", "status", "requested_tickers", "updated_tickers",
        "failed_tickers", "history_audit_tickers", "action_audit_tickers", "full_refetch_tickers", "revision_rows",
    ]
    sync_runs = pd.concat(
        [read_sqlite_if_present(database_path, "sync_runs"), read_csv_if_present(input_dir / "sync_runs.csv")],
        ignore_index=True,
    ).reindex(columns=sync_columns)
    if not sync_runs.empty:
        sync_runs = sync_runs.drop_duplicates("sync_run_id", keep="last")
    snapshot_columns = [
        "ticker", "as_of_utc", "company_type", "status", "error", "market_share",
        "total_assets", "eps_ttm", "pe", "ps", "pb", "beta", "profit_growth_qoq",
        "roe_ttm", "roa_ttm", "gross_margin_ttm", "debt_equity_ratio",
        "inventory_growth_qoq", "free_float_ratio", "dividend_yield",
        "book_value_per_share", "revenue_ttm", "net_income_ttm", "market_cap",
    ]
    if financial_snapshot.empty and not len(financial_snapshot.columns):
        financial_snapshot = pd.DataFrame(columns=snapshot_columns)
    else:
        financial_snapshot = financial_snapshot.reindex(columns=snapshot_columns)
    if not financial_snapshot.empty:
        financial_snapshot["ticker"] = normalize_ticker(financial_snapshot["ticker"])
        financial_snapshot["as_of_utc"] = pd.to_datetime(financial_snapshot["as_of_utc"], errors="coerce", utc=True)
        text_columns = {"ticker", "as_of_utc", "company_type", "status", "error"}
        for column in financial_snapshot.columns:
            if column not in text_columns:
                financial_snapshot[column] = pd.to_numeric(financial_snapshot[column], errors="coerce").astype("Float64")
    annual_wide = financial_wide(annual)
    quarterly_wide = financial_wide(quarterly)

    benchmark = read_csv_if_present(input_dir / "vnindex_daily.csv")
    if not benchmark.empty:
        benchmark["ticker"] = "VNINDEX"
        benchmark, _ = prepare_prices(benchmark)
        prices = apply_trading_calendar_returns(prices, benchmark)
        benchmark = apply_trading_calendar_returns(benchmark, benchmark)

    duplicate_counts = {
        "financial_annual": annual_dupes,
        "financial_quarterly": quarterly_dupes,
        "price_daily": price_dupes,
        "corporate_actions": action_dupes,
    }
    quality = make_quality_report(companies, annual, quarterly, prices, actions, duplicate_counts)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest = pd.DataFrame([
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated_at,
            "raw_input": str(input_dir.resolve()),
            "completed_tickers": len(ticker_dirs),
            "first_ticker": ticker_dirs[0].name,
            "last_ticker": ticker_dirs[-1].name,
            "notes": (
                "Missing values are preserved; no financial values are imputed. "
                "return_prev_session is calculated against the prior valid traded observation (volume > 0); "
                "return_gap_calendar_days and return_gap_trading_sessions expose gaps; "
                "return_1d is populated only for consecutive VNINDEX sessions."
            ),
        }
    ])

    database_temp = output_dir / "stocks_analysis.sqlite.tmp"
    database_temp.unlink(missing_ok=True)
    with closing(sqlite3.connect(database_temp)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=NORMAL")
        datasets = {
            "companies": companies,
            "financial_annual": annual,
            "financial_quarterly": quarterly,
            "financial_annual_wide": annual_wide,
            "financial_quarterly_wide": quarterly_wide,
            "price_daily": prices,
            "corporate_actions": actions,
            "coverage": coverage,
            "financial_snapshot": financial_snapshot,
            "benchmark_daily": benchmark,
            "data_quality": quality,
            "dataset_manifest": manifest,
            "price_revisions": price_revisions,
            "sync_runs": sync_runs,
        }
        for name, frame in datasets.items():
            write_dataset(frame, name, output_dir, connection)
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_price_ticker_date ON price_daily(ticker, date);
            CREATE INDEX IF NOT EXISTS idx_annual_lookup ON financial_annual(ticker, report_period, item_code);
            CREATE INDEX IF NOT EXISTS idx_quarterly_lookup ON financial_quarterly(ticker, report_period, item_code);
            CREATE INDEX IF NOT EXISTS idx_actions_lookup ON corporate_actions(ticker, ex_date);
            CREATE INDEX IF NOT EXISTS idx_coverage_lookup ON coverage(ticker, variable);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_snapshot_ticker ON financial_snapshot(ticker);
            CREATE INDEX IF NOT EXISTS idx_price_revisions_lookup ON price_revisions(ticker, date, detected_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_id ON sync_runs(sync_run_id);
            """
        )
        connection.commit()
    replace_sqlite_database(database_temp, database_path)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated_at,
        "completed_tickers": len(ticker_dirs),
        "rows": {name: len(frame) for name, frame in datasets.items()},
        "database": str(database_path.resolve()),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_temp = output_dir / "manifest.json.tmp"
    with manifest_temp.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    os.replace(manifest_temp, manifest_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare scraper output for analysis")
    parser.add_argument("--input", type=Path, default=Path("scraper_output"))
    parser.add_argument("--output", type=Path, default=Path("analysis_data"))
    args = parser.parse_args()
    print(json.dumps(build(args.input.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
