# Đối chiếu với AI_MT_Trading.py gốc

Bản FlowDesk đầu tiên đã xây một workspace order flow mới thay vì chuyển đầy đủ ứng dụng gốc. Đây là thay đổi phạm vi không đáp ứng mong muốn giữ giao diện và tính năng ban đầu. Bảng này ghi nhận phần cần khôi phục, không phải danh sách tính năng đã hoàn thành.

Nguồn đối chiếu: tệp AI_MT_Trading.py người dùng đính kèm; các lớp Main, PanelDock, ChatDock, WebAIDock, SettingsDialog và các bộ phân tích.

| Nhóm | Bản Python gốc | FlowDesk hiện tại |
| --- | --- | --- |
| Thanh công cụ | Chọn tài khoản, symbol, timeframe, preset phân tích, khẩu vị rủi ro, BEST/SCALP/SWING/PO3, Panel, Chat, AI Web, Phân tích, tự động, TV, tin, cài đặt | Chưa chuyển; thay bằng cột kết nối/order-flow |
| Chart | Nến, overlay phân tích/kế hoạch, zoom, kéo/cuộn, điều chỉnh trục giá, phím tắt | Có nến/volume/zoom/bubble; thiếu overlay và các tương tác gốc |
| Sơ đồ tín hiệu | NodeGraph kết quả các phương pháp và kết hợp tín hiệu | Thiếu |
| Phân tích | VSA, Price Action, ICT, Chart Pattern, Gann, Harmonic, Wyckoff, Elliott; regime và kế hoạch giao dịch | Thiếu |
| Thẻ kế hoạch | Hướng, điểm số, mức SL/TP và lựa chọn kế hoạch | Thiếu |
| Panel giao dịch | Lot cố định/%/$, SL/TP, BUY/SELL, lệnh chờ, đóng/trim/BE/reverse và quản lý vị thế | Thiếu; FlowDesk chỉ đọc dữ liệu, không gửi lệnh |
| Tự động hóa/rủi ro | Auto trade, trailing, break-even, partial TP, DCA, giới hạn lỗ theo tài khoản | Thiếu |
| Đa tài khoản | Thêm/sửa/xóa tài khoản, bật/tắt, tổng hợp equity/P&L, chi tiết lệnh, copy | Thiếu; chỉ có nhãn phiên demo |
| Chat AI | Gemini, OpenAI, Claude, DeepSeek/custom, lịch sử hội thoại, kèm phân tích/ảnh chart | Thiếu |
| AI Web | Dock web và trao đổi prompt/ảnh/phản hồi | Thiếu |
| Tin tức | RSS, danh sách tin, đưa tin vào ngữ cảnh phân tích | Thiếu |
| Lệnh nhanh/phím tắt | Command bar, F1/F2, hotkeys cho giao dịch và panel | Thiếu |
| Cấu hình/docking | Settings, lưu cấu hình, panel nổi/tab, tích hợp cửa sổ MT5 | Thiếu |
| Tab dưới chart | Phân tích, Tài khoản, Lệnh, Nhật ký | Chỉ có tape và log/tín hiệu order flow |
| Order flow bổ sung | Không có bộ Rithmic trade/depth chính thức | Demo big trades, DOM, wall/sweep, heatmap đã triển khai; chưa có live Rithmic |

## Nguyên tắc sửa

Giữ bố cục và chức năng gốc làm nền; thêm order flow như phần mở rộng. Không thay panel gốc bằng bảng mô phỏng mới. Ghi rõ chức năng nào hoạt động, cần cấu hình, chưa được chuyển hoặc bị chặn bởi SDK. Không dùng nút chỉ ghi log thay cho chức năng thực tế, và không coi việc thêm giao diện là đã khôi phục logic.

## Lựa chọn đã xác nhận và bản sửa

Người dùng chọn **giữ Python gốc, bổ sung tính năng**. Bản chính mới nằm trong `python/`, với `original/AI_MT_Trading.py` giữ nguyên hash SHA-256 của tệp được gửi: `b6265fb39c54e0235926be87544c5cdf177ad7728e33d582ff0fdc3554fc0909`.

`run_aimt.py` khởi động Main gốc và thêm nút/dock Order Flow. Các nhóm tính năng gốc trong bảng được giữ bằng chính mã gốc, không phải bản mô phỏng WPF hoặc các nút rỗng. Có kiểm thử mở GUI, giữ bốn tab/Panel/Chat/AI Web, chạy đủ tám phương pháp, dừng demo, thay nguồn và không tự bật giao dịch. Những tích hợp cần tài khoản/terminal/API key vẫn cần cấu hình và kiểm chứng thật; giữ mã không có nghĩa đã kiểm thử các tích hợp đó bằng credentials.

WPF và ZIP FlowDesk cũ được giữ làm bản thử nghiệm tham khảo. Chúng không phải bản khôi phục giao diện Python.
