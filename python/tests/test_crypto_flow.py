from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6 import QtWidgets

from aimt_market.data import Instrument, MarketSnapshot
from aimt_market.flow_adapter import PublicFlowAdapter
from aimt_orderflow.dock import OrderFlowDock
from aimt_orderflow.engine import FlowEngine, Settings


NOW = 1_782_000_000_000


def trade(identifier='1', size=2, side='Buy', **extra):
    result = dict(time_ms=NOW, price=100, size=size, side=side, trade_id=identifier)
    result.update(extra)
    return result


def snapshot(instrument=None, trades=None, **changes):
    item = instrument or Instrument('Bybit', 'spot', 'BTCUSDT', 'BTC', 'USDT')
    result = MarketSnapshot(item, 'M1', [], NOW, trades if trades is not None else [trade()],
        dict(bids=[[99, 450]], asks=[[101, 500]], timestamp_ms=NOW, snapshot_only=True,
             available=True, source=item.key, unit=item.volume_unit), {}, [], {'bid': 99, 'ask': 101})
    return replace(result, **changes)


def adapter():
    return PublicFlowAdapter(FlowEngine(Settings(min_trade=0)), deque(maxlen=160), clock=lambda: NOW / 1000 + 15)


def test_rest_retrieval_replay_and_overlapping_windows_do_not_double_count():
    flow, first = adapter(), snapshot(trades=[trade(), trade('2', size=1, side='Sell')])
    assert flow.ingest(first, chart_key='a').added_trades == 2
    before = flow.engine.snapshot()
    assert before.delta == 1
    assert flow.ingest(first, chart_key='a').added_trades == 0
    assert flow.engine.snapshot() == before
    assert len(flow.history) == 1
    second = replace(first, fetched_at_ms=NOW + 15000, trades=[trade('2', side='Sell'), trade('3', size=4)])
    assert flow.ingest(second, chart_key='a').added_trades == 1
    assert flow.engine.snapshot().delta == 5


def test_native_stream_shares_venue_ids_with_rest_but_empty_batch_never_replays_rest():
    flow, first = adapter(), snapshot()
    flow.ingest(first)
    result = flow.ingest(first, trades=[trade(stream_kind='native'), trade('2', size=3, stream_kind='native')])
    assert result.added_trades == 1
    assert flow.engine.snapshot().delta == 5
    fresh = replace(first, fetched_at_ms=NOW + 15000, trades=[trade('3', size=9)])
    assert flow.ingest(fresh, trades=[]).added_trades == 0
    assert flow.engine.snapshot().delta == 5


def test_aggregated_stream_resets_tape_then_excludes_native_rest_and_native_stream():
    item = Instrument('Binance', 'perpetual', 'BTCUSDT', 'BTC', 'USDT')
    flow, first = adapter(), snapshot(item, trades=[trade(size=40)])
    flow.ingest(first)
    result = flow.ingest(first, trades=[trade('100', size=3, stream_kind='aggregated', source=item.key)])
    assert result.reset and result.added_trades == 1
    assert flow.engine.snapshot().delta == 3
    assert [x.size for x in flow.engine.snapshot().big_trades] == [3]
    assert len(flow.history) == 1 and flow.engine.snapshot().book
    assert flow.ingest(replace(first, fetched_at_ms=NOW + 15000)).added_trades == 0
    assert flow.ingest(first, trades=[trade('200', size=50, stream_kind='native')]).added_trades == 0
    assert flow.ingest(first, trades=[trade('101', size=4, stream_kind='aggregated')]).added_trades == 1
    assert flow.engine.snapshot().delta == 7
    assert 'prints gộp xấp xỉ' in flow.status_text()
    assert 'tránh đếm đôi' in flow.status_text()


def test_wrong_source_or_invalid_aggregated_message_does_not_reset_valid_tape():
    flow, first = adapter(), snapshot()
    flow.ingest(first)
    before = flow.engine.snapshot()
    for invalid in [trade('2', stream_kind='aggregated', source='Binance:spot:ETHUSDT'),
                    trade('2', size=float('nan'), stream_kind='aggregated')]:
        assert flow.ingest(first, trades=[invalid]).added_trades == 0
        assert flow.engine.snapshot() == before
        assert flow.domain == 'native'


@pytest.mark.parametrize('change', ['page', 'timeframe', 'instrument'])
def test_chart_identity_resets_tape_history_and_dedup(change):
    flow, first = adapter(), snapshot()
    flow.ingest(first, chart_key='a')
    next_snapshot, key = first, 'a'
    if change == 'page':
        key = 'b'
    elif change == 'timeframe':
        next_snapshot = replace(first, timeframe='M5')
    else:
        next_snapshot = snapshot(Instrument('Bybit', 'spot', 'ETHUSDT', 'ETH', 'USDT'))
    result = flow.ingest(next_snapshot, chart_key=key)
    assert result.reset and result.added_trades == 1
    assert flow.engine.snapshot().delta == 2
    assert len(flow.seen_ids) == len(flow.history) == 1


def test_book_is_bounded_rest_history_has_explicit_age_and_no_false_streaming():
    flow, first = adapter(), snapshot()
    flow.ingest(first)
    text = flow.status_text()
    assert 'DOM REST snapshot' in text and '15.0s' in text and 'không phải DOM streaming' in text
    for _ in range(3):
        flow.ingest(first, trades=[])
    assert len(flow.history) == 1
    old = replace(first, book=dict(first.book, timestamp_ms=NOW - 1, bids=[[98, 600]]))
    assert not flow.ingest(old, trades=[]).book_updated
    assert flow.engine.snapshot().book[0].price == 99
    unavailable = replace(first, book={'available': False})
    flow.ingest(unavailable, trades=[])
    assert flow.last_book_ms == NOW and flow.engine.snapshot().book
    assert 'timestamp tương lai' in flow.status_text(now_ms=NOW - 10000)


def test_seen_ids_and_trade_batch_have_hard_bounds():
    flow = adapter()
    flow.MAX_SEEN, flow.MAX_BATCH = 5, 10
    first = snapshot(trades=[trade(str(index)) for index in range(20)])
    result = flow.ingest(first)
    assert result.added_trades == 10 and result.ignored_trades == 10
    assert len(flow.seen_ids) == len(flow._seen_order) == 5
    assert 'Batch bị giới hạn' in flow.status_text()


def test_invalid_fills_are_skipped_without_inventing_aggressor_or_volume():
    flow = adapter()
    rows = [trade('bad1', size=-1), trade('bad2', size=float('inf')), trade('bad3', price=float('nan')),
            trade('bad4', time_ms=0), trade('bad5', side='Long'),
            trade('bad6', unit='contracts'), trade('good', side='Unknown')]
    result = flow.ingest(snapshot(trades=rows))
    assert result.added_trades == 1 and result.ignored_trades == 6
    assert flow.engine.snapshot().delta == 0
    assert flow.engine.snapshot().big_trades[0].side == 'Unknown'


def test_idless_rows_deduplicate_conservatively_and_expose_limitation():
    flow, first = adapter(), snapshot(trades=[trade(None), trade(None)])
    assert flow.ingest(first).added_trades == 1
    assert 'thiếu ID' in flow.status_text()
    assert flow.ingest(replace(first, fetched_at_ms=NOW + 15000)).added_trades == 0


def test_contract_units_and_book_fallback_quote_remain_exact():
    item = Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT')
    flow, first = adapter(), snapshot(item, quote={})
    flow.ingest(first)
    assert flow.quote == {'bid': 99, 'ask': 101}
    assert 'đơn vị contracts' in flow.status_text()
    assert flow.engine.snapshot().delta == 2
    assert not flow.ingest(replace(first, book=dict(first.book, snapshot_only=False)), trades=[]).book_updated


def test_broker_data_cannot_enter_public_flow_adapter():
    flow = adapter()
    with pytest.raises(ValueError, match='public'):
        flow.ingest(snapshot(Instrument('MT5', 'broker', 'EURUSD#', 'UNKNOWN', 'UNKNOWN')))


@pytest.fixture(scope='module')
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def dock(qt_app):
    class Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.cfg = {'symbol': 'EURUSD', 'orderflow': {'min_trade': 0}}
            self.chart = QtWidgets.QWidget(self)
            self.cb_sym, self.cb_acc = QtWidgets.QComboBox(), QtWidgets.QComboBox()
            self.broker = SimpleNamespace(demo=True)
            self.logs = []

        def primary(self):
            return None

        def log(self, message):
            self.logs.append(message)

    window = Window()
    view = OrderFlowDock(window, SimpleNamespace(save_cfg=lambda cfg: None))
    yield view
    view.shutdown()
    view.deleteLater()
    window.close()
    qt_app.processEvents()


def page(first=None, identity='page-a'):
    first = first or snapshot()
    return SimpleNamespace(id=identity, instrument=first.instrument, bundle={'snapshot': first}, closed=False,
                           stream_status={'state': 'connected', 'streaming': True})


def test_dock_adds_source_without_changing_existing_indices_and_never_starts_broker_feed(dock):
    assert dock.source.count() == 4
    assert 'DEMO' in dock.source.itemText(0)
    assert 'MT5' in dock.source.itemText(1)
    assert 'Rithmic' in dock.source.itemText(2)
    assert dock.source.itemText(3) == 'Chart crypto đang xem'
    active = page()
    dock.market_updated(active)
    assert not dock.engine.snapshot().candles
    dock.source.setCurrentIndex(3)
    dock.start_feed()
    assert dock._running and dock.worker is None and dock.demo is None
    assert dock.engine.snapshot().delta == 2
    assert 'Bybit' in dock.status.text() and 'DOM REST' in dock.status.text()
    dock.market_updated(active, [trade('2', size=3, stream_kind='native')])
    assert dock.engine.snapshot().delta == 5
    dock.stop_feed()
    dock.market_updated(active, [trade('3', size=10, stream_kind='native')])
    assert dock.engine.snapshot().delta == 5


@pytest.mark.parametrize('target', [None, 'MT5'])
def test_leaving_crypto_for_primary_or_research_mt5_stops_and_clears(dock, target):
    dock.source.setCurrentIndex(3)
    dock.market_updated(page())
    dock.start_feed()
    assert dock.engine.snapshot().candles
    if target:
        target = page(snapshot(Instrument('MT5', 'broker', 'EURUSD', 'UNKNOWN', 'UNKNOWN')))
    dock.market_updated(target)
    assert not dock._running
    assert not dock.engine.snapshot().candles and not dock.engine.snapshot().book
    assert not dock.history and not dock._public_flow.seen_ids
    assert 'Đã dừng' in dock.status.text()


def test_changing_crypto_chart_follows_new_symbol_with_fresh_session(dock):
    dock.source.setCurrentIndex(3)
    dock.market_updated(page())
    dock.start_feed()
    next_page = page(snapshot(Instrument('Binance', 'spot', 'ETHUSDT', 'ETH', 'USDT'),
                              trades=[trade(size=7)]), identity='page-b')
    dock.market_updated(next_page)
    assert dock._running and dock.engine.snapshot().delta == 7
    assert dock.active_symbol == 'ETHUSDT'
    assert len(dock.history) == 1 and len(dock._public_flow.seen_ids) == 1
    # Changing an inactive primary MT5 selector does not reset the crypto chart.
    dock.app.cb_sym.addItem('USDJPY')
    assert dock._running and dock.engine.snapshot().delta == 7


def test_malformed_chart_bundle_stops_cleanly_without_retaining_previous_symbol_data(dock):
    active = page()
    dock.source.setCurrentIndex(3)
    dock.market_updated(active)
    dock.start_feed()
    active.bundle = {'invalid': True}
    dock.market_updated(active)
    assert not dock._running and not dock.engine.snapshot().candles
    assert 'không hợp lệ' in dock.status.text()
