"""Bounded worker-to-UI mailbox and conservative last-price chart updates.

REST remains authoritative for candle volume and completed bars. Streaming prices
never add a print volume to a REST candle, because those observations overlap.
"""
from collections import deque
from collections.abc import Mapping
import copy
import math
import threading

from .data import TIMEFRAMES


class TradeMailbox:
    def __init__(self, capacity=5000):
        self._lock = threading.Lock()
        self._trades = deque(maxlen=capacity)
        self._status = None
        self._dropped = 0

    def trades(self, rows):
        with self._lock:
            self._dropped += max(0, len(self._trades) + len(rows) - self._trades.maxlen)
            self._trades.extend(dict(row) for row in rows)

    def status(self, value):
        with self._lock:
            self._status = dict(value)

    def drain(self, limit=2000):
        with self._lock:
            rows = [self._trades.popleft() for _ in range(min(limit, len(self._trades)))]
            status, dropped = self._status, self._dropped
            self._status, self._dropped = None, 0
        return rows, status, dropped


def update_price_candles(candles, trades, timeframe):
    """Update unclosed prices from finite, valid UTC-millisecond observations.

    An older print can extend the current range, but cannot move its last price
    backward in event time. Return copies; never invent volume or gap bars.
    """
    result = copy.deepcopy(candles)
    seconds = TIMEFRAMES[timeframe]
    valid = []
    for trade in trades if trades is not None else ():
        if not isinstance(trade, Mapping):
            continue
        try:
            if isinstance(trade.get('price'), bool) or isinstance(trade.get('time_ms'), bool):
                continue
            price, stamp = float(trade['price']), float(trade['time_ms'])
            if not math.isfinite(price) or not 0 < price <= 1e100:
                continue
            if not math.isfinite(stamp) or not 0 <= stamp <= 253402300799999 or stamp != int(stamp):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        valid.append((int(stamp), price))
    for stamp, price in sorted(valid, key=lambda row: row[0]):
        bucket = stamp // (seconds * 1000) * seconds
        if result and bucket < result[-1]['time']:
            continue
        if not result or bucket > result[-1]['time']:
            result.append({'time': bucket, 'open': price, 'high': price, 'low': price,
                           'close': price, 'volume': 0.0, 'closed': False,
                           'volume_is_real': False, 'volume_complete': False,
                           'source': 'stream_partial_prices_only'})
        elif result[-1].get('closed') is True:
            continue
        bar = result[-1]
        bar['high'], bar['low'] = max(bar['high'], price), min(bar['low'], price)
        previous_stamp = bar.get('stream_price_time_ms', -1)
        if stamp >= previous_stamp:
            bar['close'] = price
            bar['stream_price_time_ms'] = stamp
        bar['volume_complete'] = False
    return result[-1000:]
