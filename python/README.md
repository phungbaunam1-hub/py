# AI MT Trading gốc + Order Flow bổ sung

**Đây là bản giữ ứng dụng Python bạn gửi, không phải giao diện FlowDesk WPF.**

Tệp `original/AI_MT_Trading.py` được giữ nguyên byte-for-byte. Launcher `run_aimt.py` nạp ứng dụng đó và thêm một dock độc lập. Không thay chart chính, tám phương pháp phân tích, sơ đồ tín hiệu, Panel giao dịch/quản lý, tài khoản, copy, Chat AI, AI Web, tin tức hoặc cài đặt bằng các bảng giả.

## Chạy bản Windows đóng gói

1. Tải `AI-MT-Trading-Python-Windows-x64.zip` từ [GitHub Releases](https://github.com/phungbaunam1-hub/py/releases).
2. **Extract All / Giải nén tất cả**; mở thư mục `AI_MT_Trading`.
3. Chạy **`AI_MT_Trading.exe`**. Giữ nguyên thư mục `_internal` đi cùng; bản này không cần cài Python.
4. Sử dụng các panel như ứng dụng gốc. Nút **Order Flow** cạnh thanh lệnh nhanh mở dock bổ sung; phím tắt **Ctrl+Shift+O**. Dock có thể kéo/nổi/đóng, không thay các tab cũ.

Chọn nguồn trong dock, rồi bấm **Kết nối Order Flow**:

- **DEMO order flow:** prints/depth giả được sinh riêng, không phải dữ liệu giao dịch của chart MT5, không gửi lệnh. Chart phụ của dock thể hiện chính dữ liệu demo này; không ghép bubble demo vào chart gốc để tránh lẫn nguồn.
- **MT5 đang chọn:** dùng terminal/tài khoản đã cấu hình trong ứng dụng gốc. Đọc `copy_ticks_range(..., COPY_TICKS_TRADE)` và `market_book_get`; không tạo volume từ quotes. Chỉ có dữ liệu nếu broker thực sự cấp trades/DOM cho symbol đó. CFD/forex thường chỉ có quotes: lúc đó tape/bubble/DOM có thể trống và có thông báo tương ứng. Đơn vị size theo nguồn, không mặc định là contracts của CME.
- **Rithmic plugin:** chưa khả dụng vì chưa có SDK/adapter được cấp quyền. Hiển thị lỗi rõ ràng, không gọi API giả và không tự chuyển nguồn.

Các threshold big trade, displayed wall, volume/số mức giá/thời gian sweep và timeframe nến của dock có thể chỉnh. Bubble lên chart gốc chỉ áp dụng cho nguồn MT5 thật trùng symbol, không thay zoom/pan/trục giá/overlay/kế hoạch/lệnh của chart gốc. Thay nguồn/tài khoản/symbol bắt đầu lại phiên order flow; thay timeframe của dock không đổi timeframe của chart gốc.

Wall chỉ là tổng displayed size ở một mức giá; sweep là heuristic dựa trên prints. Không xác định từng lệnh limit, iceberg hoặc stop order. Marketable limit có thể tạo prints giống market order. Công thức không phải MZpack độc quyền.

## Chức năng gốc và cấu hình

Chức năng MT5, quản lý lệnh, copy, auto trade và AI vẫn là **mã gốc**. Giữ nguyên điều kiện sử dụng của chúng: terminal/account đúng cấu hình, quyền broker, API key nếu dùng AI API. Tự động giao dịch không được addon bật thay bạn. Chưa xác minh tài khoản/broker/AI thật trong môi trường build này.

Bản `.exe` dùng `%LOCALAPPDATA%\AI_MT_Trading\ai_mt_trading_config.json` khi bạn không đặt `AIMT_CONFIG`. Nếu đã có file cấu hình gốc, bạn có thể sao lưu rồi chép nó vào đó để sử dụng lại; không gửi file chứa mật khẩu/API key lên GitHub hoặc chat. Để tự chọn vị trí, đặt biến `AIMT_CONFIG` trước khi mở chương trình. Addon chỉ lưu thêm khóa `orderflow`, không ghi đè các cấu hình gốc khác.

Gói đầy đủ có PyQt6-WebEngine để giữ khả năng mở AI Web trong khung. Các dịch vụ AI có thể từ chối đăng nhập từ trình duyệt nhúng; nút **Mở ngoài trình duyệt** và quy trình clipboard của bản gốc vẫn có sẵn.

## Chạy từ mã nguồn

Windows x64 với Python 3.12: có thể nhấp đúp **Start-Windows.bat**, hoặc chạy:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-web.txt
.venv\Scripts\python run_aimt.py
# Ép chạy mô phỏng của bản gốc, không kết nối MT5:
.venv\Scripts\python run_aimt.py --demo
```

Linux có thể chạy GUI/demo bằng `requirements.txt`; MetaTrader5 chỉ cài trên Windows. WebEngine là dependency tùy chọn khi chạy source, có trong gói Windows đầy đủ.

## Kiểm thử và đóng gói

Từ thư mục gốc kho:

```sh
python -m pip install -r python/requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest python/tests -q
```

Windows: `./python/Build-Windows.ps1`. GitHub workflow `Python original + Order Flow` chạy tests, đóng gói bằng PyInstaller rồi **mở chính EXE** với `--smoke-test`. Chỉ khi smoke test thành công mới xuất bản ZIP qua GitHub Releases. Bản `.exe` không được tạo bằng cách đổi tên tệp Linux.

Test bao gồm: byte hash của bản gốc, bốn tab/panel/chat/web giữ lại, pipeline tám phương pháp, demo tạo nến/wall/sweep, nguồn Rithmic không giả thành công, volume/side, tick overlap, depth replacement, cancellation/dừng hiển thị và cấu hình addon tách riêng. Live MT5/Rithmic và AI có credentials cần xác minh trên máy bạn.
