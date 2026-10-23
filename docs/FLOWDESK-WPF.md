# FlowDesk — WPF Order Flow Workspace

Ứng dụng desktop C# / .NET 8 WPF, kiến trúc MVVM, chart nến và order flow có thể chạy độc lập bằng dữ liệu mô phỏng. **Bản này chưa kết nối được Rithmic/R|Trader Pro.** Không gọi nó là bản giao dịch production hoặc bản thay thế đầy đủ cho NT8, Sierra Chart, Quantower hay Bookmap.

## Tải về máy

- **[Tải ứng dụng Windows x64 — ZIP khoảng 63 MB](https://github.com/phungbaunam1-hub/py/raw/refs/heads/main/downloads/FlowDesk-Windows-x64-demo.zip)**: giải nén, mở thư mục `FlowDesk`, chạy `FlowDesk.Desktop.exe`, chọn **Demo** rồi bấm **Connect**. Không cần cài .NET cho bản tự chứa này.
- [Tải mã nguồn riêng](https://github.com/phungbaunam1-hub/py/raw/refs/heads/main/downloads/FlowDesk-source.zip).
- [SHA-256 của hai gói](downloads/SHA256SUMS.txt).

Nếu trình duyệt mở trang GitHub thay vì tải, mở tệp trong thư mục `downloads` rồi chọn **Download raw**. Kho riêng tư yêu cầu đăng nhập GitHub bằng tài khoản có quyền truy cập.

## Phần đã có

- Connect/Disconnect bất đồng bộ, cancellation, nhật ký và trạng thái Disconnected/Connecting/Connected/Faulted.
- Nguồn **Demo** sinh 10 phút dữ liệu trước phiên và tiếp tục stream mỗi 150 ms; tài khoản `DEMO-SIMULATED`, không phải tài khoản broker.
- Chart OHLC theo số giây tùy chỉnh, volume, big-trade bubbles, đường giá hiện tại; lăn chuột để zoom 20–240 nến.
- Tape lọc theo khối lượng tối thiểu; DOM hai phía bid/ask, đánh dấu mức có tổng khối lượng chờ vượt ngưỡng.
- Heatmap của depth được lấy mẫu trong phiên; màu cho biết khối lượng tương đối, không phải dữ liệu lịch sử tải từ broker.
- Cumulative delta và cảnh báo sweep dựa trên prints, với ngưỡng khối lượng, số mức giá và cửa sổ thời gian có thể chỉnh.
- Bộ xử lý khóa dữ liệu đa luồng; UI được cập nhật bằng Dispatcher và timer 100 ms, không gọi WPF trực tiếp từ callback nguồn dữ liệu.
- Giới hạn bộ nhớ: 600 nến, 4.000 prints gần nhất, 300 tín hiệu, 1.000 mức book mỗi phía, 240 frame heatmap. Tape/DOM/tín hiệu trên màn hình cũng có giới hạn.

**Không có chức năng gửi/sửa/hủy lệnh, auto trade, AI/MT5, historical backfill, footprint hoặc phát hiện iceberg/MBO.** Các phần đó trong tệp Python được gửi kèm chưa được chuyển sang dự án này. Tệp Python là nguồn tham khảo MetaTrader 5, không chứa thư viện Rithmic.

## Chạy trên Windows

Windows 10/11 x64. Dùng .NET 8 SDK khi chạy từ mã nguồn (Visual Studio 2022 với workload .NET desktop development cũng được).

```powershell
dotnet restore FlowDesk.sln --locked-mode
dotnet build FlowDesk.sln -c Release --no-restore
dotnet run --project src/FlowDesk.Desktop -c Release --no-build
```

Chọn **Demo**, giữ `ES-DEMO`, `CME`, tick size `0.25`, rồi bấm **Connect**. Symbol demo chỉ là nhãn, không phải hợp đồng được tra cứu từ broker. Chart sẽ có dữ liệu sinh từ trước và tiếp tục chạy. Dùng **Apply filters** để áp dụng các ngưỡng. Thay timeframe sẽ xóa nến hiện tại và dựng lại từ các tick tiếp theo; delta theo phiên được giữ lại. **Disconnect** dừng feed và giữ ảnh chụp dữ liệu cuối; Connect lại tạo một phiên mới.

Tất cả nhãn thời gian của thị trường và log dùng UTC. Mỗi tick demo là một print; không có tiền hoặc lệnh thật.

Tạo bản `.exe` tự chứa runtime để máy đích không cần cài .NET:

```powershell
./scripts/Publish-Windows.ps1
```

Kết quả ở `artifacts/windows-x64/FlowDesk.Desktop.exe`. Bản đóng gói vẫn là **demo**, không bổ sung kết nối Rithmic. WPF chỉ chạy trên Windows; build trên Linux không xác nhận việc hiển thị hoặc kết nối trên máy Windows.

## Cấu trúc

```text
FlowDesk.sln
src/
  FlowDesk.Core/
    Models.cs                         # IRithmicPluginService, contracts, settings
    OrderFlowProcessor.cs             # OHLC, delta, walls, sweeps; thread safe
  FlowDesk.Infrastructure/
    Services/
      DemoRithmicPluginService.cs      # replay + stream, lifecycle đầy đủ
      RithmicPluginService.cs          # từ chối live rõ ràng khi chưa có adapter
      RithmicSdkUnavailableException.cs
  FlowDesk.Desktop/
    App.xaml / App.xaml.cs
    Views/MainWindow.xaml             # login/connection/monitoring dashboard
    ViewModels/                       # RelayCommand, AsyncRelayCommand, MainViewModel
    Controls/                         # CandlestickChart, DepthHeatmap
tests/
  FlowDesk.Core.Tests/
  FlowDesk.Infrastructure.Tests/
  FlowDesk.Desktop.Tests/              # yêu cầu Windows/WPF runtime
docs/
  RITHMIC-INTEGRATION.md
scripts/
  validate.sh
  Publish-Windows.ps1
```

## Công thức và ý nghĩa

| Hiển thị | Quy tắc |
| --- | --- |
| Big trade | Một print có `Size >= MinimumTradeSize`. Bán kính bubble tỷ lệ với căn bậc hai của size tương đối trong dữ liệu hiển thị. Không gom lại để đoán lệnh mẹ. |
| Delta | Buy-aggressor size trừ Sell-aggressor size. Unknown cộng volume nhưng không cộng delta. Phân loại aggressor phải do adapter có dữ liệu đáng tin cậy cung cấp. |
| Wall | Tổng displayed size ở một mức giá `>= MinimumWallSize`. Báo khi mới vượt ngưỡng hoặc tăng ít nhất 25% so với frame trước. Các lệnh có thể bị hủy hoặc thay đổi. |
| Sweep | Cùng phía aggressor, tổng size và số mức giá khác nhau đạt ngưỡng trong cửa sổ thời gian; giá cuối cao hơn giá đầu đối với Buy, thấp hơn đối với Sell. Cửa sổ được tiêu thụ sau cảnh báo để hạn chế báo trùng. |
| Nến | Bucket theo UTC, open/close theo timestamp, high/low/volume/delta từ tất cả prints nhận được. Tick đến trễ sửa lại nến còn trong bộ nhớ; không tạo sweep hồi tố. Không sinh nến rỗng. |

Đây là thuật toán order flow công khai do dự án triển khai, **không phải công thức MZpack độc quyền**. Trade prints không chứng minh đó là stop order hay lệnh market: marketable limit cũng có thể khớp tương tự. DOM tổng hợp không cho biết danh tính từng lệnh limit, hidden liquidity hoặc iceberg.

## Kiểm thử

```powershell
dotnet test tests/FlowDesk.Core.Tests -c Release
dotnet test tests/FlowDesk.Infrastructure.Tests -c Release
# Windows only: khởi tạo WPF, chạy demo, kiểm tra binding/render và live failure.
dotnet test tests/FlowDesk.Desktop.Tests -c Release
```

Trên Linux, `bash scripts/validate.sh` restore theo lockfile, biên dịch cả WPF và chạy các suite đa nền tảng. Test WPF phải chạy trên Windows. Workflow `.github/workflows/build.yml` dành cho Windows đã được thêm; chưa có lần chạy CI được xác nhận từ môi trường này.

Kết quả trên máy hiện tại: **34 test đạt**, build Release không có cảnh báo/lỗi, publish `.exe` Windows thành công. Sáu test WPF đã biên dịch nhưng chưa chạy vì cần Windows. Xem [biên bản kiểm chứng](docs/VALIDATION.md). Script `scripts/setup-cloud.sh` cài SDK 8.0.426 vào `/workspace/.dotnet` khi cần và chạy các bước xác minh cho môi trường cloud này.

## Kết nối Rithmic thật

Chọn `RithmicPlugin` hiện trả lỗi rõ ràng, không mở socket, không gửi username, không giả tài khoản, không tự chuyển qua demo. `IRithmicPluginService` đã định nghĩa nơi nhận trạng thái, account, trades và full depth snapshots; **engine adapter chính thức chưa được triển khai**.

Chỉ chép một DLL vào thư mục chương trình sẽ không kích hoạt live. Cần SDK có quyền sử dụng, API contract/sample plugin đúng phiên bản và adapter được biên dịch/kiểm thử với bộ đó. Các tên `com.rithmic.api`, `RClientListener`, `REngineParams.Environment.Plugin`, cũng như giả định password rỗng/miễn phí API trong yêu cầu ban đầu chưa được xác minh và không được dựng giả trong mã.

Xem [docs/RITHMIC-INTEGRATION.md](docs/RITHMIC-INTEGRATION.md) để biết chính xác phần còn thiếu và cách hoàn thiện mà không đoán giao thức.
