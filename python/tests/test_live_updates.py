import copy
import math

import pytest

from aimt_market.live import update_price_candles


def candle():
    return {'time': 60, 'open': 10., 'high': 11., 'low': 9., 'close': 10.,
            'volume': 99., 'closed': False}


@pytest.mark.parametrize('field,value', [
    ('price', math.nan), ('price', math.inf), ('price', -math.inf), ('price', 0),
    ('price', -1), ('price', True), ('price', 'bad'), ('price', 1e308),
    ('time_ms', math.nan), ('time_ms', math.inf), ('time_ms', -1), ('time_ms', True),
    ('time_ms', 1e20), ('time_ms', 'bad'), ('time_ms', 123.5)])
def test_invalid_price_or_time_is_ignored_without_mutating_input(field, value):
    source = [candle()]
    reference = copy.deepcopy(source)
    trade = {'time_ms': 61000, 'price': 20.}
    trade[field] = value
    assert update_price_candles(source, [trade], 'M1') == reference
    assert source == reference


def test_malformed_prints_do_not_hide_valid_prints_in_the_same_batch():
    result = update_price_candles([candle()], [None, {}, [], {'price': 1},
        {'time_ms': 62000, 'price': 12}, {'time_ms': 61000, 'price': 11}], 'M1')
    assert result[0]['close'] == 12
    assert result[0]['stream_price_time_ms'] == 62000
    assert result[0]['volume'] == 99


def test_late_print_can_extend_range_but_cannot_rewind_the_last_price():
    result = update_price_candles([candle()], [{'time_ms': 65000, 'price': 12}], 'M1')
    result = update_price_candles(result, [{'time_ms': 62000, 'price': 20}], 'M1')
    assert result[0]['high'] == 20
    assert result[0]['close'] == 12
    assert result[0]['stream_price_time_ms'] == 65000


def test_new_bucket_stays_partial_and_does_not_close_previous_candle_or_fill_gaps():
    result = update_price_candles([candle()], [{'time_ms': 240000, 'price': 12}], 'M1')
    assert [row['time'] for row in result] == [60, 240]
    assert all(row['closed'] is False for row in result)
    assert result[-1]['volume'] == 0 and result[-1]['volume_is_real'] is False
    assert result[-1]['volume_complete'] is False
