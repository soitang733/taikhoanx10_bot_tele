# Nghiên cứu tối ưu chiến lược

## Kết luận hiện tại

- Đã quét cố định 384 cấu hình với dữ liệu train 2019–2024.
- Sau khi cập nhật giá đến 21/09/2026, ứng viên số 25 tốt hơn trong train và
  toàn kỳ, nhưng kém cấu hình hiện tại trong kiểm định 2025 và walk-forward.
  Vì vậy không được đưa vào vận hành.
- Giữ nguyên `strategy_config.json`. Kết quả này quan trọng hơn việc chọn một
  bộ tham số có CAGR cao nhưng không bền qua các giai đoạn thị trường.

## Thử nghiệm giảm vòng quay và giới hạn rủi ro (22/09/2026)

Chạy `portfolio_experiments.py` với bốn cấu hình định trước: gốc, giảm vòng quay
(exit score 30, giữ tối thiểu 20 phiên, chờ mua lại 10 phiên), giới hạn tỷ trọng
mua mới theo biến động 20 phiên, và kết hợp hai cách. Mỗi cấu hình được chạy thêm
với phí một chiều 0,30% cùng giới hạn lệnh 5% giá trị giao dịch bình quân 20 phiên
cho danh mục giả định 1 tỷ đồng. Mô hình giới hạn lệnh là all-or-nothing, chỉ là
stress test; **không** thay thế dữ liệu sổ lệnh và giá khớp thực tế.

| Biến thể | Train CAGR | 2025 CAGR | 2026 CAGR quy năm | Toàn kỳ CAGR | Toàn kỳ MDD |
|---|---:|---:|---:|---:|---:|
| Gốc | 14,69% | 21,60% | -11,15% | 12,40% | -29,29% |
| Giảm vòng quay | 13,47% | 23,98% | -4,25% | 12,50% | -30,7% |
| Giới hạn biến động | 14,44% | 17,90% | -8,82% | 12,03% | -29,7% |
| Kết hợp | 11,46% | 21,39% | -8,04% | 10,32% | -33,0% |

Không nâng cấp cấu hình: phương án giảm vòng quay chỉ tăng CAGR toàn kỳ 0,10
điểm phần trăm nhưng Sharpe train giảm và drawdown xấu hơn; phương án giới hạn
biến động được chọn trên train nhưng kém cấu hình gốc ở 2025 và toàn kỳ. Kết quả
chi tiết và các cổng kiểm định nằm trong `analysis_data/portfolio_experiments.json`.
2025/2026 đã được quan sát từ trước nên mức cải thiện trong các năm này không
được coi là bằng chứng ngoài mẫu độc lập.

## Cách kiểm tra

1. Tín hiệu hình thành ở đóng cửa phiên `t`, khớp ở đóng cửa phiên `t+1`.
2. Chọn duy nhất trên 2019–2024; không đổi sang ứng viên khác sau khi xem 2025/2026.
3. Walk-forward theo năm, mỗi giai đoạn chỉ dùng quá khứ để chọn tham số.
4. Thử phí gấp đôi, FA xuất hiện trễ thêm 90 ngày và nhiều tham số lân cận.
5. Không duyệt nếu drawdown/Sharpe gần đây hoặc toàn kỳ xấu đi rõ rệt.

## Kết quả chính

| Cấu hình | Train CAGR | 2025 CAGR | 2026 CAGR quy năm | Toàn kỳ CAGR | Toàn kỳ MDD |
|---|---:|---:|---:|---:|---:|
| Hiện tại | 14,69% | 21,60% | -11,15% | 12,40% | -29,29% |
| Ứng viên 25 | 17,59% | 14,54% | -8,26% | 13,99% | -18,04% |

Walk-forward: ứng viên từng năm chỉ thắng baseline về Sharpe trong 2/4 năm;
cổng quyết định yêu cầu đa số 3/4.

Lưu ý: trước khi đồng bộ phiên 21/09, ứng viên train là số 229 và có stress
2026 rất xấu. Việc ứng viên đổi khi dữ liệu được cập nhật cũng là lý do không
nên diễn giải một lần tìm tham số là “tối ưu tuyệt đối”.

## Nguồn phương pháp

- AQR, *Fact, Fiction and Momentum Investing*:
  https://www.aqr.com/Insights/Research/Journal-Article/Fact-Fiction-and-Momentum-Investing
- AQR, *Time Series Momentum*:
  https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum
- Bailey & López de Prado, *The Deflated Sharpe Ratio*:
  https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf

Đây là nghiên cứu hồi cứu, không phải khuyến nghị đầu tư. Database chỉ có
tập mã đang niêm yết hiện tại và không có ngày công bố BCTC chính xác,
nên vẫn có survivorship bias và giả định FA trễ 90 ngày.
