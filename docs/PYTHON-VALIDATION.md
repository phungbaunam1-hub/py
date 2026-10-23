# Bản Python khôi phục — kết quả kiểm chứng

Người dùng đã xác nhận giữ Python gốc và bổ sung tính năng, thay vì chuyển sang giao diện WPF.

- `python/original/AI_MT_Trading.py` giữ nguyên SHA-256 của tệp được gửi: `b6265fb39c54e0235926be87544c5cdf177ad7728e33d582ff0fdc3554fc0909`.
- Linux/Qt offscreen: **31 tests passed**, không skip. Có test mở Main gốc, đủ bốn tab/Panel/Chat/AI Web, chạy pipeline tám phương pháp, demo order flow, không đặt lệnh, filters, cursor trùng ticks, depth, dừng feed và cleanup không đăng nhập lại tài khoản cũ.
- [Windows CI #2](https://github.com/phungbaunam1-hub/py/actions/runs/39956383980) hoàn thành thành công: test Python, PyInstaller, mở chính EXE, xác nhận đủ tám kết quả phân tích, bốn tab gốc, Panel/Chat/Web, prints/depth demo, module MetaTrader5 và WebEngine/helper.
- [Release đã xuất bản](https://github.com/phungbaunam1-hub/py/releases/tag/python-original-2). ZIP Windows tự chứa có WebEngine, vì vậy lớn hơn bản FlowDesk WPF cũ.
- Nguồn MT5 thật cần broker cung cấp trade prints/DOM và terminal/tài khoản của người dùng; chưa chạy bằng tài khoản thật. AI API chưa được xác minh bằng credentials. Rithmic vẫn chưa có SDK/adapter chính thức.
- `scripts/setup-cloud-python.sh` đã chạy thành công trong môi trường cloud; cấu hình cài đặt/khởi động đã được lưu vào draft môi trường.

Ảnh dưới đây được lấy từ chính EXE chạy trên Windows CI, không phải mockup:

![Ứng dụng Python gốc và nút Order Flow bổ sung](images/python-restored-windows.png)
