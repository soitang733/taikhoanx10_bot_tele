# Vận hành dữ liệu cho bot phân tích

> Xem giải thích ngắn về cập nhật EOD, corporate action và phát hiện DNSE sửa lịch sử tại [`DATA_SYNC_POLICY.md`](DATA_SYNC_POLICY.md).

## Luồng dữ liệu

1. `bootstrap_remaining.py` tải lịch sử cho toàn bộ mã chưa hoàn tất.
2. `daily_data_pipeline.py` cập nhật EOD cho toàn bộ mã với cửa sổ 2 phiên giao dịch gần nhất; kiểm tra lịch sử sâu được xử lý riêng theo `DATA_SYNC_POLICY.md`.
3. `prepare_analysis_data.py` kiểm tra, chuẩn hóa và xuất Parquet + SQLite.
4. `stock_data_api.py` cung cấp dữ liệu chỉ-đọc cho bot trên `127.0.0.1:8765`.

Dữ liệu thô trong `scraper_output` không bị thay đổi bởi bước chuẩn hóa. Kho bot đọc
nằm trong `analysis_data/stocks_analysis.sqlite`.

Pipeline hiện gộp dữ liệu theo khóa `ticker + date` ở lớp raw rồi tạo lại file SQLite
hoàn chỉnh trước khi thay file cũ. Đây là kết quả tương đương UPSERT nhưng **chưa phải
SQLite UPSERT tại chỗ**. Trong lúc thay file trên Windows, nếu tiến trình khác giữ DB,
bước xuất sẽ đổi tên DB cũ thành bản sao tạm để có thể phục hồi; bản sao tạm được xóa
sau khi file mới đã xuất thành công.

Nếu database đã đủ nhưng raw mirror bị dở dang, chạy một lần:

```powershell
.\.venv\Scripts\python.exe .\materialize_raw_baseline.py
```

Lệnh này chỉ tạo các file raw còn thiếu từ SQLite hiện có, không ghi đè file raw
không rỗng. Sau đó raw mirror đủ 704 mã và pipeline mới được phép rebuild database.

## Lịch đang dùng

- `VnStockDailyData`: chạy hằng ngày lúc 18:00, tự chạy bù khi máy mở trễ.
- `VnStockDataApi`: tự khởi động khi người dùng đăng nhập Windows.

Daily updater có khóa `analysis_data/data_update.lock`, vì vậy hai lượt cập nhật
không ghi dữ liệu đồng thời. Kết quả mỗi lần chạy nằm trong
`analysis_data/pipeline_state.json`; log xoay vòng nằm trong
`analysis_data/daily_pipeline.log`.

## API cho bot

```text
GET http://127.0.0.1:8765/health
GET http://127.0.0.1:8765/v1/universe
GET http://127.0.0.1:8765/v1/latest?ticker=FPT
GET http://127.0.0.1:8765/v1/prices?ticker=FPT&start=2026-01-01&limit=500
GET http://127.0.0.1:8765/v1/financials?ticker=FPT&period=annual&limit=500
GET http://127.0.0.1:8765/v1/signal?ticker=FPT
GET http://127.0.0.1:8765/v1/rankings?action=BUY&limit=20
GET http://127.0.0.1:8765/v1/market-context
```

API chỉ lắng nghe localhost, không mở cổng ra mạng LAN/Internet. `/health` là kiểm
tra service/SQLite; `/ready` trả HTTP 503 nếu pipeline lỗi, bị defer, hoặc dữ liệu
cũ quá 72 giờ. Bot hay tác vụ tự động nên dùng `/ready` trước khi đưa ra quyết định.

Telegram bot nằm trong `telegram_bot.py`. Bot cần `TELEGRAM_BOT_TOKEN` và một trong
hai chế độ truy cập: `TELEGRAM_PUBLIC_ACCESS=true` để mọi tài khoản dùng được, hoặc
`TELEGRAM_ALLOWED_CHAT_IDS` để giới hạn theo danh sách. `TELEGRAM_UPDATE_WORKERS`
mặc định là 4, cho phép xử lý đồng thời các cuộc trò chuyện khác nhau nhưng vẫn giữ
thứ tự yêu cầu trong từng chat.

## Cách bot đọc trực tiếp

```python
import sqlite3
import pandas as pd

with sqlite3.connect("analysis_data/stocks_analysis.sqlite") as connection:
    prices = pd.read_sql_query(
        """
        SELECT *
        FROM price_daily
        WHERE ticker = ? AND analysis_ready = 1
        ORDER BY date
        """,
        connection,
        params=("FPT",),
    )
```

## Chính sách nguồn và lỗi

- DNSE là nguồn chính.
- Metadata và báo cáo tài chính chỉ fallback khi DNSE lỗi hoặc trả rỗng.
- Giá ngày fallback KBS rồi VCI khi DNSE lỗi/rỗng.
- Sự kiện doanh nghiệp dùng VCI/KBS vì chưa có endpoint DNSE đã xác minh.
- Lượt gọi vnstock Community được điều tiết dưới 20 request/phút.
- Giá trùng được ưu tiên DNSE; bản ghi mới thay thế bản ghi cũ cùng nguồn.
- Không tự điền số liệu tài chính hoặc giá bị thiếu.
- Bot chỉ dùng các dòng giá có `analysis_ready = 1`.
- Tín hiệu BUY chỉ xuất hiện khi ngày giá cuối của mã bằng ngày VNINDEX cuối; WATCH có
  `watch_reason` (ví dụ `ILLIQUID`, `STALE_PRICE`, `SCORE_BELOW_ENTRY`). BUY là cơ hội
  chung; HOLD/EXIT lấy từ danh mục mô hình được lưu qua từng phiên. Paper Trading áp dụng cùng quy tắc thoát riêng cho vị thế của từng Telegram user.
- FA của tín hiệu hiện tại chỉ dùng báo cáo năm theo cùng cơ sở của backtest, không
  cộng snapshot market cap hiện tại vào điểm để tránh sai lệch live/backtest.
- Backtest theo thời điểm phải loại dòng có `point_in_time_ready = 0`.

## Bảo mật khóa DNSE

File `.env` đã được loại khỏi Git. Để các lần bootstrap hoặc refresh tài chính sau
này chạy không cần nhập lại khóa, tạo `D:\dtata\.env` từ `.env.example` và điền
`DNSE_API_KEY`, `DNSE_API_SECRET`. Không đưa file `.env` lên Git hoặc gửi qua chat.
