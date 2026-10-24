"""Read-only bridge from public chart snapshots/prints to the bounded flow engine.

The public REST recent-trades endpoints contain a finite window, not complete exchange
history. Session delta and sweep detections describe only accepted prints. Native REST
and WebSocket IDs share a deduplication domain. Aggregated WebSocket prints start a new
tape domain and permanently exclude native REST fills until the chart/session resets.
Book observations are complete REST snapshots; they are never presented as a live L2
delta feed or used to invent individual limit orders.
"""
from collections import deque
from dataclasses import dataclass
import math
import time

from aimt_orderflow.engine import Level, Trade
from .data import CAPABILITIES


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def _timestamp(value):
    result = _number(value, positive=True)
    return int(result) if result is not None and result.is_integer() and result <= 253402300799999 else None


def _text(value, limit=160):
    return ''.join(char for char in str(value) if char.isprintable())[:limit]


@dataclass(frozen=True, slots=True)
class FlowUpdate:
    reset: bool
    added_trades: int
    book_updated: bool
    ignored_trades: int


class PublicFlowAdapter:
    MAX_SEEN = 10000
    MAX_BATCH = 2000
    MAX_BOOK_LEVELS = 1000

    def __init__(self, engine, history=None, clock=None):
        self.engine = engine
        self.history = history if history is not None else deque(maxlen=160)
        self._clock = clock or time.time
        self.reset()

    def reset(self):
        self.engine.reset()
        self.history.clear()
        self.chart_key = self.instrument = None
        self.domain = 'native'
        self.transport = 'REST'
        self.seen_ids, self._seen_order = set(), deque()
        self.last_rest_ms = self.last_trade_ms = self.last_book_ms = None
        self._book_version = None
        self.quote = {}
        self.warning = ''
        self.idless_prints = False
        self.ignored_trades = 0

    def _new_tape_domain(self, domain):
        self.engine.reset()
        self.history.clear()
        self.seen_ids.clear()
        self._seen_order.clear()
        self.domain = domain
        self.last_trade_ms = self.last_book_ms = None
        self._book_version = None
        self.idless_prints = False

    def _remember(self, key):
        if key in self.seen_ids:
            return False
        self.seen_ids.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self.MAX_SEEN:
            self.seen_ids.discard(self._seen_order.popleft())
        return True

    def ingest(self, snapshot, *, chart_key=None, trades=None):
        """Ingest REST once per retrieval, or only the explicit WebSocket trade batch.

        Explicit ``trades=[]`` does not replay REST prints. Changing chart identity,
        instrument, or timeframe resets all tape, dedup and depth history state.
        """
        instrument = snapshot.instrument
        if (instrument.provider not in CAPABILITIES or
                instrument.market not in CAPABILITIES[instrument.provider]['markets']):
            raise ValueError('Order Flow crypto chỉ nhận dữ liệu public của chart crypto/FX được hỗ trợ.')
        key = (chart_key, instrument.key, snapshot.timeframe)
        reset = key != self.chart_key
        if reset:
            self.reset()
            self.chart_key, self.instrument = key, instrument
        rest_ms = _timestamp(snapshot.fetched_at_ms)
        rest_is_new = rest_ms is not None and (self.last_rest_ms is None or rest_ms > self.last_rest_ms)
        is_stream = trades is not None
        rows = list(trades) if is_stream else list(snapshot.trades or []) if rest_is_new else []
        dropped = max(0, len(rows) - self.MAX_BATCH)
        ignored = dropped
        if dropped:
            rows = rows[-self.MAX_BATCH:]
        valid_rows = []
        for row in rows:
            if not isinstance(row, dict) or not self._valid_trade(row, instrument):
                ignored += 1
                continue
            valid_rows.append(row)
        rows = valid_rows
        if any(isinstance(row, dict) and row.get('stream_kind') == 'aggregated' for row in rows):
            if self.domain != 'aggregated':
                self._new_tape_domain('aggregated')
                reset = True
        if is_stream:
            self.transport = 'WebSocket'
        parsed = []
        for row in rows:
            kind = row.get('stream_kind', 'native')
            if kind not in ('native', 'aggregated') or kind != self.domain:
                ignored += 1
                continue
            stamp = _timestamp(row.get('time_ms'))
            price, size = _number(row.get('price'), positive=True), _number(row.get('size'), positive=True)
            side = row.get('side', 'Unknown')
            if stamp is None or price is None or size is None or side not in ('Buy', 'Sell', 'Unknown'):
                ignored += 1
                continue
            identifier = row.get('trade_id')
            if isinstance(identifier, (int, str)) and not isinstance(identifier, bool) and str(identifier):
                trade_key = (kind, 'id', _text(identifier, 128))
            else:
                # Older Kraken rows can omit an ID. Conservatively coalesce exact duplicates;
                # two genuinely identical fills cannot be distinguished without venue IDs.
                self.idless_prints = True
                trade_key = (kind, 'fingerprint', stamp, price, size, side)
            if not self._remember(trade_key):
                ignored += 1
                continue
            parsed.append(Trade(stamp, price, size, side))
        parsed.sort(key=lambda trade: trade.time_ms)
        for trade in parsed:
            self.engine.trade(trade)
            self.last_trade_ms = max(self.last_trade_ms or 0, trade.time_ms)
        if rest_is_new:
            self.last_rest_ms = rest_ms
            self.warning = ' · '.join(_text(error, 160) for error in (snapshot.errors or [])[:2])
        if dropped:
            self.warning = 'Batch bị giới hạn; delta/sweep không đầy đủ.'
        self.ignored_trades += ignored
        self.quote.update({key: number for key in ('bid', 'ask')
                           if (number := _number((snapshot.quote or {}).get(key), positive=True)) is not None})
        book_updated = self._ingest_book(snapshot.book or {})
        return FlowUpdate(reset, len(parsed), book_updated, ignored)

    @staticmethod
    def _valid_trade(row, instrument):
        return (row.get('source') in (None, instrument.key) and
                row.get('venue') in (None, instrument.provider) and
                row.get('symbol') in (None, instrument.symbol) and
                row.get('volume_unit', row.get('unit')) in (None, instrument.volume_unit) and
                row.get('stream_kind', 'native') in ('native', 'aggregated') and
                _timestamp(row.get('time_ms')) is not None and
                _number(row.get('price'), positive=True) is not None and
                _number(row.get('size'), positive=True) is not None and
                row.get('side', 'Unknown') in ('Buy', 'Sell', 'Unknown'))

    def _ingest_book(self, book):
        if not isinstance(book, dict) or not book.get('available', True):
            return False
        stamp = _timestamp(book.get('timestamp_ms'))
        if stamp is None or not book.get('snapshot_only'):
            return False
        if book.get('source') not in (None, self.instrument.key):
            return False
        if book.get('unit') not in (None, self.instrument.volume_unit):
            return False
        version = (stamp, _text(book.get('sequence'), 128))
        if version == self._book_version:
            return False
        levels = []
        for side, name in (('Bid', 'bids'), ('Ask', 'asks')):
            rows = book.get(name, [])
            if not isinstance(rows, (list, tuple)):
                continue
            for row in rows[:self.MAX_BOOK_LEVELS]:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                price, size = _number(row[0], positive=True), _number(row[1])
                if price is not None and size is not None:
                    levels.append(Level(side, price, size))
        if not self.engine.depth(stamp, levels):
            return False
        self.last_book_ms, self._book_version = stamp, version
        snapshot = self.engine.snapshot()
        bids = [level for level in snapshot.book if level.side == 'Bid'][:30]
        asks = [level for level in snapshot.book if level.side == 'Ask'][:30]
        self.history.append((stamp, tuple(bids + asks)))
        if bids:
            self.quote['bid'] = bids[0].price
        if asks:
            self.quote['ask'] = asks[0].price
        return True

    def status_text(self, stream_status=None, now_ms=None):
        if self.instrument is None:
            return 'Chờ chart crypto đang xem tải dữ liệu public.'
        now_ms = int(self._clock() * 1000) if now_ms is None else int(now_ms)
        item = self.instrument
        tape = ('prints gộp xấp xỉ; đã bỏ fills REST native để tránh đếm đôi'
                if self.domain == 'aggregated' else 'prints native, khử trùng theo trade ID')
        if self.last_book_ms is None:
            book = 'DOM chưa có snapshot REST'
        elif self.last_book_ms > now_ms + 1000:
            book = 'DOM REST có timestamp tương lai; kiểm tra đồng hồ'
        else:
            book = f'DOM REST snapshot · tuổi {max(0, now_ms - self.last_book_ms) / 1000:.1f}s; không phải DOM streaming'
        text = (f'{item.provider} · {item.market} · {item.symbol} · đơn vị {item.volume_unit} · '
                f'{self.transport} {tape} · {book}. Δ/sweep chỉ từ prints đã nhận, không phải toàn thị trường.')
        if self.idless_prints:
            text += ' Có prints thiếu ID: gộp bản trùng thời gian/giá/size/phía; có thể thiếu fills giống nhau.'
        if stream_status and self.transport == 'WebSocket' and not stream_status.get('streaming'):
            text += ' Trade stream hiện chưa kết nối; không tự phát sinh prints.'
        if self.warning:
            text += ' ' + self.warning
        return text
