"""Bounded public-data scheduler. Widgets never run HTTP or MT5 RPCs on the UI thread."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import copy
import threading
import time

from PyQt6 import QtCore
from .analysis import original_studies
from .data import CAPABILITIES, FIAT, Instrument, MarketDataError, PublicMarketClient, TIMEFRAMES
from .quant import analyze_market


class HubSignals(QtCore.QObject):
    result = QtCore.pyqtSignal(str, str, object)


@dataclass
class LocalSnapshot:
    instrument: Instrument
    timeframe: str
    candles: list
    fetched_at_ms: int
    trades: list
    book: dict
    derivatives: dict
    errors: list
    quote: dict
    mode: str
    account_name: str

    def as_dict(self, include_candles=True):
        result = {'instrument': self.instrument.as_dict(), 'timeframe': self.timeframe,
                  'fetched_at_ms': self.fetched_at_ms, 'trades': self.trades, 'book': self.book,
                  'derivatives': self.derivatives, 'errors': self.errors, 'quote': self.quote,
                  'mode': self.mode, 'account_name': self.account_name,
                  'provenance': {'source': 'mt5_terminal', 'venue': 'MT5', 'market': 'broker',
                                 'synthetic': self.mode == 'demo', 'volume_unit': 'broker_native',
                                 'atomic_snapshot': False, 'cme_futures': False}}
        if include_candles:
            result['candles'] = self.candles
        return copy.deepcopy(result)


class MarketHub:
    def __init__(self, legacy, broker, client_factory=None):
        self.legacy, self.broker = legacy, broker
        self.client_factory = client_factory or PublicMarketClient
        self.signals = HubSignals()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='AIMT-data')
        self._lock = threading.RLock()
        self._catalogs, self._related, self._cooldowns = {}, {}, {}
        self._closed = False

    def _client(self):
        return self.client_factory()

    def _close_client(self, client, provider):
        remaining = getattr(client, 'cooldown_remaining', lambda _: 0)(provider)
        if remaining > 0:
            with self._lock:
                self._cooldowns[provider] = max(self._cooldowns.get(provider, 0),
                                                time.monotonic() + remaining)
        client.close()

    def _check_budget(self, provider):
        with self._lock:
            remaining = self._cooldowns.get(provider, 0) - time.monotonic()
        if remaining > 0:
            raise MarketDataError(f'{provider}: đang chờ giới hạn API, còn {int(remaining) + 1} giây.',
                                  provider=provider, retry_after=remaining)

    def _failure_budget(self, provider, error):
        code = getattr(error, 'status_code', None)
        retry = getattr(error, 'retry_after', None)
        if code in (403, 418):
            wait = max(float(retry or 0), 600)
        elif code == 429 or retry:
            wait = max(float(retry or 0), 60)
        else:
            wait = 15
        with self._lock:
            self._cooldowns[provider] = max(self._cooldowns.get(provider, 0), time.monotonic() + wait)
        return wait

    def catalog(self, provider, market, account):
        key = (provider, market, account.get('name', '') if provider == 'MT5' else '')
        with self._lock:
            cached = self._catalogs.get(key)
            if cached and time.monotonic() - cached[0] < 900:
                return list(cached[1])
        if provider == 'MT5':
            if self.broker.demo:
                items = [Instrument('MT5', 'broker', symbol, 'UNKNOWN', 'UNKNOWN', 'broker', .01)
                         for symbol in self.broker.symbols()]
            else:
                if not self.broker.acquire(account, .1):
                    raise RuntimeError('MT5 đang bận hoặc chưa kết nối. Thử lại sau khi terminal sẵn sàng.')
                try:
                    raw = self.broker.market_catalog(timeout=10)
                finally:
                    self.broker.release(account)
                items = []
                for value in raw:
                    base, quote = value.get('base') or 'UNKNOWN', value.get('quote') or 'UNKNOWN'
                    kind = 'forex' if base in FIAT and quote in FIAT else 'broker'
                    try:
                        items.append(Instrument('MT5', 'broker', value['symbol'], base, quote, kind, value['tick_size']))
                    except ValueError:
                        continue
        else:
            self._check_budget(provider)
            client = self._client()
            try:
                items = client.list_instruments(provider, market)
            finally:
                self._close_client(client, provider)
        with self._lock:
            self._catalogs[key] = (time.monotonic(), tuple(items))
        return list(items)

    def _local_snapshot(self, instrument, timeframe, account):
        if not self.broker.acquire(account, .1):
            raise RuntimeError('MT5 đang bận hoặc chưa kết nối; chart giữ dữ liệu cuối và báo thời gian cũ.')
        try:
            symbol = self.broker.resolve(instrument.symbol)
            rates = self.broker.rates(symbol, timeframe, 600)
            bid, ask = self.broker.tick(symbol)
            meta = self.broker.meta(symbol)
        finally:
            self.broker.release(account)
        if rates is None or len(rates) < 5:
            raise RuntimeError('MT5 chưa trả đủ nến cho symbol/khung được chọn.')
        now = int(time.time() * 1000)
        names = rates.dtype.names or ()
        candles = []
        for row in rates:
            real = float(row['real_volume']) if 'real_volume' in names else 0
            candles.append({'time': int(row['time']), 'open': float(row['open']), 'high': float(row['high']),
                'low': float(row['low']), 'close': float(row['close']),
                'volume': real if real > 0 else float(row['tick_volume']), 'volume_is_real': real > 0,
                'volume_unit': 'broker_real_volume' if real > 0 else 'quote_tick_count',
                'closed': (int(row['time']) + TIMEFRAMES[timeframe]) * 1000 <= now})
        actual = Instrument('MT5', 'broker', symbol, instrument.base, instrument.quote,
                            instrument.kind, float(meta.get('tick_size') or meta.get('point') or .01))
        return LocalSnapshot(actual, timeframe, candles, now, [], {}, {},
            ['Chart MT5 dùng OHLC; trades/DOM có trong dock Order Flow khi broker cấp.'],
            {'bid': bid, 'ask': ask, 'timestamp_ms': now}, 'demo' if self.broker.demo else 'live', account.get('name', ''))

    def _fetch_related(self, instrument, timeframe, account):
        if instrument.provider == 'MT5' or instrument.market != 'spot':
            return {'requested': False, 'available': False, 'reason': 'Chart đã là derivatives hoặc là nguồn broker.'}
        if 'perpetual' not in CAPABILITIES[instrument.provider]['markets']:
            return {'requested': True, 'available': False, 'reason': 'Nguồn này chưa hỗ trợ perpetual trong ứng dụng.',
                    'cme_futures': False}
        key = (instrument.key, timeframe)
        with self._lock:
            cached = self._related.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                return copy.deepcopy(cached[1])
        try:
            options = self.catalog(instrument.provider, 'perpetual', account)
            related = next((item for item in options if item.base == instrument.base and item.quote == instrument.quote), None)
            if related is None:
                result = {'requested': True, 'available': False, 'reason': 'Không tìm thấy perpetual cùng base/quote trên sàn này.',
                          'cme_futures': False}
            else:
                self._check_budget(instrument.provider)
                client = self._client()
                try:
                    snapshot = client.fetch_snapshot(related, timeframe, limit=300, include_orderflow=True)
                finally:
                    self._close_client(client, instrument.provider)
                result = {'requested': True, 'available': True, 'snapshot': snapshot.as_dict(),
                          'cme_futures': False, 'relationship': 'same_venue_base_quote_crypto_perpetual'}
        except Exception as error:
            if isinstance(error, MarketDataError):
                self._failure_budget(instrument.provider, error)
            result = {'requested': True, 'available': False, 'reason': str(error), 'cme_futures': False}
        with self._lock:
            self._related[key] = (time.monotonic(), copy.deepcopy(result))
        return result

    def fetch(self, instrument, timeframe, account, include_futures=True):
        if instrument.provider == 'MT5':
            snapshot = self._local_snapshot(instrument, timeframe, account)
        else:
            self._check_budget(instrument.provider)
            client = self._client()
            try:
                snapshot = client.fetch_snapshot(instrument, timeframe, limit=600, include_orderflow=True)
            finally:
                self._close_client(client, instrument.provider)
        quant = analyze_market(snapshot.candles, snapshot.trades, snapshot.book, snapshot.derivatives,
                               now_ms=snapshot.fetched_at_ms)
        studies = original_studies(self.legacy, snapshot.candles)
        related = self._fetch_related(instrument, timeframe, account) if include_futures else {
            'requested': False, 'available': False, 'reason': 'Người dùng tắt bổ sung futures.'}
        return {'snapshot': snapshot, 'quant': quant, 'original': studies, 'related_futures': related}

    def submit(self, kind, token, function, provider=''):
        if self._closed:
            return None
        future = self._pool.submit(function)

        def completed(result):
            try:
                value = {'ok': True, 'data': result.result()}
            except Exception as error:
                wait = self._failure_budget(provider, error) if provider else 15
                value = {'ok': False, 'error': str(error), 'retry_after': wait}
            if not self._closed:
                try:
                    self.signals.result.emit(kind, token, value)
                except RuntimeError:
                    pass

        future.add_done_callback(completed)
        return future

    def shutdown(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
