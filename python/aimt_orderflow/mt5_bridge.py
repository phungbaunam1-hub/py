"""Additive market data and guarded dispatch through serialized MT5 workers.

The original application file stays byte-for-byte unchanged. Only the launcher installs
these RPCs and acknowledgment checks; account selection and original panels are retained.
"""
from collections import Counter
import contextlib
from datetime import datetime, timezone
import math
import ntpath
import os
import threading
import time

from .engine import Level, Trade
from .async_dispatch import DispatchRejectedError, IndeterminateExecutionError, check_guard


_entry_guards = threading.local()
_ENTRY_OPS = frozenset({'order', 'pending'})
_MUTATING_OPS = frozenset({'order', 'pending', 'modify', 'close', 'modify_pos', 'close_pos',
                           'delete_order', 'manage', 'close_all_mine'})


def _terminal_key_for(self, account):
    path = (account.get('path') or '').strip() or (self.cfg.get('mt5_path') or '').strip()
    if not path:
        return 'default'
    # Real Windows execution resolves relative paths/junctions before case folding.
    # The ntpath branch also makes Windows configurations deterministic in Linux tests.
    if os.name != 'nt' and (ntpath.splitdrive(path)[0] or '\\' in path):
        return ntpath.normcase(ntpath.abspath(ntpath.normpath(path)))
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


@contextlib.contextmanager
def _order_send_guard(self, guard):
    """Keep a scope validator in the calling dispatch thread, never pickle it to MT5."""
    if not callable(guard):
        raise TypeError('An order send guard must be callable.')
    stack = getattr(_entry_guards, 'stack', None)
    if stack is None:
        _entry_guards.stack = stack = []
    stack.append((self, guard))
    try:
        yield
    finally:
        stack.pop()


def _check_order_send_guard(hub):
    for owner, guard in getattr(_entry_guards, 'stack', ()):
        if owner is hub:
            check_guard(guard)


def _worker_call(self, op, *args, timeout=60, **kwargs):
    """Validate entries under the terminal lock, at the final parent-side send boundary."""
    with self.lock:
        if not self.alive():
            self.start()
        attempted_send = False
        try:
            if op in _ENTRY_OPS:
                _check_order_send_guard(self.hub)
            attempted_send = True
            self.conn.send((op, args, kwargs))
            if not self.conn.poll(timeout):
                raise TimeoutError(f"terminal '{self.key}' không phản hồi ({op})")
            ok, result, logs = self.conn.recv()
        except DispatchRejectedError:
            raise  # No request was sent. Keep the connected worker intact.
        except (EOFError, OSError, TimeoutError) as error:
            self.kill()
            if attempted_send and op in _MUTATING_OPS:
                raise IndeterminateExecutionError(
                    f'MT5 mất phản hồi {op}; kiểm tra vị thế/lệnh trước khi gửi yêu cầu mới. Không tự gửi lại.') from error
            if isinstance(error, TimeoutError):
                raise
            raise RuntimeError(f"tiến trình terminal '{self.key}' đã dừng: {type(error).__name__}") from error
        for line in logs:
            try:
                self.hub.log(line)
            except Exception:
                pass  # A closed UI logger must not discard an authoritative broker acknowledgment.
        if not ok:
            if op in _MUTATING_OPS:
                raise IndeterminateExecutionError(
                    f'MT5 {op} không có kết quả xác thực; kiểm tra vị thế/lệnh trước khi gửi yêu cầu mới.')
            raise RuntimeError(result)
        return result


def _receipt_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _safe_text(value, limit=300):
    return ''.join(char for char in str(value) if char.isprintable())[:limit]


def _send(self, base, fills):
    """Retry only explicit INVALID_FILL; absence of a result never permits a resend."""
    from original import AI_MT_Trading as legacy
    api = legacy.mt5
    attempts = []
    action = base.get('action')
    is_pending = action == getattr(api, 'TRADE_ACTION_PENDING', 5)
    sent_at = int(time.time() * 1000)
    self._last_trade_receipt = {'source': 'MT5', 'mode': 'live', 'state': 'unknown', 'accepted': False,
        'fully_filled': False, 'retcode': None, 'order': None, 'deal': None, 'request_id': None,
        'symbol': base.get('symbol'), 'action': action, 'requested_volume': base.get('volume'),
        'sent_at_ms': sent_at, 'received_at_ms': None, 'attempts': attempts}
    last = None
    for fill in tuple(fills):
        try:
            result = api.order_send(dict(base, type_filling=fill))
        except Exception as error:
            attempts.append({'type_filling': fill, 'retcode': None, 'error': type(error).__name__})
            self._last_trade_receipt.update(state='unknown', accepted=False, fully_filled=False,
                retcode=None, order=None, deal=None, request_id=None,
                error=type(error).__name__, received_at_ms=int(time.time() * 1000))
            raise IndeterminateExecutionError('MT5 order_send lỗi sau khi gọi; trạng thái chưa xác định, không tự gửi lại.') from error
        if result is None:
            attempts.append({'type_filling': fill, 'retcode': None, 'error': 'no_result'})
            self._last_trade_receipt.update(state='unknown', accepted=False, fully_filled=False,
                retcode=None, order=None, deal=None, request_id=None,
                error='order_send returned None', received_at_ms=int(time.time() * 1000))
            raise IndeterminateExecutionError('MT5 order_send không trả kết quả; trạng thái chưa xác định, không tự gửi lại.')
        last = result
        raw_code = field(result, 'retcode', None)
        try:
            numeric_code = float(raw_code)
            code = int(numeric_code) if not isinstance(raw_code, bool) and math.isfinite(numeric_code) and numeric_code.is_integer() else None
        except (TypeError, ValueError, OverflowError):
            code = None
        attempts.append({'type_filling': fill, 'retcode': code})
        accepted = code in (10008, 10009, 10010)
        state = ('placed' if code == 10008 or code == 10009 and is_pending else
                 'filled' if code == 10009 else 'partially_filled' if code == 10010 else
                 'unknown' if code in (None, 10011, 10012, 10028, 10031) else 'rejected')
        self._last_trade_receipt.update(retcode=code, state=state, accepted=accepted,
            fully_filled=state == 'filled', order=field(result, 'order', None), deal=field(result, 'deal', None),
            request_id=field(result, 'request_id', None), volume=_receipt_number(field(result, 'volume', None)),
            price=_receipt_number(field(result, 'price', None)), bid=_receipt_number(field(result, 'bid', None)),
            ask=_receipt_number(field(result, 'ask', None)), comment=_safe_text(field(result, 'comment', '')),
            received_at_ms=int(time.time() * 1000))
        if accepted:
            return True, result
        if code == 10030:  # Official explicit rejection: unsupported filling policy.
            continue
        if state == 'unknown':
            raise IndeterminateExecutionError(f'MT5 retcode {code}: trạng thái chưa xác định; không tự gửi lại.')
        return False, result
    return False, last


def _trade_receipt(self):
    import copy
    return copy.deepcopy(getattr(self, '_last_trade_receipt', None))


def _demo_entry(original, pending=False):
    def guarded(self, *args, **kwargs):
        # Demo has shared account state, so validation must follow its account/session lock.
        with self.lock:
            _check_order_send_guard(self)
            result = original(self, *args, **kwargs)
            accepted = bool(result and result[0])
            self._last_trade_receipt = {'source': 'MT5', 'mode': 'demo', 'state': 'placed' if pending else 'filled',
                'accepted': accepted, 'fully_filled': accepted and not pending, 'retcode': None,
                'order': (self.st or {}).get('tk'), 'deal': None, 'request_id': None,
                'symbol': args[0] if args else kwargs.get('sym'), 'received_at_ms': int(time.time() * 1000),
                'comment': 'Mô phỏng; không phải xác nhận từ broker MT5.'}
            return result
    return guarded


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


def _market_catalog(self):
    from original import AI_MT_Trading as legacy
    result = []
    for info in legacy.mt5.symbols_get() or ():
        result.append({'symbol': info.name, 'base': getattr(info, 'currency_base', '') or 'UNKNOWN',
                       'quote': getattr(info, 'currency_profit', '') or 'UNKNOWN',
                       'tick_size': float(getattr(info, 'trade_tick_size', 0) or getattr(info, 'point', .01) or .01),
                       'path': getattr(info, 'path', ''), 'description': getattr(info, 'description', '')})
    return result


def _all_symbols(self):
    return [item['symbol'] for item in _market_catalog(self)]


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
    legacy.MT5Local.market_catalog = _market_catalog
    legacy.MT5Local.symbols = _all_symbols
    legacy.MT5Local._send = _send
    legacy.MT5Local.trade_receipt = _trade_receipt
    legacy.MT5Worker.call = _worker_call
    legacy.MT5Hub.order_send_guard = _order_send_guard
    legacy.MT5Hub.key_for = _terminal_key_for
    legacy.DemoBroker.order_send_guard = _order_send_guard
    legacy.DemoBroker.order = _demo_entry(legacy.DemoBroker.order)
    legacy.DemoBroker.pending = _demo_entry(legacy.DemoBroker.pending, pending=True)
    legacy.DemoBroker.trade_receipt = _trade_receipt
    legacy.RPC_METHODS.update({"orderflow_read", "orderflow_release", "market_catalog", "trade_receipt"})
    shutdown = legacy.MT5Local.shutdown

    def close(self):
        try:
            _release(self)
        finally:
            shutdown(self)

    legacy.MT5Local.shutdown = close
