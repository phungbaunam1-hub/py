"""Read-only market data through the original serialized MT5 workers.

The original application file stays byte-for-byte unchanged. Only the launcher installs
these additive RPCs; order placement, account selection and all original panels are retained.
"""
from collections import Counter
from datetime import datetime, timezone
import math
import time

from .engine import Level, Trade


def field(row, name, default=0):
    if isinstance(row, dict):
        return row.get(name, default)
    names = getattr(getattr(row, "dtype", None), "names", ()) or ()
    if name in names:
        return row[name]
    return getattr(row, name, default)


class TickCursor:
    """Overlap-safe ingestion preserving multiple identical prints in a millisecond.

    MT5 supplies no universal trade sequence ID. Counts identify repeated occurrences within
    overlapping responses. Corrections outside the retained two-second window cannot be repaired.
    """
    def __init__(self, start_ms):
        self.last_ms = int(start_ms)
        self._seen = {}

    def consume(self, rows, buy_flag, sell_flag):
        counts, result = Counter(), []
        for row in rows:
            ms = int(field(row, "time_msc") or int(field(row, "time")) * 1000)
            price = float(field(row, "last"))
            size = float(field(row, "volume_real") or field(row, "volume"))
            flags = int(field(row, "flags"))
            signature = (ms, price, size, flags, float(field(row, "bid")), float(field(row, "ask")))
            counts[signature] += 1
            if ms < self.last_ms - 2000 or counts[signature] <= self._seen.get(signature, 0):
                continue
            self.last_ms = max(self.last_ms, ms)
            if not math.isfinite(price) or not math.isfinite(size) or price == 0 or size <= 0:
                continue
            buy, sell = bool(flags & buy_flag), bool(flags & sell_flag)
            side = "Buy" if buy and not sell else "Sell" if sell and not buy else "Unknown"
            result.append(Trade(ms, price, size, side))
        for signature, count in counts.items():
            self._seen[signature] = max(self._seen.get(signature, 0), count)
        self._seen = {key: count for key, count in self._seen.items() if key[0] >= self.last_ms - 2000}
        return sorted(result, key=lambda item: item.time_ms)


def normalize_book(rows, buy_type, sell_type):
    result = []
    for row in rows:
        kind = int(field(row, "type", -1))
        if kind not in (buy_type, sell_type):
            continue  # Market-order book entries are not resting limit liquidity.
        price = float(field(row, "price"))
        size = float(field(row, "volume_dbl") or field(row, "volume"))
        if math.isfinite(price) and math.isfinite(size) and size >= 0:
            result.append(Level("Bid" if kind == buy_type else "Ask", price, size))
    return tuple(result)


def _read(self, symbol, reset=False):
    from original import AI_MT_Trading as legacy
    api = legacy.mt5
    if reset:
        _release(self)
    states = getattr(self, "_orderflow_states", None)
    if states is None:
        self._orderflow_states = states = {}
    state = states.get(symbol)
    if state is None:
        state = states[symbol] = {"cursor": TickCursor(int(time.time() * 1000) - 1000),
                                 "book": bool(api.market_book_add(symbol))}
    cursor = state["cursor"]
    now_ms = int(time.time() * 1000)
    start = datetime.fromtimestamp((cursor.last_ms - 1000) / 1000, timezone.utc)
    end = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    rows = api.copy_ticks_range(symbol, start, end, api.COPY_TICKS_TRADE)
    warning = ""
    if rows is None:
        warning = f"Không lấy được trade prints: MT5 {api.last_error()[0]}. Broker có thể chỉ cấp quotes."
        trades = []
    else:
        if len(rows) > 50000:
            raise RuntimeError("Trade backlog quá lớn; dừng feed để tránh bỏ dữ liệu âm thầm. Hãy kết nối lại.")
        trades = cursor.consume(rows, api.TICK_FLAG_BUY, api.TICK_FLAG_SELL)
    book = api.market_book_get(symbol) if state["book"] else None
    levels = normalize_book(book or (), api.BOOK_TYPE_BUY, api.BOOK_TYPE_SELL)
    if not state["book"]:
        warning += " Broker chưa cấp DOM cho symbol này."
    elif book is None:
        warning += f" DOM tạm không có dữ liệu: MT5 {api.last_error()[0]}."
    quote = api.symbol_info_tick(symbol)
    cursor.last_ms = max(cursor.last_ms, now_ms - 1000)
    return {"time_ms": now_ms, "trades": trades, "levels": levels, "symbol": symbol,
            "bid": float(quote.bid) if quote else None,
            "ask": float(quote.ask) if quote else None,
            "warning": warning.strip()}


def _release(self):
    from original import AI_MT_Trading as legacy
    for symbol, state in getattr(self, "_orderflow_states", {}).items():
        if state["book"]:
            legacy.mt5.market_book_release(symbol)
    self._orderflow_states = {}


def run_worker(conn, cfg):
    from original import AI_MT_Trading as legacy
    install(legacy)
    legacy._aimt_original_worker(conn, cfg)


def install(legacy):
    if hasattr(legacy, "_aimt_original_worker"):
        return
    legacy._aimt_original_worker = legacy._mt5_worker
    legacy._mt5_worker = run_worker
    legacy.MT5Local.orderflow_read = _read
    legacy.MT5Local.orderflow_release = _release
    legacy.RPC_METHODS.update({"orderflow_read", "orderflow_release"})
    shutdown = legacy.MT5Local.shutdown

    def close(self):
        try:
            _release(self)
        finally:
            shutdown(self)

    legacy.MT5Local.shutdown = close
