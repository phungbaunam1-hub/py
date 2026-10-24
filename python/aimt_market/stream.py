"""Optional public trade streams, with no trading credentials or order routing.

Connections are per active chart. REST books are deliberately not merged here. Binance USD-M
aggTrade and OKX trades are labeled aggregated venue prints; consumers must not double-count
them against native REST trades. Callbacks execute on the socket worker and must return quickly
(for Qt, emit a queued signal). A successful stop() joins that worker and quiesces callbacks.

Official channels:
https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Aggregate-Trade-Streams
https://bybit-exchange.github.io/docs/v5/websocket/public/trade
https://www.okx.com/docs-v5/en/#order-book-trading-market-data-ws-trades-channel
"""
from collections import deque
import json
import math
import ssl
import threading
import time

from .data import Instrument


class PublicTradeStream:
    MAX_MESSAGE_BYTES = 1_000_000
    MAX_BATCH = 2000
    MAX_RECENT_IDS = 10000
    MAX_RECENT_TRADES = 2000
    MAX_RECONNECTS = 5
    HEARTBEAT_SECONDS = 20
    SUPPORTED = frozenset({('Binance', 'spot'), ('Binance', 'perpetual'),
                           ('Bybit', 'spot'), ('Bybit', 'perpetual'),
                           ('OKX', 'spot'), ('OKX', 'perpetual')})

    def __init__(self, instrument, on_trades, on_status, websocket_factory=None):
        if not isinstance(instrument, Instrument):
            raise TypeError('An Instrument is required.')
        if not callable(on_trades) or not callable(on_status):
            raise TypeError('Trade and status callbacks must be callable.')
        self.instrument = instrument
        self.on_trades, self.on_status = on_trades, on_status
        self._factory = websocket_factory
        self._stop_event = threading.Event()
        self._lifecycle = threading.RLock()
        self._socket_gate = threading.RLock()
        self._callback_gate = threading.RLock()
        self._recent_gate = threading.Lock()
        self._thread = self._socket = None
        self._recent_ids, self._id_set = deque(), set()
        self._recent_trades = deque(maxlen=self.MAX_RECENT_TRADES)
        self._last_received = time.monotonic()
        self._connected = False
        self.callback_errors = 0

    @property
    def alive(self):
        worker = self._thread
        return bool(worker and worker.is_alive())

    @classmethod
    def supports(cls, instrument):
        return (instrument.provider, instrument.market) in cls.SUPPORTED

    def start(self):
        with self._lifecycle:
            if self.alive:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f'PublicTrades-{self.instrument.provider}')
            self._thread.start()
            return True

    def stop(self, timeout=2):
        """Returns True only when the worker is joined; False means cleanup is still pending.

        A callback already running when stop is requested cannot be forcibly interrupted.
        No new trades/status callbacks are started once the stop flag is observed.
        """
        timeout = max(0.0, float(timeout))
        self._stop_event.set()
        with self._socket_gate:
            socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
        worker = self._thread
        if worker is threading.current_thread():
            return False
        if worker is not None:
            worker.join(timeout)
        return not self.alive

    @property
    def recent_trades(self):
        with self._recent_gate:
            return tuple(dict(row) for row in self._recent_trades)

    def _url(self):
        item = self.instrument
        if item.provider == 'Binance':
            host, channel = ('data-stream.binance.vision', 'trade') if item.market == 'spot' else ('fstream.binance.com', 'aggTrade')
            return f'wss://{host}/ws/{item.symbol.lower()}@{channel}'
        if item.provider == 'Bybit':
            category = 'spot' if item.market == 'spot' else 'linear'
            return f'wss://stream.bybit.com/v5/public/{category}'
        if item.provider == 'OKX':
            return 'wss://ws.okx.com:8443/ws/v5/public'
        raise ValueError('This provider has no implemented public trade stream.')

    def _status(self, state, message, streaming=False):
        payload = {'state': state, 'message': message, 'source': self.instrument.key,
                   'streaming': bool(streaming), 'observed_at_ms': int(time.time() * 1000)}
        self._emit(self.on_status, payload)

    def _emit(self, callback, payload):
        with self._callback_gate:
            if self._stop_event.is_set():
                return
            try:
                callback(payload)
            except Exception:
                self.callback_errors += 1  # An observer cannot terminate the network worker.

    def _run(self):
        if not self.supports(self.instrument):
            self._status('error', 'Public trade WebSocket is not implemented for this venue/market. REST remains a separate source.')
            return
        factory = self._factory
        if factory is None:
            try:
                import websocket
                factory = websocket.WebSocketApp
            except ImportError:
                self._status('error', 'Optional websocket-client dependency is missing; no streaming connection was opened.')
                return
        for attempt in range(self.MAX_RECONNECTS + 1):
            if self._stop_event.is_set():
                return
            self._connected = False
            self._last_received = time.monotonic()
            self._status('connecting', f'Connecting public trades ({attempt + 1}/{self.MAX_RECONNECTS + 1}).')
            connection_stop = threading.Event()
            heartbeat = None
            try:
                socket = factory(self._url(), on_open=self._on_open,
                                 on_message=self._on_message, on_error=self._on_error,
                                 on_close=self._on_close)
                with self._socket_gate:
                    self._socket = socket
                if self._stop_event.is_set():
                    socket.close()
                    return
                heartbeat = threading.Thread(target=self._heartbeat, args=(socket, connection_stop),
                                             daemon=True, name='PublicTradeHeartbeat')
                heartbeat.start()
                # Keep certificate and hostname verification enabled; no custom trust bypass.
                socket.run_forever(ping_interval=20, ping_timeout=10,
                                   sslopt={'cert_reqs': ssl.CERT_REQUIRED, 'check_hostname': True})
            except Exception as error:
                self._status('error', f'Public WebSocket transport failed ({type(error).__name__}).')
            finally:
                connection_stop.set()
                if heartbeat is not None:
                    heartbeat.join(timeout=1)
                with self._socket_gate:
                    current = self._socket
                    self._socket = None
                if current is not None:
                    try:
                        current.close()
                    except Exception:
                        pass
            if self._stop_event.is_set():
                return
            self._status('disconnected', 'Public trade stream disconnected; retained data is no longer streaming.')
            if attempt == self.MAX_RECONNECTS:
                self._status('error', 'Public trade reconnect limit reached. Reconnect manually after checking venue access.')
                return
            delay = min(30, 2 ** attempt)
            self._status('connecting', f'Retrying public stream after {delay}s; no synthetic fallback.')
            if self._stop_event.wait(delay):
                return

    def _on_open(self, socket):
        if self._stop_event.is_set():
            socket.close()
            return
        provider, symbol = self.instrument.provider, self.instrument.symbol
        if provider == 'Bybit':
            socket.send(json.dumps({'op': 'subscribe', 'args': [f'publicTrade.{symbol}']}))
        elif provider == 'OKX':
            socket.send(json.dumps({'op': 'subscribe', 'args': [{'channel': 'trades', 'instId': symbol}]}))
        else:
            self._connected = True
            self._status('connected', 'Public trade socket open; awaiting venue trade messages.', streaming=True)

    def _on_error(self, socket, error):
        self._connected = False
        self._status('error', f'Public WebSocket error ({type(error).__name__}); no simulated data substituted.')

    def _on_close(self, socket, status_code=None, message=None):
        self._connected = False
        self._status('disconnected', 'Public trade socket closed.', streaming=False)

    def _heartbeat(self, socket, connection_stop):
        while not connection_stop.wait(self.HEARTBEAT_SECONDS):
            if self._stop_event.is_set():
                return
            with self._socket_gate:
                if self._socket is not socket:
                    return
            try:
                if self.instrument.provider == 'Bybit':
                    socket.send(json.dumps({'op': 'ping'}))
                elif self.instrument.provider == 'OKX' and time.monotonic() - self._last_received >= self.HEARTBEAT_SECONDS:
                    socket.send('ping')
            except Exception as error:
                self._status('error', f'Public heartbeat failed ({type(error).__name__}).')
                try:
                    socket.close()
                except Exception:
                    pass
                return

    def _on_message(self, socket, raw):
        if self._stop_event.is_set():
            return
        self._last_received = time.monotonic()
        if isinstance(raw, bytes):
            if len(raw) > self.MAX_MESSAGE_BYTES:
                self._reject(socket, 'Public trade frame exceeded the bounded message size.')
                return
            try:
                raw = raw.decode('utf-8')
            except UnicodeDecodeError:
                self._reject(socket, 'Public trade frame was not valid UTF-8.')
                return
        if not isinstance(raw, str) or len(raw) > self.MAX_MESSAGE_BYTES:
            self._reject(socket, 'Invalid or oversized public trade frame.')
            return
        if raw in ('pong', 'ping'):
            if raw == 'ping':
                socket.send('pong')
            return
        try:
            packet = json.loads(raw)
        except (ValueError, TypeError):
            self._reject(socket, 'Invalid JSON in public trade stream.')
            return
        if not isinstance(packet, dict):
            return
        provider = self.instrument.provider
        if provider == 'Bybit':
            if packet.get('op') == 'subscribe':
                if packet.get('success') is True:
                    self._subscribed()
                else:
                    self._reject(socket, 'Venue rejected the public trade subscription.')
                return
            if packet.get('op') in ('ping', 'pong'):
                return
            if packet.get('topic') != f'publicTrade.{self.instrument.symbol}':
                return
            rows = packet.get('data', [])
        elif provider == 'OKX':
            if packet.get('event') == 'error':
                self._reject(socket, 'Venue rejected the public trade subscription.')
                return
            arg = packet.get('arg', {})
            if not isinstance(arg, dict) or arg.get('channel') != 'trades' or arg.get('instId') != self.instrument.symbol:
                return
            if packet.get('event') == 'subscribe':
                self._subscribed()
                return
            rows = packet.get('data', [])
        else:
            # Combined-stream wrappers are accepted without changing the subscribed instrument.
            packet = packet.get('data', packet)
            if not isinstance(packet, dict) or packet.get('e') not in ('trade', 'aggTrade'):
                return
            rows = [packet]
        if not isinstance(rows, list) or len(rows) > self.MAX_BATCH:
            self._reject(socket, 'Public trade batch exceeded the bounded batch size.')
            return
        trades = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            trade = self._normalize(row)
            if trade is None:
                continue
            identifier = trade['trade_id']
            key = (trade['source'], trade['stream_kind'], identifier) if identifier is not None else None
            with self._recent_gate:
                if key is not None:
                    if key in self._id_set:
                        continue
                    self._id_set.add(key)
                    self._recent_ids.append(key)
                    while len(self._recent_ids) > self.MAX_RECENT_IDS:
                        self._id_set.discard(self._recent_ids.popleft())
                self._recent_trades.append(dict(trade))
            trades.append(trade)
        if trades:
            if not self._connected:
                self._subscribed()
            self._emit(self.on_trades, trades)

    def _subscribed(self):
        self._connected = True
        self._status('connected', 'Public trade subscription acknowledged by the venue.', streaming=True)

    def _reject(self, socket, message):
        self._connected = False
        self._status('error', message)
        try:
            socket.close()
        except Exception:
            pass

    @staticmethod
    def _positive(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    def _normalize(self, row):
        item, provider = self.instrument, self.instrument.provider
        if provider == 'Binance':
            if row.get('s') and row['s'] != item.symbol:
                return None
            expected = 'trade' if item.market == 'spot' else 'aggTrade'
            if row.get('e') != expected:
                return None
            raw_time, raw_price, raw_size = row.get('T'), row.get('p'), row.get('q')
            maker = row.get('m')
            side = 'Sell' if maker is True else 'Buy' if maker is False else 'Unknown'
            identifier = row.get('t') if item.market == 'spot' else row.get('a')
            kind = 'native' if item.market == 'spot' else 'aggregated'
        elif provider == 'Bybit':
            if row.get('s') and row['s'] != item.symbol:
                return None
            raw_time, raw_price, raw_size = row.get('T'), row.get('p'), row.get('v')
            side, identifier, kind = row.get('S'), row.get('i'), 'native'
        else:
            if row.get('instId') and row['instId'] != item.symbol:
                return None
            raw_time, raw_price, raw_size = row.get('ts'), row.get('px'), row.get('sz')
            side = {'buy': 'Buy', 'sell': 'Sell'}.get(row.get('side'))
            identifier, kind = row.get('tradeId'), 'aggregated'
        timestamp, price, size = self._positive(raw_time), self._positive(raw_price), self._positive(raw_size)
        if timestamp is None or price is None or size is None:
            return None
        if identifier is not None:
            identifier = str(identifier)
            if len(identifier) > 128 or not all(char.isprintable() for char in identifier):
                identifier = None
        result = {'time_ms': int(timestamp), 'price': price, 'size': size,
                  'side': side if side in ('Buy', 'Sell') else 'Unknown',
                  'trade_id': identifier, 'source': item.key, 'venue': provider,
                  'symbol': item.symbol, 'stream_kind': kind, 'volume_unit': item.volume_unit,
                  'unit': item.volume_unit, 'received_at_ms': int(time.time() * 1000)}
        if provider == 'Bybit' and row.get('BT') is True:
            result['block_trade'] = True
        if provider == 'OKX':
            count = self._positive(row.get('count'))
            if count is not None:
                result['aggregated_trade_count'] = int(count)
        return result
