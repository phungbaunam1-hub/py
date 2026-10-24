from collections import deque
import json
from urllib.parse import urlparse

import pytest
import requests

from aimt_market.data import CAPABILITIES, Instrument, MarketDataError, MarketSnapshot, PublicMarketClient


NOW = 1_700_000_180_000


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, routes):
        self.routes, self.calls, self.closed = routes, [], False

    def get(self, url, params, timeout, verify):
        assert url.startswith('https://') and verify is True and timeout == (5, 12)
        path = urlparse(url).path
        self.calls.append((path, dict(params)))
        assert path in self.routes, f'Unexpected request: {path}'
        route = self.routes[path]
        value = route.popleft() if isinstance(route, deque) else route(params) if callable(route) else route
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)

    def close(self):
        self.closed = True


def make(routes):
    session = Session(routes)
    return PublicMarketClient(session, clock=lambda: NOW / 1000), session


def bybit(rows, **extra):
    return {'retCode': 0, 'time': NOW, 'result': {'list': rows, **extra}}


def okx(rows):
    return {'code': '0', 'data': rows}


def kraken(pair, data):
    return {'error': [], 'result': {pair: data}}


def binance_bar(open_ms=NOW - 120000, volume='10'):
    return [open_ms, '100', '102', '99', '101', volume, open_ms + 59999, '1000', 3]


def test_binance_discovers_only_active_valid_spot_symbols():
    def symbol(name, status='TRADING', allowed=True, tick='.01'):
        return {'symbol': name, 'baseAsset': 'BTC', 'quoteAsset': 'USDT', 'status': status,
                'isSpotTradingAllowed': allowed, 'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': tick}]}
    client, session = make({'/api/v3/exchangeInfo': {'symbols': [
        symbol('BTCUSDT'), symbol('BTCUSDT'), symbol('HALTED', status='BREAK'),
        symbol('NOTSPOT', allowed=False), symbol('BROKEN', tick=None), symbol('BAD\nSYMBOL')]}})
    instruments = client.list_instruments('Binance', 'spot')
    assert [x.symbol for x in instruments] == ['BTCUSDT']
    assert instruments[0].key == 'Binance:spot:BTCUSDT'
    assert instruments[0].tick_size == .01
    assert len(client.catalog_warnings) == 2
    assert len(session.calls) == 1


def test_binance_full_spot_snapshot_has_no_fabricated_volume_or_wrong_aggressor():
    client, session = make({
        '/api/v3/klines': [binance_bar(), binance_bar(NOW - 10000), binance_bar(NOW - 60000, 'NaN')],
        '/api/v3/ticker/24hr': {'lastPrice': '101', 'bidPrice': '100.9', 'askPrice': '101.1',
                               'priceChangePercent': '-2', 'volume': '100', 'closeTime': NOW, 'apiKey': 'DO_NOT_EXPORT'},
        '/api/v3/trades': [{'id': 1, 'time': NOW - 100, 'price': '101', 'qty': '2', 'isBuyerMaker': False},
                           {'id': 2, 'time': NOW - 50, 'price': '100.9', 'qty': '3', 'isBuyerMaker': True},
                           {'id': 3, 'time': NOW, 'price': '101', 'qty': '0', 'isBuyerMaker': False}],
        '/api/v3/depth': {'lastUpdateId': 55, 'bids': [['99', '1'], ['100', '2'], ['100', '3'], ['-1', '50']],
                          'asks': [['102', '2'], ['101', '4'], ['103', 'NaN']]},
    })
    snap = client.fetch_snapshot(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'))
    assert len(snap.candles) == 2
    assert snap.candles[0]['closed'] and not snap.candles[-1]['closed']
    assert snap.candles[0]['time'] == (NOW - 120000) // 1000
    assert snap.candles[0]['volume'] == 10
    assert snap.candles[0]['volume_unit'] == 'BTC'
    assert any('history may have gaps' in message for message in snap.errors)
    assert [trade['side'] for trade in snap.trades] == ['Buy', 'Sell']
    assert [trade['unit'] for trade in snap.trades] == ['BTC', 'BTC']
    assert snap.book['bids'] == [[100, 3], [99, 1]]
    assert snap.book['asks'] == [[101, 4], [102, 2]]
    assert snap.book['snapshot_only'] and snap.book['sequence'] == 55
    assert snap.derivatives['available'] is False
    assert snap.quote['change_pct_24h'] == -2
    exported = snap.as_dict()
    assert exported['mode'] == 'live' and exported['snapshot_mode'] == 'public_rest'
    assert exported['provenance']['streaming'] is False
    assert exported['provenance']['synthetic'] is False and exported['provenance']['cme_futures'] is False
    assert exported['provenance']['execution_account'] is None
    assert 'DO_NOT_EXPORT' not in json.dumps(exported)
    assert 'candles' not in snap.as_dict(include_candles=False)
    assert len(session.calls) == 4


def test_binance_perpetual_catalog_excludes_dated_futures():
    rows = []
    for name, kind in [('BTCUSDT', 'PERPETUAL'), ('BTCUSDT_241227', 'CURRENT_QUARTER')]:
        rows.append({'symbol': name, 'contractType': kind, 'status': 'TRADING', 'baseAsset': 'BTC',
                     'quoteAsset': 'USDT', 'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '.1'}]})
    client, _ = make({'/fapi/v1/exchangeInfo': {'symbols': rows}})
    assert [x.symbol for x in client.list_instruments('Binance', 'perpetual')] == ['BTCUSDT']


def test_binance_perpetual_metrics_have_current_oi_funding_units_and_source():
    client, _ = make({
        '/fapi/v1/klines': [binance_bar()], '/fapi/v1/ticker/24hr': {'lastPrice': '101', 'closeTime': NOW},
        '/fapi/v1/premiumIndex': {'markPrice': '101.2', 'indexPrice': '101.1', 'lastFundingRate': '-.0001',
                                 'time': NOW - 20, 'nextFundingTime': NOW + 1000},
        '/fapi/v1/openInterest': {'openInterest': '2500.5', 'time': NOW - 30},
    })
    snap = client.fetch_snapshot(Instrument('Binance', 'perpetual', 'BTCUSDT', 'BTC', 'USDT'), include_orderflow=False)
    metrics = snap.derivatives
    assert metrics['open_interest'] == 2500.5 and metrics['open_interest_unit'] == 'BTC'
    assert metrics['funding_rate'] == -.0001 and metrics['mark_price'] == 101.2 and metrics['index_price'] == 101.1
    assert metrics['open_interest_timestamp_ms'] == NOW - 30
    assert metrics['next_funding_time_ms'] == NOW + 1000
    assert metrics['data_scope'] == 'public_perpetual'
    assert any('openInterest' in source for source in metrics['source_urls'])
    assert not snap.trades and snap.book['available'] is False


def test_optional_futures_failure_does_not_replace_data_with_demo():
    client, _ = make({
        '/fapi/v1/klines': [binance_bar()], '/fapi/v1/ticker/24hr': {'lastPrice': '101'},
        '/fapi/v1/premiumIndex': Response({}, status=503),
        '/fapi/v1/openInterest': {'openInterest': '25', 'time': NOW},
    })
    snap = client.fetch_snapshot(Instrument('Binance', 'perpetual', 'BTCUSDT', 'BTC', 'USDT'), include_orderflow=False)
    assert snap.derivatives['open_interest'] == 25
    assert snap.derivatives['mark_price'] is None
    assert any('503' in error for error in snap.errors)
    assert snap.candles[0]['close'] == 101 and snap.provenance['synthetic'] is False


def test_bybit_linear_catalog_paginates_and_filters_prelaunch_and_dated_contracts():
    def item(symbol, status='Trading', kind='LinearPerpetual'):
        return {'symbol': symbol, 'baseCoin': 'BTC', 'quoteCoin': 'USDT', 'status': status,
                'contractType': kind, 'priceFilter': {'tickSize': '.1'}}
    client, session = make({'/v5/market/instruments-info': deque([
        bybit([item('BTCUSDT'), item('PRE', status='PendingOpen')], nextPageCursor='next'),
        bybit([item('ETHUSDT'), item('BTCUSDT'), item('DATED', kind='LinearFutures')], nextPageCursor=''),
    ])})
    assert [x.symbol for x in client.list_instruments('Bybit', 'perpetual')] == ['BTCUSDT', 'ETHUSDT']
    assert session.calls[0][1] == {'category': 'linear', 'limit': 1000}
    assert session.calls[1][1]['cursor'] == 'next'


def test_bybit_spot_does_not_send_unsupported_cursor_or_limit():
    client, session = make({'/v5/market/instruments-info': bybit([
        {'symbol': 'BTCUSDT', 'baseCoin': 'BTC', 'quoteCoin': 'USDT', 'status': 'Trading', 'priceFilter': {'tickSize': '.01'}}],
        nextPageCursor='ignored-for-spot')})
    assert len(client.list_instruments('Bybit', 'spot')) == 1
    assert session.calls == [('/v5/market/instruments-info', {'category': 'spot'})]


def test_bybit_repeated_cursor_is_a_catalog_error_not_silent_truncation():
    client, _ = make({'/v5/market/instruments-info': bybit([], nextPageCursor='repeat')})
    with pytest.raises(MarketDataError, match='pagination cursor repeated'):
        client.list_instruments('Bybit', 'perpetual')


def test_bybit_newest_first_klines_ticker_derivatives_and_orderflow():
    rows = [[str(NOW - 10000), '101', '102', '100', '101.5', '2', '203'],
            [str(NOW - 120000), '100', '102', '99', '101', '3', '303']]
    client, _ = make({
        '/v5/market/kline': bybit(rows),
        '/v5/market/tickers': bybit([{'lastPrice': '101.5', 'bid1Price': '101.4', 'ask1Price': '101.6',
            'markPrice': '101.5', 'indexPrice': '101.3', 'fundingRate': '.0001', 'openInterest': '1234',
            'nextFundingTime': str(NOW + 1000)}]),
        '/v5/market/recent-trade': bybit([{'execId': 'abc', 'time': str(NOW), 'price': '101.5', 'size': '2', 'side': 'Sell'}]),
        '/v5/market/orderbook': {'retCode': 0, 'result': {'b': [['101.4', '5']], 'a': [['101.6', '6']], 'ts': NOW, 'u': 7}},
    })
    snap = client.fetch_snapshot(Instrument('Bybit', 'perpetual', 'BTCUSDT', 'BTC', 'USDT'))
    assert [bar['time'] for bar in snap.candles] == [(NOW - 120000) // 1000, (NOW - 10000) // 1000]
    assert [bar['closed'] for bar in snap.candles] == [True, False]
    assert snap.derivatives['open_interest'] == 1234 and snap.derivatives['open_interest_unit'] == 'BTC'
    assert snap.trades[0]['side'] == 'Sell' and snap.trades[0]['trade_id'] == 'abc'
    assert snap.book['timestamp_ms'] == NOW and snap.book['bids'] == [[101.4, 5]]


def test_okx_swaps_preserve_contract_units_and_contract_metadata():
    instrument_row = {'instId': 'BTC-USDT-SWAP', 'uly': 'BTC-USDT', 'state': 'live', 'baseCcy': '',
                      'quoteCcy': '', 'tickSz': '.1', 'ctVal': '.01', 'ctValCcy': 'BTC',
                      'ctMult': '1', 'ctType': 'linear', 'settleCcy': 'USDT'}
    client, _ = make({
        '/api/v5/public/instruments': okx([instrument_row]),
        '/api/v5/market/candles': okx([[str(NOW - 120000), '100', '102', '99', '101', '50', '.5', '50.5', '1']]),
        '/api/v5/market/ticker': okx([{'last': '101', 'bidPx': '100.9', 'askPx': '101.1', 'vol24h': '2000', 'ts': str(NOW)}]),
        '/api/v5/market/trades': okx([{'tradeId': '1', 'px': '101', 'sz': '10', 'side': 'buy', 'ts': str(NOW)}]),
        '/api/v5/market/books': okx([{'bids': [['100.9', '100', '0', '2']], 'asks': [['101.1', '150', '0', '3']], 'ts': str(NOW)}]),
        '/api/v5/public/funding-rate': okx([{'fundingRate': '-.0002', 'fundingTime': str(NOW), 'nextFundingTime': str(NOW + 1000)}]),
        '/api/v5/public/open-interest': okx([{'oi': '20000', 'oiCcy': '200', 'oiUsd': '20200', 'ts': str(NOW)}]),
        '/api/v5/public/mark-price': okx([{'markPx': '101.2', 'ts': str(NOW)}]),
        '/api/v5/market/index-tickers': okx([{'idxPx': '101.1', 'ts': str(NOW)}]),
    })
    instrument, = client.list_instruments('OKX', 'perpetual')
    assert instrument.base == 'BTC' and instrument.quote == 'USDT'
    snap = client.fetch_snapshot(instrument)
    assert snap.candles[0]['volume'] == 50 and snap.candles[0]['volume_unit'] == 'contracts'
    assert snap.trades[0]['size'] == 10 and snap.trades[0]['unit'] == 'contracts'
    assert snap.book['unit'] == 'contracts'
    assert snap.derivatives['open_interest'] == 20000 and snap.derivatives['open_interest_unit'] == 'contracts'
    assert snap.derivatives['open_interest_base'] == 200
    assert snap.derivatives['contract_metadata']['ctVal'] == '.01'
    assert snap.derivatives['index_price'] == 101.1 and snap.derivatives['funding_rate'] == -.0002


def test_okx_catalog_contract_metadata_survives_closed_client_and_fresh_fetch_client():
    metadata = {'ctVal': '.01', 'ctValCcy': 'BTC', 'ctMult': '1', 'ctType': 'linear', 'settleCcy': 'USDT'}
    catalog, catalog_session = make({'/api/v5/public/instruments': okx([
        dict(metadata, instId='BTC-USDT-SWAP', uly='BTC-USDT', state='live', tickSz='.1')])})
    instrument, = catalog.list_instruments('OKX', 'perpetual')
    catalog.close()
    assert catalog_session.closed
    assert instrument.contract_metadata == dict(metadata, uly='BTC-USDT')
    client, session = make({
        '/api/v5/market/candles': okx([[str(NOW - 120000), '100', '102', '99', '101', '50', '.5', '50.5', '1']]),
        '/api/v5/market/ticker': okx([{'last': '101', 'ts': str(NOW)}]),
        '/api/v5/public/funding-rate': okx([{'fundingRate': '.0001', 'fundingTime': str(NOW)}]),
        '/api/v5/public/open-interest': okx([{'oi': '20000', 'ts': str(NOW)}]),
        '/api/v5/public/mark-price': okx([{'markPx': '101', 'ts': str(NOW)}]),
        '/api/v5/market/index-tickers': okx([{'idxPx': '100.9', 'ts': str(NOW)}]),
    })
    snap = client.fetch_snapshot(instrument, limit=1, include_orderflow=False)
    assert snap.derivatives['contract_metadata'] == metadata
    assert snap.as_dict()['instrument']['contract_metadata'] == dict(metadata, uly='BTC-USDT')
    assert snap.derivatives['open_interest'] == 20000
    assert snap.derivatives['open_interest_unit'] == snap.candles[0]['volume_unit'] == 'contracts'
    assert snap.derivatives['open_interest_base'] is None  # Do not infer base amount from a contract multiplier.
    assert len(session.calls) == 6
    assert session.calls[-1] == ('/api/v5/market/index-tickers', {'instId': 'BTC-USDT'})


def test_contract_metadata_is_copied_allowlisted_and_exported_as_sanitized_copy():
    supplied = {'ctVal': '.01', 'ctValCcy': 'BTC\n', 'ctMult': float('nan'),
                'settleCcy': {'nested_secret': 'DO_NOT_EXPORT'}, 'apiKey': 'DO_NOT_EXPORT'}
    instrument = Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT', contract_metadata=supplied)
    supplied['ctVal'] = '500'
    assert instrument.contract_metadata == {'ctVal': '.01', 'ctValCcy': 'BTC', 'ctMult': None, 'settleCcy': None}
    exported = instrument.as_dict()
    exported['contract_metadata']['ctVal'] = '100'
    assert instrument.contract_metadata['ctVal'] == '.01'
    assert 'DO_NOT_EXPORT' not in json.dumps(instrument.as_dict(), allow_nan=False)
    assert hash(instrument) == hash(Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT'))
    with pytest.raises(ValueError, match='Contract metadata'):
        Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT', contract_metadata=['invalid'])


def test_okx_candle_pagination_deduplicates_and_uses_utc_daily_interval():
    now = NOW + 86400 * 1000 * 400
    first = [[str(now - (i + 1) * 86400000), '100', '102', '99', '101', '1', '1', '1', '1'] for i in range(300)]
    second = [[str(now - 301 * 86400000), '100', '102', '99', '101', '2', '1', '1', '1']]
    session = Session({'/api/v5/market/candles': deque([okx(first), okx(second)])})
    client = PublicMarketClient(session, clock=lambda: now / 1000)
    result = client._candles(Instrument('OKX', 'spot', 'BTC-USDT', 'BTC', 'USDT'), 'D1', 301)
    assert len(result) == 301 and result[0]['volume'] == 2
    assert session.calls[0][1]['bar'] == '1Dutc'
    assert session.calls[1][1]['after'] == min(int(row[0]) for row in first)


def test_kraken_discovers_fiat_fx_and_keeps_current_bar_unclosed():
    client, _ = make({
        '/0/public/AssetPairs': {'error': [], 'result': {
            'ZEURZUSD': {'wsname': 'EUR/USD', 'pair_decimals': 5, 'status': 'online'},
            'XXBTZUSD': {'wsname': 'XBT/USD', 'pair_decimals': 1, 'status': 'online'},
            'OFFLINE': {'wsname': 'GBP/USD', 'pair_decimals': 5, 'status': 'cancel_only'},
        }},
        '/0/public/OHLC': {'error': [], 'result': {'last': 123, 'ZEURZUSD': [
            [(NOW - 120000) // 1000, '1.1', '1.2', '1.0', '1.15', '1.12', '5', 10],
            [(NOW - 60000) // 1000, '1.15', '1.2', '1.1', '1.16', '1.15', '2', 2]]}},
        '/0/public/Ticker': kraken('ZEURZUSD', {'a': ['1.161'], 'b': ['1.159'], 'c': ['1.16']}),
        '/0/public/Trades': {'error': [], 'result': {'last': '123', 'ZEURZUSD': [['1.16', '20', NOW / 1000, 'b', 'm', '', 7]]}},
        '/0/public/Depth': kraken('ZEURZUSD', {'bids': [['1.159', '100', NOW / 1000]], 'asks': [['1.161', '200', NOW / 1000]]}),
    })
    instruments = client.list_instruments('Kraken', 'spot')
    fx = next(x for x in instruments if x.symbol == 'ZEURZUSD')
    assert fx.kind == 'forex' and fx.tick_size == pytest.approx(.00001)
    assert next(x for x in instruments if x.symbol == 'XXBTZUSD').kind == 'crypto'
    snap = client.fetch_snapshot(fx)
    assert snap.candles[-1]['closed'] is False and snap.candles[0]['closed'] is True
    assert snap.trades[0]['time_ms'] == NOW and snap.trades[0]['side'] == 'Buy'
    assert snap.trades[0]['unit'] == 'EUR'
    assert snap.book['bids'] == [[1.159, 100]] and snap.quote['ask'] == 1.161
    assert not snap.derivatives['available']


def test_rate_limit_exposes_retry_after_and_prevents_extra_requests_in_same_client():
    current = [NOW / 1000]
    session = Session({'/api/v3/exchangeInfo': deque([Response({}, 429, {'Retry-After': '2'}), {'symbols': []}])})
    client = PublicMarketClient(session, clock=lambda: current[0])
    with pytest.raises(MarketDataError) as caught:
        client.list_instruments('Binance', 'spot')
    assert caught.value.status_code == 429 and caught.value.retry_after == 2
    with pytest.raises(MarketDataError, match='cooldown'):
        client.list_instruments('Binance', 'spot')
    assert len(session.calls) == 1
    current[0] += 2.1
    assert client.list_instruments('Binance', 'spot') == []
    assert len(session.calls) == 2


def test_optional_rate_limit_stops_further_endpoint_requests_in_the_snapshot():
    client, session = make({
        '/api/v3/klines': [binance_bar()], '/api/v3/ticker/24hr': {'lastPrice': '101'},
        '/api/v3/trades': Response({}, 429, {'Retry-After': '30'}),
    })
    snap = client.fetch_snapshot(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'))
    assert len(session.calls) == 3  # Depth is suppressed locally once the rate limit is known.
    assert not snap.trades and snap.book['available'] is False
    assert any('429' in error for error in snap.errors) and any('cooldown' in error for error in snap.errors)


def test_invalid_candle_timestamps_are_not_reinterpreted_as_unix_epoch_history():
    bad = binance_bar()
    bad[0] = 'NaN'
    client, _ = make({'/api/v3/klines': [bad]})
    with pytest.raises(MarketDataError, match='no valid candles'):
        client.fetch_snapshot(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'))


@pytest.mark.parametrize('provider,market,path,payload', [
    ('Binance', 'spot', '/api/v3/exchangeInfo', {'code': -1, 'msg': 'error'}),
    ('Bybit', 'spot', '/v5/market/instruments-info', {'retCode': 10006, 'retMsg': 'rate limit'}),
    ('OKX', 'spot', '/api/v5/public/instruments', {'code': '50011', 'msg': 'rate limit'}),
    ('Kraken', 'spot', '/0/public/AssetPairs', {'error': ['EAPI:Rate limit exceeded']}),
])
def test_api_error_envelopes_are_not_empty_success(provider, market, path, payload):
    client, _ = make({path: payload})
    with pytest.raises(MarketDataError):
        client.list_instruments(provider, market)


def test_tls_failure_empty_history_and_bad_json_do_not_create_synthetic_candles():
    instrument = Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT')
    for failure in [requests.exceptions.SSLError('certificate verification failed'), Response(ValueError('bad JSON')), []]:
        client, session = make({'/api/v3/klines': failure})
        with pytest.raises(MarketDataError):
            client.fetch_snapshot(instrument)
        assert len(session.calls) == 1


def test_invalid_scope_symbol_and_nonfinite_values_are_rejected_or_sanitized():
    with pytest.raises(ValueError):
        Instrument('Binance', 'spot', 'BTC\nIGNORE', 'BTC', 'USDT')
    with pytest.raises(ValueError):
        Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT', tick_size=float('nan'))
    broker = Instrument('MT5', 'broker', 'EUR USD#', 'UNKNOWN', 'UNKNOWN', kind='broker')
    assert broker.symbol == 'EUR USD#'
    client, _ = make({})
    with pytest.raises(ValueError, match='Unsupported'):
        client.fetch_snapshot(broker)
    with pytest.raises(ValueError):
        client.list_instruments('Kraken', 'perpetual')
    for timeframe, limit in [('M2', 300), ('M1', 0), ('M1', 1001), ('M1', True)]:
        with pytest.raises(ValueError):
            client.fetch_snapshot(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), timeframe, limit)
    snap = MarketSnapshot(broker, 'M1', [], NOW, [], {}, {}, ['test\ncontrol'], {'last': float('nan')})
    exported = snap.as_dict()
    assert exported['quote']['last'] is None and exported['errors'] == ['testcontrol']
    json.dumps(exported, allow_nan=False)
    assert CAPABILITIES['Kraken']['markets'] == ('spot',)
