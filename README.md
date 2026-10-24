# AI MT Trading · Python gốc + Crypto Workspace

Bản chính hiện tại **giữ ứng dụng Python ban đầu**, không thay bằng giao diện FlowDesk WPF.

## Tải và chạy

**[Tải ứng dụng Python cho Windows x64](https://github.com/phungbaunam1-hub/py/releases/latest/download/AI-MT-Trading-Python-Windows-x64.zip)**

Giải nén → mở thư mục **AI_MT_Trading** → chạy **AI_MT_Trading.exe**. Giữ thư mục `_internal` bên cạnh EXE. Không cần cài Python.

Bản Windows được đóng gói trên GitHub Actions và chỉ xuất bản sau khi mở/kiểm chứng chính EXE thành công. Xem [Releases](https://github.com/phungbaunam1-hub/py/releases) hoặc [tiến trình build](https://github.com/phungbaunam1-hub/py/actions/workflows/python-original.yml) nếu gói chưa xuất hiện.

[Tải mã nguồn Python riêng](https://github.com/phungbaunam1-hub/py/raw/refs/heads/main/downloads/AI-MT-Trading-Python-source.zip) · [Hướng dẫn đầy đủ](python/README.md)

## Giữ lại và bổ sung

- Giữ file Python gốc nguyên byte: Panel giao dịch/quản lý, chart/overlay, sơ đồ và tám phương pháp phân tích, tab Phân tích/Tài khoản/Lệnh/Nhật ký, Chat AI, AI Web, tin tức, đa tài khoản và cài đặt.
- Thêm tab chart bằng **+**; danh mục public Binance, Bybit, OKX, Kraken và toàn bộ symbol broker MT5, có lọc crypto/forex–fiat. Spot/perpetual theo nguồn, trade stream cho tab đang xem, REST DOM được ghi tuổi dữ liệu.
- Gom tín hiệu **BUY / SELL / SIDEWAYS** và kết luận AI; mọi đường truy vấn AI có bối cảnh dữ liệu/vị thế/rủi ro và futures khi có. Kiểm tra phạm vi kết quả trước thực thi MT5.
- Cạnh **HK** có **Q xác nhận lệnh**; bỏ tích để hotkey thực hiện ngay. Thêm **Kiểm chứng** walk-forward có phí/slippage và các nhóm phương pháp định lượng.
- Thêm nút **Order Flow** cạnh thanh lệnh nhanh / **Ctrl+Shift+O**. Dock bổ sung có nến/prints, DOM, displayed walls, sweeps, heatmap và ngưỡng tùy chỉnh; không thay bố cục khởi động gốc.
- Nguồn **DEMO** được ghi nhãn rõ. Nguồn **MT5 đang chọn** chỉ có trades/DOM nếu broker cung cấp; không biến quotes hoặc tick-volume của nến thành big trade thật.
- **Rithmic chưa kết nối được**: vẫn thiếu SDK/adapter plugin chính thức. Không có kết nối giả hoặc tự chuyển sang demo.

Các chức năng MT5/AI/giao dịch gốc giữ nguyên yêu cầu terminal, tài khoản và cấu hình của chúng. Addon order flow không gửi lệnh và không tự bật auto trade.

## Mã nguồn

```text
python/
  original/AI_MT_Trading.py       # File người dùng, giữ nguyên
  run_aimt.py                    # Launcher ứng dụng gốc + addon
  aimt_orderflow/                # Engine, nguồn dữ liệu, dock và overlay bổ sung
  aimt_market/                   # Public crypto, chart tabs, AI context, định lượng/walk-forward
  tests/                        # Thuật toán, dữ liệu, GUI và tính năng gốc
  Start-Windows.bat
  Build-Windows.ps1
```

[Đối chiếu với bản gốc](docs/ORIGINAL-FEATURE-AUDIT.md). Bản C# FlowDesk cũ vẫn nằm trong `src/` và `tests/` để tham khảo; [tài liệu bản WPF cũ](docs/FLOWDESK-WPF.md). File **FlowDesk.Desktop.exe** là bản demo cũ, không phải ứng dụng Python đã khôi phục.

[Phương pháp và giới hạn nghiên cứu](docs/MARKET_METHODS.md) · [Nguồn public và đơn vị dữ liệu](docs/PUBLIC_DATA.md). HFT execution, dự báo lượng tử và router đặt lệnh crypto chưa được triển khai. [Kết quả kiểm thử/Windows](docs/PYTHON-VALIDATION.md) được cập nhật cùng bản phát hành.
