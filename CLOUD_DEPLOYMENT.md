# Triển khai X10 lên Supabase và Vercel

## Kết luận kiến trúc

Supabase và Vercel đảm nhiệm hai phần khác nhau:

- Supabase lưu PostgreSQL, áp dụng RLS và cung cấp Data API/Edge Functions.
- Vercel host các tệp tĩnh của Web App: `webapp.html`, `chart_tools.js`, manifest và hình ảnh.
- Telegram Bot và pipeline cập nhật dữ liệu vẫn là tiến trình Python có lịch chạy. Chúng không thể chạy liên tục trong một website tĩnh.

Vì vậy, nếu cần một đường dẫn Web App công khai thì nên deploy frontend lên Vercel. Có thể phục vụ HTML bằng Supabase Edge Function, nhưng đây không phải cách host frontend thuận tiện; Edge Functions phù hợp hơn với logic API phía máy chủ.

## Trạng thái hiện tại

Bundle `supabase_bundle` đã sẵn sàng để nạp dữ liệu lịch sử vào PostgreSQL và có:

- schema PostgreSQL;
- dữ liệu CSV nén, chia nhỏ;
- checksum và manifest để chống tải nhầm phiên database;
- RLS chỉ đọc cho `anon` và `authenticated`;
- bảng audit không được mở cho client;
- script tải dữ liệu bằng PostgreSQL COPY và kiểm tra số dòng.

Web App chưa thể hoạt động đầy đủ chỉ bằng cách deploy tệp HTML. Nó đang dùng API hợp nhất `/health` và `/v1/...` của `stock_data_api.py`. Bundle Supabase hiện là lớp dữ liệu, chưa thay thế các chức năng sau:

- tín hiệu và báo cáo backtest theo hợp đồng API hiện tại;
- giá khớp/nến DNSE trong phiên;
- AI có khóa bí mật Gemini/Tavily;
- Paper Trading và AI đã được bảo vệ bằng Telegram WebApp `initData`.
- Paper Trading production dùng PostgreSQL theo `telegram_user_id`; AI có giới hạn theo user và IP.

Không được đưa khóa DNSE, Gemini, Tavily, Telegram Bot, mật khẩu PostgreSQL hoặc Supabase secret/service-role key vào JavaScript phía trình duyệt. Frontend chỉ được dùng Supabase publishable key khi RLS đã đúng.

## Bước 1 - kiểm tra và tạo lại bundle

Chạy:

```powershell
.\prepare_cloud_deploy.ps1
```

Script chạy toàn bộ kiểm thử, tạo lại bundle từ SQLite mới nhất, xác minh hash/số dòng và quét chuỗi có hình dạng khóa bí mật.

## Bước 2 - nạp PostgreSQL Supabase

Lấy Session pooler connection string trong Supabase Dashboard và chỉ đặt ở biến môi trường của máy đang upload:

```powershell
$env:SUPABASE_DB_URL='postgresql://postgres.PROJECT:PASSWORD@HOST:5432/postgres?sslmode=require'
& '.\.venv\Scripts\python.exe' -m pip install -r supabase_bundle\requirements.txt
& '.\.venv\Scripts\python.exe' supabase_bundle\upload_to_supabase.py
```

Sau khi import, chạy `post_import.sql` nếu importer chưa thực hiện, rồi kiểm tra kích thước database. Supabase Free hiện giới hạn database 500 MB; dữ liệu nén cục bộ nhỏ hơn nhiều nhưng PostgreSQL còn có overhead và index nên phải kiểm tra kích thước thực sau import.

## Bước 3 - tạo API cloud

Trước khi public Web App, cần đưa `stock_data_api.py` lên một backend Python HTTPS tương thích với hợp đồng `/v1/...`. Backend xác thực chữ ký Telegram bằng bot token, lấy `telegram_user_id` đã tin cậy, rồi mới truy cập Paper Trading hoặc AI. Không đặt bot token hay chuỗi kết nối PostgreSQL ở Vercel/frontend.

Chạy lại `upload_to_supabase.py` để tạo các bảng riêng tư `telegram_users`, `paper_accounts`, `paper_trades` và `rate_limit_buckets`. Các bảng này bật RLS, không cấp quyền cho `anon`/`authenticated`, và chỉ backend có chuỗi kết nối bảo mật mới được truy cập.

## Bước 4 - deploy frontend Vercel

`.vercelignore` chỉ cho phép các tài sản frontend cần thiết đi lên Vercel; `.env`, SQLite, raw data, báo cáo và mã pipeline đều bị loại. `vercel.json` ánh xạ `/` tới `webapp.html` và thêm các security/cache header cơ bản.

Chỉ deploy Vercel sau khi đã cấu hình một backend cloud tương thích. Nếu deploy ngay bây giờ, giao diện sẽ mở nhưng các request `/health` và `/v1/...` sẽ trả 404.

Sau khi có URL API, cấu hình Vercel rewrite hoặc đổi hàm `api()` của Web App sang URL đó. Không dùng `http://127.0.0.1:8765` trong production.

## Biến môi trường bắt buộc cho backend production

```dotenv
APP_ENV=production
TELEGRAM_BOT_TOKEN=...
TELEGRAM_PUBLIC_ACCESS=true
SUPABASE_DB_URL=postgresql://...
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS=86400
AI_RATE_LIMIT_PER_MINUTE=5
AI_RATE_LIMIT_PER_DAY=30
```

Có thể dùng `PAPER_DATABASE_URL` để tách database trạng thái ứng dụng khỏi database dữ liệu lịch sử; nếu biến này trống, backend dùng `SUPABASE_DB_URL`. Mỗi request Paper Trading và AI phải mang header `X-Telegram-Init-Data`; backend không nhận `telegram_user_id` do JavaScript tự gửi. Ở `APP_ENV=production`, API từ chối khởi động nếu thiếu bot token hoặc URL PostgreSQL.
