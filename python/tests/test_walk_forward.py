import copy
import json
import math

import pytest

from aimt_market.backtest import _normalize, _signals, _simulate, walk_forward_report


def candles(count=360):
    result = []
    for index in range(count):
        price = 100 + .04 * index + 2 * math.sin(index / 8)
        opening = 100 + .04 * index + 2 * math.sin((index - .2) / 8)
        result.append({'time': index * 60, 'open': opening, 'high': max(opening, price) + .3,
                       'low': min(opening, price) - .3, 'close': price, 'closed': True})
    return result


@pytest.mark.parametrize('family', ['trend', 'breakout', 'mean_reversion'])
def test_complete_report_has_ordered_disjoint_train_test_and_flat_fold_boundaries(family):
    report = walk_forward_report(candles(), family)
    assert report['status'] == 'complete'
    assert len(report['windows']) == 3
    assert report['bar_interval_seconds'] == 60
    for window in report['windows']:
        assert window['train_end_time'] < window['test_start_time']
        assert window['train_stop_index_exclusive'] == window['test_start_index']
        assert window['flat_at_fold_end']
        assert window['test_metrics']['entry_count'] == window['test_metrics']['exit_count']
        for trade in window['test_trades']:
            assert window['test_start_time'] <= trade['entry_time'] < trade['exit_time'] <= window['test_end_time']
    assert report['combined_test_metrics']['sharpe_annualized'] is None
    assert report['combined_test_metrics']['sharpe_scaling_factor'] == 1
    json.dumps(report, allow_nan=False)


def test_appending_future_bars_cannot_rewrite_completed_folds_or_chosen_parameters():
    prefix = candles(300)
    initial = walk_forward_report(prefix)
    extended = walk_forward_report(candles(480))
    assert extended['windows'][:len(initial['windows'])] == initial['windows']
    last_time = initial['equity_curve'][-1]['time']
    assert [item for item in extended['equity_curve'] if item['time'] <= last_time] == initial['equity_curve']


def test_test_prices_do_not_select_the_same_folds_parameters_or_training_metrics():
    data = candles(300)
    reference = walk_forward_report(data)
    changed = copy.deepcopy(data)
    for index in range(150, 210):
        for key in ('open', 'high', 'low', 'close'):
            changed[index][key] *= 4 + .02 * (index - 150)
    alternate = walk_forward_report(changed)
    assert alternate['windows'][0]['chosen_parameters'] == reference['windows'][0]['chosen_parameters']
    assert alternate['windows'][0]['train_metrics'] == reference['windows'][0]['train_metrics']
    assert alternate['windows'][0]['test_metrics'] != reference['windows'][0]['test_metrics']


def test_fill_is_next_open_and_scheduled_final_open_not_signal_close_or_intrabar_extremes():
    data = [
        {'time': 0, 'open': 80, 'high': 10000, 'low': 1, 'close': 500, 'closed': True},
        {'time': 60, 'open': 100, 'high': 10000, 'low': 1, 'close': 700, 'closed': True},
        {'time': 120, 'open': 110, 'high': 10000, 'low': 1, 'close': 900, 'closed': True},
    ]
    rows, _ = _normalize(data)
    metrics, curve, _, trades = _simulate(rows, [1, 1, 1], 0, 3, 0, 0)
    trade, = trades
    assert (trade['entry_time'], trade['entry_open']) == (60, 100)
    assert (trade['exit_time'], trade['exit_open']) == (120, 110)
    assert metrics['net_return_pct'] == pytest.approx(10)
    assert metrics['trade_count'] == 1 and metrics['turnover_count'] == 2
    assert [point['position'] for point in curve] == [0, 1, 0]


def test_entry_and_exit_costs_are_both_charged_and_more_cost_cannot_help_fixed_targets():
    data = candles(300)
    rows, _ = _normalize(data)
    targets = _signals(rows, 'trend', {'fast': 8, 'slow': 21})
    free, _, _, _ = _simulate(rows, targets, 150, 270, 0, 0)
    paid, _, _, _ = _simulate(rows, targets, 150, 270, 5, 2)
    expensive, _, _, _ = _simulate(rows, targets, 150, 270, 30, 20)
    assert paid['trade_count'] > 0
    assert paid['turnover_count'] == 2 * paid['trade_count']
    assert paid['fees'] > 0 and paid['slippage'] > 0
    assert expensive['ending_equity'] < paid['ending_equity'] < free['ending_equity']
    assert expensive['gross_return_pct'] == paid['gross_return_pct'] == free['gross_return_pct']


def test_forming_shock_is_ignored_without_changing_completed_results():
    history = candles(300)
    reference = walk_forward_report(history)
    forming = {'time': 999999, 'open': 1, 'high': 1e10, 'low': .0001,
               'close': 1e10, 'closed': False}
    report = walk_forward_report(history + [forming])
    assert report['windows'] == reference['windows']
    assert report['combined_test_metrics'] == reference['combined_test_metrics']
    assert report['equity_curve'] == reference['equity_curve']
    assert any('đang hình thành' in warning for warning in report['warnings'])


def test_less_than_minimum_history_and_bad_closed_data_do_not_claim_out_of_sample_results():
    report = walk_forward_report(candles(299))
    assert report['status'] == 'insufficient'
    assert report['windows'] == [] and report['equity_curve'] == []
    assert report['combined_test_metrics']['trade_count'] == 0
    bad = candles(300)
    bad[0]['close'] = math.nan
    assert walk_forward_report(bad)['status'] == 'insufficient'


def test_flat_market_has_no_fabricated_trades_or_infinite_sharpe():
    data = [{'time': i * 60, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'closed': True}
            for i in range(300)]
    report = walk_forward_report(data)
    combined = report['combined_test_metrics']
    assert combined['trade_count'] == 0
    assert combined['net_return_pct'] == 0 and combined['max_drawdown_pct'] == 0
    assert combined['sharpe_per_bar'] is None
    assert any('Không có giao dịch' in warning for warning in report['warnings'])


def test_irregular_bars_do_not_acquire_an_annualization_factor():
    data = candles(300)
    for index, row in enumerate(data):
        row['time'] += index // 2 * 120
    report = walk_forward_report(data)
    assert report['bar_interval_seconds'] is None
    assert report['combined_test_metrics']['sharpe_annualized'] is None
    assert report['combined_test_metrics']['sharpe_scaling_factor'] == 1


@pytest.mark.parametrize('kwargs', [{'family': 'quantum'}, {'fee_bps': -1}, {'slippage_bps': math.nan},
                                   {'fee_bps': 5000, 'slippage_bps': 5000}, {'train_bars': 10}, {'test_bars': 1}])
def test_invalid_research_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        walk_forward_report(candles(), **kwargs)
