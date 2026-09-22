"""Offline checks for Tavily search and Gemini synthesis."""

import os
import unittest
from unittest.mock import patch

import requests

import stock_research
from stock_ai_reply import AIServiceError


def response(status: int, data: str) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = data.encode("utf-8")
    return result


class StockResearchTests(unittest.TestCase):
    def setUp(self):
        stock_research._SEARCH_CACHE.clear()

    def test_missing_key_is_explicit_and_does_not_call_api(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}), \
             patch.object(stock_research.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "TAVILY_API_KEY"):
                stock_research._search("FPT")
        post.assert_not_called()

    def test_search_uses_basic_recent_results_and_filters_unsafe_urls(self):
        payload = ('{"results":['
                   '{"title":"FPT filing","url":"https://example.org/news","content":"Verified",'
                   '"published_date":"2026-09-22"},'
                   '{"title":"Bad","url":"javascript:alert(1)","content":"Bad"}] }')
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}), \
             patch.object(stock_research.requests, "post", return_value=response(200, payload)) as post:
            first = stock_research._search("FPT filing")
            second = stock_research._search("FPT filing")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["search_depth"], "basic")
        self.assertEqual(post.call_args.kwargs["json"]["topic"], "general")
        self.assertEqual(post.call_args.kwargs["json"]["time_range"], "month")
        self.assertTrue(post.call_args.kwargs["json"]["filter_by_published_date"])
        self.assertTrue(post.call_args.kwargs["json"]["include_published_date"])

    def test_search_error_does_not_expose_key_or_provider_body(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "SECRET"}), \
             patch.object(stock_research.requests, "post",
                          return_value=response(401, '{"error":"SECRET"}')):
            with self.assertRaises(AIServiceError) as error:
                stock_research._search("FPT")
        self.assertNotIn("SECRET", str(error.exception))

    def test_news_requires_sources_and_uses_gemini_without_web_tool(self):
        sources = [{"title": "Filing", "url": "https://example.org/news",
                    "content": "Revenue rose", "published_date": "2026-09-22"}]
        with patch.object(stock_research, "_sources", return_value=sources), \
             patch.object(stock_research, "generate_content", return_value=("Tin [1]", [])) as generate:
            answer, citations = stock_research.grounded_news("FPT", "FPT", "Technology")
        self.assertEqual(answer, "Tin [1]")
        self.assertEqual(citations[0]["url"], "https://example.org/news")
        self.assertFalse(generate.call_args.kwargs["search"])
        prompt = generate.call_args.args[0]
        for required in ("Tác động tới", "một kênh tác động cụ thể", "tối đa 900 ký tự"):
            self.assertIn(required, prompt)

    def test_relevance_filters_reject_unrelated_company_and_foreign_macro(self):
        unrelated = {"title": "Tỷ phú trở lại câu lạc bộ USD", "content": "Thị trường quốc tế"}
        direct = {"title": "Vingroup khởi công dự án mới", "content": "Mã VIC"}
        foreign = {"title": "US inflation rises", "content": "Federal Reserve rates"}
        domestic = {"title": "Lãi suất Việt Nam", "content": "Ngân hàng Nhà nước điều hành tín dụng"}
        self.assertFalse(stock_research._direct_company_match(unrelated, "VIC", "Vingroup"))
        self.assertTrue(stock_research._direct_company_match(direct, "VIC", "Vingroup"))
        self.assertFalse(stock_research._macro_match(foreign))
        self.assertTrue(stock_research._macro_match(domestic))

    def test_long_company_names_require_two_distinctive_words(self):
        unrelated = {"title": "Giá nhựa thế giới", "content": "Chi phí nguyên liệu sản xuất"}
        direct = {"title": "Nhựa Tiền Phong công bố kết quả", "content": "Doanh thu tăng"}
        company = "CTCP Nhựa Thiếu niên Tiền Phong"
        self.assertFalse(stock_research._direct_company_match(unrelated, "NTP", company))
        self.assertTrue(stock_research._direct_company_match(direct, "NTP", company))

    def test_rejects_derivative_as_company_news_and_generic_industry_snippet(self):
        warrant = {"title": "CSHB2619: Chứng quyền SHB/LPBS/Call", "content": "SHB"}
        other_bank = {"title": "VietinBank chuẩn bị kế hoạch mới",
                      "content": "Ngành ngân hàng đang tăng trưởng"}
        self.assertFalse(stock_research._direct_company_match(warrant, "SHB", "Ngân hàng SHB"))
        self.assertFalse(stock_research._industry_match(other_bank, "ngân hàng"))
        self.assertFalse(stock_research._direct_company_match(
            {"title": "Tin bất kỳ", "content": ""}, "", ""
        ))

    def test_only_cited_sources_are_shown_with_original_numbers(self):
        sources = [
            {"category": "company", "title": "FPT công bố báo cáo", "url": "https://example.org/1",
             "content": "Doanh thu tăng", "published_date": "2026-09-21"},
            {"category": "industry", "title": "Ngành phần mềm", "url": "https://example.org/2",
             "content": "Nhu cầu tăng", "published_date": "2026-09-21"},
            {"category": "macro", "title": "Lãi suất Việt Nam", "url": "https://example.org/3",
             "content": "Lãi suất ổn định", "published_date": "2026-09-21"},
        ]
        with patch.object(stock_research, "_sources", return_value=sources), \
             patch.object(stock_research, "generate_content", return_value=("Tin ngành [2]", [])):
            _, links = stock_research.grounded_news("FPT", "FPT", "Công nghệ")
            self.assertEqual([(item["id"], item["url"]) for item in links],
                             [(2, "https://example.org/2")])
            _, links = stock_research.investment_research(
                {"signal_from_rule_engine": {"ticker": "FPT"}}, "Tin gì?"
            )
            self.assertEqual([item["id"] for item in links], [2])

    def test_no_sources_still_returns_three_explicit_groups(self):
        with patch.object(stock_research, "_sources", return_value=[]):
            answer, sources = stock_research.grounded_news("FPT", "CTCP FPT", "Công nghệ")
        self.assertEqual(sources, [])
        for label in ("Doanh nghiệp", "Ngành Công nghệ", "Kinh tế Việt Nam"):
            self.assertIn(label, answer)
        self.assertEqual(answer.count("Chưa có tin đủ sát để đánh giá."), 3)

    def test_broad_manufacturing_sector_is_refined_from_company_name(self):
        self.assertEqual(
            stock_research._industry_search_term("Sản xuất", "CTCP Tập đoàn Hoa Sen"),
            "ngành thép",
        )

    def test_sources_keep_at_most_one_result_per_required_group(self):
        def fake_search(query, *, domains=None, category=""):
            templates = {
                "company": {"title": "FPT công bố kết quả", "content": "FPT tăng doanh thu"},
                "industry": {"title": "Ngành công nghệ", "content": "Nhu cầu phần mềm tăng"},
                "macro": {"title": "Lãi suất Việt Nam", "content": "Việt Nam điều hành lãi suất"},
            }
            item = {**templates[category], "url": f"https://example.org/{category}",
                    "published_date": "2026-09-22", "category": category}
            return [item, {**item, "url": item["url"] + "-two"}]
        with patch.object(stock_research, "_search", side_effect=fake_search):
            sources = stock_research._sources("FPT", "CTCP FPT", "Công nghệ")
        self.assertEqual([item["category"] for item in sources], ["company", "industry", "macro"])

    def test_research_without_search_stays_on_internal_data(self):
        with patch.object(stock_research, "_sources", side_effect=RuntimeError("no key")), \
             patch.object(stock_research, "generate_content", return_value=("Nội bộ", [])) as generate:
            answer, sources = stock_research.investment_research(
                {"signal_from_rule_engine": {"ticker": "FPT", "final_action": "WATCH"}}, "Tại sao?")
        self.assertEqual((answer, sources), ("Nội bộ", []))
        self.assertIn("Chỉ dùng dữ liệu nội bộ", generate.call_args.args[0])
        self.assertFalse(generate.call_args.kwargs["search"])

    def test_research_strips_markdown_markers_from_plain_text_report(self):
        with patch.object(stock_research, "_sources", return_value=[]), \
             patch.object(stock_research, "generate_content",
                          return_value=("## Báo cáo\n**Kháng cự:** `67.000`", [])):
            answer, _ = stock_research.investment_research(
                {"signal_from_rule_engine": {"ticker": "FPT", "final_action": "BUY"}},
                "Vì sao BUY?",
            )
        self.assertEqual(answer, "Báo cáo\nKháng cự: 67.000")
        self.assertNotIn("*", answer)

    def test_research_bounds_large_financial_context(self):
        rows = [{"item_code": str(index), "value": index} for index in range(70)]
        context = {"signal_from_rule_engine": {"ticker": "HVN"},
                   "financial_annual_recent": rows,
                   "financial_quarterly_recent": rows}
        with patch.object(stock_research, "_sources", return_value=[]), \
             patch.object(stock_research, "generate_content", return_value=("Ổn", [])) as generate:
            stock_research.investment_research(context, "Tài chính?")
        internal = generate.call_args.args[0].split("Dữ liệu nội bộ: ", 1)[1]
        sent = __import__("json").loads(internal)
        self.assertEqual(len(sent["financial_annual_recent"]), 36)
        self.assertEqual(len(sent["financial_quarterly_recent"]), 36)


if __name__ == "__main__":
    unittest.main()
