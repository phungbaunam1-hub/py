# Dữ liệu thị trường công khai và phạm vi kiểm chứng

Các nguồn crypto trong bản Python là **dữ liệu phân tích**, tách khỏi tài khoản gửi lệnh MT5. Chọn một chart crypto không cấp quyền giao dịch trên sàn và không đổi symbol đặt lệnh của tài khoản MT5.

## Nguồn REST không dùng API key

| Nguồn | Thị trường | Catalog / nến / depth / trades | Phái sinh khi có |
| --- | --- | --- | --- |
| Binance | Spot | `/api/v3/exchangeInfo`, `/api/v3/klines`, `/api/v3/depth`, `/api/v3/trades` | Không gán thông tin perpetual cho spot |
| Binance | USD-M perpetual | `/fapi/v1/exchangeInfo`, `/fapi/v1/klines`, `/fapi/v1/depth`, `/fapi/v1/trades` | `premiumIndex` cung cấp mark/index/funding; `openInterest` |
| Bybit | Spot, linear perpetual | `/v5/market/instruments-info`, `kline`, `orderbook`, `recent-trade` | Ticker/funding/open interest theo đúng category/symbol |
| OKX | Spot, swap | `/api/v5/public/instruments`, `/api/v5/market/candles`, `books`, `trades` | `/api/v5/public/funding-rate`, `open-interest`, `mark-price` |
| Kraken | Spot; cặp fiat/fiat nếu catalog thực tế có | `/0/public/AssetPairs`, `OHLC`, `Depth`, `Trades` | Không tự coi Kraken spot là nguồn futures |

Catalog được truy vấn theo sàn; không hứa một số lượng symbol cố định, không thêm symbol không được sàn niêm yết. Cặp fiat/fiat được lấy từ catalog hoặc broker MT5, không tự tạo OHLC forex từ giá crypto. Sản phẩm crypto perpetual không phải dữ liệu futures CME/Rithmic.

Các endpoint trên thuộc phạm vi market data công khai; app không yêu cầu API key cho việc đọc chúng. Điều này **không** có nghĩa được miễn phí giao dịch, miễn phí spread/funding hoặc có quyền dùng mọi dữ liệu của sàn. Dịch vụ có thể giới hạn vùng/IP, tốc độ, sản phẩm và điều khoản sử dụng. Nguồn không truy cập được phải hiện lỗi/thiếu dữ liệu; không âm thầm thay bằng demo.

## Đơn vị và tính toàn vẹn

- Spot thường dùng khối lượng base asset. Binance USD-M và Bybit linear trong phạm vi này dùng base-asset quantity; không áp dụng quy tắc đó cho inverse contracts.
- OKX swap giữ đơn vị **contracts** khi sàn trả contracts. Không tự nhân với contract multiplier chưa có trong metadata để giả thành BTC/ETH.
- REST candle, quote, depth, trades và funding/OI đến từ các request khác nhau: snapshot không atomic. Giữ timestamp và provenance từng phần; request thành công không chứng minh dữ liệu đủ mới cho thực thi lệnh.
- Phân tích tín hiệu dùng nến đã đóng; nến đang chạy chỉ để hiển thị. Tick volume của MT5/CFD không được chuyển thành traded size hoặc dòng lệnh thật.
- REST trades là cửa sổ hữu hạn. Polling không đảm bảo giữ mọi print giữa hai lần gọi. Book snapshot chỉ cho displayed liquidity tại thời điểm lấy, không phải lịch sử từng lệnh limit hoặc hidden liquidity.

## WebSocket: trade gốc và trade gộp

| Luồng | Ý nghĩa |
| --- | --- |
| Binance Spot `<symbol>@trade` trên `data-stream.binance.vision` | Tài liệu chính thức mô tả raw trade; có trade ID riêng. Endpoint này chỉ cung cấp market data, không có user-data stream. |
| Binance USD-M `<symbol>@aggTrade` | Trade đã gộp; không được gắn nhãn native print hoặc coi aggregate size là kích thước một fill gốc. |
| Bybit `publicTrade.SYMBOL` | Một message có thể chứa đến 1.024 trades; nhiều message có thể chung `seq`. Dùng trade ID `i` để chống lặp; không bỏ toàn bộ các message có cùng `seq`. `S` là phía taker. |
| OKX public `trades` | Bản này đánh dấu theo hướng thận trọng là aggregated; nếu có `count` phải giữ thông tin gộp. Ngữ nghĩa chi tiết của tài liệu OKX gốc chưa đọc được trong môi trường hiện tại, không khẳng định đó là mọi fill gốc. |

Stream sống, REST snapshot, dữ liệu gộp và demo phải có nhãn nguồn rõ. Mất kết nối/reconnect có thể tạo khoảng trống; không được coi quá trình nối lại là bằng chứng đã backfill mọi trade bị bỏ lỡ. Các stream công khai không có quyền lấy trạng thái tài khoản hoặc gửi lệnh.

## Tốc độ và vùng truy cập đã đối chiếu

- Binance Spot: klines tối đa 1.000; recent trades tối đa 1.000; depth tối đa 5.000. Tài liệu hiện tại ghi weight klines 2, trades 25, depth đến 100 mức là 5. Cần cache/budget chung giữa các chart, không polling nhanh độc lập cho mọi tab. HTTP 429 phải tôn trọng `Retry-After` theo giây; tái phạm có thể bị 418.
- Binance Spot WebSocket: server ping 20 giây; trả pong cùng payload trong vòng một phút. Kết nối có vòng đời 24 giờ, tối đa 1.024 streams; giới hạn 5 incoming control/ping/pong messages mỗi giây và 300 lần mở kết nối/5 phút/IP.
- Bybit instrument Spot không hỗ trợ pagination. Linear mặc định 500, có nhiều hơn 500 symbol nên cần `nextPageCursor`/limit tối đa 1.000; lọc trạng thái `Trading`, không coi `PendingOpen` là đang giao dịch.
- Bybit HTTP mặc định 600 requests/5 giây/IP. `403 access too frequent` yêu cầu dừng HTTP sessions và chờ ít nhất 10 phút theo tài liệu. API retcode `10006` là rate limit; có các header `X-Bapi-Limit*`.
- Bybit guide ghi US và Mainland China IP bị 403 và có các domain theo vùng. Không suy ra mọi 403 đều do vùng địa lý; proxy, chính sách mạng và nhà cung cấp cũng có thể chặn.

Các giới hạn có thể đổi; runtime phải xử lý lỗi và headers thực tế, không coi con số trên là quyền truy cập được bảo đảm.

## Probe thực tế trong môi trường cloud này

Đã chạy:

```sh
/workspace/.venvs/aimt/bin/python scripts/probe-public-markets.py \
  --output artifacts/public-market-probe.json
```

Report được tạo lúc **2026-10-23 17:13:40 UTC**. Probe dùng HTTPS với TLS verification, không account/API key, không sửa network settings. Có 7 trường hợp, mỗi trường hợp thử catalog và một snapshot BTC canonical; catalog không lấy được thì canonical symbol chỉ dùng để kiểm tra endpoint, không phải dữ liệu thay thế hoặc symbol đã được xác nhận từ catalog.

| Trường hợp | Catalog | Snapshot |
| --- | --- | --- |
| Binance Spot | Proxy tunnel 403 | Proxy tunnel 403 |
| Binance USD-M perpetual | Proxy tunnel 403 | Proxy tunnel 403 |
| Bybit Spot | Proxy tunnel 403 | Proxy tunnel 403 |
| Bybit linear perpetual | Proxy tunnel 403 | Proxy tunnel 403 |
| OKX Spot | Proxy tunnel 403 | Proxy tunnel 403 |
| OKX swap | Proxy tunnel 403 | Proxy tunnel 403 |
| Kraken Spot | Proxy tunnel 403 | Proxy tunnel 403 |

**0/7 snapshot nhận được; cả 14 request bị proxy tunnel 403 trước khi có phản hồi API của sàn.** Đây là kết quả chặn mạng của máy cloud này, không chứng minh sàn hỏng, API cần credentials hoặc máy Windows của người dùng cũng bị chặn. Parser/fixture tests và đóng gói chương trình không thay thế cho kiểm chứng kết nối thật trên một mạng truy cập được các host đó.

Script mặc định trả exit code 0 khi đã ghi xong báo cáo chẩn đoán; đó không phải khẳng định readiness. Thêm `--require-live` để trả code 2 nếu bất kỳ snapshot được chọn không truy xuất được. Có thể dùng `--provider Binance --spot-only` để kiểm tra hẹp hơn. Report chỉ lưu host/path/status/counts/metadata và lỗi đã giới hạn; không dump headers, proxy URLs, process environment hoặc raw response bodies.

Các host REST cần truy cập: `api.binance.com`, `fapi.binance.com`, `api.bybit.com`, `www.okx.com`, `api.kraken.com`. Binance có base market-data-only chính thức `data-api.binance.vision`; việc có base khác không cho phép bỏ qua hạn chế vùng hay chính sách. Host WebSocket phụ thuộc nguồn: `data-stream.binance.vision`, `fstream.binance.com`, `stream.bybit.com`, `ws.okx.com`. Không thay network allowlist trong tác vụ probe này.

## Tài liệu đã đọc và giới hạn tìm kiếm

Nguồn GitHub chính thức đọc được:

- [Binance Spot REST](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md), [WebSocket](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md).
- [Binance USD-M SDK market endpoints](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py) — connector legacy, dùng đối chiếu endpoint, không thay tài liệu hợp đồng hiện hành.
- [Bybit instruments](https://github.com/bybit-exchange/docs/blob/master/docs/v5/market/instrument.mdx), [klines](https://github.com/bybit-exchange/docs/blob/master/docs/v5/market/kline.mdx), [trade stream](https://github.com/bybit-exchange/docs/blob/master/docs/v5/websocket/public/trade.mdx), [rate limit](https://github.com/bybit-exchange/docs/blob/master/docs/v5/rate-limit/rate-limit.mdx), [guide/vùng](https://github.com/bybit-exchange/docs/blob/master/docs/v5/guide.mdx).
- [OKX PublicData SDK](https://github.com/okxapi/python-okx/blob/master/okx/PublicData.py), [MarketData SDK](https://github.com/okxapi/python-okx/blob/master/okx/MarketData.py), [endpoint constants](https://github.com/okxapi/python-okx/blob/master/okx/consts.py). Phân biệt `/public/instruments` với `/account/instruments` có xác thực.
- [TA-Lib ADX](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ADX.c), [ATR](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ATR.c), [RSI](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_RSI.c), [EMA](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_EMA.c): công thức/thực thi có Wilder smoothing.
- [scikit-learn time-series validation](https://github.com/scikit-learn/scikit-learn/blob/main/doc/modules/cross_validation.rst): chia quá khứ/tương lai, tránh random split làm rò rỉ dữ liệu; `TimeSeriesSplit` có `gap`.
- [Qiskit Finance](https://github.com/qiskit-community/qiskit-finance): thí nghiệm pricing/portfolio optimization, không chứng minh dự báo hướng thị trường bằng “lượng tử”.

Các trang gốc Binance/Bybit/OKX/[Kraken](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/), publisher DOI và SSRN bị HTTP/CONNECT 403 tại đây. Không tuyên bố đã tìm hết mọi phương pháp giao dịch. REST polling và chỉ báo microstructure trên desktop không phải hạ tầng HFT thực thi. Weighted mid L1 là xấp xỉ theo imbalance, không đồng nhất với mô hình micro-price Markov được hiệu chỉnh của Stoikov. Các điểm số tổng hợp cần kiểm thử ngoài mẫu và chi phí thực tế; không tự coi chúng là xác suất lợi nhuận đã hiệu chỉnh.
