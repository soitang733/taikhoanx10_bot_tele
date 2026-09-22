"""Plain-language, source-labelled stock reports built from the local data API."""

from __future__ import annotations

import html
import logging
import math
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from stock_ai_reply import latest_trade, trade_status


ApiGet = Callable[[str, dict[str, Any] | None], dict[str, Any]]
LOGGER = logging.getLogger(__name__)


def number(value: Any, digits: int = 0) -> str:
    try:
        parsed = float(value)
        return f"{parsed:,.{digits}f}" if math.isfinite(parsed) else "chưa có"
    except (TypeError, ValueError):
        return "chưa có"


def percent(value: Any, digits: int = 1) -> str:
    try:
        parsed = float(value)
        return f"{parsed * 100:+,.{digits}f}%" if math.isfinite(parsed) else "chưa có"
    except (TypeError, ValueError):
        return "chưa có"


def vnd_billions(value: Any) -> str:
    try:
        parsed = float(value)
        return f"{parsed / 1_000_000_000:,.1f} tỷ đồng" if math.isfinite(parsed) else "chưa có"
    except (TypeError, ValueError):
        return "chưa có"


def safe_ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        top, bottom = float(numerator), float(denominator)
        return top / bottom if math.isfinite(top) and math.isfinite(bottom) and bottom > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def display_valuation(context: dict[str, Any]) -> dict[str, Any]:
    """Derive a transparent current P/E for display/AI, never for signal scoring."""
    signal = context.get("signal") or {}
    price = context.get("price") or {}
    snapshot = context.get("snapshot") or {}
    direct_pe = safe_ratio(snapshot.get("pe"), 1) or safe_ratio(signal.get("pe"), 1)
    if direct_pe is not None and direct_pe <= 0:
        direct_pe = None
    eod_price = price.get("close") if price.get("close") is not None else signal.get("close")
    eps_ttm = snapshot.get("eps_ttm")
    calculated_pe = safe_ratio(eod_price, eps_ttm) if direct_pe is None else None
    pe_ttm = direct_pe if direct_pe is not None else calculated_pe
    source = ("provider_pe" if direct_pe is not None else
              "latest_eod_close_divided_by_eps_ttm" if calculated_pe is not None else "unavailable")
    return {
        "pe_ttm": pe_ttm,
        "source": source,
        "formula": "latest_eod_close_vnd / eps_ttm_vnd_per_share" if calculated_pe is not None else None,
        "latest_eod_close_vnd": eod_price,
        "price_date": price.get("date") or signal.get("signal_date"),
        "eps_ttm_vnd_per_share": eps_ttm,
        "snapshot_as_of_utc": snapshot.get("as_of_utc"),
        "industry_comparison": context.get("industry_valuation") or {},
        "display_only_not_used_in_fa_signal_or_backtest": True,
    }


def trade_price_vnd(raw_price: Any) -> str:
    """DNSE stock trade prices are quoted in thousand VND per share."""
    try:
        price = Decimal(str(raw_price)) * 1000
        return f"{price:,.0f}" if price.is_finite() and price > 0 else "chưa có"
    except (TypeError, ValueError, InvalidOperation):
        return "chưa có"


def date(value: Any) -> str:
    raw = str(value or "")[:10]
    parts = raw.split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else "chưa có"


def timestamp_vn(value: Any) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "chưa rõ giờ"


def latest_period(rows: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, Any]]]:
    valid = [row for row in rows if row.get("period_end")]
    if not valid:
        return "", {}
    period = max(str(row["period_end"])[:10] for row in valid)
    return period, {str(row.get("item_code")): row for row in valid
                    if str(row["period_end"])[:10] == period}


def analysis_context(context: dict[str, Any]) -> dict[str, Any]:
    """A compact evidence bundle for the optional research assistant."""
    signal_keys = (
        "ticker", "company_name", "exchange", "industry", "signal_date", "final_action",
        "decision_reason", "watch_reason", "fa_status", "fa_coverage", "quality_score",
        "growth_score", "value_score", "safety_score", "fa_score", "ta_status", "ta_score",
        "roe", "roa", "profit_margin", "profit_growth", "revenue_cagr_3y", "pe", "pb",
        "debt_equity", "current_ratio", "close", "volume", "avg_vol20", "volume_ratio",
        "ma20", "ma50", "ma200", "high_close20", "high_close60", "return_3m",
        "return_6m", "return_12m", "trend_pass", "momentum_pass", "breakout_pass",
        "volume_pass", "market_context", "missing_data_reason",
    )
    price_keys = ("date", "open", "high", "low", "close", "adjusted_close", "volume", "source")
    benchmark_keys = ("date", "close", "return_1d", "volume", "source")
    trade_keys = ("time", "matchPrice", "matchQtty", "symbol")
    quarter_date, quarter = latest_period(context.get("quarterly") or [])
    annual_date, annual = latest_period(context.get("annual") or [])
    financial_keys = ("net_income", "revenue", "gross_profit", "operating_cash_flow",
                      "total_assets", "equity", "total_liabilities")
    def financial_summary(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {key: {"value": rows[key].get("value"), "unit": rows[key].get("unit")}
                for key in financial_keys if key in rows}
    return {
        "signal_from_rule_engine": {key: context["signal"].get(key) for key in signal_keys},
        "latest_daily_price": {key: (context.get("price") or {}).get(key) for key in price_keys},
        "latest_quarterly": {"period_end": quarter_date, "items": financial_summary(quarter)},
        "latest_annual": {"period_end": annual_date, "items": financial_summary(annual)},
        "current_financial_snapshot_not_used_in_signal": context.get("snapshot") or {},
        "display_valuation_not_used_in_signal": display_valuation(context),
        "vnindex": {key: (context.get("benchmark") or {}).get(key) for key in benchmark_keys},
        "corporate_actions": (context.get("actions") or [])[:3],
        "latest_trade_raw": {key: (context.get("trade") or {}).get(key) for key in trade_keys},
        "trade_freshness": context.get("trade_note"),
        "dnse_trade_price_unit": "thousand_vnd_per_share",
        "latest_trade_price_vnd": trade_price_vnd((context.get("trade") or {}).get("matchPrice")),
        "dnse_trade_quantity_unit_verified": False,
    }


def load_context(ticker: str, api_get: ApiGet, *, include_trade: bool = True) -> dict[str, Any]:
    ticker = ticker.upper()
    missing_sections: list[str] = []
    def optional(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return api_get(path, params)
        except Exception as exc:
            LOGGER.warning("Optional stock data unavailable: %s %s: %s", ticker, path, type(exc).__name__)
            missing_sections.append(path)
            return {}
    context = {
        "ticker": ticker,
        "signal": api_get("/v1/signal", {"ticker": ticker})["data"],
        "price": optional("/v1/latest", {"ticker": ticker}).get("data") or {},
        "quarterly": optional("/v1/financials", {"ticker": ticker, "period": "quarterly", "limit": 120}).get("data") or [],
        "annual": optional("/v1/financials", {"ticker": ticker, "period": "annual", "limit": 120}).get("data") or [],
        "snapshot": optional("/v1/snapshot", {"ticker": ticker}).get("data") or {},
        "industry_valuation": optional("/v1/industry-valuation", {"ticker": ticker}).get("data") or {},
        "benchmark": optional("/v1/benchmark", None).get("data") or {},
        "actions": optional("/v1/actions", {"ticker": ticker, "limit": 3}).get("data") or [],
        "unavailable_sections": missing_sections,
    }
    context["trade"] = None
    context["trade_note"] = "Chưa lấy được giao dịch gần nhất từ nhà cung cấp."
    if include_trade:
        try:
            context["trade"] = latest_trade(ticker)
            context["trade_note"] = trade_status(context["trade"])
        except Exception:
            pass
    return context


ACTION_LABELS = {
    "BUY": "🟢 MUA — Đang đạt điều kiện của bộ lọc",
    "WATCH": "🟡 WATCH — Chưa đủ điều kiện phát tín hiệu",
    "HOLD": "🔵 HOLD — Tiếp tục theo dõi vị thế đang nắm giữ",
    "EXIT": "🔴 EXIT — Đã chạm điều kiện thoát của mô hình",
    "DATA_REVIEW": "⚪ Cần kiểm tra dữ liệu trước khi đánh giá",
}

WATCH_REASONS = {
    "ILLIQUID": "thanh khoản chưa đáp ứng ngưỡng của hệ thống",
    "FA_HARD_REJECT": "không qua điều kiện loại trừ cứng của phân tích cơ bản",
    "SCORE_BELOW_ENTRY": "tổng hợp các điều kiện chưa đạt ngưỡng vào lệnh",
    "STALE_PRICE": "mã chưa có giá cập nhật cùng phiên với thị trường",
    "MISSING_TA": "thiếu dữ liệu giá để kiểm tra xu hướng",
}

ACTION_TYPES = {
    "stock_dividend": "trả cổ tức bằng cổ phiếu",
    "cash_dividend": "trả cổ tức tiền mặt",
    "bonus_shares": "thưởng cổ phiếu",
    "stock_split": "chia tách cổ phiếu",
    "rights_issue": "quyền mua cổ phiếu",
}


def render_report(context: dict[str, Any], section: str = "stock") -> str:
    signal = context["signal"]
    price = context.get("price") or {}
    benchmark = context.get("benchmark") or {}
    quarter_date, quarter = latest_period(context.get("quarterly") or [])
    annual_date, annual = latest_period(context.get("annual") or [])
    snapshot = context.get("snapshot") or {}
    ticker = html.escape(str(context["ticker"]))
    name = html.escape(str(signal.get("company_name") or ""))
    exchange = html.escape(str(signal.get("exchange") or ""))
    industry = html.escape(str(signal.get("industry") or "Chưa phân loại"))
    action = str(signal.get("final_action") or "DATA_REVIEW")
    heading = f"<b>🏢 {ticker} — {name}</b>\n{exchange} · {industry}\n"
    action_label = ACTION_LABELS.get(action, action)
    heading += f"<b>📡 Tín hiệu: {action_label}</b>\n"
    lines = [heading]

    if section in {"stock", "price"}:
        trade = context.get("trade")
        if trade:
            latest_price = trade_price_vnd(trade.get("matchPrice"))
            if latest_price != "chưa có":
                note = html.escape(str(context.get("trade_note") or ""))
                live_label = "⚡ GIÁ GẦN THỜI GIAN THỰC" if "trong 15 phút gần đây" in note else "🕒 GIAO DỊCH GẦN NHẤT"
                lines.extend([f"<b>{live_label}</b>", f"<b>{latest_price} đồng/cp</b> · {note}",
                              "<i>Giá khớp DNSE, chưa điều chỉnh sự kiện doanh nghiệp.</i>"])
        else:
            lines.append("<b>🕒 Giao dịch gần nhất:</b> " + html.escape(str(context.get("trade_note") or "")))
        eod_day = date(price.get("date") or signal.get("signal_date"))
        lines.extend([
            f"\n<b>💹 PHIÊN GẦN NHẤT · {eod_day}</b>",
            f"• Đóng cửa: <b>{number(price.get('close') or signal.get('close'))} đồng/cp</b>",
            f"• Cao nhất: {number(price.get('high'))} đồng",
            f"• Thấp nhất: {number(price.get('low'))} đồng",
            f"• Khối lượng: {number(price.get('volume') or signal.get('volume'))} cp",
            f"• So với bình quân 20 phiên: {number(signal.get('volume_ratio'), 2)} lần",
            f"• Hiệu suất 3 tháng: {percent(signal.get('return_3m'))}",
            f"• Hiệu suất 6 tháng: {percent(signal.get('return_6m'))}",
            f"• Hiệu suất 12 tháng: {percent(signal.get('return_12m'))}",
        ])

    if section in {"stock", "fa"}:
        lines.append("\n<b>🏦 SỨC KHỎE DOANH NGHIỆP</b>")
        net_margin = safe_ratio(snapshot.get("net_income_ttm"), snapshot.get("revenue_ttm"))
        valuation_display = display_valuation(context)
        indicative_pe = valuation_display.get("pe_ttm")
        profitability = []
        for label, value, formatter in (
            ("ROE", signal.get("roe"), percent), ("ROA", snapshot.get("roa_ttm") or signal.get("roa"), percent),
            ("Biên ròng", net_margin if net_margin is not None else signal.get("profit_margin"), percent),
            ("Tăng trưởng LN", signal.get("profit_growth"), percent),
        ):
            if value is not None:
                profitability.append(f"• {label}: {formatter(value)}")
        if profitability:
            lines.extend(profitability)
        scale = []
        for label, value in (("Doanh thu 12T", snapshot.get("revenue_ttm")),
                             ("LNST 12T", snapshot.get("net_income_ttm")),
                             ("Vốn hóa", snapshot.get("market_cap"))):
            if value is not None:
                scale.append(f"• {label}: {vnd_billions(value)}")
        if scale:
            lines.extend(scale)
        valuation = []
        for label, value, formatter in (
            ("P/E TTM", indicative_pe, lambda x: number(x, 1)),
            ("P/B", signal.get("pb"), lambda x: number(x, 1)),
            ("Nợ/VCSH", snapshot.get("debt_equity_ratio") or signal.get("debt_equity"), lambda x: number(x, 2)),
            ("EPS 12T", snapshot.get("eps_ttm"), lambda x: number(x)),
        ):
            if value is not None:
                suffix = " đồng/cp" if label == "EPS 12T" else ""
                valuation.append(f"• {label}: {formatter(value)}{suffix}")
        if valuation:
            lines.extend(valuation)
        if quarter:
            quarter_values = []
            for label, key in (("LNST", "net_income"), ("Dòng tiền KD", "operating_cash_flow"),
                               ("Vốn chủ", "equity"), ("Tổng tài sản", "total_assets")):
                value = (quarter.get(key) or {}).get("value")
                if value is not None:
                    quarter_values.append(f"{label} {vnd_billions(value)}")
            if quarter_values:
                lines.append(f"\n<b>📄 Báo cáo kỳ {date(quarter_date)}</b>")
                lines.extend(f"• {value}" for value in quarter_values)
        elif annual:
            lines.append(f"• Báo cáo gần nhất: {date(annual_date)}")
        if not profitability and not scale and not valuation and not quarter and not annual:
            lines.append("• Chưa có báo cáo tài chính đủ tin cậy để trình bày.")
        coverage = signal.get("fa_coverage")
        if coverage is not None and float(coverage) < 0.6:
            lines.extend([
                "\n<b>⚠️ GIỚI HẠN DỮ LIỆU</b>",
                f"• Độ phủ tài chính lịch sử: {percent(coverage).lstrip('+')}",
                "• Ý nghĩa: tín hiệu hiện chịu ảnh hưởng chủ yếu từ giá và thanh khoản.",
                "• Số liệu doanh nghiệp được dùng để hỗ trợ đánh giá rủi ro.",
            ])

    if section in {"stock", "ta"}:
        close = signal.get("close")
        ma20, ma50, ma200 = (signal.get("ma20"), signal.get("ma50"), signal.get("ma200"))
        comparisons = []
        for label, moving_average in (("20", ma20), ("50", ma50), ("200", ma200)):
            if close is not None and moving_average is not None:
                comparisons.append(f"• {'✅ Trên' if float(close) > float(moving_average) else '❌ Dưới'} MA{label}")
        lines.append("\n<b>📈 XU HƯỚNG & DÒNG TIỀN</b>")
        if comparisons:
            lines.extend(comparisons)
        averages = [("MA20", ma20), ("MA50", ma50), ("MA200", ma200)]
        available_averages = [f"{label} {number(value)}" for label, value in averages if value is not None]
        if available_averages:
            lines.append("\n<b>Các đường trung bình</b>")
            lines.extend(f"• {value} đồng" for value in available_averages)
        rule_checks = []
        for key, label in (("momentum_pass", "động lượng 3/6/12 tháng"),
                           ("rs_pass", "sức mạnh tương đối 6 tháng so với VN-Index"),
                           ("trend_pass", "cấu trúc giá trên MA50 và MA200"),
                           ("breakout_pass", "vượt đỉnh đóng cửa 20 phiên trước"),
                           ("volume_pass", "khối lượng đạt tối thiểu 0,8 lần TB20")):
            value = signal.get(key)
            if value is not None:
                rule_checks.append(f"• {'✅' if bool(value) else '❌'} {label.capitalize()}")
        if rule_checks:
            lines.append("\n<b>Điều kiện kỹ thuật</b>")
            lines.extend(rule_checks)

    if section == "stock":
        lines.extend([
            "\n<b>🌐 THỊ TRƯỜNG & GIẢI THÍCH TÍN HIỆU</b>",
            (f"VN-Index {number(benchmark.get('close'), 2)} điểm "
             f"({percent(benchmark.get('return_1d'), 2)} phiên {date(benchmark.get('date'))}).")
            if benchmark else "Chưa có chỉ số thị trường cùng kỳ trong kho.",
        ])
        watch = signal.get("watch_reason")
        if action == "BUY":
            fa_score = signal.get("fa_score")
            fa_coverage = signal.get("fa_coverage")
            effective_fa = signal.get("effective_fa")
            group_parts = []
            for label, key in (("chất lượng", "quality_score"), ("tăng trưởng", "growth_score"),
                               ("định giá", "value_score"), ("an toàn", "safety_score")):
                if signal.get(key) is not None:
                    group_parts.append(f"{label} {number(signal.get(key), 1)}")
            group_text = ", ".join(group_parts) if group_parts else "chưa đủ dữ liệu để tách bốn nhóm"
            momentum_detail = ", ".join(percent(signal.get(key)) for key in ("r_3m", "r_6m", "r_12m"))
            lines.extend([
                "\n<b>✅ Vì sao có tín hiệu MUA?</b>",
                f"• <b>Kết luận:</b> điểm thống nhất {number(signal.get('unified_score'), 1)}/100 đạt ngưỡng 60 "
                f"và mã đứng hạng {number(signal.get('unified_rank'))}, nằm trong giới hạn top 30 ứng viên.",
                f"• <b>FA:</b> điểm thô {number(fa_score, 1)}/100, độ phủ "
                f"{percent(fa_coverage).lstrip('+')}, FA hiệu dụng {number(effective_fa, 1)}/100; "
                f"các nhóm hiện có gồm {group_text}. FA hiệu dụng đóng góp 20% vào điểm chung. "
                "Mốc FA 65 là chỉ báo chẩn đoán, không phải điều kiện MUA độc lập.",
                f"• <b>TA — động lượng:</b> tỷ suất 3/6/12 tháng lần lượt là {momentum_detail}; "
                f"điều kiện {'đạt' if signal.get('momentum_pass') else 'chưa đạt'} vì chỉ đạt khi cả ba kỳ đều dương.",
                f"• <b>TA — sức mạnh tương đối:</b> cổ phiếu hơn VN-Index trong 6 tháng "
                f"{percent(signal.get('rs_6m'))}; điều kiện {'đạt' if signal.get('rs_pass') else 'chưa đạt'}.",
                f"• <b>TA — xu hướng:</b> giá {number(signal.get('close'))}, MA50 {number(signal.get('ma50'))}, "
                f"MA200 {number(signal.get('ma200'))} đồng/cp; cấu trúc giá &gt; MA50 &gt; MA200 "
                f"{'đạt' if signal.get('trend_pass') else 'chưa đạt'}.",
                f"• <b>TA — breakout:</b> giá so với đỉnh đóng cửa 20 phiên trước "
                f"{number(signal.get('high_close20'))} đồng/cp; điều kiện "
                f"{'đạt' if signal.get('breakout_pass') else 'chưa đạt'}.",
                f"• <b>TA — khối lượng:</b> phiên hiện tại bằng {number(signal.get('volume_ratio'), 2)} lần TB20, "
                f"so với ngưỡng 0,8 lần; điều kiện {'đạt' if signal.get('volume_pass') else 'chưa đạt'}. "
                f"Tổng điểm TA là {number(signal.get('ta_score'), 1)}/100 và đóng góp 80% vào điểm chung.",
                f"• <b>Cổng dữ liệu và thanh khoản:</b> TA {html.escape(str(signal.get('ta_status') or 'chưa rõ'))}; "
                f"giá {'còn mới' if signal.get('price_fresh') else 'chưa xác nhận còn mới'}; giá trị giao dịch "
                f"bình quân 20 phiên {vnd_billions(signal.get('avg_trading_value_20d'))}, yêu cầu tối thiểu 2 tỷ đồng/phiên.",
                f"• <b>Cổng thị trường:</b> VN-Index {'ở trên hoặc bằng MA50, cho phép mua mới' if signal.get('market_bull') else 'dưới MA50, không cho phép mua mới'}.",
                "• <b>Cách hiểu đúng:</b> mô hình không bắt buộc FA đạt 65 hoặc đủ cả năm thành phần TA. "
                "Tín hiệu MUA chỉ xuất hiện khi điểm thống nhất và toàn bộ điều kiện bắt buộc cùng đạt; đây vẫn là tín hiệu "
                "định lượng tại ngày chốt dữ liệu, không phải yêu cầu mua bằng mọi giá.",
            ])
        elif action == "EXIT":
            reason_codes = {
                "PRICE_BELOW_MA": "giá đã xuống dưới MA200",
                "TRAILING_STOP_60D": "giá đã giảm hơn 15% từ đỉnh đóng cửa 60 phiên",
                "SCORE_BELOW_EXIT": "điểm thống nhất đã xuống dưới 35/100",
                "MARKET_REGIME": "thị trường đã vi phạm bộ lọc xu hướng",
            }
            reasons = [reason_codes.get(code, code) for code in str(signal.get("exit_reason_code") or "").split("|")
                       if code and code != "NONE"]
            lines.extend([
                "\n<b>🔴 Vì sao có tín hiệu BÁN / EXIT?</b>",
                "• " + ("; ".join(reasons) if reasons else html.escape(str(signal.get("decision_reason") or "Đã chạm điều kiện thoát."))),
                f"• Vị thế mô hình đã được giữ {number(signal.get('holding_sessions'), 0)} phiên.",
                "• Tín hiệu được xác định sau EOD và lệnh bán được giả định thực hiện ở phiên kế tiếp.",
            ])
        elif action == "HOLD":
            lines.append("Tiếp tục nắm giữ. " + html.escape(str(signal.get("decision_reason") or "Chưa chạm điều kiện thoát.")))
        elif watch and str(watch) != "NONE":
            reason = WATCH_REASONS.get(str(watch), "còn điều kiện chưa đạt theo bộ quy tắc")
            lines.append("Hiện ở danh sách theo dõi vì " + reason + ".")
        else:
            lines.append("Tín hiệu do bộ quy tắc hiện tại tính từ dữ liệu giá, thanh khoản và tài chính trong kho.")
        actions = context.get("actions") or []
        if actions:
            item = actions[0]
            action_type = str(item.get("action_type") or "")
            lines.append(f"Sự kiện doanh nghiệp gần nhất trong kho: {ACTION_TYPES.get(action_type, 'sự kiện chưa phân loại')} "
                         f"ngày {date(item.get('ex_date'))}.")
        if context.get("unavailable_sections"):
            lines.append("Một số mục bổ sung chưa tải được; báo cáo chỉ sử dụng dữ liệu đã xác nhận.")
        benchmark_day = str(benchmark.get("date") or "")[:10]
        signal_day = str(signal.get("signal_date") or "")[:10]
        if benchmark_day and signal_day and signal_day < benchmark_day:
            lines.append("Lưu ý: dữ liệu giá của mã cũ hơn phiên thị trường gần nhất; "
                         "không dùng tín hiệu này như tín hiệu mới.")

    closing_note = "Dữ liệu hỗ trợ ra quyết định, không phải khuyến nghị đầu tư cá nhân."
    lines.append("\n<i>" + closing_note + "</i>")
    return "\n".join(lines)
