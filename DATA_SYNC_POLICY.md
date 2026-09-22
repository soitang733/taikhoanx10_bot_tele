# Cách cập nhật và kiểm tra dữ liệu giá

## Cập nhật hằng ngày

- Lấy **2 phiên giao dịch gần nhất của toàn bộ cổ phiếu HOSE/HNX** và VNINDEX.
- Gộp theo khóa `ticker + date` trong raw rồi tạo lại SQLite hoàn chỉnh. Hiện chưa dùng lệnh SQLite `UPSERT` tại chỗ.
- Bot, Web App, FA, TA và Backtest chỉ đọc database; không gọi DNSE trực tiếp khi người dùng hỏi.

Hai phiên gần nhất giúp cập nhật EOD nhanh, nhưng **không đủ để phát hiện mọi thay đổi lịch sử**. Sau corporate action, DNSE có thể sửa các mức giá từ nhiều tháng hoặc nhiều năm trước.

## Khi nào cần kiểm tra sâu

Một mã được tải lại full-history khi:

1. Một trong 20 mã kiểm tra corporate action luân phiên có thay đổi được xác nhận từ nguồn.
2. Checksum DNSE trong hai phiên gần nhất thay đổi và lần tải thứ hai tái hiện đúng thay đổi đó.
3. Một trong 10 mã kiểm tra lịch sử giá luân phiên đến lượt tải lại toàn bộ.

Việc kiểm tra đúng ngày không hưởng quyền, `T+1` và `T+3` là hướng cải tiến; mã hiện tại **chưa** lập lịch riêng theo các mốc đó. Hai phiên gần nhất cũng không thể tự phát hiện thay đổi chỉ xảy ra ở quá khứ xa.

## Tránh báo nhầm

Phải lưu và so sánh riêng:

- `dnse_checksum`: checksum của dữ liệu DNSE gốc.
- `row_checksum`: checksum của dữ liệu cuối cùng sau khi fallback KBS/VCI.

Không dùng `row_checksum` để kết luận DNSE đã sửa lịch sử. KBS/VCI có thể được chọn khác nhau tùy độ dài cửa sổ tải và tạo ra cảnh báo giả.

Khi DNSE khác ở cửa sổ gần nhất, hệ thống tải lại lần hai và chỉ refetch full-history nếu checksum DNSE mới tái hiện. Thay đổi nguồn fallback vẫn có thể ghi `price_revisions` nhưng không được gọi là DNSE restatement. Sau lượt cập nhật, hệ thống tạo lại signals.

## Luồng ngắn gọn

```text
Mỗi ngày: 2 phiên gần nhất của tất cả mã
                  ↓
       So sánh DNSE checksum
                  ↓
  Không đổi → gộp dữ liệu mới theo ticker/date
  Có đổi / có corporate action
                  ↓
       Xác nhận rồi refetch full đúng mã đó
                  ↓
   Ghi revision + tính lại TA/signals
```

Không tải lại full-history của toàn thị trường mỗi ngày.
