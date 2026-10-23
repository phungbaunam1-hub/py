"""Read-only feed lifecycle; Qt is used only for queued event delivery."""
import random
import threading
import time
from PyQt6 import QtCore
from .engine import Level, Trade


class FeedSignals(QtCore.QObject):
    frame = QtCore.pyqtSignal(int, object)
    status = QtCore.pyqtSignal(int, str)


class BrokerFeed(threading.Thread):
    def __init__(self, broker, account, symbol, signals, generation):
        super().__init__(daemon=True, name="AIMT-MT5-OrderFlow")
        self.broker, self.account, self.symbol = broker, dict(account), symbol
        self.signals, self.generation = signals, generation
        self._stop_event = threading.Event()
        self._delivered = threading.Event()
        self._delivered.set()

    def stop(self):
        self._stop_event.set()
        self._delivered.set()

    def acknowledge(self):
        self._delivered.set()

    def run(self):
        first = True
        try:
            self.signals.status.emit(self.generation, "MT5 · đang chờ kết nối terminal đang chọn…")
            while not self._stop_event.is_set():
                if not self._delivered.wait(.25) or self._stop_event.is_set():
                    continue
                if self.broker.acquire(self.account, 0.05):
                    try:
                        if first:
                            self.symbol = self.broker.resolve(self.symbol)
                        frame = self.broker.orderflow_read(self.symbol, reset=first, timeout=5)
                    finally:
                        self.broker.release(self.account)
                    first = False
                    if not self._stop_event.is_set():
                        self._delivered.clear()  # At most one queued market frame per session.
                        self.signals.frame.emit(self.generation, frame)
                self._stop_event.wait(0.25)
        except Exception as error:
            if not self._stop_event.is_set():
                self.signals.status.emit(self.generation, f"Lỗi MT5 order flow: {error}")
        finally:
            if not first:
                try:
                    if self.broker.acquire(self.account, 0.1):
                        try:
                            self.broker.orderflow_release()
                        finally:
                            self.broker.release(self.account)
                except Exception:
                    pass  # Original broker.shutdown also releases worker-owned subscriptions.


class DemoFeed:
    """Seeded synthetic exchange prints, deliberately separate from the MT5 candle history."""
    def __init__(self, symbol, base=4100.0, tick=0.25, seed=812):
        self.symbol, self.tick = symbol, tick
        self.base = round(base / tick) * tick
        self.mid, self.index = self.base, 0
        self.random = random.Random(seed)

    def frame(self, time_ms=None):
        time_ms = int(time.time() * 1000) if time_ms is None else time_ms
        rng, step = self.random, self.tick
        direction = rng.choice((-1, 1))
        self.mid = max(self.base - 60 * step, min(self.base + 60 * step,
                       self.mid + direction * rng.randrange(3) * step))
        book = []
        for i in range(12):
            book.append(Level("Bid", self.mid - (i + 1) * step, 1200 if i == 4 else rng.randint(10, 220)))
            book.append(Level("Ask", self.mid + i * step, 1800 if i == 6 else rng.randint(10, 220)))
        count = 4 if self.index % 31 == 0 else 1
        trades = []
        for i in range(count):
            price = self.mid + i * step if direction > 0 else self.mid - (i + 1) * step
            size = rng.randint(90, 180) if count > 1 else rng.randint(1, 650 if self.index % 13 == 0 else 45)
            trades.append(Trade(time_ms + i * 8, price, size, "Buy" if direction > 0 else "Sell"))
        self.index += 1
        return {"time_ms": time_ms, "trades": trades, "levels": tuple(book),
                "symbol": self.symbol,
                "bid": self.mid - step, "ask": self.mid, "warning": "DEMO · dữ liệu order flow được sinh riêng, không phải giao dịch MT5/Rithmic."}
