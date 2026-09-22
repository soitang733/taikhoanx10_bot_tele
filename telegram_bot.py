"""Telegram interface for the local stock-analysis API."""

from __future__ import annotations

import html
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from app_security import configured_rate_limiter
from stock_ai_reply import AIServiceError, generate_content
from stock_research import grounded_news, investment_research
import stock_local_store
from stock_report import ACTION_LABELS, analysis_context, date, load_context, number, percent, render_report

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


LOGGER = logging.getLogger("telegram_stock_bot")
PENDING_INPUT: dict[int, tuple[str, str]] = {}
API_RETRY_AFTER = 0.0
API_BASE = os.getenv("STOCK_DATA_API", "http://127.0.0.1:8765").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
PUBLIC_ACCESS = os.getenv("TELEGRAM_PUBLIC_ACCESS", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
UPDATE_WORKERS = min(8, max(1, int(os.getenv("TELEGRAM_UPDATE_WORKERS", "4"))))
UPDATE_POOL = ThreadPoolExecutor(max_workers=UPDATE_WORKERS, thread_name_prefix="telegram-update")
CHAT_LOCKS: dict[int, Lock] = {}
CHAT_LOCKS_GUARD = Lock()
STATE_DB = Path(__file__).resolve().parent / "analysis_data" / "telegram_state.sqlite"
AI_RATE_LIMITER = configured_rate_limiter()
AI_MINUTE_LIMIT = int(os.getenv("AI_RATE_LIMIT_PER_MINUTE", "5"))
AI_DAILY_LIMIT = int(os.getenv("AI_RATE_LIMIT_PER_DAY", "30"))


class DeferredNewsReply(str):
    """A normal Telegram reply carrying an internal request to deliver news afterward."""

    def __new__(cls, value: str, ticker: str):
        instance = super().__new__(cls, value)
        instance.ticker = ticker
        return instance


def chat_is_allowed(chat_id: int) -> bool:
    """Allow public use when enabled while retaining an optional private allowlist mode."""
    return chat_id > 0 and (PUBLIC_ACCESS or chat_id in ALLOWED_CHAT_IDS)


def _state_connection() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATE_DB, timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS daily_subscriptions ("
        "chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, "
        "created_at_utc TEXT NOT NULL, last_sent_date TEXT)"
    )
    return connection


def set_daily_subscription(chat_id: int, enabled: bool) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(_state_connection()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO daily_subscriptions(chat_id,enabled,created_at_utc) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
                (chat_id, int(enabled), now),
            )


def daily_subscription_enabled(chat_id: int) -> bool:
    with closing(_state_connection()) as connection:
        row = connection.execute(
            "SELECT enabled FROM daily_subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row and row[0])


def due_daily_subscribers(day: str) -> list[int]:
    with closing(_state_connection()) as connection:
        return [int(row[0]) for row in connection.execute(
            "SELECT chat_id FROM daily_subscriptions "
            "WHERE enabled=1 AND COALESCE(last_sent_date,'')<>?", (day,)
        ).fetchall() if chat_is_allowed(int(row[0]))]


def mark_daily_sent(chat_id: int, day: str) -> None:
    with closing(_state_connection()) as connection:
        with connection:
            connection.execute(
                "UPDATE daily_subscriptions SET last_sent_date=? WHERE chat_id=?", (day, chat_id)
            )


@contextmanager
def single_instance():
    """Prevent competing Telegram long-pollers after a Windows task restart."""
    if os.name != "nt":
        yield
        return
    import msvcrt

    lock_path = Path(__file__).resolve().parent / "analysis_data" / "telegram_bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Telegram bot already running") from None
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global API_RETRY_AFTER
    local_api = urlparse(API_BASE).hostname in {"127.0.0.1", "localhost", "::1"}
    if local_api and time.monotonic() < API_RETRY_AFTER:
        return stock_local_store.get(path, params)
    try:
        response = requests.get(f"{API_BASE}{path}", params=params, timeout=4)
        response.raise_for_status()
        API_RETRY_AFTER = 0.0
        return response.json()
    except (requests.ConnectionError, requests.Timeout):
        if not local_api:
            raise
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if not local_api or (status == 404 and path not in {"/v1/benchmark", "/v1/actions", "/v1/snapshot"}) or (status < 500 and status != 404):
            raise
        if status == 404:
            return stock_local_store.get(path, params)
    LOGGER.warning("Local stock API unavailable for %s; using read-only SQLite", path)
    API_RETRY_AFTER = time.monotonic() + 30.0
    return stock_local_store.get(path, params)


def fmt_number(value: Any, decimals: int = 1) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"


def source_links(sources: list[dict[str, str]]) -> str:
    links = []
    for index, source in enumerate(sources[:4], 1):
        url = str(source.get("url") or "")
        if urlparse(url).scheme != "https":
            continue
        label = html.escape(str(source.get("title") or "Nguồn tham khảo")[:100])
        citation_id = source.get("id", index)
        links.append(f'[{citation_id}] <a href="{html.escape(url, quote=True)}">{label}</a>')
    return "\n\n<b>🔗 NGUỒN KIỂM CHỨNG</b>\n" + "\n".join(links) if links else ""


def clean_analysis_text(value: str) -> str:
    """Keep model text readable in Telegram HTML mode."""
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = value.replace("*", "").replace("`", "")
    return value.strip()


def saved_market_note(context: dict[str, Any]) -> str:
    benchmark = context.get("benchmark") or {}
    if not benchmark.get("date"):
        return ""
    return (f"\nBối cảnh thị trường đã lưu: VN-Index {number(benchmark.get('close'), 2)} điểm "
            f"({percent(benchmark.get('return_1d'), 2)} phiên {date(benchmark.get('date'))}).")


def news_section(context: dict[str, Any]) -> str:
    signal = context["signal"]
    try:
        brief, sources = grounded_news(
            context["ticker"], str(signal.get("company_name") or ""),
            str(signal.get("sector") or signal.get("industry") or ""),
        )
        return "\n\n<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>\n\n" + html.escape(clean_analysis_text(brief)) + source_links(sources)
    except AIServiceError as exc:
        LOGGER.info("AI news research unavailable for %s: HTTP %s %s", context["ticker"],
                    exc.status_code, exc.provider_status)
        reason = ("Hạn mức tra cứu tin của AI đã hết trên các khóa hiện có" if exc.status_code == 429 else
                  "Tài khoản AI chưa được cấp quyền tra cứu" if exc.status_code in {401, 403} else
                  "Model AI được cấu hình hiện không khả dụng" if exc.status_code == 404 else
                  "Dịch vụ tra cứu tin đang gián đoạn")
        return ("\n\n<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>\n" + reason
                + ". Chưa có tin mới được kiểm chứng để đưa vào báo cáo."
                + saved_market_note(context))
    except Exception as exc:
        LOGGER.info("News research unavailable for %s: %s", context["ticker"], type(exc).__name__)
        return ("\n\n<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>\n"
                "Chưa thể truy xuất nguồn tin mới lúc này; báo cáo không suy đoán thông tin ngoài dữ liệu đã lưu."
                + saved_market_note(context))


def detailed_answer(context: dict[str, Any], question: str) -> str:
    ticker = html.escape(str(context["ticker"]))
    try:
        answer, sources = investment_research(analysis_context(context), question)
        source_note = (source_links(sources) if sources else
                       "\nChưa xác minh được tin mới từ nguồn công khai trong lượt tra cứu này.")
        return (f"<b>{ticker} — Phân tích chuyên sâu</b>\n"
                + html.escape(clean_analysis_text(answer)[:3150]) + source_note
                + f"\n\n<i>Dữ liệu giá đến {date(context['signal'].get('signal_date'))}; "
                "tin ngoài hệ thống chỉ được nêu khi có nguồn.</i>")
    except Exception as exc:
        LOGGER.warning("Research unavailable for %s: %s", context["ticker"], type(exc).__name__)
        if re.search(r"(?i)(?:p\s*/\s*e|\bpe\b|định giá)", question):
            valuation = analysis_context(context).get("display_valuation_not_used_in_signal") or {}
            pe_ttm = valuation.get("pe_ttm")
            industry = valuation.get("industry_comparison") or {}
            median_pe = industry.get("median_pe_ttm")
            peer_count = industry.get("peer_count")
            if pe_ttm is not None:
                lines = [
                    f"<b>{ticker} — P/E TTM hiện tại</b>",
                    f"P/E TTM ước tính là <b>{number(pe_ttm, 1)} lần</b>, tính bằng giá đóng cửa EOD "
                    f"{number(valuation.get('latest_eod_close_vnd'))} đồng/cp chia cho EPS TTM "
                    f"{number(valuation.get('eps_ttm_vnd_per_share'))} đồng/cp.",
                ]
                if median_pe is not None and peer_count:
                    difference = float(pe_ttm) / float(median_pe) - 1
                    direction = "cao hơn" if difference >= 0 else "thấp hơn"
                    lines.append(
                        f"Trung vị P/E của {number(peer_count, 0)} doanh nghiệp thuộc nhóm "
                        f"{html.escape(str(industry.get('classification') or 'cùng ngành'))} là "
                        f"<b>{number(median_pe, 1)} lần</b>. P/E của {ticker} đang {direction} khoảng "
                        f"{percent(abs(difference)).lstrip('+')} so với trung vị nhóm."
                    )
                lines.append(
                    "Đây là phép tính phục vụ hiển thị từ dữ liệu hiện tại, không được dùng để sửa điểm FA, "
                    "tín hiệu MUA/BÁN hoặc kết quả backtest. P/E cao hay thấp chưa tự nó kết luận cổ phiếu đắt/rẻ; "
                    "cần xem thêm chất lượng lợi nhuận, tăng trưởng và đặc thù dự án."
                )
                return "\n\n".join(lines)
        return (f"<b>{ticker} — Báo cáo dữ liệu hiện có</b>\n"
                "Phân tích nâng cao và tin mới tạm gián đoạn. Dưới đây là số liệu đã lưu:\n\n"
                + render_report(context, "stock"))


def signal_reason(row: dict[str, Any]) -> str:
    result = lambda value: "Đạt" if value else "Chưa đạt"
    try:
        trading_value = f"{float(row.get('avg_trading_value_20d')) / 1_000_000_000:,.2f} tỷ đồng/phiên"
    except (TypeError, ValueError):
        trading_value = "chưa có dữ liệu"
    return "\n".join([
        f"Điểm phân tích cơ bản: {number(row.get('fa_score'), 1)}/100. "
        f"Độ phủ dữ liệu tài chính: {percent(row.get('fa_coverage')).lstrip('+')}.",
        f"Điểm phân tích kỹ thuật: {number(row.get('ta_score'), 1)}/100.",
        f"Động lượng giá: {result(row.get('momentum_pass'))}. "
        f"Cổ phiếu tăng {percent(row.get('return_3m'))} trong 3 tháng, "
        f"{percent(row.get('return_6m'))} trong 6 tháng và "
        f"{percent(row.get('return_12m'))} trong 12 tháng.",
        f"Sức mạnh so với VN-Index: {result(row.get('rs_pass'))}. "
        f"Trong 6 tháng, cổ phiếu cao hơn VN-Index {percent(row.get('relative_strength_6m'))}.",
        f"Xu hướng trung và dài hạn: {result(row.get('trend_pass'))}. "
        f"Giá hiện tại {number(row.get('close'))} đồng; đường trung bình 50 phiên "
        f"{number(row.get('ma50'))} đồng; đường trung bình 200 phiên {number(row.get('ma200'))} đồng.",
        f"Vượt đỉnh 20 phiên: {result(row.get('breakout_pass'))}. "
        f"Đỉnh đóng cửa của 20 phiên trước là {number(row.get('high_close20'))} đồng.",
        f"Khối lượng phiên gần nhất: {result(row.get('volume_pass'))}. "
        f"Khối lượng bằng {number(row.get('volume_ratio'), 2)} lần mức bình quân 20 phiên; "
        "ngưỡng yêu cầu là 0,80 lần.",
        f"Thanh khoản bình quân 20 phiên: {trading_value}. "
        f"Dữ liệu giá: {'đã cập nhật đến phiên mới nhất' if row.get('price_fresh') else 'chưa cập nhật đến phiên mới nhất'}. "
        f"Bối cảnh VN-Index: {'đang ở trên hoặc bằng đường trung bình 50 phiên' if row.get('market_bull') else 'đang ở dưới đường trung bình 50 phiên'}.",
        f"Thứ hạng trong bộ lọc: {number(row.get('unified_rank'), 0)}. "
        "Mã này được phát tín hiệu MUA vì đã vượt qua toàn bộ điều kiện bắt buộc của mô hình.",
    ])


def render_signal_list(rows: list[dict[str, Any]], *, daily: bool = False) -> str:
    title = "DANH SÁCH MUA MỚI" if daily else "TÍN HIỆU MUA"
    lines = [f"<b>{title}</b>"]
    if daily:
        lines.append("Các mã đạt điều kiện sau phiên gần nhất. Chọn mã để xem toàn bộ FA, TA và tin liên quan:")
    else:
        lines.extend([
            f"Có <b>{len(rows)} mã</b> đạt bộ lọc tại phiên gần nhất.",
            "Chọn một mã ở bàn phím bên dưới để xem chi tiết điều kiện FA, TA, giá, thanh khoản và tin đã kiểm chứng.",
        ])
    if not rows:
        lines.append("Hiện chưa có mã nào phát tín hiệu MUA.")
        return "\n".join(lines)
    for index, row in enumerate(rows, 1):
        ticker = html.escape(str(row.get("ticker") or ""))
        lines.append(
            f"{index}. <b>{ticker}</b> · {number(row.get('close'))}đ · "
            f"điểm {number(row.get('unified_score'), 1)} · 6 tháng {percent(row.get('return_6m'))}"
        )
    return "\n".join(lines)


def render_market_summary(benchmark: dict[str, Any]) -> str:
    """Summarize the latest completed VN-Index session; fall back safely if AI is unavailable."""
    session_date = date(benchmark.get("date"))
    close = benchmark.get("close")
    change = benchmark.get("return_1d")
    try:
        previous_close = float(close) / (1.0 + float(change))
        intraday_range = (float(benchmark.get("high")) - float(benchmark.get("low"))) / previous_close
        close_location = ((float(close) - float(benchmark.get("low"))) /
                          (float(benchmark.get("high")) - float(benchmark.get("low"))))
    except (TypeError, ValueError, ZeroDivisionError):
        previous_close = intraday_range = close_location = None
    facts = (
        f"Phiên {session_date}: mở cửa {number(benchmark.get('open'), 2)} điểm; "
        f"cao nhất {number(benchmark.get('high'), 2)}; thấp nhất {number(benchmark.get('low'), 2)}; "
        f"đóng cửa {number(close, 2)}; thay đổi {percent(change, 2)}; "
        f"khối lượng {number(benchmark.get('volume'), 0)} cổ phiếu; "
        f"đóng cửa trước ước tính {number(previous_close, 2)}; biên độ trong phiên {percent(intraday_range, 2)}; "
        f"vị trí đóng cửa trong biên độ {percent(close_location, 1)}."
    )
    try:
        prompt = (
            "Bạn là trợ lý tổng hợp thị trường chứng khoán Việt Nam. Chỉ dùng dữ liệu phiên VN-Index "
            "được cung cấp, không bịa độ rộng thị trường, giá trị giao dịch, khối ngoại, nguyên nhân hay tin tức. "
            "Viết tiếng Việt không Markdown, 4 câu ngắn: diễn biến chính; biến động trong phiên; ý nghĩa vị trí "
            "đóng cửa; điều cần theo dõi ở phiên kế tiếp. Không đưa khuyến nghị mua bán. Dữ liệu: " + facts
        )
        answer, _ = generate_content(prompt, search=False, max_tokens=500)
        commentary = clean_analysis_text(answer)
    except Exception as exc:
        LOGGER.info("VN-Index AI summary unavailable: %s", type(exc).__name__)
        location = ("gần mức cao nhất phiên" if close_location is not None and close_location >= .8 else
                    "gần mức thấp nhất phiên" if close_location is not None and close_location <= .2 else
                    "ở vùng giữa biên độ phiên")
        commentary = (f"VN-Index đóng cửa {percent(change, 2)} so với phiên trước và {location}. "
                      f"Biên độ cao–thấp trong phiên tương đương {percent(intraday_range, 2)}. "
                      "Cần theo dõi khả năng duy trì đà giá và thanh khoản trong phiên kế tiếp.")
    return (f"<b>🌐 VN-INDEX · PHIÊN {session_date}</b>\n"
            f"Đóng cửa <b>{number(close, 2)} điểm</b> · {percent(change, 2)}\n"
            f"Mở {number(benchmark.get('open'), 2)} · Cao {number(benchmark.get('high'), 2)} · "
            f"Thấp {number(benchmark.get('low'), 2)} · Khối lượng {number(benchmark.get('volume'), 0)} cp\n\n"
            f"<b>AI tổng hợp</b>\n{html.escape(commentary)}\n\n"
            "<i>Nguồn số liệu: DNSE, phiên hoàn tất gần nhất. AI chỉ diễn giải các số liệu trên.</i>")


def render_exit_list(rows: list[dict[str, Any]], model: dict[str, Any] | None = None) -> str:
    lines = ["<b>🔴 DANH SÁCH BÁN / EXIT</b>"]
    if not rows:
        model = model or {}
        position_count = len(model.get("positions") or [])
        pending_buy_count = sum(
            str(order.get("side") or "").upper() == "BUY"
            for order in (model.get("pending_orders") or [])
        )
        if position_count:
            detail = (f"Danh mục hiện có {position_count} vị thế, nhưng chưa vị thế nào chạm điều kiện BÁN "
                      "trong phiên này.")
        elif pending_buy_count:
            detail = (f"Chưa có tín hiệu BÁN vì danh mục mô hình chưa khớp vị thế. Hiện có "
                      f"{pending_buy_count} lệnh MUA đang chờ phiên kế tiếp; hệ thống chỉ phát BÁN cho mã "
                      "đã thực sự nằm trong danh mục.")
        else:
            detail = "Danh mục mô hình chưa có vị thế nên chưa thể phát tín hiệu BÁN."
        return "\n".join(lines + [detail])
    lines.append(
        "Các mã dưới đây đang thuộc danh mục mô hình và vừa chạm điều kiện thoát. "
        "Tín hiệu hình thành sau EOD; lệnh được giả định thực hiện ở phiên kế tiếp."
    )
    labels = {
        "PRICE_BELOW_MA": "giá xuống dưới MA200",
        "TRAILING_STOP_60D": "giá giảm quá 15% từ đỉnh đóng cửa 60 phiên",
        "SCORE_BELOW_EXIT": "điểm thống nhất xuống dưới 35",
        "MARKET_REGIME": "thị trường vi phạm bộ lọc xu hướng",
    }
    for index, row in enumerate(rows, 1):
        reasons = [labels.get(code, code) for code in str(row.get("exit_reason_code") or "").split("|")
                   if code and code != "NONE"]
        lines.append(
            f"\n{index}. <b>{html.escape(str(row.get('ticker') or ''))}</b> — "
            f"giá {number(row.get('close'))} đồng/cổ phiếu · điểm {number(row.get('unified_score'), 1)}/100\n"
            f"Lý do bán: {html.escape('; '.join(reasons) or 'đã chạm điều kiện thoát')}. "
            f"Đã nắm giữ {number(row.get('holding_sessions'), 0)} phiên."
        )
    return "\n".join(lines)


def daily_digest() -> str:
    benchmark = api_get("/v1/benchmark").get("data") or {}
    rows = api_get("/v1/rankings", {"action": "BUY", "limit": 500}).get("data", [])
    exits = api_get("/v1/rankings", {"action": "EXIT", "limit": 500}).get("data", [])
    model = api_get("/v1/model-portfolio").get("data") or {}
    market = (
        f"<b>🌐 THỊ TRƯỜNG PHIÊN {date(benchmark.get('date'))}</b>\n"
        f"VN-Index <b>{number(benchmark.get('close'), 2)} điểm</b> · "
        f"{percent(benchmark.get('return_1d'), 2)} · Khối lượng {number(benchmark.get('volume'))}\n\n"
    )
    return market + render_signal_list(rows, daily=True) + "\n\n" + render_exit_list(exits, model)


def subscription_reply(chat_id: int, enabled: bool) -> str:
    status = "✅ Đã đăng ký" if enabled else "🔕 Đã ngừng"
    detail = ("Mỗi ngày lúc 08:00, bot gửi diễn biến phiên gần nhất cùng danh sách MUA mới và BÁN/EXIT."
              if enabled else "Bạn sẽ không nhận bản tin tự động. Có thể đăng ký lại bất cứ lúc nào.")
    return f"<b>🔔 BẢN TIN THỊ TRƯỜNG 8:00</b>\n{status}\n\n{detail}"


def command_reply(text: str, *, include_news: bool = True) -> str:
    parts = text.strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in {"/start", "/help", "/menu"}:
        return (
            "<b>📊 X10 | Trợ lý phân tích cổ phiếu</b>\n\n"
            "X10 chuyển dữ liệu thị trường thành một quy trình nghiên cứu có kỷ luật: đọc sức khỏe doanh nghiệp, "
            "đánh giá xu hướng giá–khối lượng, kiểm tra bối cảnh thị trường và giải thích rõ căn cứ của từng tín hiệu.\n\n"
            "<b>🔎 Phân tích một cổ phiếu</b>\n"
            "Gửi trực tiếp mã như <code>FPT</code>. Bạn sẽ nhận giá khớp gần thời gian thực nếu có, dữ liệu EOD, "
            "hiệu suất 3–6–12 tháng, MA20/50/200, thanh khoản, báo cáo tài chính, sự kiện doanh nghiệp và tin mới có nguồn.\n\n"
            "<b>📡 Danh sách tín hiệu MUA</b>\n"
            "Hiển thị toàn bộ mã đang vượt qua bộ lọc tại phiên gần nhất. Mỗi mã có điểm FA, độ phủ dữ liệu, điểm TA, "
            "Danh sách ban đầu chỉ hiện mã, giá, điểm và hiệu suất để dễ đọc. Chọn từng mã để xem đầy đủ "
            "các điều kiện động lượng, sức mạnh tương đối, xu hướng, breakout và khối lượng.\n\n"
            "<b>🔴 Danh sách tín hiệu BÁN / EXIT</b>\n"
            "Chỉ hiển thị cổ phiếu đã nằm trong danh mục mô hình và vừa vi phạm MA200, trailing stop, "
            "ngưỡng điểm thoát hoặc điều kiện thị trường. Nếu chưa có vị thế, bot sẽ nói rõ số lệnh MUA đang chờ khớp.\n\n"
            "<b>💬 Phân tích chuyên sâu</b>\n"
            "Đặt câu hỏi về rủi ro, tài chính, vùng giá cần theo dõi hoặc tác động của tin mới. AI chỉ giải thích dữ liệu "
            "và tín hiệu do bộ quy tắc tạo ra; AI không tự ý đổi MUA, THEO DÕI, NẮM GIỮ hay BÁN.\n\n"
            "<b>🔔 Bản tin thị trường lúc 08:00</b>\n"
            "Bản tin tự động tóm tắt VN-Index của phiên trước, danh sách tín hiệu MUA mới nhất và những căn cứ chính "
            "cần kiểm tra trước khi lập kế hoạch giao dịch.\n\n"
            "<i>X10 hỗ trợ nghiên cứu và quản trị quyết định. Tín hiệu định lượng không bảo đảm lợi nhuận và không phải "
            "khuyến nghị đầu tư cá nhân; người dùng vẫn cần tự xác định vùng mua, mức chịu lỗ và tỷ trọng phù hợp.</i>"
        )
    if command == "/health":
        data = api_get("/health")
        return (
            "<b>Tình trạng dữ liệu</b>\n"
            f"Cổ phiếu đang theo dõi: {data.get('companies', 0)} mã\n"
            f"Ngày giá gần nhất: {date(data.get('latest_market_date'))}\n"
            f"Cập nhật gần nhất: {fmt_number(data.get('pipeline_age_hours'))} giờ trước\n"
            f"Trạng thái: {'Sẵn sàng' if data.get('data_fresh') else 'Cần kiểm tra cập nhật'}"
        )
    if command in {"/stock", "/ask", "/fa", "/ta", "/news"}:
        if len(parts) < 2:
            return "Hãy nhập mã cổ phiếu, ví dụ <code>/stock FPT</code>."
        ticker = parts[1].upper()
        if not valid_ticker(ticker):
            return "Mã cổ phiếu không hợp lệ. Ví dụ: <code>/stock FPT</code>."
        if command == "/ask" and len(parts) < 3:
            return f"Bạn muốn hỏi gì về {html.escape(ticker)}? Ví dụ: <code>/ask {html.escape(ticker)} Rủi ro lớn nhất?</code>"
        context = load_context(ticker, api_get, include_trade=command in {"/stock", "/ask"})
        if command == "/stock":
            report = render_report(context)
            return report + news_section(context) if include_news else DeferredNewsReply(report, ticker)
        if command == "/ask":
            return detailed_answer(context, " ".join(parts[2:]))
        if command == "/news":
            return f"<b>🏢 {html.escape(ticker)} — TIN & TÁC ĐỘNG</b>" + news_section(context)
        if command == "/fa":
            return render_report(context, "fa")
        return render_report(context, "ta")
    if command in {"/top", "/sell"}:
        action = "EXIT" if command == "/sell" else (parts[1].upper() if len(parts) >= 2 else "BUY")
        if action == "MUA":
            action = "BUY"
        if action and action not in {"BUY", "WATCH", "HOLD", "EXIT", "DATA_REVIEW"}:
            return "Bộ lọc không hợp lệ. Dùng <code>/top MUA 10</code> hoặc <code>/top WATCH 10</code>."
        default_limit = 500 if action == "BUY" else 20
        limit = min(500, max(1, int(parts[2]))) if len(parts) >= 3 and parts[2].isdigit() else default_limit
        api_action = "SCREEN_BUY" if action == "BUY" else action
        payload = api_get("/v1/rankings", {"action": api_action, "limit": limit})
        rows = payload.get("data", [])
        if action == "BUY":
            return render_signal_list(rows)
        if action == "EXIT":
            model = api_get("/v1/model-portfolio").get("data") or {}
            return render_exit_list(rows, model)
        title = "👀 DANH SÁCH THEO DÕI" if action == "WATCH" else f"📋 DANH SÁCH {action or 'TỔNG HỢP'}"
        lines = [f"<b>{title}</b>"]
        for index, row in enumerate(rows, 1):
            lines.append(f"{index}. <b>{html.escape(str(row.get('ticker')))}</b> · "
                         f"{number(row.get('close'))}đ · 6T {percent(row.get('return_6m'))}")
        return "\n".join(lines + ([] if rows else ["Hiện không có mã phù hợp."]))
    if command == "/market":
        benchmark = api_get("/v1/benchmark").get("data") or {}
        return render_market_summary(benchmark)
    if command == "/daily":
        return "Dùng nút bên dưới để bật hoặc tắt bản tin thị trường lúc 08:00 mỗi ngày."
    if not command.startswith("/") and len(parts) == 1 and valid_ticker(parts[0]):
        return command_reply(f"/stock {parts[0]}")
    return "Chưa hiểu yêu cầu. Gửi một mã như <code>FPT</code> hoặc bấm /menu."


def valid_ticker(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,10}", value))


def keyboard_for(command_text: str, reply: str = "") -> dict[str, Any]:
    """Small inline keyboard; callback payloads stay under Telegram's 64-byte limit."""
    parts = command_text.strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if not command.startswith("/") and len(parts) == 1 and valid_ticker(parts[0]):
        command, parts = "/stock", ["/stock", parts[0]]
    if command in {"/stock", "/ask", "/fa", "/ta", "/news"} and len(parts) > 1 and valid_ticker(parts[1]):
        ticker = parts[1].upper()
        rows = [
            [{"text": "📰 Tin mới", "callback_data": f"news:{ticker}"},
             {"text": "💬 Hỏi AI", "callback_data": f"ask:{ticker}"}],
            [{"text": "📡 Tín hiệu MUA", "callback_data": "top:BUY"},
             {"text": "🔴 Tín hiệu BÁN", "callback_data": "top:EXIT"}],
            [{"text": "🏠 Menu", "callback_data": "menu"}],
        ]
    elif command in {"/top", "/sell"}:
        action = ("EXIT" if command == "/sell" else
                  parts[1].upper() if len(parts) > 1 and parts[1].upper() in {"BUY", "WATCH", "HOLD", "EXIT"} else "BUY")
        tickers = re.findall(r"(?m)^\d+\.\s+(?:🟢\s+)?(?:<b>)?([A-Z0-9]{1,10})", reply)[:60]
        rows = [[{"text": ticker, "callback_data": f"stock:{ticker}"} for ticker in tickers[i:i + 6]]
                for i in range(0, len(tickers), 6)]
        rows.append([{"text": "🏠 Menu", "callback_data": "menu"},
                     {"text": "🔄 Làm mới", "callback_data": f"top:{action}"}])
    elif command == "/daily":
        enabled = len(parts) > 1 and parts[1].lower() == "on"
        rows = [[{"text": "🔕 Ngừng nhận bản tin" if enabled else "🔔 Đăng ký bản tin 8:00",
                  "callback_data": "daily:off" if enabled else "daily:on"}],
                [{"text": "🏠 Menu", "callback_data": "menu"}]]
    else:
        rows = [
            [{"text": "🔎 Tra cứu cổ phiếu", "callback_data": "input:stock"},
             {"text": "📡 Tín hiệu MUA", "callback_data": "top:BUY"}],
            [{"text": "🔴 Tín hiệu BÁN", "callback_data": "top:EXIT"}],
            [{"text": "🌐 Thị trường", "callback_data": "market"},
             {"text": "🔔 Bản tin 8:00", "callback_data": "daily:on"}],
            [{"text": "🩺 Tình trạng dữ liệu", "callback_data": "health"}],
        ]
        if urlparse(WEBAPP_URL).scheme == "https":
            rows.insert(0, [{"text": "🚀 Mở X10 Web App", "web_app": {"url": WEBAPP_URL}}])
    return {"inline_keyboard": rows}


MENU_TEXT_COMMANDS = {
    "📡 Tín hiệu MUA": "/top BUY",
    "🔴 Tín hiệu BÁN": "/sell",
    "🌐 VN-Index phiên trước": "/market",
    "🔔 Bản tin 8:00": "/daily on",
    "🩺 Tình trạng dữ liệu": "/health",
    "🏠 Menu": "/menu",
}


def reply_keyboard_for(command_text: str, reply: str = "") -> dict[str, Any] | None:
    """Use Telegram reply buttons where a tap must appear as a short user message."""
    parts = command_text.strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command in {"/start", "/help", "/menu"}:
        rows: list[list[dict[str, Any]]] = [
            [{"text": "🔎 Tra cứu cổ phiếu"}, {"text": "📡 Tín hiệu MUA"}],
            [{"text": "🔴 Tín hiệu BÁN"}, {"text": "🌐 VN-Index phiên trước"}],
            [{"text": "🔔 Bản tin 8:00"}, {"text": "🩺 Tình trạng dữ liệu"}],
        ]
        if urlparse(WEBAPP_URL).scheme == "https":
            rows.insert(0, [{"text": "🚀 Mở X10 Web App", "web_app": {"url": WEBAPP_URL}}])
        return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True,
                "input_field_placeholder": "Chọn chức năng hoặc nhập mã cổ phiếu"}
    return None


def telegram_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=70)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        # requests' default exception includes the URL, which embeds the bot token.
        status = exc.response.status_code if exc.response is not None else "network"
        if method == "editMessageText" and exc.response is not None:
            try:
                description = str(exc.response.json().get("description") or "")
            except ValueError:
                description = ""
            if "message is not modified" in description.lower():
                return {"ok": True}
        raise RuntimeError(f"Telegram {method} failed (HTTP {status})") from None


def split_complete_sections(reply: str, limit: int = 3900) -> list[str]:
    """Split at blank-line section boundaries without dropping or cutting content."""
    sections = reply.strip().split("\n\n")
    messages: list[str] = []
    current = ""
    for section in sections:
        candidate = section if not current else current + "\n\n" + section
        if current and len(candidate) > limit:
            messages.append(current)
            current = section
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def reply_parts(command_text: str, reply: str) -> list[str]:
    """Keep stock reports complete while respecting Telegram's per-message limit."""
    command = command_text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    news_marker = "<b>📰 TIN DOANH NGHIỆP · NGÀNH · VĨ MÔ</b>"
    if command == "/stock" and news_marker in reply:
        report, news = reply.split(news_marker, 1)
        news = news_marker + news
        if len(report.rstrip()) <= 3900 and len(news) <= 3900:
            return [report.rstrip(), news]
        return split_complete_sections(report.rstrip() + "\n\n" + news)
    return split_complete_sections(reply) if len(reply) > 3900 else [reply]


def telegram_safe_text(reply: str) -> str:
    if len(reply) <= 3900:
        return reply
    return html.escape(html.unescape(re.sub(r"<[^>]+>", "", reply))[:3800]) + "\n…"


def processing_text(command_text: str) -> str:
    """Return an immediate, visible status for commands that may take noticeable time."""
    command = command_text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    return {
        "/ask": "⏳ Đang phân tích dữ liệu, đối chiếu thông tin và soạn câu trả lời…",
        "/stock": "⏳ Đang tổng hợp báo cáo giá, FA, TA và tin liên quan…",
        "/news": "⏳ Đang tìm và kiểm chứng tin mới từ các nguồn phù hợp…",
        "/fa": "⏳ Đang tổng hợp các chỉ tiêu cơ bản của doanh nghiệp…",
        "/ta": "⏳ Đang kiểm tra xu hướng, động lượng và thanh khoản…",
        "/top": "⏳ Đang lọc và xếp hạng các tín hiệu mới nhất…",
        "/sell": "⏳ Đang kiểm tra các vị thế chạm điều kiện BÁN…",
        "/market": "⏳ Đang tổng hợp diễn biến thị trường gần nhất…",
    }.get(command, "")


def consume_ai_quota(chat_id: int) -> tuple[bool, int]:
    """Share the same persistent AI budget used by the Web App."""
    for scope, limit, window in (("ai:minute", AI_MINUTE_LIMIT, 60),
                                 ("ai:day", AI_DAILY_LIMIT, 86_400)):
        result = AI_RATE_LIMITER.consume(f"tg:{chat_id}", scope, limit, window)
        if not result.allowed:
            return False, result.retry_after
    return True, 0


def send_reply(chat_id: int, command_text: str, message_id: int | None = None) -> None:
    command_name = command_text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command_name in {"/ask", "/news", "/market"}:
        try:
            allowed, retry_after = consume_ai_quota(chat_id)
        except Exception:
            LOGGER.exception("AI rate-limit storage is unavailable")
            telegram_call("sendMessage", {"chat_id": chat_id,
                                           "text": "Bộ giới hạn AI đang tạm gián đoạn. Vui lòng thử lại sau."})
            return
        if not allowed:
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": f"Bạn đã dùng quá nhiều lượt AI. Vui lòng thử lại sau {retry_after} giây.",
            })
            return
    progress = processing_text(command_text)
    if progress:
        try:
            telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            progress_markup = ({"remove_keyboard": True}
                               if command_name in {"/top", "/sell"} else {"inline_keyboard": []})
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": progress,
                "reply_markup": progress_markup,
            })
        except RuntimeError:
            # A progress indicator is best-effort; the actual answer must still be generated.
            LOGGER.info("Could not publish processing status for %s", command_text.split(maxsplit=1)[0])
    deferred_news_ticker = ""
    try:
        parts = command_text.strip().split()
        if parts and parts[0].split("@", 1)[0].lower() == "/daily":
            enabled = not (len(parts) > 1 and parts[1].lower() == "off")
            set_daily_subscription(chat_id, enabled)
            reply = subscription_reply(chat_id, enabled)
            command_text = f"/daily {'on' if enabled else 'off'}"
        else:
            command = parts[0].split("@", 1)[0].lower() if parts else ""
            if command == "/stock" and len(parts) >= 2 and valid_ticker(parts[1]):
                # The saved-data report is useful on its own and is much faster than web/AI news.
                # Publish it first, then retrieve verified news as a second message.
                reply = command_reply(command_text, include_news=False)
                if isinstance(reply, DeferredNewsReply):
                    deferred_news_ticker = reply.ticker
            else:
                reply = command_reply(command_text)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            reply = "Không tìm thấy mã này trong kho dữ liệu. Hãy thử mã HOSE/HNX khác."
        else:
            LOGGER.exception("Stock API request failed")
            reply = "Không đọc được dữ liệu lúc này. Thử lại sau bằng nút 🔄."
    except LookupError:
        reply = "Mã này chưa có trong kho dữ liệu. Hãy kiểm tra lại mã HOSE/HNX."
    except Exception:
        LOGGER.exception("Command failed")
        reply = "Kho dữ liệu tạm thời không khả dụng. Vui lòng thử lại sau ít phút."
    messages = [telegram_safe_text(part) for part in reply_parts(command_text, reply)]
    for index, message in enumerate(messages):
        is_last = index == len(messages) - 1
        reply_markup = reply_keyboard_for(command_text, reply) if is_last else None
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": (reply_markup or keyboard_for(command_text, reply)) if is_last else {"inline_keyboard": []},
        }
        telegram_call("sendMessage", payload)
    if deferred_news_ticker:
        try:
            allowed, retry_after = consume_ai_quota(chat_id)
            if not allowed:
                telegram_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"Phần dữ liệu đã gửi đầy đủ. Tin AI tạm hoãn vì giới hạn lượt; thử lại sau {retry_after} giây.",
                })
                return
            try:
                telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            except RuntimeError:
                LOGGER.info("Could not publish deferred-news typing status for %s", deferred_news_ticker)
            news_reply = command_reply(f"/news {deferred_news_ticker}")
            for news_part in reply_parts(f"/news {deferred_news_ticker}", news_reply):
                telegram_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": telegram_safe_text(news_part),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard_for(command_text, news_reply),
                })
        except Exception:
            LOGGER.exception("Deferred news delivery failed for %s", deferred_news_ticker)


def callback_command(data: str) -> str | None:
    if data in {"menu", "market", "health"}:
        return "/menu" if data == "menu" else f"/{data}"
    action, sep, ticker = data.partition(":")
    if not sep:
        return None
    if action in {"stock", "fa", "ta", "news"} and valid_ticker(ticker):
        return f"/{action} {ticker.upper()}"
    if action == "top" and ticker in {"BUY", "WATCH", "HOLD", "EXIT"}:
        return f"/top {ticker}"
    if action == "daily" and ticker in {"on", "off"}:
        return f"/daily {ticker}"
    return None


def pending_from_reply(message: dict[str, Any]) -> tuple[str, str] | None:
    """Recover a ForceReply prompt if the bot restarted after showing a button."""
    quoted = message.get("reply_to_message") or {}
    sender = quoted.get("from") or {}
    bot_id = TOKEN.split(":", 1)[0]
    if not sender.get("is_bot") or not bot_id.isdigit() or int(sender.get("id") or 0) != int(bot_id):
        return None
    prompt = str(quoted.get("text") or "")
    if prompt == "Nhập mã cổ phiếu (ví dụ SHS).":
        return "stock", ""
    matched = re.fullmatch(r"Bạn muốn tìm hiểu điều gì về ([A-Z0-9]{1,10})\?", prompt)
    return ("ask", matched.group(1)) if matched else None


def handle_update(update: dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    if callback:
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        user_id = int((callback.get("from") or {}).get("id", 0))
        if not chat_is_allowed(chat_id) or user_id != chat_id:
            try:
                telegram_call("answerCallbackQuery", {
                    "callback_query_id": callback.get("id"),
                    "text": "Tài khoản này chưa được cấp quyền sử dụng bot.",
                    "show_alert": True,
                })
            except RuntimeError:
                pass
            return
        try:
            telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except RuntimeError:
            # Telegram rejects expired callback IDs; the requested action can still be completed.
            LOGGER.info("Callback acknowledgement expired; continuing with requested action")
        data = str(callback.get("data") or "")
        if data == "input:stock" or (data.startswith("ask:") and valid_ticker(data[4:])):
            action, ticker = ("stock", "") if data == "input:stock" else ("ask", data[4:].upper())
            PENDING_INPUT[chat_id] = (action, ticker)
            hint = "Nhập mã cổ phiếu (ví dụ SHS)." if action == "stock" else f"Bạn muốn tìm hiểu điều gì về {ticker}?"
            telegram_call("sendMessage", {
                "chat_id": chat_id, "text": hint,
                "reply_markup": {"force_reply": True, "input_field_placeholder": "SHS" if action == "stock" else "Nhập câu hỏi của bạn"},
            })
            return
        command = callback_command(data)
        if command:
            send_reply(chat_id, command, message.get("message_id"))
        return

    message = update.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", 0))
    if message.get("chat", {}).get("type") != "private":
        return
    if not chat_is_allowed(chat_id):
        telegram_call("sendMessage", {
            "chat_id": chat_id,
            "text": "Tài khoản này chưa được cấp quyền sử dụng bot.",
        })
        return
    text = str(message.get("text") or "").strip()
    if not text:
        return
    if text == "🔎 Tra cứu cổ phiếu":
        PENDING_INPUT[chat_id] = ("stock", "")
        telegram_call("sendMessage", {
            "chat_id": chat_id,
            "text": "Nhập mã cổ phiếu (ví dụ SHS).",
            "reply_markup": {"force_reply": True, "input_field_placeholder": "SHS"},
        })
        return
    if text in MENU_TEXT_COMMANDS:
        text = MENU_TEXT_COMMANDS[text]
    if text.startswith("/"):
        PENDING_INPUT.pop(chat_id, None)
    else:
        pending = PENDING_INPUT.pop(chat_id, None) or pending_from_reply(message)
        if pending:
            action, ticker = pending
            if action == "stock":
                if not valid_ticker(text):
                    telegram_call("sendMessage", {"chat_id": chat_id, "text": "Chỉ nhập một mã, ví dụ SHS. Bấm 🔎 Tra cứu cổ phiếu để thử lại."})
                    return
                text = f"/stock {text.upper()}"
            else:
                text = f"/ask {ticker} {text[:500]}"
    send_reply(chat_id, text)


def update_chat_id(update: dict[str, Any]) -> int:
    callback = update.get("callback_query") or {}
    if callback:
        return int((((callback.get("message") or {}).get("chat") or {}).get("id") or 0))
    return int((((update.get("message") or {}).get("chat") or {}).get("id") or 0))


def handle_update_safely(update: dict[str, Any]) -> None:
    """Process different chats concurrently while preserving order within one chat."""
    chat_id = update_chat_id(update)
    with CHAT_LOCKS_GUARD:
        chat_lock = CHAT_LOCKS.setdefault(chat_id, Lock())
    with chat_lock:
        try:
            handle_update(update)
        except Exception:
            LOGGER.exception("Update failed")


def send_due_daily_reports(now: datetime | None = None) -> int:
    """Send once after 08:00 Vietnam time; catch-up works after a late restart."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    if local_now.hour < 8:
        return 0
    day = local_now.date().isoformat()
    subscribers = due_daily_subscribers(day)
    if not subscribers:
        return 0
    report = daily_digest()
    sent = 0
    for chat_id in subscribers:
        try:
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": report,
                "parse_mode": "HTML",
                "reply_markup": keyboard_for("/top BUY", report),
            })
            mark_daily_sent(chat_id, day)
            sent += 1
        except Exception:
            LOGGER.exception("Daily report delivery failed for authorized chat")
    return sent


def poll_forever() -> None:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
    if not PUBLIC_ACCESS and not ALLOWED_CHAT_IDS:
        raise SystemExit("Enable TELEGRAM_PUBLIC_ACCESS or configure TELEGRAM_ALLOWED_CHAT_IDS in .env")
    log_path = Path(__file__).resolve().parent / "analysis_data" / "telegram_bot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
    )
    LOGGER.info("Telegram bot polling started")
    LOGGER.info("Telegram access mode: %s; update workers: %s",
                "public" if PUBLIC_ACCESS else "allowlist", UPDATE_WORKERS)
    try:
        telegram_call("setMyCommands", {"commands": [
            {"command": "menu", "description": "Mở menu nút bấm"},
            {"command": "stock", "description": "Báo cáo đầy đủ một cổ phiếu"},
            {"command": "ask", "description": "Hỏi chuyên sâu về một mã"},
            {"command": "news", "description": "Tin mới và bối cảnh ngành"},
            {"command": "top", "description": "Toàn bộ tín hiệu MUA"},
            {"command": "sell", "description": "Tín hiệu BÁN / EXIT"},
            {"command": "market", "description": "AI tổng hợp VN-Index phiên trước"},
            {"command": "daily", "description": "Đăng ký bản tin 8:00"},
            {"command": "health", "description": "Độ mới dữ liệu"},
        ]})
    except RuntimeError:
        LOGGER.warning("Could not register Telegram command menu")
    offset = 0
    while True:
        try:
            send_due_daily_reports()
            updates = telegram_call("getUpdates", {"offset": offset, "timeout": 50,
                                                    "allowed_updates": ["message", "callback_query"]}).get("result", [])
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                UPDATE_POOL.submit(handle_update_safely, update)
        except Exception:
            LOGGER.exception("Telegram polling failed")
            time.sleep(5)


def main() -> None:
    try:
        with single_instance():
            poll_forever()
    except KeyboardInterrupt:
        LOGGER.info("Telegram bot stopped by console interrupt")


if __name__ == "__main__":
    main()
