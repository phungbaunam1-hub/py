Giữ nguyên ứng dụng Python AI_MT_Trading.py người dùng cung cấp và bổ sung Order Flow như dock riêng.

- Giữ Panel giao dịch/quản lý, Phân tích/Tài khoản/Lệnh/Nhật ký, tám phương pháp, Chat AI, AI Web, tin và cài đặt.
- Thêm nút Order Flow / Ctrl+Shift+O: chart phụ, big trades, DOM, wall/sweep, heatmap, ngưỡng tùy chỉnh.
- Đọc trade prints/DOM MT5 khi broker cấp dữ liệu; demo được ghi nhãn riêng. Rithmic vẫn cần SDK/adapter chính thức.
- Gói Windows đã được mở bằng smoke test trên runner Windows trước khi upload.

**Cách chạy:** tải `AI-MT-Trading-Python-Windows-x64.zip` → Extract All → mở thư mục `AI_MT_Trading` → chạy `AI_MT_Trading.exe`. Không chạy file FlowDesk.Desktop.exe cũ.

Giữ thư mục `_internal` cạnh EXE. Không cần cài Python. Dữ liệu và giao dịch thật vẫn cần terminal/broker/credentials tương ứng; chưa được kiểm thử bằng tài khoản thật.
