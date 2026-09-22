"""Quote-board freshness and fallback behavior."""

from datetime import datetime, timezone
import unittest

import market_quotes


class MarketQuotesTests(unittest.TestCase):
    def setUp(self):
        market_quotes._CACHE.clear()

    def test_fresh_trade_is_marked_realtime(self):
        now = datetime.now(timezone.utc).isoformat()
        row = market_quotes.quote_snapshot(
            "FPT", {"close": 100000, "date": "2026-09-21"},
            fetch=lambda _ticker: {"time": now, "matchPrice": 101.5},
        )
        self.assertEqual(row["price_vnd"], 101500)
        self.assertEqual(row["source"], "DNSE_REALTIME")
        self.assertTrue(row["fresh"])

    def test_stale_trade_falls_back_to_eod(self):
        row = market_quotes.quote_snapshot(
            "FPT", {"close": 100000, "date": "2026-09-21"},
            fetch=lambda _ticker: {"time": "2020-01-01T10:00:00+07:00", "matchPrice": 101.5},
        )
        self.assertEqual(row["price_vnd"], 100000)
        self.assertEqual(row["source"], "EOD_FALLBACK")
        self.assertFalse(row["fresh"])

    def test_board_preserves_order_and_caches_quotes(self):
        calls = []
        def fetch(ticker):
            calls.append(ticker)
            return {"time": "2020-01-01T10:00:00+07:00", "matchPrice": 1}
        eod = {"FPT": {"close": 100000}, "NTP": {"close": 50000}}
        first = market_quotes.quote_board(["FPT", "NTP"], eod, fetch)
        second = market_quotes.quote_board(["FPT", "NTP"], eod, fetch)
        self.assertEqual([row["ticker"] for row in first], ["FPT", "NTP"])
        self.assertEqual(first, second)
        self.assertEqual(sorted(calls), ["FPT", "NTP"])


if __name__ == "__main__":
    unittest.main()
