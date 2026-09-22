# X10 Investment Lab

X10 là hệ thống nghiên cứu cổ phiếu Việt Nam gồm Telegram Bot, Web App, bộ lọc tín hiệu MUA/BÁN, Paper Trading theo Telegram user ID và quy trình cập nhật dữ liệu hằng ngày.

## Chức năng chính

- Tra cứu giá EOD và giá khớp DNSE gần thời gian thực.
- Phân tích cơ bản, kỹ thuật, động lượng, sức mạnh tương đối và thanh khoản.
- Bộ lọc tín hiệu MUA theo điểm thống nhất FA/TA và trạng thái thị trường.
- Quản lý danh mục mô hình với trạng thái MUA, NẮM GIỮ và BÁN/EXIT.
- Paper Trading tách biệt theo Telegram user ID.
- AI giải thích tín hiệu, tổng hợp VN-Index và tin doanh nghiệp, ngành, vĩ mô có nguồn.
- P/E TTM tính minh bạch từ giá EOD/EPS TTM khi nguồn không trả P/E trực tiếp.
- Backtest có phí giao dịch, khớp lệnh phiên kế tiếp, kiểm soát look-ahead và cooldown.

## Kiến trúc

- `telegram_bot.py`: Telegram Bot và menu hội thoại.
- `stock_data_api.py`: API hợp nhất cho Web App và bot.
- `webapp.html`: giao diện Telegram Web App/PWA.
- `strategy_engine.py`: tính tín hiệu và quản lý danh mục mô hình.
- `backtest_engine.py`: mô phỏng lịch sử và báo cáo hiệu quả.
- `daily_data_pipeline.py`: cập nhật dữ liệu và tái tạo tín hiệu hằng ngày.
- `paper_trading.py`: sổ lệnh mô phỏng theo người dùng.
- `stock_research.py`: tìm kiếm và tổng hợp tin có kiểm chứng.
- `supabase_bundle/`: schema và công cụ nhập dữ liệu vào PostgreSQL/Supabase.

## Chiến lược đang chạy

Điểm thống nhất gồm 20% FA hiệu dụng và 80% TA. Một mã chỉ được phát tín hiệu MUA khi:

- điểm thống nhất từ 60/100;
- dữ liệu kỹ thuật đầy đủ và giá thuộc phiên mới nhất;
- giá trị giao dịch bình quân 20 phiên từ 2 tỷ đồng/phiên;
- VN-Index ở trên hoặc bằng MA50;
- mã thuộc Top 30 theo điểm và sức mạnh lựa chọn.

Danh mục phát tín hiệu BÁN khi vị thế đang nắm giữ vi phạm MA200, trailing stop 15% từ đỉnh 60 phiên, hoặc điểm xuống dưới 35 sau thời gian nắm giữ tối thiểu. Chi tiết phương pháp nằm trong [README_LOCAL.md](README_LOCAL.md) và [OPTIMIZATION_RESEARCH.md](OPTIMIZATION_RESEARCH.md).

## Chạy cục bộ trên Windows

Yêu cầu Python 3.10+, Node.js và file `.env` tạo từ `.env.example`.

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
.\run_stock_api.ps1
```

Mở <http://127.0.0.1:8765>. Chạy bot bằng:

```powershell
.\run_telegram_bot.ps1
```

Không commit `.env` hoặc đưa API key vào mã nguồn.

## Kiểm thử

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -q
node .\test_chart_tools.js
& '.\.venv\Scripts\python.exe' .\validate_analysis_data.py
```

Bộ kiểm thử hiện có 127 test Python, kiểm thử công cụ biểu đồ và kiểm tra toàn vẹn dữ liệu/tín hiệu.

## Dữ liệu không lưu trong Git

Repository không chứa khóa bí mật, môi trường Python, SQLite, log, cache, dữ liệu raw và các bundle dữ liệu lớn. Các phần này được loại bằng `.gitignore` vì có thể chứa thông tin nhạy cảm, được sinh lại hoặc vượt giới hạn lưu trữ GitHub.

- Tạo `.env` từ `.env.example`.
- Tạo dữ liệu bằng pipeline hoặc khôi phục từ kho dữ liệu vận hành.
- Tạo lại bundle Supabase bằng `prepare_cloud_deploy.ps1`.

## Deploy

- Vercel host tài sản Web App tĩnh.
- Supabase/PostgreSQL lưu dữ liệu và trạng thái Paper Trading production.
- Python backend HTTPS xử lý Telegram `initData`, API `/v1`, AI và dữ liệu thị trường.
- Telegram Bot cần một worker Python chạy liên tục hoặc chuyển sang webhook.

Xem [CLOUD_DEPLOYMENT.md](CLOUD_DEPLOYMENT.md) để biết biến môi trường, quy trình import Supabase và giới hạn triển khai.

## Lưu ý

Kết quả là tín hiệu định lượng phục vụ nghiên cứu, không phải khuyến nghị đầu tư cá nhân và không bảo đảm lợi nhuận.
