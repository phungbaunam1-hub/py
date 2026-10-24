import copy
from dataclasses import replace
import time
from types import SimpleNamespace

from PyQt6 import QtWidgets
import pytest

from original import AI_MT_Trading as legacy
from aimt_market.data import Instrument, MarketSnapshot
from aimt_market.live import TradeMailbox, update_price_candles
from aimt_market.workspace import MarketWorkspace


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeStream:
    instances = []

    def __init__(self, instrument, trades, status):
        self.instrument, self.trades, self.status = instrument, trades, status
        self.started = self.stopped = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=2):
        self.stopped = True
        return True


def snapshot(item=None):
    item = item or Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT')
    now = int(time.time())
    start = now // 60 * 60 - 600 * 60
    candles = [{'time': start + n * 60, 'open': 100 + n * .1,
                'high': 101 + n * .1, 'low': 99 + n * .1, 'close': 100.2 + n * .1,
                'volume': 100, 'volume_is_real': True, 'closed': True} for n in range(600)]
    candles.append(dict(candles[-1], time=start + 600 * 60, closed=False))
    return MarketSnapshot(item, 'M1', candles, now * 1000, [], {}, {}, [], {'bid': 160, 'ask': 160.1})


@pytest.fixture
def workspace(app):
    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = {'symbol': 'EURUSD', 'tf': 'M1', 'ai_provider': 'gemini'}
            self.broker = SimpleNamespace(demo=True)
            self.last_live = self.payload = self.armed = None
            self.card, self.txt_ai = QtWidgets.QLabel(), QtWidgets.QTextEdit()
            self.nodes = SimpleNamespace(set_state=lambda *_a, **_k: None)
            self.messages = []
            self.rendered = 0

        def primary(self):
            return {'name': 'Demo', 'enabled': True}

        def say(self, text):
            self.messages.append(text)

        log = say

        def render_card(self):
            self.rendered += 1

    window = Window()
    value = MarketWorkspace(window, legacy, stream_factory=FakeStream)
    value.timer.stop()
    value.live_timer.stop()
    # This fixture never opens public HTTP connections.
    value.hub.submit = lambda *args, **kwargs: None
    yield value
    value.shutdown()
    value.close()
    window.close()


def bundle(item=None):
    snap = snapshot(item)
    from aimt_market.analysis import original_studies
    from aimt_market.quant import analyze_market
    return {'snapshot': snap, 'quant': analyze_market(snap.candles),
            'original': original_studies(legacy, snap.candles), 'related_futures': {'available': False}}


def test_independent_tabs_keep_primary_execution_and_original_chart(workspace):
    chart = legacy.ChartWidget()
    workspace.attach_primary_chart(chart)
    before = copy.deepcopy(workspace.window.cfg)
    one = workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)
    two = workspace.add_chart(Instrument('Kraken', 'spot', 'EURUSD', 'EUR', 'USD', 'forex'), 'H1', load=False)
    assert workspace.tabs.tabText(workspace.tabs.count() - 1) == '+'
    assert workspace.active_page is two
    assert not workspace.execution_allowed()
    assert one.chart is not two.chart and chart is not two.chart
    assert workspace.window.cfg == before
    workspace.tabs.setCurrentWidget(workspace.primary_page)
    assert workspace.execution_allowed()
    workspace._close_tab(workspace.tabs.indexOf(one))
    assert one.closed and one.id not in workspace._pages
    assert chart.parent() is workspace.primary_page


def test_late_http_and_ai_results_cannot_replace_new_timeframe(workspace):
    page = workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)
    workspace.load_page(page)
    old = page.token
    page.timeframe.setCurrentText('H1')
    assert page.token != old
    workspace._result('chart', old, {'ok': True, 'data': bundle()})
    assert page.bundle is None
    import weakref
    workspace._tokens['old-ai'] = weakref.ref(page)
    page.ai_token = 'new-ai'
    workspace._result('ai', 'old-ai', {'ok': True, 'data': {'signal': 'BUY'}})
    assert page.ai_result is None


def test_stream_updates_prices_without_double_counting_rest_volume(workspace):
    page = workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)
    page.bundle = bundle()
    workspace._start_stream()
    stream = workspace._stream
    assert stream.started
    last = page.bundle['snapshot'].candles[-1]
    stream.trades([{'time_ms': last['time'] * 1000 + 1000, 'price': 161.5, 'size': 2,
                    'side': 'Buy', 'trade_id': '1', 'stream_kind': 'native'}])
    workspace._drain_stream()
    current = page.bundle['snapshot'].candles[-1]
    assert current['close'] == 161.5
    assert current['volume'] == last['volume']
    assert not current['volume_complete'] and not current['closed']
    assert len(page.stream_trades) == 1
    workspace.update_cache()
    cached = workspace.ai_snapshot()
    assert cached['charts'][0]['trade_stream']['trades'][0]['trade_id'] == '1'
    cached['charts'][0]['candles'].clear()
    assert workspace.ai_snapshot()['charts'][0]['candles']
    workspace.tabs.setCurrentWidget(workspace.primary_page)
    assert workspace._stream is None


def test_current_snapshot_displays_analysis_and_retains_provenance(workspace):
    page = workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)
    workspace.load_page(page)
    workspace._result('chart', page.token, {'ok': True, 'data': bundle()})
    assert page.bundle and not page.inflight
    assert 'REST snapshot' in page.status.text()
    assert 'BTCUSDT' in workspace.window.card.text()
    assert len(page.bundle['original']['results']) == 8
    assert workspace.ai_snapshot()['charts'][0]['provenance']['synthetic'] is False


def test_stale_prices_and_old_ai_are_labeled_on_dashboard(workspace):
    page = workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)
    page.bundle = bundle()
    page.bundle['snapshot'] = replace(page.bundle['snapshot'], fetched_at_ms=int(time.time() * 1000) - 91000)
    page.ai_result = {'signal': 'BUY', 'conclusion': 'Old view',
                      'received_at_ms': int(time.time() * 1000) - 181000}
    workspace.present_active()
    assert 'DỮ LIỆU CŨ' in workspace.window.card.text()
    assert 'NHẬN ĐỊNH CŨ' in workspace.window.card.text()


def test_primary_closed_flags_use_feed_receipt_and_quant_cache_is_account_specific(workspace, monkeypatch):
    import numpy as np
    import aimt_market.workspace as module
    records = [(60, 10., 11., 9., 10., 1), (120, 10., 11., 9., 10., 2)]
    rates = np.array(records, dtype=[('time', 'i8'), ('open', 'f8'), ('high', 'f8'),
                                    ('low', 'f8'), ('close', 'f8'), ('tick_volume', 'i8')])
    calls = []
    monkeypatch.setattr(module, 'analyze_market', lambda *a, **k: calls.append(a) or {'signal': 'SIDEWAYS'})
    workspace.window._market_last_live_ms = 150000
    workspace.window.last_live = {'name': 'One', 'sym': 'EURUSD', 'tf': 'M1', 'rates': rates, 'demo': False}
    first = workspace._primary_snapshot()
    assert [bar['closed'] for bar in first['candles']] == [True, False]
    # Wall clock is far later, but an unreceived final candle is still unclosed.
    workspace._primary_snapshot()
    assert len(calls) == 1
    workspace.window.last_live['name'] = 'Other'
    workspace._primary_snapshot()
    assert len(calls) == 2


def test_mailbox_reports_overflow_and_has_bounded_delivery():
    box = TradeMailbox(3)
    box.trades([{'time_ms': n} for n in range(5)])
    rows, _, dropped = box.drain(2)
    assert dropped == 2 and [row['time_ms'] for row in rows] == [2, 3]
    assert box.drain()[0] == [{'time_ms': 4}]


def test_prices_do_not_mutate_closed_bars_or_fill_missing_intervals():
    bars = [{'time': 60, 'open': 10., 'high': 11., 'low': 9., 'close': 10.,
             'volume': 99., 'closed': True}]
    result = update_price_candles(bars, [{'time_ms': 61000, 'price': 12.},
                                      {'time_ms': 240001, 'price': 15.}], 'M1')
    assert result[0] == bars[0]
    assert [row['time'] for row in result] == [60, 240]
    assert result[-1]['volume'] == 0 and result[-1]['closed'] is False
