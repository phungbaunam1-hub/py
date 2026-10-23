"""Bounded, thread-safe trade/depth analytics. No broker or GUI dependency."""
from collections import Counter, deque
from dataclasses import dataclass
import math
import threading


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class Trade:
    time_ms: int
    price: float
    size: float
    side: str = "Unknown"


@dataclass(frozen=True, slots=True)
class Level:
    side: str
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Signal:
    time_ms: int
    kind: str
    price: float
    size: float
    detail: str


@dataclass(frozen=True, slots=True)
class Candle:
    time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    delta: float


@dataclass(frozen=True, slots=True)
class Settings:
    min_trade: float = 40
    min_wall: float = 400
    sweep_volume: float = 120
    sweep_levels: int = 3
    sweep_ms: int = 750
    candle_seconds: int = 60

    def validate(self):
        for value in (self.min_trade, self.min_wall, self.sweep_volume):
            if not finite(value) or value < 0:
                raise ValueError("Ngưỡng khối lượng phải hữu hạn và không âm.")
        for name, lo, hi in (("sweep_levels", 2, 1000), ("sweep_ms", 1, 60000),
                             ("candle_seconds", 1, 86400)):
            value = getattr(self, name)
            if not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name}: cần số nguyên từ {lo} đến {hi}.")


@dataclass(frozen=True, slots=True)
class Snapshot:
    candles: tuple
    big_trades: tuple
    book: tuple
    signals: tuple
    delta: float
    last_ms: int | None


class _Sweep:
    def __init__(self):
        self.trades = deque()
        self.prices = Counter()
        self.volume = 0.0

    def remove(self):
        item = self.trades.popleft()
        self.volume -= item.size
        self.prices[item.price] -= 1
        if not self.prices[item.price]:
            del self.prices[item.price]

    def trim(self, oldest):
        while self.trades and self.trades[0].time_ms < oldest:
            self.remove()

    def add(self, item):
        self.trades.append(item)
        self.prices[item.price] += 1
        self.volume += item.size
        while len(self.trades) > 10000:
            self.remove()


class FlowEngine:
    MAX_CANDLES = 600
    MAX_TRADES = 4000
    MAX_SIGNALS = 300
    MAX_LEVELS_PER_SIDE = 1000

    def __init__(self, settings=None):
        self._lock = threading.RLock()
        self.settings = settings or Settings()
        self.settings.validate()
        self.reset()

    def reset(self):
        with self._lock:
            self._candles = {}
            self._trades = deque(maxlen=self.MAX_TRADES)
            self._signals = deque(maxlen=self.MAX_SIGNALS)
            self._book = ()
            self._walls = {}
            self._sweeps = {"Buy": _Sweep(), "Sell": _Sweep()}
            self._last_trade = self._last_depth = self._last_ms = None
            self._delta = 0.0

    def update_settings(self, settings):
        settings.validate()
        with self._lock:
            if settings.candle_seconds != self.settings.candle_seconds:
                self._candles.clear()
            self.settings = settings
            self._sweeps = {"Buy": _Sweep(), "Sell": _Sweep()}
            self._walls.clear()

    def trade(self, item):
        if (not isinstance(item.time_ms, int) or not finite(item.price) or
                not finite(item.size) or item.size <= 0 or item.side not in {"Buy", "Sell", "Unknown"}):
            raise ValueError("Trade không hợp lệ.")
        with self._lock:
            span = self.settings.candle_seconds * 1000
            bucket = item.time_ms // span * span
            if len(self._candles) >= self.MAX_CANDLES and bucket < min(self._candles):
                return
            delta = item.size if item.side == "Buy" else -item.size if item.side == "Sell" else 0.0
            self._delta += delta
            bar = self._candles.get(bucket)
            if bar is None:
                # open/high/low/close/volume/delta/first timestamp/last timestamp
                self._candles[bucket] = [item.price] * 4 + [item.size, delta, item.time_ms, item.time_ms]
            else:
                bar[1], bar[2] = max(bar[1], item.price), min(bar[2], item.price)
                if item.time_ms < bar[6]:
                    bar[0], bar[6] = item.price, item.time_ms
                if item.time_ms >= bar[7]:
                    bar[3], bar[7] = item.price, item.time_ms
                bar[4] += item.size
                bar[5] += delta
            while len(self._candles) > self.MAX_CANDLES:
                del self._candles[min(self._candles)]
            self._trades.append(item)
            if self._last_trade is None or item.time_ms >= self._last_trade:
                self._detect_sweep(item)
                self._last_trade = item.time_ms
            self._last_ms = max(self._last_ms or item.time_ms, item.time_ms)

    def _detect_sweep(self, item):
        cfg = self.settings
        for window in self._sweeps.values():
            window.trim(item.time_ms - cfg.sweep_ms)
        if item.side == "Unknown":
            return
        window = self._sweeps[item.side]
        window.add(item)
        first = window.trades[0]
        direction = item.price > first.price if item.side == "Buy" else item.price < first.price
        if (direction and len(window.prices) >= cfg.sweep_levels and window.volume >= cfg.sweep_volume):
            self._signals.append(Signal(item.time_ms, item.side + " sweep", item.price, window.volume,
                f"{len(window.prices)} mức giá / {cfg.sweep_ms} ms. Heuristic từ prints, không chứng minh stop order."))
            self._sweeps[item.side] = _Sweep()

    def depth(self, time_ms, levels):
        if not isinstance(time_ms, int):
            raise ValueError("Depth timestamp phải là milliseconds UTC.")
        unique = {}
        for level in levels:
            if (level.side not in {"Bid", "Ask"} or not finite(level.price) or
                    not finite(level.size) or level.size < 0):
                raise ValueError("Depth không hợp lệ.")
            key = (level.side, level.price)
            if level.size:
                unique[key] = level.size
            else:
                unique.pop(key, None)
        with self._lock:
            if self._last_depth is not None and time_ms < self._last_depth:
                return False
            self._last_depth = time_ms
            bids = sorted((Level(s, p, v) for (s, p), v in unique.items() if s == "Bid"), key=lambda x: -x.price)
            asks = sorted((Level(s, p, v) for (s, p), v in unique.items() if s == "Ask"), key=lambda x: x.price)
            self._book = tuple(bids[:self.MAX_LEVELS_PER_SIDE] + asks[:self.MAX_LEVELS_PER_SIDE])
            current = {}
            for level in self._book:
                if level.size < self.settings.min_wall:
                    continue
                key = (level.side, level.price)
                if key not in self._walls or level.size >= self._walls[key] * 1.25:
                    self._signals.append(Signal(time_ms, level.side + " wall", level.price, level.size,
                        "Displayed size tại mức giá, có thể bị hủy; không phải danh tính một lệnh limit."))
                current[key] = level.size
            self._walls = current
            self._last_ms = max(self._last_ms or time_ms, time_ms)
            return True

    def snapshot(self):
        with self._lock:
            candles = tuple(Candle(t, *bar[:6]) for t, bar in sorted(self._candles.items()))
            trades = tuple(sorted((x for x in self._trades if x.size >= self.settings.min_trade),
                                  key=lambda x: x.time_ms, reverse=True))
            return Snapshot(candles, trades, self._book, tuple(reversed(self._signals)), self._delta, self._last_ms)
