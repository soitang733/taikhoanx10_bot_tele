"""Deterministic tests for the isolated paper-trading ledger."""

from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import sqlite3
import tempfile
import unittest

from paper_trading import PaperTradingError, PaperTradingStore


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.market = root / "market.sqlite"
        self.signals = root / "signals.sqlite"
        with closing(sqlite3.connect(self.market)) as connection, connection:
            connection.executescript(
                "CREATE TABLE companies(ticker TEXT PRIMARY KEY,company_name TEXT,exchange TEXT);"
                "CREATE TABLE price_daily(ticker TEXT,date TEXT,adjusted_close REAL,close REAL,analysis_ready INTEGER);"
                "INSERT INTO companies VALUES('FPT','CTCP FPT','HOSE');"
                "INSERT INTO price_daily VALUES('FPT','2026-09-21',100000,99000,1);"
            )
        with closing(sqlite3.connect(self.signals)) as connection, connection:
            connection.executescript(
                "CREATE TABLE signals_latest(ticker TEXT PRIMARY KEY,final_action TEXT);"
                "INSERT INTO signals_latest VALUES('FPT','BUY');"
            )
        self.store = PaperTradingStore(root / "paper.sqlite", self.market, self.signals,
                                       initial_cash=1_000_000,
                                       realtime_quote=lambda _ticker: {
                                           "time": "2020-01-01T10:00:00+07:00", "matchPrice": 90
                                       })

    def tearDown(self):
        self.temp.cleanup()

    def test_buy_and_sell_update_cash_positions_and_pnl(self):
        buy = self.store.order("fpt", "buy", 5)
        self.assertEqual(buy["price"], 99000)
        self.assertEqual(buy["price_source"], "EOD_FALLBACK")
        self.assertEqual(buy["signal_action"], "BUY")
        after_buy = self.store.portfolio()
        self.assertEqual(after_buy["positions"][0]["quantity"], 5)
        self.assertEqual(after_buy["cash"], 504257.5)
        self.store.order("FPT", "SELL", 2)
        after_sell = self.store.portfolio()
        self.assertEqual(after_sell["positions"][0]["quantity"], 3)
        self.assertAlmostEqual(after_sell["realized_pnl"], -594)

    def test_rejects_overspend_and_short_sale(self):
        with self.assertRaisesRegex(PaperTradingError, "Tiền mặt không đủ"):
            self.store.order("FPT", "BUY", 100)
        with self.assertRaisesRegex(PaperTradingError, "nắm giữ 0"):
            self.store.order("FPT", "SELL", 1)

    def test_reset_clears_only_paper_ledger(self):
        self.store.order("FPT", "BUY", 1)
        result = self.store.reset()
        self.assertEqual(result["cash"], 1_000_000)
        self.assertEqual(result["positions"], [])
        with closing(sqlite3.connect(self.market)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], 1)

    def test_fresh_dnse_trade_is_execution_price(self):
        now = datetime.now(timezone.utc).isoformat()
        store = PaperTradingStore(
            self.root / "fresh-paper.sqlite", self.market, self.signals,
            initial_cash=1_000_000,
            realtime_quote=lambda _ticker: {"time": now, "matchPrice": 99.5},
        )
        fill = store.order("FPT", "BUY", 5)
        self.assertEqual(fill["price"], 99500)
        self.assertEqual(fill["price_source"], "DNSE_REALTIME")

    def test_eod_fallback_uses_latest_analysis_ready_row(self):
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
        with closing(sqlite3.connect(self.market)) as connection, connection:
            connection.execute("INSERT INTO price_daily VALUES(?,?,?,?,1)",
                               ("FPT", today, 120000, 120000))
        fill = self.store.order("FPT", "BUY", 1)
        self.assertEqual(fill["price"], 120000)
        self.assertEqual(fill["price_source"], "EOD_FALLBACK")

    def test_live_portfolio_mark_is_display_only_and_adjusted_to_eod_basis(self):
        self.store.order("FPT", "BUY", 1)
        before = self.store.portfolio()
        marked = self.store.marked_portfolio({"FPT": {"fresh": True, "price_vnd": 101000,
                                                       "trade_time": "2026-09-22T10:00:00+07:00"}})
        position = marked["positions"][0]
        self.assertAlmostEqual(position["price"], 101000 * 100000 / 99000)
        self.assertEqual(position["price_source"], "DNSE_LIVE_ADJUSTED_ESTIMATE")
        self.assertEqual(marked["live_position_count"], 1)
        self.assertEqual(self.store.portfolio()["equity"], before["equity"])

    def set_exit_signal(self, *, price=99000, ma200=100000, score=80):
        with closing(sqlite3.connect(self.signals)) as connection, connection:
            connection.execute("DROP TABLE signals_latest")
            connection.execute(
                "CREATE TABLE signals_latest(ticker TEXT PRIMARY KEY,final_action TEXT,signal_date TEXT,"
                "price REAL,ma200 REAL,high_close60 REAL,unified_score REAL,price_fresh INTEGER,market_bull INTEGER)"
            )
            connection.execute(
                "INSERT INTO signals_latest VALUES('FPT','WATCH','2026-09-21',?,?,?,?,1,1)",
                (price, ma200, 110000, score),
            )

    def test_position_exit_is_personalized_from_risk_rules(self):
        self.store.order("FPT", "BUY", 1)
        self.set_exit_signal(price=99000, ma200=100000, score=80)
        position = self.store.portfolio()["positions"][0]
        self.assertEqual(position["position_action"], "EXIT")
        self.assertIn("MA200", position["position_action_reason"])

    def test_low_score_waits_for_minimum_holding_period(self):
        self.store.order("FPT", "BUY", 1)
        self.set_exit_signal(price=105000, ma200=100000, score=30)
        position = self.store.portfolio()["positions"][0]
        self.assertEqual(position["position_action"], "HOLD")
        self.assertIn("chưa đủ thời gian", position["position_action_reason"])


if __name__ == "__main__":
    unittest.main()
