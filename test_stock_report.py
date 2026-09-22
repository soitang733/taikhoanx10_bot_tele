"""Read-only report checks against saved database snapshots."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import stock_report
import stock_local_store


ROOT = Path(__file__).resolve().parent / "analysis_data"


def rows(database: Path, sql: str, params: tuple = ()) -> list[dict]:
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def local_api(path: str, params: dict | None = None) -> dict:
    params = params or {}
    ticker = params.get("ticker")
    main = ROOT / "stocks_analysis.sqlite"
    if path == "/v1/signal":
        data = rows(ROOT / "signals.sqlite", "SELECT * FROM signals_latest WHERE ticker=?", (ticker,))
        return {"data": data[0]}
    if path == "/v1/latest":
        data = rows(main, "SELECT * FROM price_daily WHERE ticker=? ORDER BY date DESC LIMIT 1", (ticker,))
        return {"data": data[0] if data else None}
    if path == "/v1/financials":
        table = "financial_quarterly" if params["period"] == "quarterly" else "financial_annual"
        return {"data": rows(main, f"SELECT * FROM {table} WHERE ticker=? AND row_valid=1 "
                             "ORDER BY period_end DESC LIMIT 120", (ticker,))}
    if path == "/v1/snapshot":
        data = rows(main, "SELECT * FROM financial_snapshot WHERE ticker=? ORDER BY as_of_utc DESC LIMIT 1",
                    (ticker,))
        return {"data": data[0] if data else None}
    if path == "/v1/industry-valuation":
        return stock_local_store.get(path, params)
    if path == "/v1/benchmark":
        data = rows(main, "SELECT * FROM benchmark_daily ORDER BY date DESC LIMIT 1")
        return {"data": data[0] if data else None}
    if path == "/v1/actions":
        return {"data": rows(main, "SELECT * FROM corporate_actions WHERE ticker=? "
                             "ORDER BY ex_date DESC LIMIT 3", (ticker,))}
    raise AssertionError(path)


@unittest.skipUnless((ROOT / "signals.sqlite").exists(), "local data not available")
class StockReportTests(unittest.TestCase):
    def test_hhp_report_is_plain_language_and_complete(self):
        with patch.object(stock_report, "latest_trade", side_effect=RuntimeError("offline")):
            context = stock_report.load_context("HHP", local_api)
        report = stock_report.render_report(context)
        for section in ("PHIÊN GẦN NHẤT", "SỨC KHỎE DOANH NGHIỆP", "XU HƯỚNG & DÒNG TIỀN",
                        "THỊ TRƯỜNG & GIẢI THÍCH TÍN HIỆU", "VN-Index", "Báo cáo kỳ"):
            self.assertIn(section, report)
        self.assertNotIn("Gemini", report)
        self.assertNotIn("EOD", report)
        self.assertNotIn("stock_dividend", report)
        self.assertIn("trả cổ tức bằng cổ phiếu", report)
        self.assertLess(len(report), 3900)

    def test_missing_metrics_are_not_replaced_with_zero(self):
        context = {"ticker": "XYZ", "signal": {"ticker": "XYZ", "final_action": "WATCH"},
                   "price": {}, "benchmark": {}, "quarterly": [], "annual": [], "actions": [],
                   "trade": None, "trade_note": "chưa có"}
        report = stock_report.render_report(context)
        self.assertIn("chưa có", report)
        self.assertIn("Chưa có báo cáo tài chính", report)

    def test_watch_reason_is_plain_language(self):
        context = {"ticker": "XYZ", "signal": {"ticker": "XYZ", "final_action": "WATCH",
                   "watch_reason": "ILLIQUID"}, "price": {}, "benchmark": {}, "quarterly": [],
                   "annual": [], "actions": [], "trade": None, "trade_note": "chưa có"}
        report = stock_report.render_report(context)
        self.assertIn("thanh khoản chưa đáp ứng", report)
        self.assertNotIn("ILLIQUID", report)

    def test_ntp_snapshot_fills_display_without_changing_signal(self):
        with patch.object(stock_report, "latest_trade", side_effect=RuntimeError("offline")):
            context = stock_report.load_context("NTP", local_api)
        report = stock_report.render_report(context, "fa")
        self.assertEqual(context["signal"]["fa_status"], "INSUFFICIENT_DATA")
        self.assertIn("ROA: +17.0%", report)
        self.assertIn("Doanh thu 12T: 7,136.3 tỷ đồng", report)
        self.assertIn("P/E TTM: 9.2", report)
        self.assertIn("Độ phủ tài chính lịch sử: 26.2%", report)
        self.assertIn("tín hiệu hiện chịu ảnh hưởng chủ yếu từ giá và thanh khoản", report)
        self.assertNotIn("Chưa đủ dữ liệu lịch sử cho", report)

    def test_vpi_ai_context_gets_calculated_pe_and_industry_median(self):
        with patch.object(stock_report, "latest_trade", side_effect=RuntimeError("offline")):
            context = stock_report.load_context("VPI", local_api)
        valuation = stock_report.analysis_context(context)["display_valuation_not_used_in_signal"]
        self.assertAlmostEqual(valuation["pe_ttm"], 52.1, places=1)
        self.assertEqual(valuation["source"], "latest_eod_close_divided_by_eps_ttm")
        self.assertGreater(valuation["industry_comparison"]["peer_count"], 0)
        self.assertIsNotNone(valuation["industry_comparison"]["median_pe_ttm"])


if __name__ == "__main__":
    unittest.main()
