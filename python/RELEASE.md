Giữ ứng dụng Python AI_MT_Trading.py gốc và thêm Crypto Workspace.

- Giữ Panel giao dịch/quản lý, Phân tích/Tài khoản/Lệnh/Nhật ký, tám phương pháp, Chat AI, AI Web, tin và cài đặt.
- Thêm nút Order Flow / Ctrl+Shift+O: chart phụ, big trades, DOM, wall/sweep, heatmap, ngưỡng tùy chỉnh.
- Đọc trade prints/DOM MT5 khi broker cấp dữ liệu; demo được ghi nhãn riêng. Rithmic vẫn cần SDK/adapter chính thức.
- Chart tabs có dấu +: Binance, Bybit, OKX, Kraken và danh mục MT5; tìm cặp crypto/forex–fiat, spot/perpetual theo nguồn. Trade WebSocket cho chart đang xem; DOM là REST snapshot.
- Tín hiệu BUY/SELL/SIDEWAYS và kết luận AI ngắn, có bối cảnh chart/order flow/futures/vị thế/rủi ro. Phạm vi dữ liệu, nguồn, đơn vị và các phần bị cắt đều ghi rõ trong prompt.
- Ô Q xác nhận lệnh cạnh HK; bỏ tích để hotkey thực hiện ngay. Chặn phím giữ lặp và kết quả AI sai chart/tài khoản/symbol hoặc đã cũ.
- Phương pháp định lượng và kiểm chứng walk-forward có phí/slippage; nghiên cứu long/flat, chưa có HFT execution, dự báo lượng tử hay router lệnh crypto.
- Gói Windows đã được mở bằng smoke test trên runner Windows trước khi upload.

**Cách chạy:** tải `AI-MT-Trading-Python-Windows-x64.zip` → Extract All → mở thư mục `AI_MT_Trading` → chạy `AI_MT_Trading.exe`. Không chạy file FlowDesk.Desktop.exe cũ.

Giữ thư mục `_internal` cạnh EXE. Không cần cài Python. Dữ liệu và giao dịch thật vẫn cần terminal/broker/credentials tương ứng; chưa được kiểm thử bằng tài khoản thật.

Các nguồn public có thể bị giới hạn theo mạng/vùng. Xem `public-market-probe.json` đính kèm để biết kết quả truy cập thật từ runner Windows; test thuật toán và GUI không tự chứng minh live API khả dụng.
