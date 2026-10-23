from types import SimpleNamespace
import threading
from aimt_orderflow.engine import FlowEngine, Level
from aimt_orderflow.feeds import DemoFeed
from aimt_orderflow.mt5_bridge import TickCursor, normalize_book, _read, _release


def row(ms, price=100, volume=50, flags=32):
    return dict(time_msc=ms, time=ms // 1000, last=price, volume=volume,
                volume_real=float(volume), flags=flags, bid=99, ask=100)


def test_overlap_deduplicates_replays_but_preserves_identical_print_occurrences():
    cursor = TickCursor(0)
    a, b = row(1000), row(1001, 101)
    first = cursor.consume([a, a, b], 32, 64)
    assert len(first) == 3
    assert not cursor.consume([a, a, b], 32, 64)
    assert len(cursor.consume([a, a, a, b], 32, 64)) == 1


def test_quote_only_or_zero_volume_cannot_create_trade_and_ambiguous_side_is_unknown():
    cursor = TickCursor(0)
    trades = cursor.consume([row(1, volume=0), row(2, price=0), row(3, flags=0), row(4, flags=96)], 32, 64)
    assert len(trades) == 2
    assert all(t.side == 'Unknown' for t in trades)
    engine = FlowEngine()
    for trade in trades:
        engine.trade(trade)
    assert engine.snapshot().delta == 0


def test_depth_filters_market_entries_and_uses_fractional_volume():
    levels = normalize_book([dict(type=2, price=100, volume=1, volume_dbl=1.25),
        dict(type=1, price=101, volume=5), dict(type=3, price=101, volume=1000)], 2, 1)
    assert levels == (Level('Bid', 100, 1.25), Level('Ask', 101, 5))


def test_demo_feeds_complete_pipeline_with_deterministic_prices_and_signals():
    a, b = DemoFeed('TEST'), DemoFeed('TEST')
    engine = FlowEngine()
    expected = 0
    for i in range(600):
        frame = a.frame(100000 + i * 1000)
        assert frame == b.frame(100000 + i * 1000)
        for trade in frame['trades']:
            engine.trade(trade)
            expected += trade.size if trade.side == 'Buy' else -trade.size
        engine.depth(frame['time_ms'], frame['levels'])
    snap = engine.snapshot()
    assert len(snap.candles) >= 10
    assert snap.big_trades
    assert {x.side for x in snap.book} == {'Bid', 'Ask'}
    assert any('wall' in s.kind for s in snap.signals)
    assert any('sweep' in s.kind for s in snap.signals)
    assert snap.delta == expected == sum(c.delta for c in snap.candles)


def test_read_rpc_reset_releases_and_rebuilds_cursor_and_book(monkeypatch):
    from original import AI_MT_Trading as legacy

    class FakeApi:
        COPY_TICKS_TRADE, TICK_FLAG_BUY, TICK_FLAG_SELL, BOOK_TYPE_BUY, BOOK_TYPE_SELL = 1, 32, 64, 2, 1
        def __init__(self): self.adds, self.releases = [], []
        def market_book_add(self, symbol): self.adds.append(symbol); return True
        def market_book_release(self, symbol): self.releases.append(symbol); return True
        def copy_ticks_range(self, *args): return []
        def market_book_get(self, symbol): return [dict(type=2, price=100, volume=10)]
        def symbol_info_tick(self, symbol): return SimpleNamespace(bid=100, ask=101)

    api = FakeApi()
    monkeypatch.setattr(legacy, 'mt5', api, raising=False)
    owner = SimpleNamespace()
    first = _read(owner, 'TEST', reset=True)
    cursor = owner._orderflow_states['TEST']['cursor']
    assert first['levels'] == (Level('Bid', 100, 10),)
    _read(owner, 'TEST', reset=True)
    assert owner._orderflow_states['TEST']['cursor'] is not cursor
    assert api.adds == ['TEST', 'TEST'] and api.releases == ['TEST']
    _release(owner)
    assert api.releases == ['TEST', 'TEST'] and not owner._orderflow_states


def test_worker_backpressure_and_stop_join_leave_no_queued_market_stream():
    from aimt_orderflow.feeds import BrokerFeed
    first, second = threading.Event(), threading.Event()
    frames = []

    class Emit:
        def __init__(self, callback): self.emit = callback

    def frame(generation, payload):
        frames.append((generation, payload))
        (first if len(frames) == 1 else second).set()

    signals = SimpleNamespace(frame=Emit(frame), status=Emit(lambda *_: None))

    class Broker:
        released = False
        def acquire(self, *_): return True
        def release(self, *_): pass
        def resolve(self, symbol): return symbol
        def orderflow_read(self, *_, **__): return {'trades': []}
        def orderflow_release(self): self.released = True

    broker = Broker()
    worker = BrokerFeed(broker, {'name': 'TEST'}, 'TEST', signals, 7)
    worker.start()
    try:
        assert first.wait(2)
        assert not second.wait(.35)  # The queued frame must be acknowledged before another read.
        worker.acknowledge()
        assert second.wait(2)
    finally:
        worker.stop()
        worker.join(2)
    assert not worker.is_alive() and broker.released
    assert len(frames) == 2 and all(generation == 7 for generation, _ in frames)


def test_stop_uses_existing_rpc_worker_without_logging_back_into_old_account():
    from aimt_orderflow.feeds import BrokerFeed
    ready = threading.Event()
    calls = []

    class Emit:
        def __init__(self, fn): self.emit = fn

    rpc = SimpleNamespace(lock=threading.RLock(), alive=lambda: True,
                          call=lambda op, **_: calls.append(op))

    class Broker:
        def __init__(self): self.tl, self.logins = threading.local(), 0
        def acquire(self, *_): self.logins += 1; self.tl.w = rpc; return True
        def release(self, *_): self.tl.w = None
        def resolve(self, symbol): return symbol
        def orderflow_read(self, *_, **__): return {'trades': []}

    broker = Broker()
    signals = SimpleNamespace(frame=Emit(lambda *_: ready.set()), status=Emit(lambda *_: None))
    worker = BrokerFeed(broker, {'name': 'old account'}, 'TEST', signals, 1)
    worker.start()
    assert ready.wait(2)
    worker.stop()
    worker.join(2)
    assert not worker.is_alive()
    assert broker.logins == 1 and calls == ['orderflow_release']
