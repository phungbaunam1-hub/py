import copy
import math
from types import SimpleNamespace

import numpy as np

from original import AI_MT_Trading as legacy
from aimt_market.analysis import bars_from_candles, original_studies


def rows(count=130):
    return [{'time': n * 60, 'open': 100 + n * .1, 'high': 101 + n * .1,
             'low': 99 + n * .1, 'close': 100.2 + n * .1, 'volume': 100,
             'closed': True} for n in range(count)]


def test_all_eight_original_studies_ignore_forming_unknown_and_invalid_bars():
    source = rows()
    expected = original_studies(legacy, source)
    bad = [dict(source[-1], time=999999, closed=False, high=1e9, close=1e9),
           dict(source[-1], time=999998, closed=1),
           dict(source[-1], time=999997, close=math.nan),
           dict(source[-1], time=999996, high=1),
           dict(source[-1], time=999995, low=-1)]
    unknown = dict(source[-1], time=999994)
    del unknown['closed']
    actual = original_studies(legacy, source + bad + [unknown])
    assert len(actual['results']) == 8
    assert actual['serialized'] == expected['serialized']
    assert actual['overlay'] == expected['overlay']
    assert any('closed' in value for value in actual['data_quality'])
    assert any('OHLC' in value for value in actual['data_quality'])


def test_closed_requirement_is_explicit_and_numpy_boolean_is_supported():
    source = rows(119)
    unknown = dict(source[-1], time=9999)
    del unknown['closed']
    assert not original_studies(legacy, source + [unknown])['results']
    source.append(dict(source[-1], time=9999, closed=np.bool_(True)))
    assert len(original_studies(legacy, source)['results']) == 8


def test_bar_conversion_sorts_deduplicates_and_does_not_mutate_inputs():
    source = list(reversed(rows()))
    source.extend([dict(source[-1], close=100.3), dict(source[-1], time=9999, volume=math.inf),
                   dict(source[-1], time=math.inf), {'time': 'bad'}, None])
    original = copy.deepcopy(source)
    bars = bars_from_candles(legacy, source)
    assert bars.n == 131
    assert np.all(np.diff(bars.t) > 0)
    assert bars.c[0] == 100.3
    assert bars.v[-1] == 0
    assert np.all(np.isfinite(bars.c)) and np.all(np.isfinite(bars.v))
    assert source == original


zigzag = legacy.zigzag


def _swing_reader(bars, atr):
    result = legacy.Res('TEST', 'test')
    result.levels = zigzag(bars, 3, 3)
    return result


def test_synthetic_stub_never_confirms_an_additional_swing_or_mutates_legacy_globals():
    source = [{'time': n * 60, 'open': 100, 'high': 101, 'low': 99,
               'close': 100, 'volume': 100, 'closed': True} for n in range(120)]
    source[117].update(high=110, low=100)  # Needs three real right bars; only two exist.
    real_bars = bars_from_candles(legacy, source)
    expected = legacy.zigzag(real_bars, 3, 3)
    stub = dict(source[-1], time=120 * 60, high=100, low=100)
    assert legacy.zigzag(bars_from_candles(legacy, source + [stub]), 3, 3) != expected
    adapter = SimpleNamespace(Bars=legacy.Bars, atr_arr=legacy.atr_arr, Res=legacy.Res,
        zigzag=legacy.zigzag, ANALYZERS=[('TEST', 'test', _swing_reader)])
    actual = original_studies(adapter, source)
    assert actual['results'][0].levels == expected
    assert _swing_reader.__globals__['zigzag'] is legacy.zigzag
