"""Closed-bar quantitative research and read-only market microstructure analysis.

This is a transparent rule ensemble, not a trained predictor or an execution system.
Polling public snapshots is not HFT; no quantum-computing claim is made. Bar-volume
VWAP/profile estimates are explicitly approximations and must never be shown as an
exchange footprint, order identities, hidden liquidity or true tick-level volume profile.
"""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import math

import numpy as np


RESEARCH_REFS = [
    {"name": "Wilder indicators: RSI, ADX and ATR (TA-Lib reference)",
     "url": "https://ta-lib.github.io/ta-lib-python/func_groups/momentum_indicators.html"},
    {"name": "Parkinson (1980), range-based volatility",
     "url": "https://doi.org/10.1086/296071"},
    {"name": "Lo and MacKinlay (1988), variance-ratio research",
     "url": "https://doi.org/10.1093/rfs/1.1.41"},
    {"name": "Stoikov (2018), the micro-price",
     "url": "https://doi.org/10.1080/14697688.2018.1489139"},
    {"name": "Time-ordered validation and gaps",
     "url": "https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split"},
]


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _number(value):
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and abs(result) <= 1e100 else None


def _clip(value, low=-1.0, high=1.0):
    return min(high, max(low, float(value)))


def _safe_ratio(numerator, denominator, default=0.0):
    if not denominator or not math.isfinite(denominator):
        return default
    result = numerator / denominator
    return result if math.isfinite(result) else default


def _signal(score, threshold=.22):
    return "BUY" if score >= threshold else "SELL" if score <= -threshold else "SIDEWAYS"


def _method(key, name, group, score=0.0, observations=(), limitations=(),
            *, available=True, voting=True, weight=1.0):
    return {"key": key, "name": name, "group": group,
            "signal": _signal(score) if available else "INSUFFICIENT_DATA",
            "score": _clip(score), "observations": list(observations),
            "limitations": list(limitations), "available": bool(available),
            "voting": bool(voting and available), "weight": float(weight)}


@dataclass(frozen=True)
class _Bar:
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    volume_real: bool


def _bars(candles):
    rows, invalid, forming, missing_closed, duplicates = {}, 0, 0, 0, 0
    for candle in candles if candles is not None else ():
        closed = _get(candle, 'closed')
        if closed is None:
            missing_closed += 1
            continue
        if not isinstance(closed, (bool, np.bool_)) or not bool(closed):
            forming += 1
            continue
        values = [_number(_get(candle, key)) for key in ('time', 'open', 'high', 'low', 'close')]
        if any(value is None for value in values):
            invalid += 1
            continue
        timestamp, opening, high, low, close = values
        if min(opening, high, low, close) <= 0 or low > min(opening, close) or high < max(opening, close) or low > high:
            invalid += 1
            continue
        volume = _number(_get(candle, 'volume'))
        if volume is not None and volume < 0:
            volume = None
        real = _get(candle, 'volume_is_real', False) is True
        if timestamp in rows:
            duplicates += 1
        rows[timestamp] = _Bar(timestamp, opening, high, low, close, volume, real)
    quality = []
    if invalid:
        quality.append(f"Bỏ {invalid} nến có giá/timestamp/OHLC không hợp lệ; cần giá dương cho phương pháp log.")
    if forming:
        quality.append(f"Không dùng {forming} nến đang hình thành để ra tín hiệu.")
    if missing_closed:
        quality.append(f"Bỏ {missing_closed} nến thiếu trạng thái đóng; không tự giả định nến đã hoàn tất.")
    if duplicates:
        quality.append(f"Gộp {duplicates} timestamp nến trùng; bản cuối cùng đã đóng được giữ lại.")
    return [rows[key] for key in sorted(rows)[-2000:]], quality


def _ema(values, period):
    current = float(values[0])
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        current += alpha * (float(value) - current)
    return current


def _rma(values, period):
    if len(values) < period:
        return []
    result, current = [], float(np.mean(values[:period]))
    result.append(current)
    for value in values[period:]:
        current += (float(value) - current) / period
        result.append(current)
    return result


def _wilder(high, low, close):
    tr, plus, minus, gains, losses = [], [], [], [], []
    for index in range(1, len(close)):
        change = close[index] - close[index - 1]
        up, down = high[index] - high[index - 1], low[index - 1] - low[index]
        tr.append(max(high[index] - low[index], abs(high[index] - close[index - 1]), abs(low[index] - close[index - 1])))
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    atrs, plus_smooth, minus_smooth = _rma(tr, 14), _rma(plus, 14), _rma(minus, 14)
    dx = []
    for atr, pos, neg in zip(atrs, plus_smooth, minus_smooth):
        di_pos, di_neg = _safe_ratio(100 * pos, atr), _safe_ratio(100 * neg, atr)
        dx.append(_safe_ratio(100 * abs(di_pos - di_neg), di_pos + di_neg))
    gain, loss = _rma(gains, 14)[-1], _rma(losses, 14)[-1]
    rsi = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return atrs[-1], _rma(dx, 14)[-1], rsi, np.asarray(tr, dtype=float)


def _book_levels(book, side):
    unique = {}
    for level in _get(book, side, ()) or ():
        if isinstance(level, Mapping):
            price, size = _number(level.get('price')), _number(level.get('size'))
        else:
            try:
                price, size = _number(level[0]), _number(level[1])
            except (TypeError, IndexError, KeyError):
                continue
        if price is not None and price > 0 and size is not None and size > 0:
            unique[price] = size
    return sorted(unique.items(), reverse=side == 'bids')[:10]


def _evidence_age(stamp, reference_ms, max_age_ms=90000):
    """Use event/observation time, never a fresh enclosing JSON envelope."""
    value = _number(stamp)
    if value is None or value < 0:
        return 'unknown', None
    age = reference_ms - value
    if age < -30000:
        return 'future', age / 1000
    return ('stale' if age > max_age_ms else 'fresh'), max(0.0, age / 1000)


def _microstructure(trades, book, metrics, quality, reference_ms):
    parts, observations, available_parts, limitations = [], [], 0, [
        "Snapshot/polling chỉ mô tả hiện tại, có thể thay đổi/hủy; không phải HFT hay bộ định tuyến lệnh.",
        "Không suy ra iceberg, danh tính lệnh hoặc aggressor từ riêng biến động giá."]
    bids, asks = _book_levels(book, 'bids'), _book_levels(book, 'asks')
    if bids and asks and bids[0][0] < asks[0][0]:
        bid, bid_size = bids[0]
        ask, ask_size = asks[0]
        bsum, asum = sum(size for _, size in bids), sum(size for _, size in asks)
        imbalance = _safe_ratio(bsum - asum, bsum + asum)
        microprice = bid + (ask - bid) * _safe_ratio(bid_size, bid_size + ask_size)
        metrics.update(orderbook_imbalance=imbalance, microprice=microprice,
                       spread_bps=_safe_ratio((ask - bid) * 10000, (bid + ask) / 2))
        available_parts += 1
        state, age = _evidence_age(_get(book, 'timestamp_ms'), reference_ms)
        metrics['orderbook_age_seconds'] = age
        if state == 'fresh':
            parts.append((imbalance, .6))
        else:
            quality.append(f"Book có độ mới {state}; chỉ mô tả, không bỏ phiếu tín hiệu (giới hạn 90 giây).")
        observations.append(f"Imbalance top-10 {imbalance:+.2f}; weighted-mid L1 {microprice:.8g}.")
        limitations.append("Weighted-mid L1 là xấp xỉ theo queue size, không phải mô hình micro-price Markov đã hiệu chỉnh của Stoikov.")
    elif bids or asks:
        quality.append("Book thiếu một phía hoặc bị crossed/locked; không tính imbalance/microprice từ book đó.")
    total, signed, classified, valid_count = 0.0, 0.0, 0.0, 0
    fresh_total, fresh_signed, fresh_classified, fresh_count = 0.0, 0.0, 0.0, 0
    ignored_ages = defaultdict(int)
    for trade in trades if trades is not None else ():
        size, price = _number(_get(trade, 'size')), _number(_get(trade, 'price'))
        if size is None or size <= 0 or price is None or price <= 0:
            continue
        valid_count += 1
        total += size
        side = _get(trade, 'side', 'Unknown')
        if side in ('Buy', 'Sell'):
            classified += size
            signed += size if side == 'Buy' else -size
        state, _ = _evidence_age(_get(trade, 'time_ms'), reference_ms)
        if state != 'fresh':
            ignored_ages[state] += 1
            continue
        fresh_count += 1
        fresh_total += size
        if side in ('Buy', 'Sell'):
            fresh_classified += size
            fresh_signed += size if side == 'Buy' else -size
    metrics['trade_sample_count'] = valid_count
    metrics['fresh_trade_sample_count'] = fresh_count
    if total:
        metrics.update(trade_delta=signed, trade_delta_ratio=_safe_ratio(signed, total),
                       classified_trade_fraction=_safe_ratio(classified, total))
        observations.append(f"Delta toàn bộ mẫu {signed:+.5g}; {_safe_ratio(classified * 100, total):.0f}% size có aggressor xác định.")
        if classified:
            available_parts += 1
            if fresh_classified:
                parts.append((_safe_ratio(fresh_signed, fresh_total), .4))
                metrics.update(fresh_trade_delta=fresh_signed,
                               fresh_trade_delta_ratio=_safe_ratio(fresh_signed, fresh_total))
        else:
            quality.append("Prints không có aggressor đáng tin cậy; không dùng chúng làm tín hiệu mua/bán.")
    if ignored_ages:
        detail = ', '.join(f'{key}: {value}' for key, value in sorted(ignored_ages.items()))
        quality.append(f"Prints không dùng bỏ phiếu do độ mới ({detail}); thiếu timestamp không được coi là dữ liệu hiện tại.")
    score = _safe_ratio(sum(value * weight for value, weight in parts), sum(weight for _, weight in parts))
    return _method('microstructure', 'Order book + dòng lệnh thực thi', 'microstructure', score,
                   observations, limitations, available=bool(available_parts), voting=bool(parts))


def _derivatives(derivatives, close, metrics, quality, reference_ms):
    observations, parts = [], []
    for key in ('funding_rate', 'open_interest', 'mark_price', 'index_price'):
        value = _number(_get(derivatives, key))
        if value is not None and (key == 'funding_rate' or value >= 0):
            metrics[key] = value
    funding = metrics.get('funding_rate')
    if funding is not None:
        observations.append(f"Funding {funding * 100:.5g}%/kỳ của venue; dấu dương là long trả short.")
        kind = _get(derivatives, 'funding_timestamp_kind')
        scope = _get(derivatives, 'funding_rate_scope')
        historical = kind == 'settlement' or scope == 'last_settled_rate'
        if kind == 'venue_funding_time':
            # A scheduled funding event may be in the future; it is not the time
            # at which this indicative rate was observed.
            stamp = _get(derivatives, 'timestamp_ms')
        else:
            stamp = _get(derivatives, 'funding_rate_timestamp_ms',
                         _get(derivatives, 'funding_timestamp_ms', _get(derivatives, 'timestamp_ms')))
        state, age = _evidence_age(stamp, reference_ms)
        metrics['funding_age_seconds'] = age
        if state == 'fresh' and not historical:
            parts.append((-.25 * math.tanh(funding / .001), .4))
        else:
            label = 'last_settled_rate' if historical else state
            quality.append(f"Funding {label}: chỉ mô tả; không dùng rate cũ/không rõ thời điểm làm tín hiệu hiện tại.")
    oi = metrics.get('open_interest')
    if oi is not None:
        observations.append(f"Open interest {oi:.8g}; một ảnh chụp không xác định long/short hoặc thay đổi OI.")
        oi_state, oi_age = _evidence_age(_get(derivatives, 'open_interest_timestamp_ms',
                                           _get(derivatives, 'timestamp_ms')), reference_ms, 360000)
        metrics['open_interest_age_seconds'] = oi_age
        if oi_state != 'fresh':
            quality.append(f"Open interest có độ mới {oi_state}; không coi ảnh chụp này là OI hiện tại.")
    mark, index = metrics.get('mark_price'), metrics.get('index_price')
    if mark is not None and index is not None and mark > 0 and index > 0:
        metrics['mark_index_basis_bps'] = (mark / index - 1) * 10000
        observations.append(f"Basis mark/index {metrics['mark_index_basis_bps']:+.3g} bps.")
    oi_change = _number(_get(derivatives, 'open_interest_change_pct'))
    if oi_change is not None:
        metrics['open_interest_change_pct'] = oi_change
        change_state, change_age = _evidence_age(_get(derivatives, 'open_interest_change_timestamp_ms',
            _get(derivatives, 'open_interest_timestamp_ms', _get(derivatives, 'timestamp_ms'))), reference_ms, 360000)
        metrics['open_interest_change_age_seconds'] = change_age
        if oi_change > 0 and len(close) >= 21 and change_state == 'fresh':
            price_direction = float(np.sign(close[-1] - close[-21]))
            parts.append((.4 * price_direction * math.tanh(oi_change / 10), .6))
            observations.append(f"OI thay đổi {oi_change:+.3g}% theo khoảng thời gian do nguồn cung cấp.")
        elif change_state != 'fresh':
            quality.append(f"OI change có độ mới {change_state}; không dùng bỏ phiếu.")
    if observations and not _get(derivatives, 'source'):
        quality.append("Chỉ số futures thiếu timestamp/source: nguồn không xác định; không dùng bỏ phiếu.")
        parts = []
    score = _safe_ratio(sum(value * weight for value, weight in parts), sum(weight for _, weight in parts))
    return _method('derivatives', 'Futures: funding / OI / basis', 'derivatives', score, observations,
        ["Funding và OI phụ thuộc venue, kỳ funding và đơn vị contract; không trộn dữ liệu khác instrument.",
         "Crowding là mô tả rủi ro, không chứng minh đảo chiều; không suy ra OI change từ một giá trị."],
        available=bool(observations), voting=bool(parts))


def analyze_market(candles, trades=(), book=None, derivatives=None, *, now_ms=None):
    """Return a JSON-safe research snapshot using only explicitly closed, valid bars.

    Candle schema: time (UTC seconds), open/high/low/close, volume, closed,
    volume_is_real. Missing real-volume provenance disables volume-derived votes.
    Confidence measures heuristic evidence agreement; it is never a probability of profit.
    Methods within a shared family are averaged before voting to limit duplicate votes.
    now_ms is the snapshot receipt/as-of time in UTC milliseconds. If omitted,
    evidence age is measured against the final closed bar's inferred close time;
    this keeps historical research deterministic. Untimestamped microstructure
    remains descriptive and cannot vote as current evidence.
    """
    bars, quality = _bars(candles)
    count = len(bars)
    metrics = {'closed_candle_count': count, 'last_closed_time': bars[-1].time if bars else None,
               'last_close': bars[-1].close if bars else None}
    result = {'signal': 'INSUFFICIENT_DATA', 'score': 0.0, 'confidence': 0.0,
              'confidence_kind': 'heuristic_evidence_agreement_not_probability',
              'methods': [], 'metrics': metrics, 'summary': 'Chưa đủ nến đã đóng để kết luận thị trường.',
              'data_quality': quality, 'research_refs': [dict(ref) for ref in RESEARCH_REFS]}
    if count < 60:
        quality.append(f"Cần ít nhất 60 nến đã đóng hợp lệ; hiện có {count}.")
        return result
    intervals = [bars[index].time - bars[index - 1].time for index in range(1, count)
                 if bars[index].time > bars[index - 1].time]
    inferred_reference = (bars[-1].time + (float(np.median(intervals)) if intervals else 0)) * 1000
    reference_ms = _number(now_ms)
    if reference_ms is None or reference_ms < 0:
        reference_ms = inferred_reference
        if now_ms is not None:
            quality.append('now_ms không hợp lệ; dùng thời điểm đóng suy ra từ chuỗi nến để kiểm tra độ mới.')
    metrics['evidence_reference_time_ms'] = reference_ms
    close = np.asarray([bar.close for bar in bars], dtype=float)
    high = np.asarray([bar.high for bar in bars], dtype=float)
    low = np.asarray([bar.low for bar in bars], dtype=float)
    atr, adx, rsi, tr = _wilder(high, low, close)
    scale = max(atr, close[-1] * 1e-9)
    ema20, ema50 = _ema(close, 20), _ema(close, 50)
    trend_strength = min(adx / 35, 1)
    trend_score = (.8 * math.tanh((ema20 - ema50) / (2 * scale)) + .2 * _clip((rsi - 50) / 30)) * trend_strength
    metrics.update(ema20=ema20, ema50=ema50, rsi14=rsi, adx14=adx, atr14=atr,
                   atr_pct=_safe_ratio(atr * 100, close[-1]), stop_distance_2atr=2 * atr)
    methods = [_method('trend_momentum', 'Xu hướng + động lượng: EMA / ADX / RSI', 'trend', trend_score,
        [f"EMA20 {ema20:.8g}; EMA50 {ema50:.8g}; ADX14 {adx:.1f}; RSI14 {rsi:.1f}."],
        ["Các chỉ báo cùng nhóm được gộp thành một phiếu, không phải bốn phương pháp độc lập.",
         "Chỉ báo trễ; RSI quá mua/quá bán không tự chứng minh đảo chiều."])]

    mean20, std20 = float(np.mean(close[-20:])), float(np.std(close[-20:]))
    zscore = _safe_ratio(close[-1] - mean20, std20)
    metrics.update(zscore20=zscore, bollinger_mid=mean20, bollinger_upper=mean20 + 2 * std20,
                   bollinger_lower=mean20 - 2 * std20)
    methods.append(_method('mean_reversion', 'Hồi quy về trung bình: z-score / Bollinger', 'mean_reversion',
        -math.tanh(zscore / 2), [f"z20 {zscore:+.2f}; vùng ±2σ quanh trung bình 20 nến."],
        ["Chỉ có trọng số khi ADX không quá cao; thị trường có thể duy trì lệch trung bình lâu."]))

    previous_high, previous_low = float(np.max(high[-21:-1])), float(np.min(low[-21:-1]))
    if close[-1] > previous_high:
        structure_score = .6 + .4 * math.tanh((close[-1] - previous_high) / scale)
        structure_text = "Đóng cửa vượt đỉnh 20 nến trước."
    elif close[-1] < previous_low:
        structure_score = -.6 - .4 * math.tanh((previous_low - close[-1]) / scale)
        structure_text = "Đóng cửa thủng đáy 20 nến trước."
    else:
        structure_score = .2 * _clip(_safe_ratio(2 * close[-1] - previous_high - previous_low, previous_high - previous_low))
        structure_text = "Đóng cửa vẫn trong biên 20 nến trước."
    metrics.update(donchian_previous_high20=previous_high, donchian_previous_low20=previous_low)
    methods.append(_method('market_structure', 'Cấu trúc giá / Donchian breakout', 'structure', structure_score,
        [structure_text], ["Biên loại trừ nến hiện tại để tránh look-ahead; breakout có thể thất bại."]))

    times = np.asarray([bar.time for bar in bars], dtype=float)
    intervals = np.diff(times)
    median_interval = float(np.median(intervals))
    irregular = int(np.sum(np.abs(intervals - median_interval) > max(1.0, median_interval * .05)))
    metrics['irregular_interval_count'] = irregular
    if irregular:
        quality.append(f"Có {irregular} khoảng nến không đều/thiếu; thống kê theo bar có giới hạn.")
    logs = np.log(close[-120:])
    x = (times[-len(logs):] - times[-len(logs)]) / max(1, median_interval)
    centered_x, centered_y = x - np.mean(x), logs - np.mean(logs)
    slope = _safe_ratio(float(np.dot(centered_x, centered_y)), float(np.dot(centered_x, centered_x)))
    predicted = np.mean(logs) + slope * centered_x
    ss_total, ss_residual = float(np.sum(centered_y ** 2)), float(np.sum((logs - predicted) ** 2))
    r_squared = _clip(1 - _safe_ratio(ss_residual, ss_total, 1), 0, 1)
    predicted_move = close[-1] * math.expm1(_clip(slope * 20, -5, 5))
    statistical_score = math.tanh(predicted_move / (4 * scale)) * math.sqrt(r_squared)
    returns = np.diff(logs)
    variance = float(np.var(returns, ddof=1))
    ratio = _safe_ratio(float(np.var(logs[5:] - logs[:-5], ddof=1)), 5 * variance, None) if variance > 1e-20 else None
    metrics.update(regression_slope_pct_per_bar=math.expm1(_clip(slope, -5, 5)) * 100,
                   regression_r2=r_squared, variance_ratio_5=ratio,
                   realized_vol_pct_per_bar=math.sqrt(max(0, variance)) * 100)
    methods.append(_method('statistical_trend', 'Định lượng: hồi quy log-price / variance ratio', 'statistical', statistical_score,
        [f"R² {r_squared:.2f}; slope {metrics['regression_slope_pct_per_bar']:+.4g}%/bar.",
         f"Variance ratio lag-5 {ratio:.3g}." if ratio is not None else "Variance ratio không xác định khi return gần như không biến thiên."],
        ["Regression/variance ratio là mô tả mẫu quá khứ; không phải kiểm định dự báo đã hiệu chỉnh.",
         "Overlapping returns và mẫu nhỏ gây sai lệch; chưa có backtest ngoài mẫu."],
        available=irregular <= count * .2))

    log_ranges = np.log(high) - np.log(low)
    parkinson = math.sqrt(max(0, float(np.mean(log_ranges[-60:] ** 2))) / (4 * math.log(2))) * 100
    metrics['parkinson_vol_pct_per_bar'] = parkinson
    methods.append(_method('volatility_risk', 'Biến động / tham chiếu rủi ro ATR + Parkinson', 'risk', 0,
        [f"ATR14 {atr:.8g}; Parkinson {parkinson:.3g}%/bar; khoảng giá 2ATR {2 * atr:.8g}."],
        ["Không bỏ phiếu BUY/SELL. Khoảng giá 2ATR không phải size lệnh; cần account, contract multiplier, fees và slippage.",
         "Parkinson giả định quá trình giá liên tục; gap và microstructure có thể làm lệch ước lượng."], voting=False))

    day = math.floor(bars[-1].time / 86400)
    session = [bar for bar in bars if math.floor(bar.time / 86400) == day]
    volume_bars = session if len(session) >= 10 else bars[-96:]
    real_volume = (len(volume_bars) >= 5 and all(bar.volume_real and bar.volume is not None for bar in volume_bars)
                   and sum(bar.volume for bar in volume_bars) > 0)
    if real_volume:
        volumes = np.asarray([bar.volume for bar in volume_bars], dtype=float)
        normalized = volumes / np.max(volumes)
        typical = np.asarray([(bar.high / 3 + bar.low / 3 + bar.close / 3) for bar in volume_bars])
        vwap = float(np.dot(typical, normalized) / np.sum(normalized))
        metrics['bar_vwap'] = vwap
        methods.append(_method('bar_vwap', 'VWAP xấp xỉ từ nến có volume thật', 'volume_reference',
            math.tanh((close[-1] - vwap) / (2 * scale)),
            [f"Bar-VWAP {vwap:.8g}; {len(volume_bars)} nến phiên UTC hoặc cửa sổ gần nhất."],
            ["Giá điển hình × volume mỗi bar là xấp xỉ; không phải VWAP chính xác từ toàn bộ trade prints."], weight=.6))
        profile_bars = bars[-120:]
        profile_real = all(bar.volume_real and bar.volume is not None for bar in profile_bars)
        if profile_real:
            typical_profile = np.asarray([(bar.high / 3 + bar.low / 3 + bar.close / 3) for bar in profile_bars])
            profile_volume = np.asarray([bar.volume for bar in profile_bars], dtype=float)
            profile_volume /= max(1e-300, float(np.max(profile_volume)))
            lower, upper = float(np.min(typical_profile)), float(np.max(typical_profile))
            if upper - lower <= max(1e-12, upper * 1e-12):
                poc = value_low = value_high = lower
            else:
                bins = np.linspace(lower, upper, 25)
                histogram, _ = np.histogram(typical_profile, bins=bins, weights=profile_volume)
                poc_index = int(np.argmax(histogram))
                low_index = high_index = poc_index
                included, target = float(histogram[poc_index]), float(np.sum(histogram)) * .7
                while included < target and (low_index > 0 or high_index < len(histogram) - 1):
                    left = histogram[low_index - 1] if low_index > 0 else -1
                    right = histogram[high_index + 1] if high_index < len(histogram) - 1 else -1
                    if left >= right:
                        low_index -= 1
                        included += float(histogram[low_index])
                    else:
                        high_index += 1
                        included += float(histogram[high_index])
                poc = float((bins[poc_index] + bins[poc_index + 1]) / 2)
                value_low, value_high = float(bins[low_index]), float(bins[high_index + 1])
            metrics.update(bar_profile_poc=poc, bar_profile_value_low70=value_low, bar_profile_value_high70=value_high)
            methods.append(_method('bar_volume_profile', 'Volume profile xấp xỉ / POC / value area', 'volume_reference',
                .6 * math.tanh((close[-1] - poc) / (2 * scale)),
                [f"POC {poc:.8g}; vùng volume 70% [{value_low:.8g}, {value_high:.8g}]."],
                ["Gán volume mỗi bar vào giá điển hình/24 bins; không phải volume-at-price thực từ tick hay footprint."], weight=.4))
    else:
        quality.append("Không đủ volume giao dịch thật có provenance; bỏ VWAP/profile khỏi ensemble, không dùng tick volume thay thế.")
        methods.append(_method('bar_vwap', 'VWAP / volume profile', 'volume_reference', available=False,
            limitations=["Cần volume giao dịch thật; quotes/tick-volume/volume chưa xác minh không được thay thế."]))

    methods.append(_microstructure(trades, book, metrics, quality, reference_ms))
    methods.append(_derivatives(derivatives, close, metrics, quality, reference_ms))
    group_weights = {'trend': .30, 'structure': .20, 'mean_reversion': .12 * (1 - min(adx / 50, 1)),
                     'statistical': .18, 'volume_reference': .14, 'microstructure': .12, 'derivatives': .05}
    groups = defaultdict(list)
    for method in methods:
        if method['voting'] and group_weights.get(method['group'], 0) > 0:
            groups[method['group']].append(method)
    evidence = []
    for group, members in groups.items():
        score = sum(method['score'] * method['weight'] for method in members) / sum(method['weight'] for method in members)
        evidence.append((score, group_weights[group]))
    score = _clip(_safe_ratio(sum(value * weight for value, weight in evidence), sum(weight for _, weight in evidence)))
    signal = _signal(score)
    agreement = _safe_ratio(sum(weight for value, weight in evidence if value * score > 0), sum(weight for _, weight in evidence))
    confidence = min(80, 20 + 45 * abs(score) * agreement + 15 * min(count / 200, 1) - min(15, len(quality) * 2))
    median_tr = float(np.median(tr[-60:]))
    shock = bool(median_tr > 0 and tr[-1] > 5 * median_tr)
    if shock:
        quality.append("Nến cuối có true range >5 lần trung vị gần đây; giảm độ tin cậy vì biến động đột biến.")
        confidence = min(confidence, 40)
    if count < 120:
        quality.append("Lịch sử ngắn; EMA50/thống kê cần thêm dữ liệu và kiểm chứng ngoài mẫu.")
        confidence = min(confidence, 55)
    conclusion = {'BUY': 'thiên về tăng', 'SELL': 'thiên về giảm', 'SIDEWAYS': 'đi ngang / chưa đồng thuận'}[signal]
    result.update(signal=signal, score=score, confidence=round(max(0, confidence), 1), methods=methods,
                  regime='shock' if shock else 'trend' if adx >= 25 else 'range',
                  summary=f"{signal}: thị trường {conclusion}; ADX {adx:.0f}, RSI {rsi:.0f}. Kết luận heuristic từ nến đã đóng, không phải xác suất thắng."[:200])
    # Permit strict json.dumps(..., allow_nan=False), including unusual extreme input ranges.
    for key, value in metrics.items():
        metrics[key] = _number(value) if value is not None else None
    return result
