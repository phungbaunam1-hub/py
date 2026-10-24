# AI MT Trading · Python gốc + Crypto Workspace

**Đây là bản giữ ứng dụng Python bạn gửi, không phải giao diện FlowDesk WPF.**

Tệp `original/AI_MT_Trading.py` được giữ nguyên byte-for-byte. Launcher `run_aimt.py` nạp ứng dụng đó rồi bổ sung chart, dữ liệu và các kiểm tra phạm vi thực thi. Panel giao dịch/quản lý, tám phương pháp gốc, tài khoản, Chat AI, AI Web, tin tức và cài đặt được giữ lại.

## Chart, crypto và forex

- Bấm **+ dưới chart** để thêm tab độc lập: Binance, Bybit, OKX, Kraken hoặc MT5. Danh sách được lấy từ nguồn, có tìm kiếm và lọc Crypto / Forex–fiat; không giới hạn vào một danh sách vài đồng cố định. Các sàn/vùng có thể giới hạn truy cập. Không tự chuyển sang dữ liệu demo nếu HTTP thất bại.
- Binance/Bybit/OKX có spot và perpetual tuyến tính; Kraken có spot và cặp fiat khi sàn cung cấp. MT5 lấy toàn bộ danh mục broker. Không tạo cặp hoặc biến crypto perpetual thành CME futures.
- Chọn **Kèm futures/perpetual** để gửi thêm nến, funding, open interest, mark/index và order flow cùng sàn, cùng base/quote khi có. Phần thiếu được ghi rõ. Mỗi mục có nguồn, thời gian và đơn vị; OKX swap dùng contracts.
- Tab đang xem dùng trade WebSocket ở các nguồn được hỗ trợ; Kraken dùng REST. DOM là snapshot REST, không phải order book tái dựng từ incremental deltas. Candle volume lấy REST; prints trực tiếp chỉ cập nhật giá nến đang chạy để tránh cộng trùng volume.
- Tối đa 8 chart bổ sung; đổi tab không đổi symbol/tài khoản MT5 thực thi. Nút **Dùng cho MT5 giao dịch** chỉ có trên chart MT5 để chuyển lựa chọn một cách rõ ràng. Chưa có router lệnh crypto riêng.

## Tín hiệu, AI và hotkey

Vùng phân tích hiện có gom **BUY / SELL / SIDEWAYS**, các phương pháp và kết luận AI. Thiếu dữ liệu thì ghi rõ, không ép thành tín hiệu mua/bán. AI API, Chat, AI Web và các yêu cầu quản lý lệnh đều được bọc bối cảnh: chart mở, nến đã đóng/đang chạy, quote, prints, DOM, futures khi có, phương pháp, vị thế/tài khoản và giới hạn rủi ro. Có giới hạn kích thước prompt; số bản ghi giữ/bỏ được báo trong bối cảnh. AI được yêu cầu trả lời tiếng Việt ngắn, có kết luận hiện tại, lý do và điều kiện mất hiệu lực. Cần API key để gọi AI API.

Kết quả AI chỉ được dùng cho lệnh MT5 nếu khớp nguồn, symbol, timeframe, tài khoản, phiên phân tích và yêu cầu mới nhất. Kết quả nghiên cứu crypto không điều khiển lệnh MT5. Tab nghiên cứu chặn các lệnh mở mới qua đường MT5; quản lý/đóng vị thế vẫn theo tài khoản gốc.

Ngay cạnh **HK** có ô **Q xác nhận lệnh**, mặc định bật. Bật: dùng hotkey rồi Q theo thao tác gốc. Bỏ tích: hotkey gửi hành động ngay; phím bị giữ lặp tự động được bỏ qua. Lựa chọn được lưu trong cấu hình.

Nút **Kiểm chứng** chạy walk-forward từ dữ liệu đã tải, chọn tham số trên tập quá khứ rồi kiểm tra đoạn tương lai, có phí và slippage. Ba nhóm thực thi kiểm chứng: trend, breakout và mean reversion. Có thêm EMA/ADX/RSI, Bollinger/z-score, Donchian, regression/variance ratio, ATR/Parkinson, VWAP/profile xấp xỉ, microstructure và derivatives. Đây là công cụ nghiên cứu, không phải hệ thống HFT thực thi hay dự báo lượng tử. Không khẳng định bao phủ mọi phương pháp hoặc tạo lợi nhuận.

## Gửi lệnh MT5 nền và đồng thời

Các thao tác Panel và trade chạy qua hàng đợi có giới hạn, không chặn GUI. Tối đa 4 terminal khác nhau xử lý song song; tác vụ cùng terminal theo FIFO, mở đúng session của tài khoản đã chọn trong worker. Có mã tác vụ, trạng thái queued/running/completed/rejected/cancelled/unknown và xác nhận broker riêng (retcode, order/deal, filled/partially filled/placed khi SDK cung cấp). `completed` chỉ nói tác vụ đã trả về, không khẳng định đã khớp đủ lệnh.

Đây là async ở cấp ứng dụng Python. **MetaTrader5 Python không cung cấp `CTrade.SetAsyncMode`/`OrderSendAsync` của MQL5**; cùng terminal vẫn dùng `order_send` đồng bộ. Để gửi song song qua Python, cấu hình các terminal MT5 riêng cho các tài khoản. Không thêm một EA hoặc giao thức giả vào MT5.

Đổi chart/tài khoản/symbol/khung hoặc điều kiện rủi ro sẽ hủy lệnh mở mới còn chờ; phạm vi được kiểm tra lại ngay trước gửi RPC. Không thể hủy một yêu cầu đã gửi đi. Khi mất phản hồi/timeout, trạng thái là **unknown**: không gửi lại tự động, khóa lệnh mới trên terminal bị ảnh hưởng để tránh trùng lệnh. Kiểm tra Orders/Positions trong MT5, sau đó nhập **`ASYNC RESUME`** trong ô lệnh để mở lại; lệnh cũ không được phát lại. **`ASYNC STATUS`** xem hàng đợi. Đóng/quản lý vị thế vẫn có thể thực hiện theo tài khoản đã chụp khi thao tác.

## Chạy bản Windows đóng gói

1. Tải **`AI_MT_Trading.exe`** từ [GitHub Releases](https://github.com/phungbaunam1-hub/py/releases), hoặc tải ZIP chứa đúng EXE này.
2. Nếu tải ZIP, giải nén lấy EXE.
3. Chạy **`AI_MT_Trading.exe`**. Không cần Python hay thư mục `_internal` đi kèm. EXE tự giải nén Qt/WebEngine/MT5 vào thư mục tạm khi mở; có thể khởi động chậm hơn bản thư mục. Terminal MT5 vẫn cần cài và đăng nhập để giao dịch thật.
4. Sử dụng các panel như ứng dụng gốc. Nút **Order Flow** cạnh thanh lệnh nhanh mở dock bổ sung; phím tắt **Ctrl+Shift+O**. Dock có thể kéo/nổi/đóng, không thay các tab cũ.

Chọn nguồn trong dock, rồi bấm **Kết nối Order Flow**:

- **DEMO order flow:** prints/depth giả được sinh riêng, không phải dữ liệu giao dịch của chart MT5, không gửi lệnh. Chart phụ của dock thể hiện chính dữ liệu demo này; không ghép bubble demo vào chart gốc để tránh lẫn nguồn.
- **MT5 đang chọn:** dùng terminal/tài khoản đã cấu hình trong ứng dụng gốc. Đọc `copy_ticks_range(..., COPY_TICKS_TRADE)` và `market_book_get`; không tạo volume từ quotes. Chỉ có dữ liệu nếu broker thực sự cấp trades/DOM cho symbol đó. CFD/forex thường chỉ có quotes: lúc đó tape/bubble/DOM có thể trống và có thông báo tương ứng. Đơn vị size theo nguồn, không mặc định là contracts của CME.
- **Rithmic plugin:** chưa khả dụng vì chưa có SDK/adapter được cấp quyền. Hiển thị lỗi rõ ràng, không gọi API giả và không tự chuyển nguồn.
- **Chart crypto đang xem:** dùng cùng dữ liệu public của tab hiện tại; tape phân biệt native/aggregate, không cộng hai loại. DOM ghi tuổi snapshot; đổi chart bắt đầu lại phiên.

Các threshold big trade, displayed wall, volume/số mức giá/thời gian sweep và timeframe nến của dock có thể chỉnh. Bubble lên chart gốc chỉ áp dụng cho nguồn MT5 thật trùng symbol, không thay zoom/pan/trục giá/overlay/kế hoạch/lệnh của chart gốc. Thay nguồn/tài khoản/symbol bắt đầu lại phiên order flow; thay timeframe của dock không đổi timeframe của chart gốc.

Wall chỉ là tổng displayed size ở một mức giá; sweep là heuristic dựa trên prints. Không xác định từng lệnh limit, iceberg hoặc stop order. Marketable limit có thể tạo prints giống market order. Công thức không phải MZpack độc quyền.

## Chức năng gốc và cấu hình

Chức năng MT5, quản lý lệnh, copy, auto trade và AI giữ yêu cầu terminal/account đúng cấu hình, quyền broker, API key nếu dùng AI API. Lớp mở rộng bổ sung kiểm tra nguồn/thời hạn kết quả AI và tách nghiên cứu khỏi thực thi. Tự động giao dịch không được bật thay bạn. Chưa xác minh tài khoản/broker/AI thật trong môi trường build này.

Bản `.exe` dùng `%LOCALAPPDATA%\AI_MT_Trading\ai_mt_trading_config.json` khi bạn không đặt `AIMT_CONFIG`. Nếu đã có file cấu hình gốc, bạn có thể sao lưu rồi chép nó vào đó để sử dụng lại; không gửi file chứa mật khẩu/API key lên GitHub hoặc chat. Để tự chọn vị trí, đặt biến `AIMT_CONFIG` trước khi mở chương trình. Lớp mở rộng lưu thêm `orderflow` và `hotkey_confirm`.

Gói đầy đủ có PyQt6-WebEngine để giữ khả năng mở AI Web trong khung. Các dịch vụ AI có thể từ chối đăng nhập từ trình duyệt nhúng; nút **Mở ngoài trình duyệt** và quy trình clipboard của bản gốc vẫn có sẵn.

## Chạy từ mã nguồn

Nếu muốn **một tệp `.py` duy nhất**, tải `AI_MT_Trading.py` tại Releases. Tệp nhúng toàn bộ module ứng dụng và mã Python gốc; lúc chạy tự nạp vào thư mục tạm, giữ cấu hình/AI Web trong thư mục dữ liệu người dùng. Windows x64 + Python 3.12:

```powershell
py -3.12 AI_MT_Trading.py --install-deps
py -3.12 AI_MT_Trading.py
# Xuất mã đầy đủ để xem/sửa, không cần dùng khi chỉ chạy:
py -3.12 AI_MT_Trading.py --extract-source .\AI-MT-source
```

Các thư viện bên thứ ba vẫn cần cài khi chạy Python; EXE đã chứa các thư viện này. Không đóng gói mật khẩu/API key/cấu hình tài khoản vào tệp chia sẻ.

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
