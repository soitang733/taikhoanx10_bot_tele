"""DNSE current-session OHLC parsing never contaminates EOD history."""

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dnse_session_candle import fetch_today_candle, parse_daily_candle


class SessionCandleTests(TestCase):
    def setUp(self):
        self.today = datetime(2026, 9, 22, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        self.payload = {"t": [int(self.today.timestamp())],
                        "o": [66.4], "h": [67.0], "l": [65.5], "c": [66.6], "v": [1234567]}

    def test_parse_today_ohlcv_with_vnd_units(self):
        candle = parse_daily_candle(self.payload, "FPT", self.today.date())
        self.assertEqual(candle["date"], "2026-09-22")
        self.assertEqual(candle["open"], 66400)
        self.assertEqual(candle["close"], 66600)
        self.assertEqual(candle["volume"], 1234567)
        self.assertTrue(candle["provisional"])

    def test_never_relabel_previous_session_as_today(self):
        self.assertIsNone(parse_daily_candle(self.payload, "FPT",
                                               self.today.date().replace(day=23)))

    def test_fetch_uses_daily_resolution_and_local_session_bounds(self):
        calls = []

        class FakeClient:
            def get_ohlc(self, **kwargs):
                calls.append(kwargs)
                return 200, self_payload

        self_payload = self.payload
        with patch.dict("os.environ", {"DNSE_API_KEY": "test", "DNSE_API_SECRET": "test"}):
            candle = fetch_today_candle("fpt", now=self.today,
                                        client_factory=lambda **_kwargs: FakeClient())
        self.assertEqual(candle["ticker"], "FPT")
        self.assertEqual(calls[0]["bar_type"], "STOCK")
        self.assertEqual(calls[0]["query"]["resolution"], "1D")
        self.assertEqual(calls[0]["query"]["from"], int(self.today.timestamp()))
