"""Reuse the original eight studies with closed bars; add independently computed quant studies."""
from collections.abc import Mapping
import math
from types import FunctionType
import numpy as np


def _clean_candles(candles, *, closed_only=False):
    """Validate bar geometry before it reaches NumPy or the original studies."""
    rows, rejected, unknown, forming, bad_volume, duplicates = {}, 0, 0, 0, 0, 0
    for item in candles if candles is not None else ():
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        if closed_only:
            closed = item.get('closed')
            if not isinstance(closed, (bool, np.bool_)):
                unknown += 1
                continue
            if not bool(closed):
                forming += 1
                continue
        try:
            if any(isinstance(item.get(key), (bool, np.bool_)) for key in ('time', 'open', 'high', 'low', 'close')):
                raise ValueError('boolean bar value')
            stamp, opening, high, low, close = (float(item[key]) for key in ('time', 'open', 'high', 'low', 'close'))
            if not all(math.isfinite(value) for value in (stamp, opening, high, low, close)):
                raise ValueError('nonfinite bar')
            if stamp < 0 or stamp > 253402300799 or stamp != int(stamp):
                raise ValueError('invalid UTC seconds')
            if min(opening, high, low, close) <= 0 or max(opening, high, low, close) > 1e100:
                raise ValueError('invalid price')
            if low > min(opening, close) or high < max(opening, close) or low > high:
                raise ValueError('invalid OHLC geometry')
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
            continue
        row = dict(item, time=int(stamp), open=opening, high=high, low=low, close=close)
        try:
            raw_volume = item.get('volume', 0)
            volume = float(raw_volume) if not isinstance(raw_volume, (bool, np.bool_)) else math.nan
            if not math.isfinite(volume) or not 0 <= volume <= 1e100:
                raise ValueError('invalid volume')
        except (TypeError, ValueError, OverflowError):
            volume = 0.0
            bad_volume += 1
            row['volume_is_real'] = False
        row['volume'] = volume
        if row['time'] in rows:
            duplicates += 1
        rows[row['time']] = row
    quality = []
    if rejected:
        quality.append(f'Bỏ {rejected} nến có timestamp/giá/OHLC không hợp lệ.')
    if unknown:
        quality.append(f'Bỏ {unknown} nến thiếu cờ closed kiểu boolean; không tự giả định đã đóng.')
    if forming:
        quality.append(f'Không dùng {forming} nến đang hình thành cho tám phương pháp gốc.')
    if bad_volume:
        quality.append(f'{bad_volume} nến thiếu volume hợp lệ; dùng 0, không suy ra volume giao dịch.')
    if duplicates:
        quality.append(f'Gộp {duplicates} timestamp trùng; giữ bản cuối cùng hợp lệ.')
    return [rows[stamp] for stamp in sorted(rows)], quality


def bars_from_candles(legacy, candles):
    candles, _ = _clean_candles(candles)
    dtype = [('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'),
             ('close', 'f8'), ('tick_volume', 'f8')]
    values = np.zeros(len(candles), dtype=dtype)
    for index, candle in enumerate(candles):
        for name in ('time', 'open', 'high', 'low', 'close'):
            values[name][index] = candle[name]
        values['tick_volume'][index] = candle.get('volume', 0) or 0
    return legacy.Bars(values)


def original_studies(legacy, candles):
    """Adapt the original mixed [-1]/[-2] indexing to explicitly closed bars.

    The flat stub supplies the latest closed price to [-1] readers and leaves [-2]
    readers on the last real bar. Swing confirmation excludes that synthetic stub.
    Original source/globals and the MT5 execution pipeline remain untouched.
    """
    closed, quality = _clean_candles(candles, closed_only=True)
    if len(closed) < 120:
        return {'results': [], 'serialized': [], 'overlay': {'zones': [], 'levels': []},
                'warning': 'Cần ít nhất 120 nến đã đóng cho tám phương pháp gốc.',
                'data_quality': quality}
    stub = dict(closed[-1])
    stub.update(time=stub['time'] + 1, open=stub['close'], high=stub['close'],
                low=stub['close'], volume=0)
    bars = bars_from_candles(legacy, closed + [stub])
    confirmed_bars = bars_from_candles(legacy, closed)
    original_zigzag = getattr(legacy, 'zigzag', None)

    def confirmed_zigzag(value, *args, **kwargs):
        return original_zigzag(confirmed_bars if value is bars else value, *args, **kwargs)
    atr = float(legacy.atr_arr(bars, 14)[-2])
    if not math.isfinite(atr) or atr <= 0:
        atr = max(float(np.mean(bars.h - bars.l)), abs(float(bars.c[-2])) * 1e-6, 1e-8)
    results, serialized = [], []
    overlay = {'zones': [], 'levels': []}
    for key, name, fn in legacy.ANALYZERS:
        try:
            study = fn
            if original_zigzag is not None and isinstance(fn, FunctionType):
                study = FunctionType(fn.__code__, dict(fn.__globals__, zigzag=confirmed_zigzag),
                                     fn.__name__, fn.__defaults__, fn.__closure__)
                study.__kwdefaults__ = fn.__kwdefaults__
            result = study(bars, atr)
            result.key, result.name = key, name
        except Exception as error:
            result = legacy.Res(key, name, state='Lỗi')
            result.notes = [f'{type(error).__name__}: {error}']
        results.append(result)
        serialized.append({'key': key, 'name': name, 'signal': {1: 'BUY', -1: 'SELL', 0: 'SIDEWAYS'}.get(result.sig, 'SIDEWAYS'),
                           'score': result.sig, 'heuristic_confidence': result.conf,
                           'state': result.state, 'notes': list(result.notes),
                           'zones': list(result.zones), 'levels': list(result.levels)})
        overlay['zones'].extend(result.zones)
        overlay['levels'].extend(result.levels)
    return {'results': results, 'serialized': serialized, 'overlay': overlay,
            'warning': ' · '.join(quality), 'data_quality': quality}
