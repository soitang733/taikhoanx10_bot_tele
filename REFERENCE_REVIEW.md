# Tham khảo Python_Goi1_GuiBaoNguyen.zip

Đã đọc 6 file Python trực tiếp trong ZIP; không chạy các script của bộ tham khảo.
Các CSV kết quả đi kèm không được nhập vào kho dữ liệu của dự án.

## Những điểm đã áp dụng

- **Khớp lệnh phiên sau:** tín hiệu dùng đóng cửa t, mua/bán ở đóng cửa t+1.
  Mua tại t+1 không được hưởng mức tăng từ t đến t+1. Giá khớp là adjusted close.
- **Tiền mặt và phí:** chia tiền còn lại cho các chỗ trống; không dồn 100% vào
  một mã khi danh mục còn thiếu mã. Phí mặc định 0,15% mỗi chiều, là giả định
  nghiên cứu; tính trên giá trị thực mua/bán. Mã đang giữ không tự tái cân bằng.
- **Thiếu phiên:** không khớp ở phiên thiếu giá hoặc volume bằng 0. Lệnh mua
  không khớp bị hủy; lệnh thoát được chờ đến phiên có giao dịch. Khi định giá
  tạm bằng giá cũ, báo cáo đếm số mã-phiên dùng giá cũ và tính đủ biến động khi có giá mới.
- **Sổ giao dịch:** `analysis_data/backtest_trades.csv` có ngày/giá mua, ngày/giá bán,
  số phiên giữ, lãi trước/sau phí. Lệnh còn mở được ghi OPEN, không gộp vào tỷ lệ thắng.
- **FA đúng năm:** ghép báo cáo theo mã và năm thực tế. Thiếu năm Y−1/Y−3 thì
  giữ trống chỉ số tăng trưởng; nợ vay và thành phần EBITDA thiếu không tự điền 0.
- **Thử từng thay đổi:** baseline, bỏ điểm momentum/RS/trend/breakout/volume,
  bỏ trailing stop, đổi MA thoát sang MA50. Tổng cộng 8 kịch bản, vẫn dùng điểm
  thống nhất FA + giá/khối lượng. Bỏ điểm một yếu tố không có nghĩa bỏ mọi cách
  dùng biến đó: ví dụ momentum vẫn tham gia phá hòa thứ hạng.

## Cách chạy và đọc kết quả

```powershell
.\.venv\Scripts\python.exe backtest_engine.py --compare-components
```

Chỉ dữ liệu 2019–2024 được dùng chọn ứng viên: Sharpe cao nhất, hòa thì drawdown
nhẹ hơn, trung bình ít nhất 5 vị thế. Chỉ baseline và ứng viên được so trên
2025/2026 và khi phí gấp đôi. Danh mục được chạy liên tục qua các giai đoạn.
Kết quả nằm ở `analysis_data/strategy_ablation.json`. Lệnh không đổi `strategy_config.json`.
2025 và 2026 đã từng được xem ở các lượt trước, nên không gọi là tập kiểm tra chưa từng thấy.

Kết quả chạy ngày 21/09/2026, dữ liệu giá kết thúc 18/09/2026:

| CAGR quy năm | Cấu hình hiện tại | Bỏ điểm breakout |
|---|---:|---:|
| Train 2019–2024 | 14,82% | 16,82% |
| 2025 | 21,53% | 18,81% |
| 2026 đến phiên cuối | −11,16% | −13,49% |

Ứng viên bỏ breakout đứng đầu train nhưng kém hơn ở cả hai giai đoạn sau,
nên giữ nguyên tham số hiện tại. CAGR toàn kỳ của cấu hình hiện tại sau sửa là
12,49%, Sharpe 0,86, drawdown −30,83%; 1.125 lệnh đóng và 28 lệnh còn mở.
Đây là kết quả tính lại với mô hình đúng hơn, không phải bằng chứng đã nâng lợi nhuận.

## Những phần không bê nguyên từ ZIP

- `signal_rules.py` dùng FA làm cổng chặn riêng; dự án giữ điểm FA + giá/khối lượng thống nhất.
- `fa_metrics.py` đặt nhiều hàm trong `if __name__ == '__main__'`, nên import không dùng được các hàm ấy.
- `technical_core.py` tính Top30_Momentum và BreakoutValid (3 phiên), nhưng
  `ta_pipeline.py` chưa sử dụng chúng trong TA_PASS. Chưa đủ bằng chứng để bật chúng mặc định.
- Bộ TA đọc lại lịch sử từng mã nhiều lần; dự án tiếp tục nạp dữ liệu theo lô.
- Backtest của ZIP chỉ kiểm tra có báo giá khi khớp, chưa kiểm tra volume > 0.
  Bộ mô phỏng cập nhật của dự án có kiểm tra này.

Kết quả cũ không so trực tiếp với kết quả mới vì cách khớp lệnh, phân bổ tiền và
FA đã thay đổi. Phiên bản mô phỏng được lưu trong báo cáo để không tái dùng
số liệu tối ưu cũ. Vẫn là mô hình đơn giản dùng đơn vị cổ phiếu điều chỉnh có thể
lẻ: chưa mô phỏng lô giao dịch, thanh toán, trượt giá hay hạn chế khớp lệnh thực tế.
