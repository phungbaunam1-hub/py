# Các phương pháp thị trường thực sự có trong bản mở rộng

Ứng dụng giữ tám bộ phân tích gốc và thêm các nhóm dưới đây. Đây là danh sách phạm vi đã triển khai, không phải tuyên bố bao phủ mọi phương pháp giao dịch. Các chỉ báo tương tự được gộp theo nhóm để tránh đếm nhiều phiếu cho cùng một thông tin.

## Phân tích định lượng

`python/aimt_market/quant.py::analyze_market` chỉ dùng nến có cờ `closed=True`, OHLC hợp lệ và giá dương. Cần tối thiểu 60 nến đã đóng; lịch sử ngắn hơn trả `INSUFFICIENT_DATA`. Nến đang hình thành không được phép thay đổi chỉ báo hoặc phiếu định hướng.

| Nhóm | Phép tính | Giới hạn chính |
| --- | --- | --- |
| Xu hướng / động lượng | EMA20/50, Wilder ADX14, RSI14 gộp một phiếu | Chỉ báo trễ; RSI cực trị không tự chứng minh đảo chiều |
| Hồi quy về trung bình | Z-score20 và Bollinger ±2σ | Trọng số giảm khi ADX cao; thị trường có thể duy trì lệch trung bình |
| Cấu trúc | Donchian 20 nến trước, loại trừ nến hiện tại | Breakout có thể thất bại, không suy ra stop order |
| Thống kê | Hồi quy log-price, R², variance ratio lag-5 | Mô tả mẫu; không phải kiểm định dự báo đã hiệu chỉnh; mẫu nhỏ/gaps làm giảm chất lượng |
| Rủi ro / biến động | Wilder ATR14, Parkinson range volatility, realized volatility | Không bỏ phiếu BUY/SELL; 2ATR là khoảng giá tham khảo, chưa phải size lệnh |
| Tham chiếu volume | Bar-VWAP, POC và vùng volume 70% xấp xỉ | Chỉ khi volume thực có provenance; gán volume bar vào typical price/24 bins, không phải tick-VWAP/footprint |
| Microstructure | Top-10 book imbalance, L1 weighted mid, delta từ aggressor thật | Snapshot có thể đổi/hủy; Unknown không bị đoán thành Buy/Sell |
| Futures / perpetual | Funding, open interest, mark/index basis | Không suy ra OI change từ một snapshot; không trộn venue, kỳ funding, đơn vị contract khác nhau |

L1 weighted mid được tính `(ask × bid_size + bid × ask_size)/(bid_size + ask_size)`. Trường metric `microprice` chứa **xấp xỉ weighted-mid** này, không phải mô hình micro-price Markov được hiệu chỉnh của Stoikov. Không đánh đồng hai khái niệm.

Các phương pháp chung một nhóm volume được lấy trung bình trước khi bỏ phiếu. Những mục không có dữ liệu được đánh dấu unavailable và không nhận trọng số; không điền 0 giả để thể hiện đã đo được. Kết luận là `BUY`, `SELL`, `SIDEWAYS` hoặc `INSUFFICIENT_DATA`. Confidence chỉ là mức đồng thuận heuristic, **không phải xác suất thắng**; giới hạn tối đa 80, thấp hơn khi ít dữ liệu hoặc biến động đột biến.

## Kiểm chứng phương pháp ngoài mẫu

`python/aimt_market/backtest.py::walk_forward_report` và hộp thoại **Kiểm chứng phương pháp** nghiên cứu lịch sử đã cache; chúng không gọi mạng hoặc gửi lệnh.

- Cần tối thiểu 300 nến đã đóng hợp lệ. Mặc định train 150, test 60; chỉ lấy fold hoàn chỉnh.
- Grid nhỏ công khai: EMA `(8,21)/(12,36)/(20,50)`; Donchian `10/20/40`; z-score window `20/40`, entry threshold `1.5/2.0`.
- Chỉ chọn tham số theo **train net return − 0,5 × train maximum drawdown**. Test cùng fold không được dùng để chọn. Fold sau có thể train trên dữ liệu test đã đi qua của fold trước; đó là walk-forward hợp lệ.
- Tín hiệu ở close t chỉ khớp tại **open t+1**. High/low không được dùng để tạo khớp intrabar. Open cuối mỗi fold là mốc thanh lý đã định trước; vì vậy test 60 bars chứa 59 khoảng open-to-open, không giữ vị thế sang fold sau.
- Mô phỏng long/flat, fully funded, vốn đầu 10.000 đơn vị và tái đầu tư equity; không leverage, không giả có quyền short trên tài khoản crypto spot.
- Phí và slippage tính theo turnover ở **cả entry và exit**. Không có stop-fill intrabar, funding/borrow, spread động, giới hạn thanh khoản, latency hoặc thuế. Đây là mô hình nghiên cứu, không phải trình mô phỏng khớp lệnh exchange.
- Hiển thị train/test riêng, equity ngoài mẫu, net/gross return, drawdown, round trips, chi phí và Sharpe/bar. Sharpe dùng hệ số 1, risk-free=0, không tự annualize hoặc đoán số phiên/năm. Không có trade thì không coi đường equity phẳng là bằng chứng chiến lược hiệu quả.

Tests kiểm tra prefix invariance, sửa dữ liệu tương lai không đổi tham số/metrics train của fold trước, đúng next-open fills, không lợi dụng high/low/close cùng bar, phí tăng làm giảm kết quả của cùng target strategy, dữ liệu hình thành bị loại và không tạo kết quả khi thiếu lịch sử. Walk-forward cùng một lịch sử vẫn có data-snooping risk; cần dữ liệu thời kỳ/venue độc lập để kiểm chứng tiếp.

## HFT và lượng tử

Book/tape thời gian thực và microstructure analytics **không tạo ra HFT execution**. REST/websocket tới desktop không có colocated matching-engine access, deterministic latency, exchange risk controls hoặc bằng chứng đạt độ trễ HFT. Ứng dụng không gắn nhãn engine HFT hoạt động.

Không có thuật toán hoặc phần cứng lượng tử dự đoán thị trường trong bản này. Quantum-finance research thường liên quan định giá hay tối ưu danh mục; nó không chứng minh một hệ thống dự báo BUY/SELL có lợi nhuận. Không tạo nút “Quantum” với logic giả để lấp danh sách phương pháp.

## Tài liệu tham khảo

- [TA-Lib ADX / Wilder smoothing](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ADX.c), [ATR](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ATR.c), [nhóm momentum](https://ta-lib.github.io/ta-lib-python/func_groups/momentum_indicators.html).
- Parkinson (1980), *The Extreme Value Method for Estimating the Variance of the Rate of Return*: [DOI 10.1086/296071](https://doi.org/10.1086/296071).
- Lo & MacKinlay (1988), variance-ratio research: [DOI 10.1093/rfs/1.1.41](https://doi.org/10.1093/rfs/1.1.41).
- Stoikov (2018), micro-price research: [DOI 10.1080/14697688.2018.1489139](https://doi.org/10.1080/14697688.2018.1489139), để phân biệt với weighted-mid approximation.
- [Time-series cross-validation và gap](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split).
- [Qiskit Finance](https://github.com/qiskit-community/qiskit-finance): phạm vi nghiên cứu quantum finance, không phải bằng chứng dự đoán giá.
