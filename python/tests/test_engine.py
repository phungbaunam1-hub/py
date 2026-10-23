from concurrent.futures import ThreadPoolExecutor
import math
import pytest
from aimt_orderflow.engine import FlowEngine, Level, Settings, Trade


def test_utc_buckets_late_open_close_and_unknown_delta():
    engine = FlowEngine()
    for item in [Trade(20000, 102, 5, 'Buy'), Trade(50000, 105, 3, 'Sell'),
                 Trade(10000, 99, 2, 'Unknown'), Trade(60000, 101, 7, 'Buy')]:
        engine.trade(item)
    snap = engine.snapshot()
    first, second = snap.candles
    assert (first.time_ms, first.open, first.high, first.low, first.close, first.volume, first.delta) == (0, 99, 105, 99, 105, 10, 2)
    assert second.time_ms == 60000
    assert snap.delta == 9
    assert snap.last_ms == 60000


def test_filters_reapply_without_altering_total_volume():
    engine = FlowEngine()
    engine.trade(Trade(0, 100, 39, 'Buy'))
    engine.trade(Trade(1, 101, 40, 'Buy'))
    engine.trade(Trade(2, 102, 200, 'Buy'))
    assert [t.size for t in engine.snapshot().big_trades] == [200, 40]
    engine.update_settings(Settings(min_trade=100))
    assert [t.size for t in engine.snapshot().big_trades] == [200]
    assert engine.snapshot().candles[0].volume == 279


@pytest.mark.parametrize('side,prices', [('Buy', [100, 100.25, 100.5]), ('Sell', [100, 99.75, 99.5])])
def test_sweep_requires_direction_size_levels_and_inclusive_window(side, prices):
    engine = FlowEngine(Settings(sweep_volume=120, sweep_ms=750))
    for ms, price in zip([0, 375, 750], prices):
        engine.trade(Trade(ms, price, 40, side))
    signal, = engine.snapshot().signals
    assert signal.kind == side + ' sweep'
    assert signal.size == 120
    engine.trade(Trade(800, prices[-1] + .25, 50, side))
    assert len(engine.snapshot().signals) == 1


def test_expired_late_repeated_or_mixed_side_prints_cannot_fake_sweep():
    engine = FlowEngine()
    for t in [Trade(0, 100, 100, 'Buy'), Trade(100, 100, 100, 'Buy'),
              Trade(200, 101, 100, 'Sell'), Trade(201, 102, 100, 'Unknown'),
              Trade(2000, 102, 100, 'Buy'), Trade(300, 103, 100, 'Buy')]:
        engine.trade(t)
    assert not engine.snapshot().signals


def test_depth_replacement_zero_delete_stale_rejection_and_wall_threshold():
    engine = FlowEngine(Settings(min_wall=100))
    assert engine.depth(2, [Level('Bid', 99, 100), Level('Ask', 101, 10), Level('Bid', 98, 10), Level('Bid', 98, 0)])
    assert [x.price for x in engine.snapshot().book] == [99, 101]
    assert engine.snapshot().signals[0].kind == 'Bid wall'
    engine.depth(3, [Level('Bid', 99, 110)])
    assert len(engine.snapshot().signals) == 1
    engine.depth(4, [Level('Bid', 99, 140)])
    assert len(engine.snapshot().signals) == 2
    engine.depth(5, [])
    assert not engine.depth(1, [Level('Ask', 100, 1000)])
    assert not engine.snapshot().book


def test_timeframe_reset_does_not_reset_session_delta_or_trade_tape():
    engine = FlowEngine()
    engine.trade(Trade(0, 100, 100, 'Buy'))
    engine.update_settings(Settings(candle_seconds=5))
    assert not engine.snapshot().candles
    assert engine.snapshot().delta == 100
    assert engine.snapshot().big_trades
    engine.trade(Trade(7000, 101, 1, 'Sell'))
    assert engine.snapshot().candles[0].time_ms == 5000
    engine.reset()
    assert engine.snapshot().delta == 0
    assert not engine.snapshot().big_trades


@pytest.mark.parametrize('settings', [Settings(min_trade=math.nan), Settings(min_wall=-1),
    Settings(sweep_volume=math.inf), Settings(sweep_levels=1), Settings(sweep_ms=0), Settings(candle_seconds=0)])
def test_invalid_thresholds_rejected(settings):
    with pytest.raises(ValueError):
        FlowEngine(settings)


@pytest.mark.parametrize('trade', [Trade(0, math.nan, 1), Trade(0, 100, 0),
    Trade(0, 100, math.inf), Trade(0, 100, 1, 'invented')])
def test_invalid_market_data_rejected(trade):
    with pytest.raises(ValueError):
        FlowEngine().trade(trade)


def test_concurrent_callbacks_preserve_volume_and_snapshot_isolation():
    engine = FlowEngine()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: engine.trade(Trade(0, 100, 1, 'Buy')), range(2000)))
    snap = engine.snapshot()
    assert snap.delta == snap.candles[0].volume == 2000
    engine.reset()
    assert snap.candles[0].volume == 2000


def test_session_memory_limits_and_old_trade_drop():
    engine = FlowEngine(Settings(candle_seconds=1))
    for i in range(5000):
        engine.trade(Trade(i * 1000, 100, 100, 'Buy'))
    snap = engine.snapshot()
    assert len(snap.candles) == 600 and len(snap.big_trades) == 4000
    engine.trade(Trade(0, 100, 1e8, 'Buy'))
    assert engine.snapshot().delta == snap.delta
    engine.depth(5000000, [Level('Bid', float(i), 1) for i in range(2000)])
    assert len(engine.snapshot().book) == 1000
    assert engine.snapshot().book[0].price == 1999
