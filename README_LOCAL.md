# VNStock local (Python + VS Code)

Project chạy bằng Python thuần, không dùng Google Colab hoặc Notebook.

Trước khi đăng ký Scheduled Task hoặc chuyển sang máy chạy thật, chạy
`./deploy_check.ps1`. Script kiểm tra cấu hình, dependency, toàn bộ test và độ
đồng bộ dữ liệu mà không in giá trị bí mật trong `.env`.

## Chạy dashboard

```powershell
.\run_stock_api.ps1
```

Mở <http://127.0.0.1:8765/>. Dashboard chỉ đọc database local, không gọi DNSE
mỗi lần người dùng tra cứu.

## Tạo lại tín hiệu, tối ưu và kiểm tra

```powershell
.\.venv\Scripts\python.exe .\robust_optimizer.py --count 384
.\.venv\Scripts\python.exe .\strategy_engine.py
.\.venv\Scripts\python.exe .\backtest_engine.py --compare-components
.\.venv\Scripts\python.exe -m unittest -v .\test_strategy_engine.py .\test_pipeline_safety.py .\test_robust_optimizer.py
.\.venv\Scripts\python.exe .\validate_analysis_data.py
```

Nếu raw mirror thiếu file nhưng SQLite hiện có đã đủ dữ liệu, chạy
`.\.venv\Scripts\python.exe .\materialize_raw_baseline.py`. Lệnh chỉ bù file raw
còn thiếu, không ghi đè dữ liệu nguồn đã tải.

## Phương pháp thống nhất

- `Unified Score = 20% FA hiệu dụng + 80% điểm giá/khối lượng`.
- FA hiệu dụng được co về mức trung tính theo độ phủ dữ liệu. Thiếu chỉ tiêu FA
  không tự động loại mã, nhưng điều kiện loại trừ cứng (lợi nhuận/vốn chủ không
  hợp lệ, CFO âm hai năm hoặc diện hạn chế) luôn chặn BUY ở cả tín hiệu và backtest.
- Điểm giá/khối lượng gồm Momentum 3M/6M/12M (30%), RS6M (25%), xu hướng (25%),
  breakout 20 phiên (15%) và Volume Ratio (5%). Nếu hai mã hòa Unified Score, ưu tiên
  sức mạnh liên tục của R3M/R6M/R12M/RS6M, không ưu tiên theo mã ticker.
- Chỉ xét mã có giá trị giao dịch bình quân 20 phiên từ 2 tỷ đồng.
- Chỉ mở vị thế khi VNINDEX ở trên MA50; chọn tối đa Top 30 theo Unified Score.
- Entry Score 60; thoát khi Unified Score dưới 35 sau tối thiểu 10 phiên, giá dưới
  MA200, hoặc giá thấp hơn 15% so với đỉnh đóng cửa của 60 phiên trước. Cooldown 5 phiên sau khi thoát.
- Return dùng tháng lịch; cổ phiếu và VNINDEX dùng đúng cùng ngày đầu/cuối kỳ.
- Tín hiệu hình thành sau đóng cửa `t`, khớp tại đóng cửa `t+1`; bắt đầu hưởng
  biến động giá từ `t+1` đến `t+2`. Tính phí cả mua và bán; chia tiền mặt cho số
  chỗ còn trống trong danh mục, không tự cân bằng lại mã đang giữ.

## Xử lý FA lịch sử

Không cần `published_date` chính xác. Backtest giả định báo cáo năm khả dụng sau
90 ngày kể từ `period_end`. Snapshot FA hiện tại không được áp ngược vào quá khứ.
Đây là giả định bảo thủ để tránh nhìn trước tương lai, không phải ngày công bố thật.
Tín hiệu hiện tại dùng cùng cơ sở FA theo báo cáo năm; không trộn market cap/snapshot
chỉ biết ở thời điểm hiện tại. Không thể chứng minh FA point-in-time chính xác khi
chưa có ngày công bố thật.

WATCH không đồng nghĩa có thể mua ngay: xem `watch_reason` trên web để phân biệt
thanh khoản thấp, giá cũ, thị trường phòng thủ, điểm thấp hoặc ngoài Top 30.
BUY/EXIT của mô hình được sinh từ danh mục EOD có trạng thái trong `signals.sqlite`: BUY ngày T chờ thực hiện T+1, vị thế sau đó nhận HOLD hoặc EXIT, và mã đã bán chịu cooldown 5 phiên. Paper Trading đánh giá HOLD/EXIT riêng theo các vị thế của từng Telegram user; người dùng vẫn tự xác nhận lệnh demo.

## File chính

- `analysis_data/stocks_analysis.sqlite`: dữ liệu giá, VNINDEX và tài chính.
- `analysis_data/signals.sqlite`: Unified Score và tín hiệu mới nhất.
- `analysis_data/robust_optimization.json`: quét tham số, walk-forward và các cổng kiểm định.
- `analysis_data/backtest_report.json`: kết quả backtest cuối.
- `analysis_data/backtest_trades.csv`: từng lệnh đóng/mở, giá khớp và lợi nhuận sau phí.
- `analysis_data/strategy_ablation.json`: thử bỏ từng thành phần, chọn bằng train,
  sau đó so baseline và ứng viên trên 2025/2026, kèm thử phí gấp đôi.

Xem [REFERENCE_REVIEW.md](REFERENCE_REVIEW.md) để biết những cải tiến tham khảo từ bộ ZIP.

Giới hạn còn lại: database không có lịch sử mã đã hủy niêm yết, vì vậy vẫn có
survivorship bias. Tối ưu xếp hạng bằng 2019–2024; năm 2025 là validation. Năm 2026
đã được xem khi chẩn đoán phương pháp, nên chỉ được ghi là stress diagnostic chứ không phải holdout sạch.

## Tối ưu chặt chẽ

```powershell
.\.venv\Scripts\python.exe .\robust_optimizer.py --count 384
```

Lệnh này chọn tham số bằng dữ liệu 2019–2024, rồi kiểm tra 2025, stress 2026,
phí gấp đôi, FA trễ thêm 90 ngày, walk-forward và độ nhạy tham số. Nó không tự
ghi đè `strategy_config.json`; kết quả nằm trong
`analysis_data/robust_optimization.json`. Xem [OPTIMIZATION_RESEARCH.md](OPTIMIZATION_RESEARCH.md)
để đọc kết luận hiện tại.
