"""Finalize a daily sync whose raw download succeeded but Windows blocked DB swap.

Run only after the validated temporary DB has been promoted and signals rebuilt.
Preserves the original failure as an auditable recovery event.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from daily_data_pipeline import atomic_json


def recover(root: Path) -> dict:
    analysis = root / "analysis_data"
    previous = json.loads((analysis / "pipeline_state.json").read_text(encoding="utf-8"))
    if previous.get("status") != "failed" or "WinError 32" not in previous.get("error", ""):
        raise RuntimeError("No matching Windows database-swap failure to recover")
    with (root / "scraper_output" / "sync_runs.csv").open(encoding="utf-8-sig", newline="") as stream:
        runs = list(csv.DictReader(stream))
    run = next((row for row in reversed(runs) if row["sync_run_id"] == previous["sync_run_id"]), None)
    if not run or run["status"] != "success" or int(run["failed_tickers"]) != 0:
        raise RuntimeError("The matching raw sync did not finish successfully")
    database = analysis / "stocks_analysis.sqlite"
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Canonical database integrity failed")
        companies, latest = db.execute("SELECT COUNT(*), MAX(date) FROM price_daily").fetchone()
        benchmark = db.execute("SELECT MAX(date) FROM benchmark_daily").fetchone()[0]
    with sqlite3.connect(f"file:{(analysis / 'signals.sqlite').as_posix()}?mode=ro", uri=True) as db:
        count, signal_date = db.execute("SELECT COUNT(*), MAX(signal_date) FROM signals_latest").fetchone()
    if count != int(run["requested_tickers"]) or latest[:10] != signal_date[:10] or benchmark[:10] < latest[:10]:
        raise RuntimeError("Signals, benchmark and recovered prices are not synchronized")
    if not (analysis / "backtest_report.json").exists():
        raise RuntimeError("Backtest was not rebuilt")
    recovered = {
        "status": "degraded",
        "started_at_utc": previous["started_at_utc"],
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "sync_run_id": run["sync_run_id"],
        "requested_tickers": int(run["requested_tickers"]),
        "updated_tickers": int(run["updated_tickers"]),
        "failed_tickers": [],
        "full_refetch_tickers": int(run["full_refetch_tickers"]),
        "revision_rows": int(run["revision_rows"]),
        "latest_market_date": latest,
        "benchmark_date": benchmark,
        "signal_date": signal_date,
        "recovered_from_error": previous["error"],
        "recovery": "validated raw sync and SQLite temp, promoted DB after closing readers, rebuilt signals and backtest",
    }
    atomic_json(analysis / "pipeline_state.json", recovered)
    return recovered


if __name__ == "__main__":
    print(json.dumps(recover(Path(__file__).resolve().parent), ensure_ascii=False, indent=2))
