# X10 Web App local

Mở `http://127.0.0.1:8765/` sau khi tác vụ `VnStockDataApi` chạy. Dashboard kỹ thuật cũ được giữ tại `http://127.0.0.1:8765/admin`.

## Chức năng

- **Tổng quan:** tín hiệu rule engine, giá EOD, giá khớp DNSE gần nhất, FA và giá–khối lượng. Khi mở mã, giá hiển thị ưu tiên khớp DNSE còn mới; nếu đã hết hạn nhưng DNSE có nến phiên hôm nay thì dùng giá đóng tạm tính của nến; cuối cùng mới về EOD. Bảng mã BUY lấy giá khớp theo lô, tự kiểm tra mỗi 90 giây khi đang mở trang Tổng quan. Chỉ giá khớp có timestamp trong 15 phút mới được đánh dấu realtime; thay đổi giá mới nhấp nháy. Ngoài giờ hoặc khi DNSE không phản hồi, bảng ghi rõ giá đóng cửa EOD dự phòng. Không dùng giá này để chấm lại tín hiệu.
- **Biểu đồ:** dùng TradingView Lightweight Charts với nến EOD đã điều chỉnh từ database; có MA20/50/200, Bollinger, RSI14, đường xu hướng và đường giá ngang. Khi mở mã, app lấy riêng nến OHLCV của phiên hiện tại từ DNSE và kiểm tra lại mỗi 30 giây khi đang xem biểu đồ. Nến này được gắn nhãn **tạm tính**, quy đổi bằng tỷ lệ điều chỉnh EOD gần nhất để vẽ cùng thang giá; không ghi DB và không dùng cho tín hiệu/backtest. Nến và nhãn thông tin chớp sáng ngắn **chỉ khi OHLCV thay đổi**, không nhấp nháy liên tục khi giá đứng yên. Giá khớp mới (nếu có trong 15 phút) hiển thị riêng. Khi DB đã có nến EOD cùng ngày, không chèn nến tạm trùng ngày. Nét vẽ lưu trên thiết bị theo từng mã. Logo TradingView trên nến được ẩn; thông báo bản quyền và liên kết được giữ ở chân biểu đồ theo yêu cầu ghi nguồn của thư viện.
- **Hỏi AI:** gửi dữ liệu nội bộ của đúng mã, giao dịch DNSE gần nhất, nến OHLCV phiên hiện tại (nếu có) và câu hỏi sang luồng phân tích AI. Nến phiên chỉ là thông tin tạm tính, không đổi tín hiệu. Tin mới chỉ được nêu khi có nguồn kiểm chứng.
- **Paper Trading:** sổ lệnh riêng tại `analysis_data/paper_trading.sqlite`, vốn mặc định 1 tỷ đồng, phí giả định 0,15% mỗi chiều. Lệnh ưu tiên giá khớp DNSE có timestamp còn mới (15 phút), nếu không có thì dùng giá đóng cửa **gốc** phiên gần nhất trước ngày hiện tại. Danh mục hiển thị định giá tạm theo giá khớp mới khi có, quy đổi theo tỷ lệ điều chỉnh EOD gần nhất; không thay đổi lệnh đã ghi. Mỗi lệnh lưu nguồn giá để kiểm tra lại. Không dùng tiền thật và không ghi vào kho giá.
- **Chiến lược & Backtest:** giải thích quy tắc thống nhất FA + giá/khối lượng, hiển thị CAGR, benchmark, alpha, Sharpe, drawdown, giao dịch và giả định kiểm chứng.

## Tích hợp Telegram

Sau khi deploy bằng HTTPS, đặt `WEBAPP_URL=https://...` trong `.env` rồi khởi động lại bot. Nút **Mở X10 Web App** sẽ xuất hiện trong menu Telegram.

Các endpoint AI và Paper Trading hiện bắt buộc `Telegram.WebApp.initData` hợp lệ. Khi có `PAPER_DATABASE_URL` hoặc `SUPABASE_DB_URL`, sổ lệnh và bộ đếm giới hạn AI được lưu trên PostgreSQL/Supabase theo Telegram user ID. Nếu không có URL PostgreSQL, backend chỉ dùng SQLite tách riêng từng user cho phát triển cục bộ.
