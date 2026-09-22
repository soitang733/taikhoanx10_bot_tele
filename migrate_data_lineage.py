"""Upgrade an existing analysis SQLite database with reproducibility metadata."""

from __future__ import annotations

import argparse
import hashlib
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "1.2.0"


def number_token(value: object) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
        return (format(number, ".8f").rstrip("0").rstrip(".") or "0") if math.isfinite(number) else ""
    except (TypeError, ValueError):
        return ""


def checksum(row: tuple[object, ...]) -> str:
    ticker, date, open_, high, low, close, volume = row
    values = [str(ticker or "").strip().upper(), str(date or "")[:10]]
    values.extend(number_token(value) for value in (open_, high, low, close, volume))
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def add_column(connection: sqlite3.Connection, table: str, declaration: str) -> None:
    column = declaration.split()[0]
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {declaration}')


def backfill_table(connection: sqlite3.Connection, table: str, fetched_at: str) -> int:
    if not table_exists(connection, table):
        return 0
    add_column(connection, table, "fetched_at TEXT")
    add_column(connection, table, "data_version INTEGER")
    add_column(connection, table, "row_checksum TEXT")
    connection.execute(
        f'UPDATE "{table}" SET fetched_at=COALESCE(fetched_at, ?), data_version=COALESCE(data_version, 1)',
        (fetched_at,),
    )
    cursor = connection.execute(
        f'SELECT rowid, ticker, date, open, high, low, close, volume FROM "{table}" '
        "WHERE row_checksum IS NULL OR trim(row_checksum)=''"
    )
    updated = 0
    while True:
        batch = cursor.fetchmany(20_000)
        if not batch:
            break
        payload = [(checksum(row[1:]), row[0]) for row in batch]
        connection.executemany(f'UPDATE "{table}" SET row_checksum=? WHERE rowid=?', payload)
        updated += len(payload)
    return updated


def migrate(database: Path) -> dict[str, object]:
    database = database.resolve()
    if not database.exists():
        raise FileNotFoundError(database)
    migrated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        price_rows = backfill_table(connection, "price_daily", migrated_at)
        benchmark_rows = backfill_table(connection, "benchmark_daily", migrated_at)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_revisions (
                sync_run_id TEXT, ticker TEXT, date TEXT, detected_at TEXT, reason TEXT, change_type TEXT,
                old_open REAL, old_high REAL, old_low REAL, old_close REAL, old_volume REAL,
                old_source TEXT, old_checksum TEXT,
                new_open REAL, new_high REAL, new_low REAL, new_close REAL, new_volume REAL,
                new_source TEXT, new_checksum TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_price_revisions_lookup
                ON price_revisions(ticker, date, detected_at);
            CREATE TABLE IF NOT EXISTS sync_runs (
                sync_run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
                requested_tickers INTEGER, updated_tickers INTEGER, failed_tickers INTEGER,
                history_audit_tickers INTEGER, action_audit_tickers INTEGER,
                full_refetch_tickers INTEGER, revision_rows INTEGER
            );
            """
        )
        baseline_id = "migration-" + migrated_at.replace("-", "").replace(":", "")
        connection.execute(
            "INSERT OR IGNORE INTO sync_runs VALUES (?, ?, ?, 'baseline_migration', 0, 0, 0, 0, 0, 0, 0)",
            (baseline_id, migrated_at, migrated_at),
        )
        if table_exists(connection, "dataset_manifest"):
            connection.execute("UPDATE dataset_manifest SET schema_version=?", (SCHEMA_VERSION,))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.commit()
    return {
        "database": str(database),
        "schema_version": SCHEMA_VERSION,
        "price_rows_backfilled": price_rows,
        "benchmark_rows_backfilled": benchmark_rows,
        "integrity_check": integrity,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("analysis_data/stocks_analysis.sqlite"))
    print(migrate(parser.parse_args().database))


if __name__ == "__main__":
    main()
