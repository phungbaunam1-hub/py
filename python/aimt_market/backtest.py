"""Small train-only parameter grids and strictly ordered out-of-sample research.

This module does not send orders. It simulates fully funded long/flat exposure, so a
crypto spot chart does not silently acquire shorting or leverage capabilities. Signals
at candle close can change exposure only at the following candle open. Each fold is
liquidated at its final bar's open, a scheduled boundary, and has no overnight position
carried into the next parameter-selection window. That last bar is a liquidation bar,
not an additional return period. High/low are never used to claim intrabar fills.
"""
from dataclasses import dataclass
import math
from statistics import mean, median, stdev


INITIAL_CAPITAL = 10_000.0
MINIMUM_CANDLES = 300
GRIDS = {
    'trend': ({'fast': 8, 'slow': 21}, {'fast': 12, 'slow': 36}, {'fast': 20, 'slow': 50}),
    'breakout': ({'lookback': 10}, {'lookback': 20}, {'lookback': 40}),
    'mean_reversion': ({'window': 20, 'entry_z': 1.5}, {'window': 20, 'entry_z': 2.0},
                       {'window': 40, 'entry_z': 1.5}, {'window': 40, 'entry_z': 2.0}),
}


@dataclass(frozen=True)
class _Bar:
    time: float
    open: float
    high: float
    low: float
    close: float


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and abs(value) <= 1e100 else None


def _normalize(candles):
    rows, rejected, forming, duplicate = {}, 0, 0, 0
    for item in candles if candles is not None else ():
        if not isinstance(item, dict):
            rejected += 1
            continue
        if item.get('closed') is not True:
            forming += 1
            continue
        values = [_number(item.get(key)) for key in ('time', 'open', 'high', 'low', 'close')]
        if any(value is None for value in values):
            rejected += 1
            continue
        timestamp, opening, high, low, close = values
        if min(opening, high, low, close) <= 0 or high < max(opening, close) or low > min(opening, close) or high < low:
            rejected += 1
            continue
        if timestamp in rows:
            duplicate += 1
        rows[timestamp] = _Bar(timestamp, opening, high, low, close)
    warnings = []
    if rejected:
        warnings.append(f"Bỏ {rejected} nến OHLC/timestamp không hợp lệ; không lấp bằng dữ liệu giả.")
    if forming:
        warnings.append(f"Bỏ {forming} nến đang hình thành hoặc thiếu cờ closed=True.")
    if duplicate:
        warnings.append(f"Có {duplicate} timestamp trùng; dùng nến đã đóng cuối cùng tại timestamp đó.")
    return [rows[key] for key in sorted(rows)], warnings


def _ema_series(close, period):
    """SMA-seeded EMA. Every output depends only on its prefix."""
    values = [None] * len(close)
    if len(close) < period:
        return values
    current = mean(close[:period])
    values[period - 1] = current
    alpha = 2.0 / (period + 1)
    for index in range(period, len(close)):
        current += alpha * (close[index] - current)
        values[index] = current
    return values


def _signals(rows, family, parameters):
    """Long/flat target after each closed bar; no future index is consulted."""
    close = [bar.close for bar in rows]
    result = [0] * len(rows)
    if family == 'trend':
        fast = _ema_series(close, parameters['fast'])
        slow = _ema_series(close, parameters['slow'])
        for index, (short, long) in enumerate(zip(fast, slow)):
            result[index] = int(short is not None and long is not None and short > long)
    elif family == 'breakout':
        lookback, position = parameters['lookback'], 0
        for index in range(lookback, len(rows)):
            previous = rows[index - lookback:index]
            if close[index] > max(bar.high for bar in previous):
                position = 1
            elif close[index] < min(bar.low for bar in previous):
                position = 0
            result[index] = position
    elif family == 'mean_reversion':
        window, position = parameters['window'], 0
        for index in range(window - 1, len(rows)):
            sample = close[index - window + 1:index + 1]
            center = mean(sample)
            deviation = math.sqrt(mean((value - center) ** 2 for value in sample))
            zscore = (close[index] - center) / deviation if deviation else 0.0
            if position == 0 and zscore <= -parameters['entry_z']:
                position = 1
            elif position == 1 and close[index] >= center:
                position = 0
            result[index] = position
    else:
        raise ValueError("family phải là trend, breakout hoặc mean_reversion.")
    return result


def _max_drawdown(curve, capital):
    peak, maximum = capital, 0.0
    for item in curve:
        peak = max(peak, item['equity'])
        maximum = max(maximum, (peak - item['equity']) / peak if peak else 0.0)
    return maximum * 100


def _sharpe(returns):
    if len(returns) < 2:
        return None
    volatility = stdev(returns)
    if volatility <= 1e-15:
        return None
    value = mean(returns) / volatility
    return value if math.isfinite(value) else None


def _simulate(rows, signals, start, stop, fee_bps, slippage_bps, capital=INITIAL_CAPITAL):
    """Execute precomputed causal targets at opens within [start, stop).

    Turnover costs are illustrative proportional deductions from equity at each entry
    and exit; they are not a claim about a venue's actual fees or available liquidity.
    The final open in the interval is a pre-scheduled flat boundary.
    """
    equity = gross = capital
    position, entries, exits, exposure = 0, 0, 0, 0
    fees, slippage = 0.0, 0.0
    curve, returns, trades = [], [], []
    entry = None
    fee_rate, slip_rate = fee_bps / 10_000, slippage_bps / 10_000
    for index in range(start, stop):
        previous_equity = equity
        if index > start and position:
            factor = rows[index].open / rows[index - 1].open
            equity *= factor
            gross *= factor
            exposure += 1
        if not math.isfinite(equity) or not math.isfinite(gross):
            raise ValueError("Biến động/giá gây tràn số trong mô phỏng; kiểm tra dữ liệu nguồn.")
        desired = 0 if index == stop - 1 else signals[index - 1] if index > 0 else 0
        if desired != position:
            before_cost = equity
            fee, slip = before_cost * fee_rate, before_cost * slip_rate
            equity -= fee + slip
            fees += fee
            slippage += slip
            if desired:
                entries += 1
                entry = {'entry_time': rows[index].time, 'entry_open': rows[index].open,
                         'equity_before_entry': before_cost}
            else:
                exits += 1
                if entry is not None:
                    trades.append({'entry_time': entry['entry_time'], 'entry_open': entry['entry_open'],
                                   'exit_time': rows[index].time, 'exit_open': rows[index].open,
                                   'net_pnl_pct': (equity / entry['equity_before_entry'] - 1) * 100})
                    entry = None
            position = desired
        returns.append(equity / previous_equity - 1 if previous_equity else 0.0)
        curve.append({'time': rows[index].time, 'equity': equity,
                      'gross_equity': gross, 'position': position})
    stats = {'initial_capital': capital, 'ending_equity': equity,
             'gross_return_pct': (gross / capital - 1) * 100,
             'net_return_pct': (equity / capital - 1) * 100,
             'max_drawdown_pct': _max_drawdown(curve, capital),
             'trade_count': exits, 'entry_count': entries, 'exit_count': exits,
             'turnover_count': entries + exits, 'fees': fees, 'slippage': slippage,
             'costs': fees + slippage, 'costs_pct_initial': (fees + slippage) / capital * 100,
             'exposure_bars': exposure, 'return_observations': len(returns),
             'win_rate_pct': sum(trade['net_pnl_pct'] > 0 for trade in trades) / len(trades) * 100 if trades else None,
             'sharpe_per_bar': _sharpe(returns), 'sharpe_annualized': None,
             'sharpe_scaling_factor': 1.0, 'risk_free_rate_per_bar': 0.0}
    return stats, curve, returns, trades


def _empty_metrics():
    return {'initial_capital': INITIAL_CAPITAL, 'ending_equity': INITIAL_CAPITAL,
            'gross_return_pct': 0.0, 'net_return_pct': 0.0, 'max_drawdown_pct': 0.0,
            'trade_count': 0, 'entry_count': 0, 'exit_count': 0, 'turnover_count': 0,
            'fees': 0.0, 'slippage': 0.0, 'costs': 0.0, 'costs_pct_initial': 0.0,
            'exposure_bars': 0, 'return_observations': 0, 'win_rate_pct': None,
            'sharpe_per_bar': None, 'sharpe_annualized': None,
            'sharpe_scaling_factor': 1.0, 'risk_free_rate_per_bar': 0.0}


def walk_forward_report(candles, family='trend', fee_bps=5.0, slippage_bps=2.0,
                        train_bars=150, test_bars=60):
    """Select each fold's parameters on past data, then evaluate its unseen test bars.

    Objective: train net return minus half train maximum drawdown. The small grid is
    disclosed, not a claim of finding a globally optimal or profitable strategy. Test
    data is never used to pick that fold's parameters. Later folds may legitimately
    train on previously observed test history. Incomplete final folds are not reported.
    """
    if family not in GRIDS:
        raise ValueError("family phải là trend, breakout hoặc mean_reversion.")
    fee_bps, slippage_bps = _number(fee_bps), _number(slippage_bps)
    if fee_bps is None or slippage_bps is None or min(fee_bps, slippage_bps) < 0 or fee_bps + slippage_bps >= 10_000:
        raise ValueError("Phí/slippage phải hữu hạn, không âm và tổng dưới 10.000 bps mỗi chiều.")
    if (not isinstance(train_bars, int) or isinstance(train_bars, bool) or train_bars < 60 or
            not isinstance(test_bars, int) or isinstance(test_bars, bool) or test_bars < 10):
        raise ValueError("Cần train_bars nguyên ≥60 và test_bars nguyên ≥10.")
    rows, warnings = _normalize(candles)
    grid = [dict(parameters) for parameters in GRIDS[family]]
    required = max(MINIMUM_CANDLES, train_bars + test_bars)
    report = {'status': 'insufficient', 'family': family, 'closed_candle_count': len(rows),
              'required_candle_count': required, 'train_bars': train_bars, 'test_bars': test_bars,
              'fee_bps': fee_bps, 'slippage_bps': slippage_bps,
              'candidate_grid': grid, 'optimization_objective': 'train_net_return_pct_minus_half_max_drawdown_pct',
              'bar_interval_seconds': None, 'windows': [], 'equity_curve': [],
              'combined_test_metrics': _empty_metrics(), 'warnings': warnings,
              'assumptions': [
                  "Nghiên cứu long/flat, fully funded, vốn giả định 10.000 đơn vị; không margin, short hay gửi lệnh.",
                  "Tín hiệu từ close t chỉ được thực thi tại open t+1. Không dùng high/low để tạo khớp intrabar.",
                  "Open của bar cuối mỗi fold là mốc thanh lý đã định trước; fold có test_bars-1 khoảng open-to-open.",
                  "Phí và slippage là chi phí turnover ở cả entry/exit. Không có funding, borrow, spread động, thanh khoản hay thuế.",
                  "Mỗi nhóm là một họ quy tắc với grid nhỏ; train dùng return ròng trừ 0,5×drawdown, không nhìn test để chọn.",
                  "Sharpe là mean/std của return theo bar, risk-free=0, hệ số=1; không tự annualize.",
              ]}
    if len(rows) < required:
        warnings.append(f"Cần ít nhất {required} nến đã đóng hợp lệ; hiện có {len(rows)}. Chưa có kết quả ngoài mẫu.")
        return report
    intervals = [rows[index].time - rows[index - 1].time for index in range(1, len(rows))]
    interval = median(intervals)
    if interval > 0 and all(abs(value - interval) <= max(1e-6, interval * .001) for value in intervals):
        report['bar_interval_seconds'] = interval
    else:
        warnings.append("Khoảng nến không đều; Sharpe chỉ theo quan sát bar, không annualized hoặc suy ra số phiên/năm.")
    signals = [_signals(rows, family, parameters) for parameters in grid]
    combined_curve, combined_returns, all_test_trades = [], [], []
    combined_capital, combined_gross = INITIAL_CAPITAL, INITIAL_CAPITAL
    totals = {key: 0.0 for key in ('fees', 'slippage', 'exposure_bars', 'trade_count', 'entry_count', 'exit_count', 'turnover_count')}
    test_start = train_bars
    while test_start + test_bars <= len(rows):
        train_start, test_stop = test_start - train_bars, test_start + test_bars
        candidates = []
        for index, target in enumerate(signals):
            training, _, _, _ = _simulate(rows, target, train_start, test_start, fee_bps, slippage_bps)
            objective = training['net_return_pct'] - .5 * training['max_drawdown_pct']
            candidates.append((objective, -index, training))
        _, negative_index, training = max(candidates, key=lambda item: (item[0], item[1]))
        selected = -negative_index
        testing, curve, returns, trades = _simulate(rows, signals[selected], test_start, test_stop, fee_bps, slippage_bps)
        window = {'fold': len(report['windows']) + 1,
                  'train_start_time': rows[train_start].time, 'train_end_time': rows[test_start - 1].time,
                  'test_start_time': rows[test_start].time, 'test_end_time': rows[test_stop - 1].time,
                  'train_start_index': train_start, 'train_stop_index_exclusive': test_start,
                  'test_start_index': test_start, 'test_stop_index_exclusive': test_stop,
                  'chosen_parameters': dict(grid[selected]), 'train_metrics': training,
                  'test_metrics': testing, 'test_trades': trades,
                  'flat_at_fold_end': True}
        report['windows'].append(window)
        net_scale, gross_scale = combined_capital / INITIAL_CAPITAL, combined_gross / INITIAL_CAPITAL
        for item in curve:
            combined_curve.append({'time': item['time'], 'equity': item['equity'] * net_scale,
                                   'gross_equity': item['gross_equity'] * gross_scale,
                                   'position': item['position']})
        for key in totals:
            totals[key] += testing[key] * (net_scale if key in ('fees', 'slippage') else 1)
        combined_capital = testing['ending_equity'] * net_scale
        combined_gross *= 1 + testing['gross_return_pct'] / 100
        combined_returns.extend(returns)
        all_test_trades.extend(trades)
        test_start = test_stop
    combined = report['combined_test_metrics']
    combined.update(totals)
    for key in ('exposure_bars', 'trade_count', 'entry_count', 'exit_count', 'turnover_count'):
        combined[key] = int(combined[key])
    combined.update(ending_equity=combined_capital,
                    net_return_pct=(combined_capital / INITIAL_CAPITAL - 1) * 100,
                    gross_return_pct=(combined_gross / INITIAL_CAPITAL - 1) * 100,
                    max_drawdown_pct=_max_drawdown(combined_curve, INITIAL_CAPITAL),
                    costs=totals['fees'] + totals['slippage'],
                    costs_pct_initial=(totals['fees'] + totals['slippage']) / INITIAL_CAPITAL * 100,
                    return_observations=len(combined_returns), sharpe_per_bar=_sharpe(combined_returns),
                    win_rate_pct=sum(trade['net_pnl_pct'] > 0 for trade in all_test_trades) / len(all_test_trades) * 100 if all_test_trades else None)
    report.update(status='complete', equity_curve=combined_curve,
                  unused_tail_bars=len(rows) - test_start)
    warnings.extend([
        "Đây là kết quả ngoài mẫu theo các fold trong cùng lịch sử, không phải cam kết lợi nhuận tương lai.",
        "Grid search vẫn có rủi ro data snooping; kiểm chứng tiếp ở thời kỳ/venue độc lập trước khi dùng thực tế.",
        "Chi phí thật, thiếu nến, latency, spread và chất lượng khớp có thể làm kết quả khác đáng kể.",
    ])
    if combined['trade_count'] == 0:
        warnings.append("Không có giao dịch round-trip ngoài mẫu; không suy diễn hiệu quả từ đường equity phẳng.")
    return report
