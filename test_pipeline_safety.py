"""Regression tests for safeguards around daily-data rebuilds."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from daily_data_pipeline import RAW_BUILD_REQUIRED_FILES, analysis_rebuild_deferred_reason, date_window, dnse_overlap_revisions
from dnse_financial_snapshot import refresh
from prepare_analysis_data import prepare_actions, replace_sqlite_database
from vn_stock_scraper_complete import _reconcile_price_sources


class PipelineSafetyTests(unittest.TestCase):
    def test_mixed_corporate_action_date_formats_are_preserved(self) -> None:
        raw = pd.DataFrame([
            {"ticker": "AAA", "ex_date": "2026-09-21", "action_type": "cash_dividend", "cash_dividend": 500, "source": "vnstock VCI"},
            {"ticker": "AAA", "ex_date": "2026-09-22 00:00:00", "action_type": "stock_dividend", "stock_ratio": .1, "source": "vnstock VCI"},
        ])
        prepared, _ = prepare_actions(raw)
        self.assertEqual(int(prepared["row_valid"].sum()), 2)
        self.assertEqual(set(prepared["ex_date"].dt.strftime("%Y-%m-%d")), {"2026-09-21", "2026-09-22"})

    def test_overlap_means_market_sessions_not_calendar_days(self) -> None:
        market_dates = pd.DatetimeIndex(pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21"]))
        existing = pd.DataFrame({"date": ["2026-09-18", "2026-09-21"]})
        start, _ = date_window(existing, 2, 3650, market_dates)
        self.assertEqual(start.date().isoformat(), "2026-09-18")

    def test_fallback_change_does_not_claim_dnse_restatement(self) -> None:
        old = pd.DataFrame({"ticker": ["AAA"], "date": ["2026-09-21"],
                            "open": [100], "high": [100], "low": [100], "close": [100],
                            "volume": [1000], "source": ["vnstock KBS"]})
        new = old.copy()
        new[["open", "high", "low", "close"]] = 101
        self.assertTrue(dnse_overlap_revisions(old, new, "sync-test", "2026-09-22").empty)
        old["source"] = "DNSE"
        new["source"] = "DNSE"
        self.assertEqual(len(dnse_overlap_revisions(old, new, "sync-test", "2026-09-22")), 1)

    def test_fallback_preserves_underlying_dnse_checksum(self) -> None:
        primary = pd.DataFrame({"ticker": ["AAA"], "date": ["2026-09-21"],
                                "open": [100], "high": [100], "low": [100], "close": [100],
                                "volume": [1000], "source": ["DNSE"], "dnse_checksum": ["abc"]})
        fallback = primary.copy()
        fallback[["open", "high", "low", "close"]] = 130
        fallback["source"] = "vnstock KBS"
        fallback["dnse_checksum"] = pd.NA
        reconciled, replacements = _reconcile_price_sources(primary, fallback)
        self.assertEqual(replacements, 1)
        self.assertEqual(reconciled.iloc[0]["dnse_checksum"], "abc")
        self.assertEqual(reconciled.iloc[0]["source"], "vnstock KBS")

    def test_sqlite_publish_uses_recoverable_rename_on_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, destination = root / "new.sqlite", root / "live.sqlite"
            for path, value in ((staged, "new"), (destination, "old")):
                connection = sqlite3.connect(path)
                try:
                    connection.execute("CREATE TABLE marker (value TEXT)")
                    connection.execute("INSERT INTO marker VALUES (?)", (value,))
                    connection.commit()
                finally:
                    connection.close()
            import os
            real_replace = os.replace
            calls = 0

            def locked_once(source, target):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("simulated Windows lock")
                return real_replace(source, target)

            with patch("prepare_analysis_data.os.replace", side_effect=locked_once):
                replace_sqlite_database(staged, destination, retries=1)
            connection = sqlite3.connect(destination)
            try:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "new")
            finally:
                connection.close()
            self.assertFalse(staged.exists())
            self.assertFalse(list(root.glob("*.bak")))

    def test_analysis_rebuild_is_deferred_when_raw_mirror_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analysis = root / "analysis"
            raw = root / "raw"
            analysis.mkdir()
            raw.mkdir()
            connection = sqlite3.connect(analysis / "stocks_analysis.sqlite")
            try:
                connection.execute("CREATE TABLE companies (ticker TEXT)")
                connection.executemany("INSERT INTO companies VALUES (?)", [("AAA",), ("BBB",)])
                connection.commit()
            finally:
                connection.close()

            for ticker in ("AAA",):
                folder = raw / ticker
                folder.mkdir()
                for filename in RAW_BUILD_REQUIRED_FILES:
                    (folder / filename).write_text("ok", encoding="utf-8")
            self.assertIn("1/2", analysis_rebuild_deferred_reason(raw, analysis) or "")

            folder = raw / "BBB"
            folder.mkdir()
            for filename in RAW_BUILD_REQUIRED_FILES:
                (folder / filename).write_text("ok", encoding="utf-8")
            self.assertIsNone(analysis_rebuild_deferred_reason(raw, analysis))

    def test_empty_financial_snapshot_request_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.csv"
            output.write_text("previous snapshot", encoding="utf-8")
            result = refresh([], output)
            self.assertEqual(result["skipped"], "no symbols selected")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous snapshot")


if __name__ == "__main__":
    unittest.main()
