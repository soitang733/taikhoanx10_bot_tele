"""Bot stays useful when the localhost API or optional AI is down."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

import stock_local_store
import stock_report
import telegram_bot


class BotResilienceTests(unittest.TestCase):
    def test_public_access_accepts_a_new_private_chat(self):
        update = {"message": {"chat": {"id": 987654321, "type": "private"}, "text": "/start"}}
        with patch.object(telegram_bot, "PUBLIC_ACCESS", True), \
             patch.object(telegram_bot, "send_reply") as send:
            telegram_bot.handle_update(update)
        send.assert_called_once_with(987654321, "/start")

    def test_allowlist_mode_returns_an_explicit_denial(self):
        update = {"message": {"chat": {"id": 987654321, "type": "private"}, "text": "/start"}}
        with patch.object(telegram_bot, "PUBLIC_ACCESS", False), \
             patch.object(telegram_bot, "ALLOWED_CHAT_IDS", {123}), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.handle_update(update)
        self.assertEqual(call.call_args.args[0], "sendMessage")
        self.assertIn("chưa được cấp quyền", call.call_args.args[1]["text"])

    def test_stock_report_is_delivered_before_slow_news(self):
        context = {"ticker": "HHP", "signal": {"ticker": "HHP", "company_name": "HHP"}}
        with patch.object(telegram_bot, "load_context", return_value=context), \
             patch.object(telegram_bot, "render_report", return_value="CORE REPORT"), \
             patch.object(telegram_bot, "news_section", return_value="NEWS REPORT"), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.send_reply(123, "/stock HHP")
        sent_texts = [item.args[1].get("text") for item in call.call_args_list
                      if item.args[0] in {"sendMessage", "editMessageText"}]
        core_index = sent_texts.index("CORE REPORT")
        news_index = next(index for index, text in enumerate(sent_texts) if text and "NEWS REPORT" in text)
        self.assertLess(core_index, news_index)

    def test_source_links_keep_citation_number_and_hide_empty_list(self):
        self.assertEqual(telegram_bot.source_links([]), "")
        links = telegram_bot.source_links([
            {"id": 2, "title": "Tin ngành", "url": "https://example.org/news"}
        ])
        self.assertIn("[2]", links)
        self.assertNotIn("[1]", links)

    def test_read_only_fallback_and_circuit_breaker(self):
        with patch.object(telegram_bot, "API_RETRY_AFTER", 0.0), \
             patch.object(telegram_bot.requests, "get", side_effect=requests.ConnectionError("offline")) as get:
            first = telegram_bot.api_get("/v1/signal", {"ticker": "HHP"})
            second = telegram_bot.api_get("/v1/benchmark")
        self.assertEqual(first["data"]["ticker"], "HHP")
        self.assertEqual(second["data"]["ticker"], "VNINDEX")
        get.assert_called_once()

    def test_missing_symbol_is_explicit(self):
        with self.assertRaises(LookupError):
            stock_local_store.get("/v1/signal", {"ticker": "ZZZZZZZZZZ"})

    def test_local_store_health_and_rankings_match_api_contract(self):
        health = stock_local_store.get("/health")
        self.assertIn("pipeline_ok", health)
        self.assertIn("database_ok", health)
        ranked = stock_local_store.get("/v1/rankings", {"limit": 30})["data"]
        order = {"BUY": 1, "HOLD": 2, "WATCH": 3, "DATA_REVIEW": 4}
        self.assertEqual([order.get(row["final_action"], 5) for row in ranked],
                         sorted(order.get(row["final_action"], 5) for row in ranked))

    def test_api_recovers_after_circuit_breaker(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":{"ticker":"HHP"}}'
        with patch.object(telegram_bot, "API_RETRY_AFTER", 0.0), \
             patch.object(telegram_bot.requests, "get", return_value=response) as get:
            result = telegram_bot.api_get("/v1/signal", {"ticker": "HHP"})
            self.assertEqual(telegram_bot.API_RETRY_AFTER, 0.0)
        self.assertEqual(result["data"]["ticker"], "HHP")
        get.assert_called_once()

    def test_full_report_works_without_api_or_ai(self):
        with patch.object(telegram_bot, "API_RETRY_AFTER", float("inf")), \
             patch.object(stock_report, "latest_trade", side_effect=RuntimeError("offline")), \
             patch.object(telegram_bot, "news_section", return_value="\nTin mới chưa xác minh được."):
            report = telegram_bot.command_reply("/stock HHP")
        for label in ("SỨC KHỎE DOANH NGHIỆP", "XU HƯỚNG & DÒNG TIỀN", "VN-Index", "Tin mới"):
            self.assertIn(label, report)
        self.assertNotIn("Gemini", report)
        self.assertLess(len(report), 3900)

    def test_legacy_company_command_does_not_repeat_news_context(self):
        with patch.object(telegram_bot, "API_RETRY_AFTER", float("inf")), \
             patch.object(telegram_bot, "news_section", return_value="\n<b>Tin doanh nghiệp, ngành & vĩ mô</b>\nAI tạm lỗi"):
            report = telegram_bot.command_reply("/fa NTP")
        self.assertIn("Doanh thu 12T", report)
        self.assertNotIn("Tin doanh nghiệp, ngành & vĩ mô", report)

    def test_telegram_unmodified_edit_is_success_and_replies_are_new_messages(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"ok":false,"description":"Bad Request: message is not modified"}'
        with patch.object(telegram_bot.requests, "post", return_value=response):
            self.assertTrue(telegram_bot.telegram_call("editMessageText", {}).get("ok"))
        with patch.object(telegram_bot, "command_reply", return_value="Same report"), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.send_reply(123, "/stock HHP", message_id=5)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(call.call_args_list[0].args[0], "sendChatAction")
        self.assertIn("Đang tổng hợp báo cáo", call.call_args_list[1].args[1]["text"])
        self.assertEqual(call.call_args.args[0], "sendMessage")
        self.assertFalse(any(item.args[0] == "editMessageText" for item in call.call_args_list))

    def test_overlong_reply_remains_valid_html(self):
        with patch.object(telegram_bot, "command_reply", return_value="<b>" + "A" * 5000 + "</b>"), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.send_reply(123, "/menu")
        message = call.call_args.args[1]["text"]
        self.assertLessEqual(len(message), 3900)
        self.assertNotIn("<b>", message)

    def test_stock_report_and_news_are_sent_as_two_complete_messages(self):
        report = ("<b>BÁO CÁO</b>\nDữ liệu" +
                  "\n\n<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>\n\n"
                  "🏢 Doanh nghiệp\nSự kiện: A\n\n🏭 Ngành\nSự kiện: B\n\n"
                  "🌐 Kinh tế Việt Nam\nSự kiện: C")
        with patch.object(telegram_bot, "command_reply", return_value=report), \
             patch.object(telegram_bot, "telegram_call", return_value={"ok": True}) as call:
            telegram_bot.send_reply(123, "/stock HSG")
        self.assertEqual(call.call_count, 4)
        first = call.call_args_list[2].args[1]["text"]
        second = call.call_args_list[3].args[1]["text"]
        self.assertIn("BÁO CÁO", first)
        self.assertNotIn("TIN DOANH NGHIỆP", first)
        for label in ("Doanh nghiệp", "Ngành", "Kinh tế Việt Nam"):
            self.assertIn(label, second)
        self.assertNotIn("…", second)

    def test_long_stock_news_is_rebalanced_without_truncation(self):
        report = "<b>BÁO CÁO</b>\n" + "D" * 1500
        company = "🏢 DOANH NGHIỆP\n" + "C" * 1200
        industry = "🏭 NGÀNH\n" + "I" * 1200
        macro = "🌐 VĨ MÔ\n" + "M" * 1200
        sources = "<b>🔗 NGUỒN</b>\n" + "S" * 300
        full = report + "\n\n<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>\n\n" + \
            "\n\n".join((company, industry, macro, sources))
        parts = telegram_bot.reply_parts("/stock HSG", full)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(part) <= 3900 for part in parts))
        self.assertEqual("\n\n".join(parts), full)
        self.assertFalse(any("…" in part for part in parts))

    def test_force_reply_survives_bot_restart(self):
        bot_id = int(telegram_bot.TOKEN.split(":", 1)[0])
        message = {"reply_to_message": {"from": {"id": bot_id, "is_bot": True},
                   "text": "Bạn muốn tìm hiểu điều gì về HHP?"}}
        self.assertEqual(telegram_bot.pending_from_reply(message), ("ask", "HHP"))
        message["reply_to_message"]["from"]["id"] = 123
        self.assertIsNone(telegram_bot.pending_from_reply(message))

    def test_unknown_rank_filter_is_helpful(self):
        self.assertIn("Bộ lọc không hợp lệ", telegram_bot.command_reply("/top UNKNOWN 10"))

    def test_ai_formatting_and_quota_fallback_are_readable(self):
        self.assertEqual(telegram_bot.clean_analysis_text("### Kết luận\n**Theo dõi** `HHP`"),
                         "Kết luận\nTheo dõi HHP")
        context = {"ticker": "HHP", "signal": {"ticker": "HHP", "signal_date": "2026-09-21"},
                   "price": {}, "benchmark": {}, "quarterly": [], "annual": [], "actions": [],
                   "trade": None, "trade_note": "chưa có"}
        with patch.object(telegram_bot, "investment_research", side_effect=RuntimeError("quota")):
            reply = telegram_bot.detailed_answer(context, "Rủi ro?")
        self.assertIn("Phân tích nâng cao và tin mới tạm gián đoạn", reply)
        self.assertNotIn("Gemini", reply)


if __name__ == "__main__":
    unittest.main()
