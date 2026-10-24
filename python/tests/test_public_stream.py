from collections import deque
import json
import ssl
import threading

import pytest

from aimt_market.data import Instrument
from aimt_market.stream import PublicTradeStream


NOW = 1_700_000_000_000


class Socket:
    def __init__(self, owner, url, callbacks):
        self.owner, self.url, self.callbacks = owner, url, callbacks
        self.sent, self.options = [], None
        self.closed = threading.Event()
        self.ready = threading.Event()

    def send(self, message):
        self.sent.append(message)
        if message == 'ping' or (message.startswith('{') and json.loads(message).get('op') == 'ping'):
            self.owner.ping.set()

    def close(self):
        self.closed.set()

    def run_forever(self, **options):
        self.options = options
        assert options['sslopt']['cert_reqs'] == ssl.CERT_REQUIRED
        assert options['sslopt']['check_hostname'] is True
        self.callbacks['on_open'](self)
        for message in self.owner.messages:
            self.callbacks['on_message'](self, message if isinstance(message, (str, bytes)) else json.dumps(message))
        self.ready.set()
        if self.owner.transport_error:
            raise self.owner.transport_error
        if self.owner.immediate_close:
            self.callbacks['on_close'](self, 1001, 'untrusted close reason with private URL')
            return
        self.closed.wait(5)


class Factory:
    def __init__(self, messages=(), immediate_close=False, transport_error=None):
        self.messages = messages
        self.immediate_close, self.transport_error = immediate_close, transport_error
        self.sockets = []
        self.created, self.ping = threading.Event(), threading.Event()

    def __call__(self, url, **callbacks):
        socket = Socket(self, url, callbacks)
        self.sockets.append(socket)
        self.created.set()
        return socket


def binance(trade_id=1, symbol='BTCUSDT', **extra):
    return {'e': 'trade', 's': symbol, 't': trade_id, 'T': NOW, 'p': '100', 'q': '2', 'm': False, **extra}


def bybit(rows):
    return {'topic': 'publicTrade.BTCUSDT', 'type': 'snapshot', 'data': rows}


def bybit_row(trade_id=1, **extra):
    return {'s': 'BTCUSDT', 'T': NOW, 'p': '100', 'v': '2', 'S': 'Buy', 'i': str(trade_id), **extra}


def okx(rows):
    return {'arg': {'channel': 'trades', 'instId': 'BTC-USDT-SWAP'}, 'data': rows}


def start(item, messages):
    trades, statuses, threads = [], [], []
    factory = Factory(messages)

    def on_trades(batch):
        trades.extend(batch)
        threads.append(threading.get_ident())

    def on_status(status):
        statuses.append(status)
        threads.append(threading.get_ident())

    stream = PublicTradeStream(item, on_trades, on_status, factory)
    assert stream.start()
    assert factory.created.wait(2)
    assert factory.sockets[0].ready.wait(2)
    return stream, factory, trades, statuses, threads


def test_binance_native_prints_deduplicate_ids_and_keep_side_source_and_tls():
    item = Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT')
    stream, factory, trades, statuses, threads = start(item, [
        binance(1), binance(1), binance(2, m=True), binance(3, symbol='ETHUSDT'),
        {'e': 'bookTicker', 's': 'BTCUSDT', 'p': '500'},
    ])
    try:
        assert len(trades) == 2
        assert [row['side'] for row in trades] == ['Buy', 'Sell']
        assert all(row['source'] == item.key and row['stream_kind'] == 'native' for row in trades)
        assert all(row['volume_unit'] == 'BTC' for row in trades)
        assert factory.sockets[0].url == 'wss://data-stream.binance.vision/ws/btcusdt@trade'
        assert any(row['state'] == 'connected' and row['streaming'] for row in statuses)
        assert all(identifier != threading.get_ident() for identifier in threads)
        assert stream.alive and not stream.start()
    finally:
        assert stream.stop()
    before = len(trades), len(statuses)
    factory.sockets[0].callbacks['on_message'](factory.sockets[0], json.dumps(binance(99)))
    assert (len(trades), len(statuses)) == before and not stream.alive
    assert stream.stop()


def test_binance_usdm_aggtrade_is_not_mislabeled_as_native_rest_trade():
    item = Instrument('Binance', 'perpetual', 'BTCUSDT', 'BTC', 'USDT')
    packet = {'e': 'aggTrade', 's': 'BTCUSDT', 'a': 55, 'f': 100, 'l': 110,
              'T': NOW, 'p': '100', 'q': '20', 'm': True}
    stream, factory, trades, _, _ = start(item, [packet, binance(99)])
    try:
        assert len(trades) == 1
        assert trades[0]['stream_kind'] == 'aggregated' and trades[0]['trade_id'] == '55'
        assert trades[0]['side'] == 'Sell' and trades[0]['size'] == 20
        assert factory.sockets[0].url == 'wss://fstream.binance.com/ws/btcusdt@aggTrade'
    finally:
        stream.stop()


@pytest.mark.parametrize('market,path', [('spot', 'spot'), ('perpetual', 'linear')])
def test_bybit_subscription_ack_batch_sides_and_block_print_provenance(market, path):
    item = Instrument('Bybit', market, 'BTCUSDT', 'BTC', 'USDT')
    stream, factory, trades, statuses, _ = start(item, [
        {'op': 'subscribe', 'success': True},
        bybit([bybit_row(1), bybit_row(2, S='Sell', BT=True), bybit_row(2, S='Sell', BT=True),
               bybit_row(3, S='unexpected'), bybit_row(4, s='ETHUSDT')]),
    ])
    try:
        assert factory.sockets[0].url == 'wss://stream.bybit.com/v5/public/' + path
        assert json.loads(factory.sockets[0].sent[0]) == {'op': 'subscribe', 'args': ['publicTrade.BTCUSDT']}
        assert [row['side'] for row in trades] == ['Buy', 'Sell', 'Unknown']
        assert trades[1]['block_trade'] is True
        assert all(row['stream_kind'] == 'native' for row in trades)
        assert any(row['state'] == 'connected' for row in statuses)
    finally:
        stream.stop()


def test_okx_contract_prints_are_labeled_aggregated_and_not_base_coin_volume():
    item = Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT')
    stream, factory, trades, statuses, _ = start(item, [
        {'event': 'subscribe', 'arg': {'channel': 'trades', 'instId': item.symbol}},
        okx([{'instId': item.symbol, 'tradeId': '10', 'ts': str(NOW), 'px': '100', 'sz': '3', 'side': 'sell', 'count': '2'}]),
        'pong',
    ])
    try:
        assert factory.sockets[0].url == 'wss://ws.okx.com:8443/ws/v5/public'
        assert json.loads(factory.sockets[0].sent[0]) == {'op': 'subscribe', 'args': [{'channel': 'trades', 'instId': item.symbol}]}
        assert len(trades) == 1 and trades[0]['side'] == 'Sell'
        assert trades[0]['volume_unit'] == 'contracts' and trades[0]['stream_kind'] == 'aggregated'
        assert trades[0]['aggregated_trade_count'] == 2
        assert any(row['streaming'] for row in statuses)
    finally:
        stream.stop()


def test_nonfinite_zero_bad_side_and_unrelated_channel_never_create_fake_prints():
    item = Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT')
    stream, _, trades, _, _ = start(item, [binance(1, p='NaN'), binance(2, q='0'),
        binance(3, T='not-a-time'), binance(4, m=None)])
    try:
        assert len(trades) == 1 and trades[0]['side'] == 'Unknown'
    finally:
        stream.stop()


def test_unsupported_kraken_reports_rest_only_without_opening_a_websocket():
    statuses, done = [], threading.Event()
    factory = Factory()

    def status(payload):
        statuses.append(payload)
        done.set()

    stream = PublicTradeStream(Instrument('Kraken', 'spot', 'XXBTZUSD', 'XBT', 'USD'), lambda _: None, status, factory)
    stream.start()
    assert done.wait(2)
    assert stream.stop()
    assert not factory.sockets
    assert statuses[-1]['state'] == 'error' and statuses[-1]['streaming'] is False
    assert 'REST' in statuses[-1]['message']


def test_reconnect_backoff_is_interruptible_and_does_not_retry_after_stop():
    ready, statuses = threading.Event(), []

    def status(payload):
        statuses.append(payload)
        if 'Retrying' in payload['message']:
            ready.set()

    factory = Factory(immediate_close=True)
    stream = PublicTradeStream(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), lambda _: None, status, factory)
    stream.start()
    assert ready.wait(2)
    assert stream.stop(timeout=.5)
    assert len(factory.sockets) == 1 and not stream.alive
    assert any(row['state'] == 'disconnected' for row in statuses)


def test_reconnect_attempts_are_bounded_even_when_the_server_accepts_then_closes():
    class InstantBackoff(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()

    factory, done, statuses = Factory(immediate_close=True), threading.Event(), []

    def status(payload):
        statuses.append(payload)
        if 'limit reached' in payload['message']:
            done.set()

    stream = PublicTradeStream(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), lambda _: None, status, factory)
    stream._stop_event = InstantBackoff()
    stream.start()
    assert done.wait(2)
    assert stream.stop()
    assert len(factory.sockets) == stream.MAX_RECONNECTS + 1
    assert statuses[-1]['state'] == 'error'


@pytest.mark.parametrize('provider,market,symbol', [('Bybit', 'spot', 'BTCUSDT'), ('OKX', 'spot', 'BTC-USDT')])
def test_application_heartbeats_use_official_ping_packets(provider, market, symbol):
    factory = Factory()
    stream = PublicTradeStream(Instrument(provider, market, symbol, 'BTC', 'USDT'), lambda _: None, lambda _: None, factory)
    stream.HEARTBEAT_SECONDS = .02
    stream.start()
    try:
        assert factory.ping.wait(2)
        sent = factory.sockets[0].sent
        assert ('ping' in sent) if provider == 'OKX' else any(json.loads(row).get('op') == 'ping' for row in sent)
    finally:
        assert stream.stop()


def test_observer_exceptions_do_not_crash_feed_and_sensitive_error_text_is_not_logged():
    statuses = []

    def broken(_):
        raise RuntimeError('observer failed')

    factory = Factory([binance()], transport_error=ssl.SSLCertVerificationError('https://private.example/key=SECRET'))

    def status(payload):
        statuses.append(payload)

    stream = PublicTradeStream(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), broken, status, factory)
    stream.start()
    assert factory.created.wait(2) and factory.sockets[0].ready.wait(2)
    assert stream.stop()
    assert stream.callback_errors == 1 and stream.recent_trades
    assert 'SECRET' not in json.dumps(statuses) and 'private.example' not in json.dumps(statuses)


def test_recent_buffers_are_bounded_without_dropping_current_valid_batch():
    messages = [bybit([bybit_row(offset + i) for i in range(1000)]) for offset in range(0, 11000, 1000)]
    batches = []
    factory = Factory(messages)
    stream = PublicTradeStream(Instrument('Bybit', 'spot', 'BTCUSDT', 'BTC', 'USDT'),
                               lambda rows: batches.append(len(rows)), lambda _: None, factory)
    stream.start()
    try:
        assert factory.created.wait(2) and factory.sockets[0].ready.wait(4)
        assert sum(batches) == 11000
        assert len(stream.recent_trades) == stream.MAX_RECENT_TRADES
        assert len(stream._id_set) == len(stream._recent_ids) == stream.MAX_RECENT_IDS
        recent = stream.recent_trades
        recent[0]['price'] = -100
        assert stream.recent_trades[0]['price'] == 100
    finally:
        stream.stop()


def test_bad_json_and_oversized_batches_fail_explicitly_instead_of_partial_silent_data():
    for packet in ['not-json', bybit([bybit_row(i) for i in range(PublicTradeStream.MAX_BATCH + 1)])]:
        stream, _, trades, statuses, _ = start(Instrument('Bybit', 'spot', 'BTCUSDT', 'BTC', 'USDT'), [packet])
        try:
            assert not trades
            assert any(row['state'] == 'error' and row['streaming'] is False for row in statuses)
        finally:
            stream.stop()


def test_optional_dependency_failure_is_explicit_and_does_not_replace_market_data(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'websocket', None)
    ready, statuses = threading.Event(), []

    def status(payload):
        statuses.append(payload)
        ready.set()

    stream = PublicTradeStream(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), lambda _: None, status)
    stream.start()
    assert ready.wait(2)
    assert stream.stop()
    assert statuses[-1]['state'] == 'error' and 'dependency is missing' in statuses[-1]['message']
