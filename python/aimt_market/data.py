"""Public REST snapshots; no keys, trading endpoints, synthetic fallback or book deltas.

Official contracts:
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data
https://bybit-exchange.github.io/docs/v5/market/instrument
https://www.okx.com/docs-v5/en/#public-data-rest-api
https://docs.kraken.com/api/docs/rest-api/get-tradable-asset-pairs/

Book/trade/candle volumes are venue-native and explicitly labeled. In particular OKX swap
sizes are contracts, not fabricated base-asset quantities. Snapshots are not atomic across
HTTP endpoints; every section records its observed/exchange timestamp and provenance.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import time

import requests


CAPABILITIES = {
    'Binance': {'markets': ('spot', 'perpetual'), 'trades': True, 'depth': True, 'derivatives': True},
    'Bybit': {'markets': ('spot', 'perpetual'), 'trades': True, 'depth': True, 'derivatives': True},
    'OKX': {'markets': ('spot', 'perpetual'), 'trades': True, 'depth': True, 'derivatives': True},
    'Kraken': {'markets': ('spot',), 'trades': True, 'depth': True, 'derivatives': False},
}
TIMEFRAMES = {'M1': 60, 'M5': 300, 'M15': 900, 'M30': 1800, 'H1': 3600, 'H4': 14400, 'D1': 86400}
BASE_URLS = {
    ('Binance', 'spot'): 'https://api.binance.com',
    ('Binance', 'perpetual'): 'https://fapi.binance.com',
    ('Bybit', 'spot'): 'https://api.bybit.com',
    ('Bybit', 'perpetual'): 'https://api.bybit.com',
    ('OKX', 'spot'): 'https://www.okx.com',
    ('OKX', 'perpetual'): 'https://www.okx.com',
    ('Kraken', 'spot'): 'https://api.kraken.com',
}
FIAT = frozenset({'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'CNH', 'CNY', 'AED'})
_ID = re.compile(r'^[A-Za-z0-9._/:-]{1,128}$')
_CONTRACT_METADATA_FIELDS = ('ctVal', 'ctValCcy', 'ctMult', 'ctType', 'settleCcy', 'uly')


def _text(value, limit=240):
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def _number(value, *, nonnegative=False, positive=False):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (nonnegative and result < 0) or (positive and result <= 0):
        return None
    return result


def _ms(value, default=0):
    number = _number(value, nonnegative=True)
    return int(number) if number is not None else int(default)


def _json_safe(value):
    if isinstance(value, dict):
        return {_text(k, 128): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v) for v in value]
    if isinstance(value, str):
        return _text(value, 2000)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _text(value)


@dataclass(frozen=True, slots=True)
class Instrument:
    provider: str
    market: str
    symbol: str
    base: str
    quote: str
    kind: str = 'crypto'
    tick_size: float = .01
    contract_metadata: dict = field(default_factory=dict, compare=False, hash=False, repr=False)

    def __post_init__(self):
        for name in ('provider', 'market', 'symbol', 'base', 'quote', 'kind'):
            value = getattr(self, name)
            if name == 'symbol' and self.provider == 'MT5':
                if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(c.isprintable() for c in value):
                    raise ValueError('Invalid broker instrument symbol.')
                continue
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError(f'Invalid instrument {name}.')
        if _number(self.tick_size, positive=True) is None:
            raise ValueError('Tick size must be finite and positive.')
        if self.contract_metadata is not None and not isinstance(self.contract_metadata, dict):
            raise ValueError('Contract metadata must be a dictionary.')
        # Carry only public contract descriptors across per-task HTTP clients. Copy the
        # caller's dictionary; do not retain arbitrary exchange fields or credentials.
        metadata = {}
        for key, value in (self.contract_metadata or {}).items():
            if key in _CONTRACT_METADATA_FIELDS:
                scalar = value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool)
                metadata[key] = _json_safe(value) if scalar else None
        object.__setattr__(self, 'contract_metadata', metadata)

    @property
    def key(self):
        return f'{self.provider}:{self.market}:{self.symbol}'

    @property
    def volume_unit(self):
        return 'contracts' if self.provider == 'OKX' and self.market == 'perpetual' else self.base

    def as_dict(self):
        return dict(provider=self.provider, market=self.market, symbol=self.symbol,
                    base=self.base, quote=self.quote, kind=self.kind, tick_size=self.tick_size,
                    key=self.key, volume_unit=self.volume_unit,
                    contract_metadata=_json_safe(self.contract_metadata))


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    instrument: Instrument
    timeframe: str
    candles: list
    fetched_at_ms: int
    trades: list
    book: dict
    derivatives: dict
    errors: list
    quote: dict

    @property
    def provenance(self):
        return {'source': 'public_exchange_rest', 'venue': self.instrument.provider,
                'market': self.instrument.market, 'symbol': self.instrument.symbol,
                'fetched_at_ms': self.fetched_at_ms, 'volume_unit': self.instrument.volume_unit,
                'execution_account': None, 'atomic_snapshot': False,
                'cme_futures': False, 'synthetic': False, 'mode': 'live',
                'snapshot_mode': 'public_rest', 'streaming': False}

    def as_dict(self, include_candles=True):
        result = {'instrument': self.instrument.as_dict(), 'timeframe': self.timeframe,
                  'fetched_at_ms': self.fetched_at_ms, 'retrieved_at_ms': self.fetched_at_ms,
                  'snapshot_mode': 'public_rest', 'mode': 'live', 'provenance': self.provenance,
                  'trades': self.trades, 'book': self.book, 'derivatives': self.derivatives,
                  'errors': self.errors, 'quote': self.quote}
        if include_candles:
            result['candles'] = self.candles
        return _json_safe(result)


class MarketDataError(RuntimeError):
    def __init__(self, message, *, provider=None, status_code=None, retry_after=None):
        super().__init__(_text(message, 500))
        self.provider, self.status_code, self.retry_after = provider, status_code, retry_after


class PublicMarketClient:
    capabilities = CAPABILITIES

    def __init__(self, session=None, clock=None):
        self.session = session or requests.Session()
        self.clock = clock or time.time
        self.catalog_warnings = []
        self._metadata = {}
        self._cooldown_until = {}
        self._invalid_candles = 0

    def close(self):
        self.session.close()

    def _now(self):
        return int(self.clock() * 1000)

    def cooldown_remaining(self, provider):
        return max(0.0, self._cooldown_until.get(provider, 0) - self.clock())

    @staticmethod
    def _validate(provider, market):
        if (provider, market) not in BASE_URLS:
            raise ValueError(f'Unsupported public market: {provider}/{market}.')

    def _get(self, provider, market, path, params):
        self._validate(provider, market)
        remaining = self._cooldown_until.get(provider, 0) - self.clock()
        if remaining > 0:
            seconds = max(1, math.ceil(remaining))
            raise MarketDataError(f'{provider}: rate-limit cooldown; retry after {seconds}s.',
                                  provider=provider, status_code=429, retry_after=seconds)
        url = BASE_URLS[(provider, market)] + path
        try:
            response = self.session.get(url, params=params, timeout=(5, 12), verify=True)
        except requests.RequestException as error:
            raise MarketDataError(f'{provider}: public request failed ({type(error).__name__}).', provider=provider) from error
        status = response.status_code
        if status >= 400:
            retry_after = None
            value = response.headers.get('Retry-After', '')
            if value:
                try:
                    retry_after = max(0, int(float(value)))
                except ValueError:
                    try:
                        retry_after = max(0, int(parsedate_to_datetime(value).timestamp() - self.clock()))
                    except (ValueError, TypeError, OverflowError):
                        pass
            suffix = f'; Retry-After {retry_after}s' if retry_after is not None else ''
            if status in (429, 418):
                self._cooldown_until[provider] = self.clock() + max(1, retry_after or 60)
            elif status == 403:
                self._cooldown_until[provider] = self.clock() + max(600, retry_after or 0)
            raise MarketDataError(f'{provider}: HTTP {status}{suffix}; no simulated fallback.',
                                  provider=provider, status_code=status, retry_after=retry_after)
        try:
            data = response.json()
        except (ValueError, TypeError) as error:
            raise MarketDataError(f'{provider}: invalid JSON response.', provider=provider) from error
        if provider == 'Binance' and isinstance(data, dict) and isinstance(data.get('code'), int) and data['code'] < 0:
            limited = data['code'] == -1003
            if limited:
                self._cooldown_until[provider] = self.clock() + 60
            raise MarketDataError(f"Binance: {_text(data.get('msg', 'API error'))}", provider=provider,
                                  status_code=429 if limited else None, retry_after=60 if limited else None)
        if provider == 'Bybit' and (not isinstance(data, dict) or str(data.get('retCode')) != '0'):
            limited = isinstance(data, dict) and str(data.get('retCode')) == '10006'
            if limited:
                self._cooldown_until[provider] = self.clock() + 60
            message = data.get('retMsg', 'invalid envelope') if isinstance(data, dict) else 'invalid envelope'
            raise MarketDataError(f'Bybit: {_text(message)}', provider=provider,
                                  status_code=429 if limited else None, retry_after=60 if limited else None)
        if provider == 'OKX' and (not isinstance(data, dict) or str(data.get('code')) != '0'):
            limited = isinstance(data, dict) and str(data.get('code')) in ('50011', '50040')
            if limited:
                self._cooldown_until[provider] = self.clock() + 30
            message = data.get('msg', 'invalid envelope') if isinstance(data, dict) else 'invalid envelope'
            raise MarketDataError(f'OKX: {_text(message)}', provider=provider,
                                  status_code=429 if limited else None, retry_after=30 if limited else None)
        if provider == 'Kraken' and (not isinstance(data, dict) or data.get('error')):
            message = data.get('error', 'invalid envelope') if isinstance(data, dict) else 'invalid envelope'
            limited = 'rate limit' in str(message).lower() or 'throttled' in str(message).lower()
            if limited:
                self._cooldown_until[provider] = self.clock() + 30
            raise MarketDataError(f'Kraken: {_text(message)}', provider=provider,
                                  status_code=429 if limited else None, retry_after=30 if limited else None)
        return data

    def _optional(self, callback, errors, name):
        try:
            return callback()
        except (MarketDataError, ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
            errors.append(f'{name}: {_text(error, 350)}')
            return None

    def list_instruments(self, provider, market):
        self._validate(provider, market)
        self.catalog_warnings = []
        rows = []
        if provider == 'Binance':
            prefix = '/api/v3' if market == 'spot' else '/fapi/v1'
            payload = self._get(provider, market, prefix + '/exchangeInfo', {})
            for row in payload.get('symbols', []):
                if row.get('status') != 'TRADING' or (market == 'spot' and row.get('isSpotTradingAllowed') is False):
                    continue
                if market == 'perpetual' and row.get('contractType') != 'PERPETUAL':
                    continue
                tick = next((f.get('tickSize') for f in row.get('filters', []) if f.get('filterType') == 'PRICE_FILTER'), None)
                rows.append((row.get('symbol'), row.get('baseAsset'), row.get('quoteAsset'), tick, row))
        elif provider == 'Bybit':
            category = 'spot' if market == 'spot' else 'linear'
            cursor, visited = '', set()
            for page in range(30):
                params = {'category': category}
                if market == 'perpetual':
                    params['limit'] = 1000
                    if cursor:
                        params['cursor'] = cursor
                payload = self._get(provider, market, '/v5/market/instruments-info', params)['result']
                for row in payload.get('list', []):
                    if row.get('status') != 'Trading':
                        continue
                    if market == 'perpetual' and row.get('contractType') != 'LinearPerpetual':
                        continue
                    rows.append((row.get('symbol'), row.get('baseCoin'), row.get('quoteCoin'),
                                 row.get('priceFilter', {}).get('tickSize'), row))
                cursor = str(payload.get('nextPageCursor') or '') if market == 'perpetual' else ''
                if not cursor:
                    break
                if cursor in visited:
                    raise MarketDataError('Bybit: instrument pagination cursor repeated; catalogue incomplete.', provider=provider)
                visited.add(cursor)
            else:
                raise MarketDataError('Bybit: instrument pagination limit reached; catalogue incomplete.', provider=provider)
        elif provider == 'OKX':
            payload = self._get(provider, market, '/api/v5/public/instruments', {'instType': 'SPOT' if market == 'spot' else 'SWAP'})
            for row in payload.get('data', []):
                if row.get('state') != 'live':
                    continue
                pair = str(row.get('uly') or row.get('instId') or '').split('-')
                base = row.get('baseCcy') or (pair[0] if pair else '')
                quote = row.get('quoteCcy') or (pair[1] if len(pair) > 1 else '')
                rows.append((row.get('instId'), base, quote, row.get('tickSz'), row))
        else:
            payload = self._get(provider, market, '/0/public/AssetPairs', {})
            for symbol, row in payload.get('result', {}).items():
                if row.get('status', 'online') != 'online' or '.d' in symbol:
                    continue
                pair = str(row.get('wsname') or '').split('/')
                if len(pair) != 2:
                    continue
                decimals = row.get('pair_decimals')
                tick = row.get('tick_size')
                if not tick and isinstance(decimals, int) and 0 <= decimals <= 18:
                    tick = 10 ** -decimals
                rows.append((symbol, pair[0], pair[1], tick, row))
        instruments = {}
        for symbol, base, quote, tick, metadata in rows:
            try:
                base, quote = str(base or ''), str(quote or '')
                kind = 'forex' if base in FIAT and quote in FIAT else 'crypto'
                number = _number(tick, positive=True)
                if number is None:
                    raise ValueError('missing/invalid tick size')
                contract = ({key: metadata.get(key) for key in _CONTRACT_METADATA_FIELDS if key in metadata}
                            if provider == 'OKX' and market == 'perpetual' else {})
                item = Instrument(provider, market, str(symbol or ''), base, quote, kind, number,
                                  contract_metadata=contract)
                instruments[item.key] = item
                self._metadata[item.key] = metadata
            except ValueError as error:
                self.catalog_warnings.append(f'{provider}: skipped malformed instrument {_text(symbol, 80)} ({error}).')
        return sorted(instruments.values(), key=lambda item: (item.kind, item.symbol))

    def fetch_snapshot(self, instrument, timeframe='M1', limit=300, include_orderflow=True):
        if not isinstance(instrument, Instrument):
            raise TypeError('An Instrument is required.')
        self._validate(instrument.provider, instrument.market)
        if timeframe not in TIMEFRAMES:
            raise ValueError('Unsupported timeframe.')
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError('Candle limit must be an integer from 1 to 1000.')
        errors = []
        self._invalid_candles = 0
        try:
            candles = self._candles(instrument, timeframe, limit)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
            raise MarketDataError(f'{instrument.provider}: malformed candle payload; no simulated fallback.',
                                  provider=instrument.provider) from error
        if not candles:
            raise MarketDataError(f'{instrument.provider}: no valid candles; no simulated fallback.', provider=instrument.provider)
        if self._invalid_candles:
            errors.append(f'{instrument.provider}: omitted {self._invalid_candles} malformed candle rows; history may have gaps.')
        ticker = self._optional(lambda: self._ticker(instrument), errors, 'quote') or {}
        trades, book = [], {'bids': [], 'asks': [], 'timestamp_ms': None, 'snapshot_only': True,
                           'unit': instrument.volume_unit, 'source': instrument.key, 'available': False}
        if include_orderflow:
            trades = self._optional(lambda: self._trades(instrument), errors, 'trades') or []
            book = self._optional(lambda: self._book(instrument), errors, 'depth') or book
        derivatives = self._derivatives(instrument, ticker, errors)
        quote = {key: value for key, value in ticker.items() if not key.startswith('_')}
        quote.setdefault('source', instrument.key)
        quote.setdefault('timestamp_ms', self._now())
        return MarketSnapshot(instrument, timeframe, candles, self._now(), trades, book, derivatives, errors, quote)

    def _candle(self, raw, time_s, closed, instrument):
        if len(raw) < 5:
            self._invalid_candles += 1
            return None
        values = [_number(value, positive=True) for value in raw[:4]]
        volume = _number(raw[4], nonnegative=True)
        if any(value is None for value in values) or volume is None or time_s < 0:
            self._invalid_candles += 1
            return None
        op, high, low, close = values
        if high < max(op, close, low) or low > min(op, close, high):
            self._invalid_candles += 1
            return None
        return {'time': int(time_s), 'open': op, 'high': high, 'low': low, 'close': close,
                'volume': volume, 'closed': bool(closed), 'volume_is_real': True,
                'volume_unit': instrument.volume_unit, 'source': instrument.key}

    def _candles(self, item, timeframe, limit):
        p, m, symbol, now = item.provider, item.market, item.symbol, self._now()
        seconds = TIMEFRAMES[timeframe]
        results = {}
        if p == 'Binance':
            prefix = '/api/v3' if m == 'spot' else '/fapi/v1'
            interval = {'M1': '1m', 'M5': '5m', 'M15': '15m', 'M30': '30m', 'H1': '1h', 'H4': '4h', 'D1': '1d'}[timeframe]
            rows = self._get(p, m, prefix + '/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
            if not isinstance(rows, list):
                raise MarketDataError('Binance: invalid candle payload.', provider=p)
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    self._invalid_candles += 1
                    continue
                timestamp = _number(row[0], positive=True)
                end = _number(row[6], positive=True)
                if timestamp is None or end is None or end < timestamp:
                    self._invalid_candles += 1
                    continue
                candle = self._candle(row[1:6], int(timestamp) // 1000, end < now, item)
                if candle:
                    results[candle['time']] = candle
        elif p == 'Bybit':
            interval = str(seconds // 60) if timeframe != 'D1' else 'D'
            payload = self._get(p, m, '/v5/market/kline', {'category': 'spot' if m == 'spot' else 'linear',
                'symbol': symbol, 'interval': interval, 'limit': limit})
            for row in payload.get('result', {}).get('list', []):
                if len(row) < 6:
                    self._invalid_candles += 1
                    continue
                timestamp = _ms(row[0])
                if timestamp <= 0:
                    self._invalid_candles += 1
                    continue
                candle = self._candle(row[1:6], timestamp // 1000, timestamp + seconds * 1000 <= now, item)
                if candle:
                    results[candle['time']] = candle
        elif p == 'OKX':
            bar = {'M1': '1m', 'M5': '5m', 'M15': '15m', 'M30': '30m', 'H1': '1H', 'H4': '4H', 'D1': '1Dutc'}[timeframe]
            after = None
            for _ in range(4):
                params = {'instId': symbol, 'bar': bar, 'limit': min(limit - len(results), 300)}
                if after is not None:
                    params['after'] = after
                payload = self._get(p, m, '/api/v5/market/candles', params)
                rows = payload.get('data', [])
                if not rows:
                    break
                timestamps = []
                for row in rows:
                    if len(row) < 9:
                        self._invalid_candles += 1
                        continue
                    timestamp = _ms(row[0])
                    if timestamp <= 0:
                        self._invalid_candles += 1
                        continue
                    timestamps.append(timestamp)
                    candle = self._candle(row[1:6], timestamp // 1000, str(row[8]) == '1' and timestamp + seconds * 1000 <= now, item)
                    if candle:
                        results[candle['time']] = candle
                if len(results) >= limit or not timestamps:
                    break
                oldest = min(timestamps)
                if after is not None and oldest >= after:
                    break
                after = oldest
        else:
            payload = self._get(p, m, '/0/public/OHLC', {'pair': symbol, 'interval': seconds // 60})
            rows = self._kraken_result(payload)
            for index, row in enumerate(rows):
                if len(row) < 8:
                    self._invalid_candles += 1
                    continue
                timestamp = int(_number(row[0], nonnegative=True) or 0)
                if timestamp <= 0:
                    self._invalid_candles += 1
                    continue
                candle = self._candle([*row[1:5], row[6]], timestamp,
                    index < len(rows) - 1 and (timestamp + seconds) * 1000 <= now, item)
                if candle:
                    results[candle['time']] = candle
        return [results[key] for key in sorted(results)[-limit:]]

    @staticmethod
    def _kraken_result(payload):
        values = [value for key, value in payload.get('result', {}).items() if key != 'last']
        if len(values) != 1:
            raise MarketDataError('Kraken: ambiguous or missing pair response.', provider='Kraken')
        return values[0]

    def _ticker(self, item):
        p, m, symbol = item.provider, item.market, item.symbol
        if p == 'Binance':
            row = self._get(p, m, ('/api/v3' if m == 'spot' else '/fapi/v1') + '/ticker/24hr', {'symbol': symbol})
            mapping = {'last': 'lastPrice', 'bid': 'bidPrice', 'ask': 'askPrice', 'volume_24h': 'volume',
                       'quote_volume_24h': 'quoteVolume', 'change_pct_24h': 'priceChangePercent'}
            stamp = _ms(row.get('closeTime'), self._now())
        elif p == 'Bybit':
            payload = self._get(p, m, '/v5/market/tickers', {'category': 'spot' if m == 'spot' else 'linear', 'symbol': symbol})
            rows = payload.get('result', {}).get('list', [])
            if not rows:
                raise MarketDataError('Bybit: no ticker for instrument.', provider=p)
            row = rows[0]
            mapping = {'last': 'lastPrice', 'bid': 'bid1Price', 'ask': 'ask1Price', 'volume_24h': 'volume24h',
                       'quote_volume_24h': 'turnover24h', 'change_fraction_24h': 'price24hPcnt'}
            stamp = _ms(payload.get('time'), self._now())
        elif p == 'OKX':
            payload = self._get(p, m, '/api/v5/market/ticker', {'instId': symbol})
            rows = payload.get('data', [])
            if not rows:
                raise MarketDataError('OKX: no ticker for instrument.', provider=p)
            row = rows[0]
            mapping = {'last': 'last', 'bid': 'bidPx', 'ask': 'askPx', 'volume_24h': 'vol24h'}
            stamp = _ms(row.get('ts'), self._now())
        else:
            row = self._kraken_result(self._get(p, m, '/0/public/Ticker', {'pair': symbol}))
            return {'last': _number((row.get('c') or [None])[0]),
                    'bid': _number((row.get('b') or [None])[0]), 'ask': _number((row.get('a') or [None])[0]),
                    'source': item.key, 'timestamp_ms': self._now(), '_raw': row}
        result = {key: _number(row.get(field)) for key, field in mapping.items()}
        result.update(source=item.key, timestamp_ms=stamp, volume_unit=item.volume_unit, _raw=row)
        return result

    def _trades(self, item):
        p, m, symbol, rows = item.provider, item.market, item.symbol, []
        if p == 'Binance':
            rows = self._get(p, m, ('/api/v3' if m == 'spot' else '/fapi/v1') + '/trades', {'symbol': symbol, 'limit': 100})
        elif p == 'Bybit':
            rows = self._get(p, m, '/v5/market/recent-trade', {'category': 'spot' if m == 'spot' else 'linear',
                'symbol': symbol, 'limit': 60}).get('result', {}).get('list', [])
        elif p == 'OKX':
            rows = self._get(p, m, '/api/v5/market/trades', {'instId': symbol, 'limit': 100}).get('data', [])
        else:
            rows = self._kraken_result(self._get(p, m, '/0/public/Trades', {'pair': symbol, 'count': 100}))
        results = []
        for row in rows:
            if p == 'Binance':
                timestamp, price, size = _ms(row.get('time')), _number(row.get('price'), positive=True), _number(row.get('qty'), positive=True)
                maker = row.get('isBuyerMaker')
                side = 'Sell' if maker is True else 'Buy' if maker is False else 'Unknown'
                trade_id = row.get('id')
            elif p == 'Bybit':
                timestamp, price, size = _ms(row.get('time')), _number(row.get('price'), positive=True), _number(row.get('size'), positive=True)
                side, trade_id = row.get('side'), row.get('execId')
            elif p == 'OKX':
                timestamp, price, size = _ms(row.get('ts')), _number(row.get('px'), positive=True), _number(row.get('sz'), positive=True)
                side, trade_id = {'buy': 'Buy', 'sell': 'Sell'}.get(row.get('side')), row.get('tradeId')
            else:
                if len(row) < 4:
                    continue
                seconds = _number(row[2], nonnegative=True)
                timestamp = int(seconds * 1000) if seconds is not None else 0
                price, size = _number(row[0], positive=True), _number(row[1], positive=True)
                side, trade_id = {'b': 'Buy', 's': 'Sell'}.get(row[3]), row[6] if len(row) > 6 else None
            if timestamp <= 0 or price is None or size is None:
                continue
            results.append({'time_ms': timestamp, 'price': price, 'size': size,
                            'side': side if side in ('Buy', 'Sell') else 'Unknown',
                            'venue': p, 'symbol': symbol, 'unit': item.volume_unit,
                            'trade_id': _text(trade_id, 128) if trade_id is not None else None})
        return sorted(results, key=lambda row: row['time_ms'])

    def _book(self, item):
        p, m, symbol = item.provider, item.market, item.symbol
        if p == 'Binance':
            row = self._get(p, m, ('/api/v3' if m == 'spot' else '/fapi/v1') + '/depth', {'symbol': symbol, 'limit': 50})
            stamp, sequence = _ms(row.get('T') or row.get('E'), self._now()), row.get('lastUpdateId')
        elif p == 'Bybit':
            payload = self._get(p, m, '/v5/market/orderbook', {'category': 'spot' if m == 'spot' else 'linear',
                                                           'symbol': symbol, 'limit': 50})
            data = payload.get('result', {})
            row, stamp, sequence = {'bids': data.get('b', []), 'asks': data.get('a', [])}, _ms(data.get('ts'), self._now()), data.get('u')
        elif p == 'OKX':
            data = self._get(p, m, '/api/v5/market/books', {'instId': symbol, 'sz': 50}).get('data', [])
            if not data:
                raise MarketDataError('OKX: empty order-book envelope.', provider=p)
            row, stamp, sequence = data[0], _ms(data[0].get('ts'), self._now()), data[0].get('seqId')
        else:
            row = self._kraken_result(self._get(p, m, '/0/public/Depth', {'pair': symbol, 'count': 50}))
            stamp, sequence = self._now(), None
        result = {'timestamp_ms': stamp, 'snapshot_only': True, 'unit': item.volume_unit,
                  'source': item.key, 'sequence': sequence, 'available': True}
        for side in ('bids', 'asks'):
            levels = {}
            for level in row.get(side, []):
                if len(level) < 2:
                    continue
                price, size = _number(level[0], positive=True), _number(level[1], positive=True)
                if price is not None and size is not None:
                    levels[price] = size
            result[side] = [[price, levels[price]] for price in sorted(levels, reverse=side == 'bids')[:50]]
        return result

    def _derivatives(self, item, ticker, errors):
        p, m, symbol = item.provider, item.market, item.symbol
        if m != 'perpetual':
            return {'available': False, 'source': item.key, 'data_scope': 'spot',
                    'reason': 'Spot instrument; no derivatives metrics requested.'}
        result = {'available': False, 'venue': p, 'market': m, 'symbol': symbol,
                  'source': item.key, 'source_urls': [], 'data_scope': 'public_perpetual',
                  'timestamp_ms': self._now(), 'open_interest_unit': item.volume_unit,
                  'funding_rate': None, 'open_interest': None, 'mark_price': None, 'index_price': None}

        def request(path, params):
            result['source_urls'].append(BASE_URLS[(p, m)] + path)
            return self._optional(lambda: self._get(p, m, path, params), errors, f'{p} derivatives {path}')

        if p == 'Binance':
            premium = request('/fapi/v1/premiumIndex', {'symbol': symbol}) or {}
            interest = request('/fapi/v1/openInterest', {'symbol': symbol}) or {}
            for target, key in [('funding_rate', 'lastFundingRate'), ('mark_price', 'markPrice'), ('index_price', 'indexPrice')]:
                result[target] = _number(premium.get(key), positive=target != 'funding_rate')
            result.update(open_interest=_number(interest.get('openInterest'), nonnegative=True),
                          next_funding_time_ms=_ms(premium.get('nextFundingTime')) or None,
                          funding_timestamp_ms=_ms(premium.get('time')) or None,
                          open_interest_timestamp_ms=_ms(interest.get('time')) or None,
                          funding_rate_scope='latest_published_rate', funding_timestamp_kind='observed')
        elif p == 'Bybit':
            row = ticker.get('_raw', {})
            for target, key in [('funding_rate', 'fundingRate'), ('open_interest', 'openInterest'),
                                ('mark_price', 'markPrice'), ('index_price', 'indexPrice')]:
                result[target] = _number(row.get(key), nonnegative=target == 'open_interest',
                                         positive=target in ('mark_price', 'index_price'))
            result['source_urls'].append(BASE_URLS[(p, m)] + '/v5/market/tickers')
            result.update(next_funding_time_ms=_ms(row.get('nextFundingTime')) or None,
                          funding_timestamp_ms=ticker.get('timestamp_ms'),
                          open_interest_timestamp_ms=ticker.get('timestamp_ms'),
                          funding_rate_scope='indicative_current_rate', funding_timestamp_kind='observed')
            if result['open_interest'] is None:
                payload = request('/v5/market/open-interest', {'category': 'linear', 'symbol': symbol, 'intervalTime': '5min', 'limit': 1}) or {}
                entries = payload.get('result', {}).get('list', [])
                if entries:
                    result['open_interest'] = _number(entries[0].get('openInterest'), nonnegative=True)
                    result['open_interest_timestamp_ms'] = _ms(entries[0].get('timestamp')) or None
                    result['open_interest_sampling'] = '5min'
            if result['funding_rate'] is None:
                payload = request('/v5/market/funding/history', {'category': 'linear', 'symbol': symbol, 'limit': 1}) or {}
                entries = payload.get('result', {}).get('list', [])
                if entries:
                    result['funding_rate'] = _number(entries[0].get('fundingRate'))
                    result['funding_timestamp_ms'] = _ms(entries[0].get('fundingRateTimestamp')) or None
                    result['funding_rate_scope'] = 'last_settled_rate'
                    result['funding_timestamp_kind'] = 'settlement'
        else:
            for path, params, assignments in [
                ('/api/v5/public/funding-rate', {'instId': symbol}, [('funding_rate', 'fundingRate')]),
                ('/api/v5/public/open-interest', {'instType': 'SWAP', 'instId': symbol}, [('open_interest', 'oi'), ('open_interest_base', 'oiCcy'), ('open_interest_usd', 'oiUsd')]),
                ('/api/v5/public/mark-price', {'instType': 'SWAP', 'instId': symbol}, [('mark_price', 'markPx')]),
            ]:
                payload = request(path, params) or {}
                rows = payload.get('data', [])
                if not rows:
                    continue
                row = rows[0]
                for target, key in assignments:
                    result[target] = _number(row.get(key), nonnegative=target.startswith('open_interest'),
                                             positive=target == 'mark_price')
                stamp = _ms(row.get('ts') or row.get('fundingTime')) or self._now()
                for target, _ in assignments:
                    result[target + '_timestamp_ms'] = stamp
                if 'funding-rate' in path:
                    result['next_funding_time_ms'] = _ms(row.get('nextFundingTime')) or None
                    result['funding_timestamp_ms'] = stamp
                    result['funding_timestamp_kind'] = 'venue_funding_time'
                    result['funding_rate_scope'] = 'published_current_rate'
            metadata = dict(self._metadata.get(item.key, {}))
            metadata.update(item.contract_metadata)
            underlying = metadata.get('uly') or f'{item.base}-{item.quote}'
            payload = request('/api/v5/market/index-tickers', {'instId': underlying}) or {}
            rows = payload.get('data', [])
            if rows:
                result['index_price'] = _number(rows[0].get('idxPx'), positive=True)
                result['index_price_timestamp_ms'] = _ms(rows[0].get('ts')) or None
            result['contract_metadata'] = {key: metadata.get(key) for key in ('ctVal', 'ctValCcy', 'ctMult', 'ctType', 'settleCcy')}
        result['available'] = any(result.get(key) is not None for key in ('funding_rate', 'open_interest', 'mark_price', 'index_price'))
        result['timestamp_ms'] = self._now()
        return result
