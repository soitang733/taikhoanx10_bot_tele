"""Validate the active SQLite analysis layer and generated signals."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def scalar(connection: sqlite3.Connection, sql: str) -> int | float | str | None:
    return connection.execute(sql).fetchone()[0]


def main() -> None:
    root = Path("analysis_data").resolve()
    database = root / "stocks_analysis.sqlite"
    signals_database = root / "signals.sqlite"
    assert database.exists() and database.stat().st_size > 0, "Missing stocks_analysis.sqlite"
    assert signals_database.exists() and signals_database.stat().st_size > 0, "Missing signals.sqlite"
    with sqlite3.connect(database) as connection:
        report = {
            "database_integrity": scalar(connection, "PRAGMA integrity_check"),
            "companies": scalar(connection, "SELECT COUNT(*) FROM companies"),
            "price_rows": scalar(connection, "SELECT COUNT(*) FROM price_daily"),
            "price_tickers": scalar(connection, "SELECT COUNT(DISTINCT ticker) FROM price_daily"),
            "benchmark_rows": scalar(connection, "SELECT COUNT(*) FROM benchmark_daily"),
            "latest_market_date": scalar(connection, "SELECT MAX(date) FROM price_daily WHERE analysis_ready=1"),
            "duplicate_price_keys": scalar(connection, "SELECT COUNT(*) FROM (SELECT ticker,date FROM price_daily GROUP BY ticker,date HAVING COUNT(*)>1)"),
            "analysis_ready_rows": scalar(connection, "SELECT COALESCE(SUM(analysis_ready),0) FROM price_daily"),
            "adjusted_close_rows": scalar(connection, "SELECT COUNT(*) FROM price_daily WHERE adjusted_close IS NOT NULL"),
            "ready_missing_adjusted_close": scalar(connection, "SELECT COUNT(*) FROM price_daily WHERE analysis_ready=1 AND adjusted_close IS NULL"),
            "dnse_rows_missing_source_checksum": scalar(connection, "SELECT COUNT(*) FROM price_daily WHERE source='DNSE' AND (dnse_checksum IS NULL OR dnse_checksum='')"),
            "corporate_actions": scalar(connection, "SELECT COUNT(*) FROM corporate_actions"),
            "corporate_actions_missing_ex_date": scalar(connection, "SELECT COUNT(*) FROM corporate_actions WHERE ex_date IS NULL"),
        }
    with sqlite3.connect(signals_database) as connection:
        report.update({
            "signals_integrity": scalar(connection, "PRAGMA integrity_check"),
            "signals": scalar(connection, "SELECT COUNT(*) FROM signals_latest"),
            "fa_ready": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE fa_status='READY'"),
            "fa_pass": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE fa_pass=1"),
            "ta_ready": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE ta_status='READY'"),
            "stale_buys": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE final_action='BUY' AND price_fresh=0"),
            "hard_rejected_buys": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE final_action='BUY' AND COALESCE(hard_reject_reason,'')<>''"),
            "watch_without_reason": scalar(connection, "SELECT COUNT(*) FROM signals_latest WHERE final_action='WATCH' AND (watch_reason IS NULL OR watch_reason='NONE')"),
            "model_positions": scalar(connection, "SELECT COUNT(*) FROM model_positions"),
            "model_pending_orders": scalar(connection, "SELECT COUNT(*) FROM model_pending_orders"),
            "model_runs": scalar(connection, "SELECT COUNT(*) FROM model_runs"),
            "exit_without_position": scalar(connection, "SELECT COUNT(*) FROM signals_latest s WHERE s.final_action='EXIT' AND NOT EXISTS (SELECT 1 FROM model_positions p WHERE p.ticker=s.ticker)"),
            "buy_already_held": scalar(connection, "SELECT COUNT(*) FROM signals_latest s WHERE s.final_action='BUY' AND EXISTS (SELECT 1 FROM model_positions p WHERE p.ticker=s.ticker)"),
        })
    backtest_path = root / "backtest_report.json"
    backtest = json.loads(backtest_path.read_text(encoding="utf-8")) if backtest_path.exists() else {}
    report["backtest_data_latest_date"] = backtest.get("data_latest_date")
    report["backtest_stale"] = str(backtest.get("data_latest_date") or "")[:10] < str(report["latest_market_date"] or "")[:10]
    assert report["database_integrity"] == "ok"
    assert report["signals_integrity"] == "ok"
    assert report["duplicate_price_keys"] == 0
    assert report["companies"] == report["signals"]
    assert report["ready_missing_adjusted_close"] == 0
    assert report["dnse_rows_missing_source_checksum"] == 0
    assert report["corporate_actions_missing_ex_date"] == 0
    assert report["stale_buys"] == 0
    assert report["hard_rejected_buys"] == 0
    assert report["watch_without_reason"] == 0
    assert report["model_positions"] <= 30
    assert report["model_runs"] >= 1
    assert report["exit_without_position"] == 0
    assert report["buy_already_held"] == 0
    assert not report["backtest_stale"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
