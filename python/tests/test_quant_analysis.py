import copy
import json
import math

import pytest

from aimt_market.quant import analyze_market


def candles(count=200, *, step=0.0, start=100.0, real_volume=True):
    result = []
    for index in range(count):
        price = start + index * step
        previous = start + max(0, index - 1) * step
        result.append({'time': index * 60, 'open': previous, 'high': max(previous, price) + .2,
                       'low': min(previous, price) - .2, 'close': price, 'volume': 100.0,
                       'volume_is_real': real_volume, 'closed': True})
    return result


def method(result, key):
    return next(item for item in result['methods'] if item['key'] == key)


@pytest.mark.parametrize('step,expected', [(1.0, 'BUY'), (-.3, 'SELL'), (0.0, 'SIDEWAYS')])
def test_clear_trend_or_flat_has_explicit_signal_without_probability_claim(step, expected):
    result = analyze_market(candles(step=step))
    assert result['signal'] == expected
    assert -1 <= result['score'] <= 1
    assert 0 <= result['confidence'] <= 80
    assert result['confidence_kind'] == 'heuristic_evidence_agreement_not_probability'
    assert expected in result['summary'] and len(result['summary']) <= 200
    json.dumps(result, allow_nan=False)


def test_forming_bar_cannot_change_any_signal_or_indicator():
    history = candles(step=.2)
    reference = analyze_market(history)
    forming = {'time': 999999, 'open': 100, 'high': 1e9, 'low': 1e-6, 'close': 1e9,
               'volume': 1e12, 'volume_is_real': True, 'closed': False}
    actual = analyze_market(history + [forming])
    assert actual['signal'] == reference['signal']
    assert actual['score'] == reference['score']
    assert actual['metrics'] == reference['metrics']
    assert actual['methods'] == reference['methods']
    assert any('đang hình thành' in text for text in actual['data_quality'])


def test_insufficient_closed_history_stays_insufficient_even_with_book_imbalance():
    data = candles(59)
    result = analyze_market(data, book={'bids': [[99, 1000]], 'asks': [[101, 1]]})
    assert result['signal'] == 'INSUFFICIENT_DATA'
    assert result['confidence'] == 0 and result['methods'] == []
    assert result['metrics']['closed_candle_count'] == 59


def test_closed_flag_is_required_and_nonfinite_zero_price_data_is_rejected():
    rows = candles(60)
    rows[0]['close'] = math.nan
    rows[1].pop('closed')
    rows[2].update(open=0, high=0, low=0, close=0)
    result = analyze_market(rows)
    assert result['signal'] == 'INSUFFICIENT_DATA'
    assert result['metrics']['closed_candle_count'] == 57
    assert any('không hợp lệ' in text for text in result['data_quality'])
    assert any('thiếu trạng thái đóng' in text for text in result['data_quality'])
    json.dumps(result, allow_nan=False)


def test_donchian_uses_previous_window_and_does_not_include_current_high():
    history = candles(80)
    history[-1].update(open=100, high=150, low=99.8, close=105)
    result = analyze_market(history)
    assert result['metrics']['donchian_previous_high20'] == pytest.approx(100.2)
    assert method(result, 'market_structure')['signal'] == 'BUY'
    assert method(result, 'market_structure')['score'] > .6
    # A future unclosed shock is never allowed to contaminate the prior-window calculation.
    history.append({'time': 5000, 'open': 100, 'high': 10000, 'low': 1, 'close': 5000, 'closed': False})
    assert analyze_market(history)['metrics'] == result['metrics']


def test_price_only_or_tick_volume_disables_volume_votes_instead_of_fabricating_real_volume():
    rows = candles(step=.1, real_volume=False)
    for row in rows:
        row['volume'] = 9999999  # Huge quote/tick counts must not acquire real-volume provenance.
    result = analyze_market(rows)
    volume = method(result, 'bar_vwap')
    assert not volume['available'] and not volume['voting']
    assert 'bar_vwap' not in result['metrics']
    assert 'bar_profile_poc' not in result['metrics']
    assert any('volume giao dịch thật' in text for text in result['data_quality'])


def test_bar_vwap_and_profile_are_finite_explicit_approximations_and_share_family():
    rows = candles(step=.1)
    result = analyze_market(rows)
    vwap = method(result, 'bar_vwap')
    profile = method(result, 'bar_volume_profile')
    assert vwap['group'] == profile['group'] == 'volume_reference'
    assert vwap['weight'] + profile['weight'] == pytest.approx(1)
    assert 'xấp xỉ' in vwap['name'] and 'xấp xỉ' in profile['name']
    assert min(row['low'] for row in rows) <= result['metrics']['bar_vwap'] <= max(row['high'] for row in rows)
    assert result['metrics']['bar_profile_value_low70'] <= result['metrics']['bar_profile_poc'] <= result['metrics']['bar_profile_value_high70']


def test_book_imbalance_weighted_mid_and_delta_use_real_supplied_size_and_unknown_reduces_evidence():
    result = analyze_market(candles(), trades=[
        {'price': 100, 'size': 3, 'side': 'Buy'}, {'price': 100, 'size': 1, 'side': 'Sell'},
        {'price': 100, 'size': 6, 'side': 'Unknown'}],
        book={'bids': [[99, 3]], 'asks': [[101, 1]], 'snapshot_only': True})
    metrics = result['metrics']
    assert metrics['orderbook_imbalance'] == pytest.approx(.5)
    assert metrics['microprice'] == pytest.approx(100.5)
    assert metrics['trade_delta'] == 2
    assert metrics['trade_delta_ratio'] == pytest.approx(.2)
    assert metrics['classified_trade_fraction'] == pytest.approx(.4)
    assert method(result, 'microstructure')['available']
    assert any('không phải HFT' in limitation for limitation in method(result, 'microstructure')['limitations'])
    assert any('không phải mô hình' in limitation for limitation in method(result, 'microstructure')['limitations'])


def test_crossed_book_and_unknown_trades_do_not_create_directional_microstructure_vote():
    result = analyze_market(candles(), trades=[{'price': 101, 'size': 500, 'side': 'Unknown'}],
                            book={'bids': [[102, 500]], 'asks': [[101, 1]]})
    micro = method(result, 'microstructure')
    assert not micro['available'] and not micro['voting']
    assert result['metrics']['trade_delta'] == 0
    assert 'microprice' not in result['metrics']
    assert result['signal'] == 'SIDEWAYS'


def test_futures_snapshot_does_not_invent_open_interest_change_and_missing_provenance_cannot_vote():
    data = {'funding_rate': .0001, 'open_interest': 100000, 'mark_price': 101, 'index_price': 100,
            'timestamp_ms': 200 * 60 * 1000, 'source': 'https://example.com/public-feed'}
    result = analyze_market(candles(), derivatives=data)
    assert result['metrics']['funding_rate'] == .0001
    assert result['metrics']['mark_index_basis_bps'] == pytest.approx(100)
    assert 'open_interest_change_pct' not in result['metrics']
    assert method(result, 'derivatives')['available'] and method(result, 'derivatives')['voting']
    del data['timestamp_ms']
    del data['source']
    incomplete = analyze_market(candles(), derivatives=data)
    assert method(incomplete, 'derivatives')['available']
    assert not method(incomplete, 'derivatives')['voting']
    assert any('timestamp/source' in message for message in incomplete['data_quality'])


def test_open_interest_only_is_descriptive_and_missing_futures_is_explicit():
    result = analyze_market(candles(), derivatives={'open_interest': 100, 'source': 'venue'})
    assert method(result, 'derivatives')['available']
    assert not method(result, 'derivatives')['voting']
    unavailable = analyze_market(candles())
    assert not method(unavailable, 'derivatives')['available']
    assert 'open_interest' not in unavailable['metrics']


def test_volatility_and_risk_method_never_votes_direction_and_shock_caps_confidence():
    rows = candles(step=.2)
    rows[-1].update(high=1000, low=1, close=200)
    result = analyze_market(rows)
    assert not method(result, 'volatility_risk')['voting']
    assert result['regime'] == 'shock'
    assert result['confidence'] <= 40
    assert result['metrics']['stop_distance_2atr'] == pytest.approx(result['metrics']['atr14'] * 2)


def test_duplicate_and_unsorted_history_is_normalized_without_mutating_inputs():
    rows = candles(100, step=.1)
    shuffled = list(reversed(rows)) + [copy.deepcopy(rows[0])]
    original = copy.deepcopy(shuffled)
    actual = analyze_market(shuffled)
    reference = analyze_market(rows)
    assert actual['metrics'] == reference['metrics']
    assert actual['signal'] == reference['signal']
    assert shuffled == original
    assert any('timestamp nến trùng' in text for text in actual['data_quality'])


def test_irregular_timestamps_disable_regression_vote_when_gap_sample_is_too_large():
    rows = candles(100, step=.1)
    for index, row in enumerate(rows):
        row['time'] = index * 60 + index // 2 * 60
    result = analyze_market(rows)
    assert result['metrics']['irregular_interval_count'] > 20
    assert not method(result, 'statistical_trend')['voting']


def test_flat_series_has_no_divide_by_zero_and_reports_undefined_variance_ratio():
    rows = candles()
    for row in rows:
        row.update(open=100, high=100, low=100, close=100, volume=0)
    result = analyze_market(rows)
    assert result['signal'] == 'SIDEWAYS'
    assert result['metrics']['atr14'] == 0
    assert result['metrics']['rsi14'] == 50
    assert result['metrics']['variance_ratio_5'] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, 1e308, None, 'bad'])
def test_unusable_inputs_never_emit_nonfinite_json_or_fake_signal(value):
    rows = candles(60)
    for row in rows:
        row['close'] = value
    result = analyze_market(rows, derivatives={'funding_rate': math.inf})
    assert result['signal'] == 'INSUFFICIENT_DATA'
    json.dumps(result, allow_nan=False)


def test_untimestamped_book_and_prints_are_descriptive_not_current_votes():
    result = analyze_market(candles(), trades=[{'price': 100, 'size': 10, 'side': 'Buy'}],
                            book={'bids': [[99, 100]], 'asks': [[101, 1]]})
    assert method(result, 'microstructure')['available']
    assert not method(result, 'microstructure')['voting']
    assert method(result, 'microstructure')['score'] == 0
    assert result['metrics']['trade_delta'] == 10
    assert result['metrics']['fresh_trade_sample_count'] == 0
    assert any('unknown' in row for row in result['data_quality'])


def test_stale_or_future_events_cannot_vote_even_with_a_fresh_enclosing_snapshot():
    stamp = 200 * 60 * 1000
    stale = analyze_market(candles(), now_ms=stamp,
        trades=[{'price': 100, 'size': 100, 'side': 'Buy', 'time_ms': stamp - 90001}],
        book={'bids': [[99, 100]], 'asks': [[101, 1]], 'timestamp_ms': stamp + 30001},
        derivatives={'funding_rate': .01, 'timestamp_ms': stamp, 'source': 'venue',
                     'funding_timestamp_ms': stamp - 90001})
    assert not method(stale, 'microstructure')['voting']
    assert not method(stale, 'derivatives')['voting']
    assert any('future' in row for row in stale['data_quality'])
    assert any('stale' in row for row in stale['data_quality'])
    json.dumps(stale, allow_nan=False)


def test_only_fresh_trade_subset_drives_delta_vote():
    stamp = 200 * 60 * 1000
    result = analyze_market(candles(), now_ms=stamp, trades=[
        {'price': 100, 'size': 10000, 'side': 'Sell', 'time_ms': stamp - 90001},
        {'price': 100, 'size': 2, 'side': 'Buy', 'time_ms': stamp - 1000}])
    assert result['metrics']['trade_delta'] < 0  # Descriptive full sample remains disclosed.
    assert result['metrics']['fresh_trade_delta'] == 2
    assert result['metrics']['fresh_trade_sample_count'] == 1
    assert method(result, 'microstructure')['signal'] == 'BUY'


def test_timestamped_fresh_book_uses_snapshot_receipt_time_not_machine_clock():
    stamp = 1234567890000
    result = analyze_market(candles(), now_ms=stamp,
        book={'bids': [[99, 100]], 'asks': [[101, 1]], 'timestamp_ms': stamp - 100})
    assert method(result, 'microstructure')['voting']
    assert result['metrics']['orderbook_age_seconds'] == pytest.approx(.1)
    assert result['metrics']['evidence_reference_time_ms'] == stamp


def test_last_settled_funding_is_historical_but_scheduled_event_is_not_observation_time():
    stamp = 200 * 60 * 1000
    common = {'funding_rate': .01, 'timestamp_ms': stamp, 'source': 'venue'}
    settled = analyze_market(candles(), derivatives=dict(common,
        funding_timestamp_kind='settlement', funding_rate_scope='last_settled_rate',
        funding_timestamp_ms=stamp))
    assert not method(settled, 'derivatives')['voting']
    scheduled = analyze_market(candles(), derivatives=dict(common,
        funding_timestamp_kind='venue_funding_time', funding_timestamp_ms=stamp + 8 * 3600000))
    assert method(scheduled, 'derivatives')['voting']
    assert scheduled['metrics']['funding_age_seconds'] == 0


def test_stale_oi_change_does_not_vote_when_common_futures_timestamp_is_fresh():
    stamp = 200 * 60 * 1000
    result = analyze_market(candles(step=.1), derivatives={'source': 'venue', 'timestamp_ms': stamp,
        'open_interest': 100, 'open_interest_change_pct': 20,
        'open_interest_timestamp_ms': stamp - 360001})
    assert method(result, 'derivatives')['available']
    assert not method(result, 'derivatives')['voting']
    assert any('OI change' in row for row in result['data_quality'])


def test_numeric_closed_flag_is_not_an_explicit_boolean_confirmation():
    rows = candles(60)
    rows[0]['closed'] = 1
    result = analyze_market(rows)
    assert result['metrics']['closed_candle_count'] == 59
