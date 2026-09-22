"""Offline checks for the on-demand quote / Gemini boundary."""

import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import stock_ai_reply
import stock_local_store
import stock_report
import telegram_bot


class StockAiReplyTests(unittest.TestCase):
    def test_stale_trade_not_called_realtime(self):
        note = stock_ai_reply.trade_status({"time": "2020-01-01 14:45:00"})
        self.assertIn("không còn là giá tức thời", note)

    def test_small_provider_clock_skew_is_still_fresh(self):
        slightly_ahead = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        note = stock_ai_reply.trade_status({"time": slightly_ahead})
        self.assertIn("trong 15 phút gần đây", note)
        self.assertTrue(stock_ai_reply.is_fresh_trade({"time": slightly_ahead}))

    def test_old_trade_is_not_fresh_for_paper_execution(self):
        self.assertFalse(stock_ai_reply.is_fresh_trade({"time": "2020-01-01T14:45:00+07:00"}))

    def test_missing_trade_keeps_saved_data_report(self):
        def fake_api(path, params=None):
            if path == "/v1/signal":
                return {"data": {"ticker": "FPT", "signal_date": "2026-09-21", "final_action": "WATCH"}}
            return {"data": []}
        with patch.object(stock_report, "latest_trade", side_effect=RuntimeError("offline")):
            context = stock_report.load_context("FPT", fake_api)
        reply = stock_report.render_report(context)
        self.assertIn("Chưa lấy được giao dịch", reply)
        self.assertIn("Tín hiệu: 🟡 WATCH", reply)

    def test_trade_is_in_memory_only(self):
        trade = {"symbol": "FPT", "time": "2020-01-01 14:45:00", "matchPrice": 66.4}
        def fake_api(path, params=None):
            return {"data": {"ticker": "FPT", "signal_date": "2026-09-21"}} if path == "/v1/signal" else {"data": []}
        with patch.object(stock_report, "latest_trade", return_value=trade), \
             patch.object(stock_report, "trade_status", return_value="giá cũ"):
            context = stock_report.load_context("FPT", fake_api)
        self.assertEqual(context["trade"], trade)
        report = stock_report.render_report(context)
        self.assertIn("giá cũ", report)
        self.assertIn("66,400 đồng/cp", report)
        self.assertNotIn("66.40", report)

    def test_dnse_thousand_vnd_conversion_is_explicit_and_safe(self):
        self.assertEqual(stock_report.trade_price_vnd("68.1"), "68,100")
        self.assertEqual(stock_report.trade_price_vnd(None), "chưa có")
        self.assertEqual(stock_report.trade_price_vnd("-1"), "chưa có")

    def test_no_gemini_key_is_explicit(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GEMINI_FALLBACK_API_KEYS": ""}):
            with self.assertRaisesRegex(RuntimeError, "Thiếu khóa truy cập AI"):
                stock_ai_reply.gemini_answer("FPT", {}, None, "EOD")

    def test_command_uses_requested_ticker_not_fpt(self):
        for ticker in ("AAA", "REE", "SHS", "BTS"):
            with self.subTest(ticker=ticker):
                with patch.object(telegram_bot, "load_context", return_value={"ticker": ticker}) as load, \
                     patch.object(telegram_bot, "render_report", return_value="OK"), \
                     patch.object(telegram_bot, "news_section", return_value=""):
                    self.assertEqual(telegram_bot.command_reply(f"/stock {ticker}"), "OK")
                load.assert_called_once_with(ticker, telegram_bot.api_get, include_trade=True)

    def test_telegram_error_does_not_expose_token(self):
        response = requests.Response()
        response.status_code = 401
        response.url = "https://api.telegram.org/botSECRET/sendMessage"
        with patch.object(telegram_bot.requests, "post", return_value=response):
            with self.assertRaises(RuntimeError) as error:
                telegram_bot.telegram_call("sendMessage", {})
        self.assertNotIn("SECRET", str(error.exception))
        self.assertIn("HTTP 401", str(error.exception))

    def test_menu_and_ranking_buttons(self):
        menu = telegram_bot.keyboard_for("/menu")["inline_keyboard"]
        self.assertEqual(menu[0][0]["callback_data"], "input:stock")
        self.assertIn("top:EXIT", [button["callback_data"] for row in menu for button in row])
        top = telegram_bot.keyboard_for("/top WATCH 10", "<b>Top WATCH</b>\n1. SHS — WATCH\n2. REE — WATCH")
        flat = [button["callback_data"] for row in top["inline_keyboard"] for button in row]
        self.assertIn("stock:SHS", flat)
        self.assertIn("stock:REE", flat)
        self.assertIn("top:WATCH", flat)

    def test_reply_keyboard_taps_become_short_user_messages(self):
        menu = telegram_bot.reply_keyboard_for("/menu")["keyboard"]
        labels = [button["text"] for row in menu for button in row]
        self.assertIn("📡 Tín hiệu MUA", labels)
        self.assertIn("🌐 VN-Index phiên trước", labels)
        reply = "<b>TÍN HIỆU MUA</b>\n" + "\n".join(
            f"{index}. <b>T{index}</b> · 10,000đ" for index in range(1, 8)
        )
        self.assertIsNone(telegram_bot.reply_keyboard_for("/top BUY", reply))
        top = telegram_bot.keyboard_for("/top BUY", reply)["inline_keyboard"]
        self.assertEqual(len(top[0]), 6)
        self.assertEqual(len(top[1]), 1)

    def test_market_summary_uses_ai_without_inventing_extra_inputs(self):
        benchmark = {"date": "2026-09-22", "open": 1798.9, "high": 1816.93,
                     "low": 1787.62, "close": 1816.93, "return_1d": 0.00959,
                     "volume": 531618144}
        with patch.object(telegram_bot, "generate_content",
                          return_value=("Chỉ số tăng và đóng cửa gần vùng cao nhất phiên.", {})) as generate:
            reply = telegram_bot.render_market_summary(benchmark)
        self.assertIn("AI tổng hợp", reply)
        self.assertIn("1,816.93 điểm", reply)
        prompt = generate.call_args.args[0]
        self.assertIn("không bịa độ rộng thị trường", prompt)

    def test_vpi_pe_question_uses_calculation_when_ai_is_unavailable(self):
        context = stock_report.load_context("VPI", stock_local_store.get, include_trade=False)
        with patch.object(telegram_bot, "investment_research", side_effect=RuntimeError("offline")):
            reply = telegram_bot.detailed_answer(context, "P/E hiện tại và so với ngành thế nào?")
        self.assertIn("52.1 lần", reply)
        self.assertIn("42 doanh nghiệp", reply)
        self.assertIn("14.5 lần", reply)
        self.assertNotIn("P/E null", reply)

    def test_sell_command_explains_pending_model_buys(self):
        def fake_api(path, params=None):
            if path == "/v1/model-portfolio":
                return {"data": {"positions": [], "pending_orders": [
                    {"ticker": "PET", "side": "BUY"}, {"ticker": "MSB", "side": "BUY"}
                ]}}
            return {"data": []}
        with patch.object(telegram_bot, "api_get", side_effect=fake_api):
            reply = telegram_bot.command_reply("/sell")
        self.assertIn("DANH SÁCH BÁN / EXIT", reply)
        self.assertIn("2 lệnh MUA đang chờ phiên kế tiếp", reply)

    def test_buy_list_is_compact_and_keeps_every_current_signal(self):
        rows = [{"ticker": f"T{i}", "close": 10000 + i, "return_6m": 0.1,
                 "trend_pass": 1, "momentum_pass": 1, "breakout_pass": i % 2,
                 "volume_pass": 1, "volume_ratio": 1.2} for i in range(24)]
        with patch.object(telegram_bot, "api_get", return_value={"data": rows}):
            reply = telegram_bot.command_reply("/top BUY")
        self.assertIn("Có <b>24 mã</b>", reply)
        self.assertIn("24. <b>T23</b>", reply)
        self.assertIn("TÍN HIỆU MUA", reply)
        self.assertIn("Chọn một mã", reply)
        self.assertNotIn("Động lượng giá:", reply)
        self.assertNotIn("Sức mạnh so với VN-Index", reply)
        self.assertNotIn("TA M", reply)
        self.assertTrue(all(len(part) <= 3900 for part in telegram_bot.reply_parts("/top BUY", reply)))

    def test_menu_removes_duplicate_report_buttons_and_adds_daily(self):
        stock = telegram_bot.keyboard_for("/stock FPT")["inline_keyboard"]
        labels = [button["text"] for row in stock for button in row]
        self.assertNotIn("📋 Báo cáo", labels)
        self.assertNotIn("🔄 Cập nhật", labels)
        menu = telegram_bot.keyboard_for("/menu")["inline_keyboard"]
        callbacks = [button["callback_data"] for row in menu for button in row]
        self.assertIn("daily:on", callbacks)

    def test_menu_adds_web_app_only_for_https_deployment(self):
        with patch.object(telegram_bot, "WEBAPP_URL", "https://app.example.com"):
            menu = telegram_bot.keyboard_for("/menu")["inline_keyboard"]
        self.assertEqual(menu[0][0]["web_app"]["url"], "https://app.example.com")
        with patch.object(telegram_bot, "WEBAPP_URL", "http://127.0.0.1:8765"):
            menu = telegram_bot.keyboard_for("/menu")["inline_keyboard"]
        self.assertNotIn("web_app", menu[0][0])

    def test_daily_subscription_persists_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_bot, "STATE_DB", Path(temp_dir) / "state.sqlite"):
            telegram_bot.set_daily_subscription(123, True)
            self.assertTrue(telegram_bot.daily_subscription_enabled(123))
            telegram_bot.set_daily_subscription(123, False)
            self.assertFalse(telegram_bot.daily_subscription_enabled(123))

    def test_daily_report_sends_once_after_eight(self):
        chat_id = 8990865327
        morning = datetime(2026, 9, 22, 8, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_bot, "STATE_DB", Path(temp_dir) / "state.sqlite"), \
             patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {chat_id}), \
             patch.object(telegram_bot, "daily_digest", return_value="<b>Daily</b>"), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.set_daily_subscription(chat_id, True)
            self.assertEqual(telegram_bot.send_due_daily_reports(morning), 1)
            self.assertEqual(telegram_bot.send_due_daily_reports(morning), 0)
        call.assert_called_once()

    def test_authorized_callback_routes_and_acknowledges(self):
        chat_id = 8990865327
        update = {"callback_query": {"id": "cb1", "from": {"id": chat_id},
                  "message": {"chat": {"id": chat_id}, "message_id": 7}, "data": "fa:SHS"}}
        with patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {chat_id}), \
             patch.object(telegram_bot, "telegram_call") as call, \
             patch.object(telegram_bot, "send_reply") as reply:
            telegram_bot.handle_update(update)
        call.assert_called_once_with("answerCallbackQuery", {"callback_query_id": "cb1"})
        reply.assert_called_once_with(chat_id, "/fa SHS", 7)

    def test_slow_command_keeps_progress_and_sends_answer_as_new_message(self):
        calls = []

        def fake_telegram(method, payload):
            calls.append((method, payload))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 91}}
            return {"ok": True}

        with patch.object(telegram_bot, "telegram_call", side_effect=fake_telegram), \
             patch.object(telegram_bot, "command_reply", return_value="Báo cáo hoàn chỉnh"):
            telegram_bot.send_reply(123, "/ask FPT Vì sao BUY?")

        self.assertEqual([method for method, _ in calls],
                         ["sendChatAction", "sendMessage", "sendMessage"])
        self.assertIn("Đang phân tích", calls[1][1]["text"])
        self.assertEqual(calls[2][1]["text"], "Báo cáo hoàn chỉnh")
        self.assertNotIn("message_id", calls[2][1])

    def test_expired_callback_ack_does_not_block_action(self):
        chat_id = 8990865327
        update = {"callback_query": {"id": "expired", "from": {"id": chat_id},
                  "message": {"chat": {"id": chat_id}, "message_id": 7}, "data": "market"}}
        with patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {chat_id}), \
             patch.object(telegram_bot, "telegram_call", side_effect=RuntimeError("HTTP 400")), \
             patch.object(telegram_bot, "send_reply") as reply:
            telegram_bot.handle_update(update)
        reply.assert_called_once_with(chat_id, "/market", 7)

    def test_foreign_callback_gets_explicit_denial(self):
        update = {"callback_query": {"id": "cb2", "from": {"id": 123},
                  "message": {"chat": {"id": 123}, "message_id": 7}, "data": "stock:SHS"}}
        with patch.object(telegram_bot, "PUBLIC_ACCESS", False), \
             patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {8990865327}), \
             patch.object(telegram_bot, "telegram_call") as call:
            telegram_bot.handle_update(update)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0], "answerCallbackQuery")
        self.assertTrue(call.call_args.args[1]["show_alert"])

    def test_button_prompt_uses_only_requested_ticker(self):
        chat_id = 8990865327
        prompt = {"callback_query": {"id": "cb3", "from": {"id": chat_id},
                  "message": {"chat": {"id": chat_id}, "message_id": 9}, "data": "ask:SHS"}}
        answer = {"message": {"chat": {"id": chat_id, "type": "private"}, "text": "Rủi ro gì?"}}
        with patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {chat_id}), \
             patch.object(telegram_bot, "telegram_call"), \
             patch.object(telegram_bot, "send_reply") as reply:
            telegram_bot.handle_update(prompt)
            telegram_bot.handle_update(answer)
        reply.assert_called_once_with(chat_id, "/ask SHS Rủi ro gì?")

    @unittest.skipUnless(os.name == "nt", "Windows task lock")
    def test_second_bot_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(telegram_bot, "__file__", str(Path(temp_dir) / "bot.py")):
            with telegram_bot.single_instance():
                with self.assertRaisesRegex(SystemExit, "already running"):
                    with telegram_bot.single_instance():
                        pass

    def test_gemini_retries_transient_error_with_low_thinking(self):
        busy = requests.Response()
        busy.status_code = 503
        ok = requests.Response()
        ok.status_code = 200
        ok._content = b'{"candidates":[{"content":{"parts":[{"text":"EOD only"}]}}]}'
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
             patch.object(stock_ai_reply.requests, "post", side_effect=[busy, ok]) as post, \
             patch.object(stock_ai_reply.time, "sleep"):
            answer = stock_ai_reply.gemini_answer("SHS", {}, None, "EOD")
        self.assertEqual(answer, "EOD only")
        self.assertEqual(post.call_count, 2)
        config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "minimal")
        self.assertNotIn("temperature", config)

    def test_gemini_switches_model_after_repeated_503(self):
        busy = requests.Response()
        busy.status_code = 503
        ok = requests.Response()
        ok.status_code = 200
        ok._content = b'{"candidates":[{"content":{"parts":[{"text":"Fallback OK"}]}}]}'
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "primary",
                                     "GEMINI_FALLBACK_MODEL": "fallback"}), \
             patch.object(stock_ai_reply.requests, "post", side_effect=[busy, busy, ok]) as post, \
             patch.object(stock_ai_reply.time, "sleep"):
            answer, _ = stock_ai_reply.generate_content("brief")
        self.assertEqual(answer, "Fallback OK")
        self.assertEqual(post.call_count, 3)
        self.assertIn("/models/primary:", post.call_args_list[0].args[0])
        self.assertIn("/models/fallback:", post.call_args_list[2].args[0])

    def test_gemini_receives_only_raw_trade_fields_without_unit_inference(self):
        ok = requests.Response()
        ok.status_code = 200
        ok._content = b'{"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}'
        trade = {"symbol": "SHS", "time": "2026-09-21 14:45:01",
                 "matchPrice": 14.1, "matchQtty": 10, "totalVolumeTraded": 511540}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
             patch.object(stock_ai_reply.requests, "post", return_value=ok) as post:
            stock_ai_reply.gemini_answer("SHS", {}, trade, "cũ")
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        context = json.loads(prompt.split("\n\n", 1)[1])
        self.assertNotIn("totalVolumeTraded", context["latest_trade_dnse_raw"])
        self.assertEqual(context["dnse_trade_price_unit"], "thousand_vnd_per_share")
        self.assertFalse(context["dnse_trade_quantity_unit_verified"])

    def test_ai_search_retries_with_fallback_key_and_keeps_grounding(self):
        exhausted = requests.Response()
        exhausted.status_code = 429
        exhausted._content = b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'
        ok = requests.Response()
        ok.status_code = 200
        ok._content = (b'{"candidates":[{"content":{"parts":[{"text":"Verified news"}]},'
                       b'"groundingMetadata":{"groundingChunks":[{"web":'
                       b'{"title":"Exchange","uri":"https://example.org/news"}}]}}]}')
        with patch.dict(os.environ, {"GEMINI_API_KEY": "first-key", "GEMINI_FALLBACK_API_KEYS": "second-key",
                                      "GEMINI_MODEL": "gemini-3.6-flash"}), \
             patch.object(stock_ai_reply.requests, "post", side_effect=[exhausted, ok]) as post:
            text, sources = stock_ai_reply.generate_content("latest news", search=True)
        self.assertEqual(text, "Verified news")
        self.assertEqual(sources[0]["url"], "https://example.org/news")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["x-goog-api-key"], "first-key")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["x-goog-api-key"], "second-key")
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["thinkingConfig"],
                         {"thinkingLevel": "minimal"})
        self.assertIn("google_search", post.call_args.kwargs["json"]["tools"][0])

    def test_model_not_found_is_not_misreported_as_quota(self):
        unavailable = requests.Response()
        unavailable.status_code = 404
        unavailable._content = b'{"error":{"status":"NOT_FOUND"}}'
        with patch.dict(os.environ, {"GEMINI_API_KEY": "first-key", "GEMINI_FALLBACK_API_KEYS": "second-key"}), \
             patch.object(stock_ai_reply.requests, "post", return_value=unavailable) as post:
            with self.assertRaises(stock_ai_reply.AIServiceError) as error:
                stock_ai_reply.generate_content("news", search=True)
        self.assertEqual(error.exception.status_code, 404)
        post.assert_called_once()

    def test_all_keys_exhausted_reports_quota_not_model_error(self):
        exhausted = requests.Response()
        exhausted.status_code = 429
        exhausted._content = b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'
        with patch.dict(os.environ, {"GEMINI_API_KEY": "first-key", "GEMINI_FALLBACK_API_KEYS": "second-key,third-key"}), \
             patch.object(stock_ai_reply.requests, "post", return_value=exhausted) as post:
            with self.assertRaises(stock_ai_reply.AIServiceError) as error:
                stock_ai_reply.generate_content("news", search=True)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(post.call_count, 3)

    def test_ai_news_failure_names_rate_limit_without_provider_brand(self):
        context = {"ticker": "VHM", "signal": {"company_name": "Vinhomes", "industry": "Real estate"},
                   "benchmark": {"date": "2026-09-21", "close": 1799.67, "return_1d": -0.0088}}
        with patch.object(telegram_bot, "grounded_news", side_effect=stock_ai_reply.AIServiceError(429)):
            reply = telegram_bot.news_section(context)
        self.assertIn("Hạn mức tra cứu tin", reply)
        self.assertIn("VN-Index 1,799.67 điểm", reply)
        self.assertNotIn("Gemini", reply)

    def test_research_without_web_sources_reasks_using_internal_data_only(self):
        with patch.object(stock_ai_reply, "generate_content",
                          side_effect=[("Unsourced news", []), ("Internal explanation", [])]) as generate:
            answer, sources = stock_ai_reply.investment_research({"signal_from_rule_engine": {"final_action": "WATCH"}}, "Rủi ro?")
        self.assertEqual((answer, sources), ("Internal explanation", []))
        self.assertTrue(generate.call_args_list[0].kwargs["search"])
        self.assertFalse(generate.call_args_list[1].kwargs["search"])
        self.assertIn("Không dùng thông tin ngoài JSON", generate.call_args_list[1].args[0])

    def test_research_with_sources_keeps_grounded_answer(self):
        cited = [{"title": "Sở giao dịch", "url": "https://example.org/report"}]
        with patch.object(stock_ai_reply, "generate_content", return_value=("Verified", cited)) as generate:
            self.assertEqual(stock_ai_reply.investment_research({}, "Tin gì?")[0], "Verified")
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
