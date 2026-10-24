import copy
import sys
import time

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from aimt_market.data import Instrument
from aimt_market.execution import validate_ai_scope
from aimt_orderflow.application import create_window
from original import AI_MT_Trading as legacy


def trusted_pair():
    now = int(time.time() * 1000)
    state = {'source': 'MT5', 'market': 'broker', 'mode': 'demo', 'symbol': 'XAUUSD',
        'timeframe': 'M5', 'analysis_bar_id': 123, 'analysis_generation': 4,
        'account_names': ['A'], 'request_ids': ['nonce'], 'live_received_ms': now}
    subject = {key: value for key, value in state.items() if key not in ('request_ids', 'live_received_ms')}
    subject.update(chart_id='mt5-execution', execution_eligible=True, request_id='nonce', requested_at_ms=now)
    return {'_aimt_subject_valid': True, '_aimt_subject': subject}, state


@pytest.mark.parametrize('key,value', [
    ('source', 'Binance'), ('market', 'perpetual'), ('mode', 'live'), ('symbol', 'BTCUSDT'),
    ('timeframe', 'M1'), ('analysis_bar_id', 124), ('analysis_generation', 5),
    ('account_names', ['B']), ('chart_id', 'research'), ('request_id', 'replayed'),
    ('execution_eligible', False), ('requested_at_ms', 0)])
def test_execution_scope_rejects_cross_venue_stale_replayed_or_other_account_results(key, value):
    ai, state = trusted_pair()
    assert validate_ai_scope(ai, state)[0]
    ai['_aimt_subject'][key] = value
    assert not validate_ai_scope(ai, state)[0]


def test_scope_requires_current_quote_enabled_account_and_verified_metadata():
    ai, state = trusted_pair()
    assert not validate_ai_scope(ai, state, account={'name': 'A', 'enabled': False})[0]
    assert not validate_ai_scope(ai, state, account={'name': 'B', 'enabled': True})[0]
    state['live_received_ms'] -= 91000
    assert not validate_ai_scope(ai, state)[0]
    ai['_aimt_subject_valid'] = False
    assert not validate_ai_scope(ai, state)[0]


@pytest.fixture(scope='module')
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    old_hook = sys.excepthook
    monkeypatch.setattr(legacy, 'CFG_PATH', str(tmp_path / 'config.json'))
    load = legacy.load_cfg

    def configuration():
        cfg = load()
        cfg.update(force_demo=True, use_ai=False, news_feeds=[], auto_trade=False)
        return cfg

    monkeypatch.setattr(legacy, 'load_cfg', configuration)
    monkeypatch.setattr(legacy, 'fetch_news', lambda _: [])
    view = create_window(legacy)
    view.market_workspace.timer.stop()
    view.market_workspace.live_timer.stop()
    view.show()
    view.activateWindow()
    qt_app.processEvents()
    wait_for(qt_app, lambda: view.last_live is not None)
    yield view
    view.close()
    view.feed.join(timeout=2)
    view.copy.join(timeout=2)
    qt_app.processEvents()
    sys.excepthook = old_hook


def wait_for(app, predicate, timeout=8):
    until = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < until
        app.processEvents()
        time.sleep(.01)
    app.processEvents()


def key(char, *, repeat=False):
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, getattr(QtCore.Qt.Key, 'Key_' + char.upper()),
                          QtCore.Qt.KeyboardModifier.NoModifier, char.lower(), repeat)


def public_page(window):
    return window.market_workspace.add_chart(Instrument('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'), load=False)


def test_checkbox_controls_immediate_hotkeys_q_confirmation_typing_and_repeat(window, qt_app, monkeypatch):
    commands = []
    monkeypatch.setattr(window, 'exec_command', commands.append)
    window.cfg['hotkeys'] = True
    window.b_hk.setFocus()
    qt_app.processEvents()
    assert window.confirm_hotkeys.isChecked()
    assert window.eventFilter(window, key('B'))
    assert window.armed and commands == []
    assert window.eventFilter(window, key('Q'))
    assert commands == ['BUY'] and window.armed is None
    window.confirm_hotkeys.setChecked(False)
    assert window.eventFilter(window, key('S'))
    assert commands == ['BUY', 'SELL']
    assert window.eventFilter(window, key('S', repeat=True))
    assert commands == ['BUY', 'SELL']
    window.cmd.setFocus()
    qt_app.processEvents()
    assert not window.eventFilter(window, key('B'))
    assert commands == ['BUY', 'SELL']
    window.b_hk.setFocus()
    window.confirm_hotkeys.setChecked(True)
    window.arm('BUY')
    public_page(window)
    assert window.armed is None
    window.arm('BUY')
    assert window.armed is None


def test_research_tab_blocks_all_new_broker_entries_and_reverse_but_preserves_close(window):
    broker, account = window.broker, window.primary()
    with broker.session(account):
        assert broker.order('XAUUSD', 'BUY', .01, 0, 0)[0]
        assert len(broker.positions('XAUUSD')) == 1
    public_page(window)
    assert not window._entries_allowed(notify=False)
    assert not window.b_buy.isEnabled() and not window.b_sell.isEnabled()
    panel_buttons = {button.text(): button for button in window.panel.findChildren(QtWidgets.QPushButton)}
    assert not panel_buttons['Buy Stop'].isEnabled() and not panel_buttons['Reverse'].isEnabled()
    assert panel_buttons['Close ALL'].isEnabled() and panel_buttons['BE'].isEnabled()
    with broker.session(account):
        assert not broker.order('XAUUSD', 'SELL', .01, 0, 0)[0]
        assert not broker.pending('XAUUSD', 'BUY', .01, 1, 0, 0)[0]
        assert 'bị chặn' in window.ops.reverse_all('XAUUSD')
        assert len(broker.positions('XAUUSD')) == 1
        window.ops.close_all('XAUUSD')
        assert not broker.positions('XAUUSD')
    window.market_workspace.tabs.setCurrentWidget(window.market_workspace.primary_page)
    assert window.b_buy.isEnabled() and panel_buttons['Reverse'].isEnabled()


def test_analysis_generation_prevents_old_ai_levels_and_cross_symbol_execution(window, qt_app, monkeypatch):
    window.start_analysis(True)
    wait_for(qt_app, lambda: window.payload is not None and not window.busy)
    payload = window.payload
    assert len(payload['results']) == 8
    assert payload['tf'] == window.cfg['tf'] and window._analysis_allowed(payload)
    scope = window._scope_snapshot()
    subject = {name: scope[name] for name in ('source', 'market', 'mode', 'symbol', 'timeframe',
        'analysis_bar_id', 'analysis_generation', 'account_names', 'chart_id')}
    window._aimt_latest_execution_request_id = 'current-request'
    subject.update(request_id='current-request', requested_at_ms=int(time.time() * 1000), execution_eligible=True)
    ai = {'signal': 'BUY', 'confidence': 50, 'summary': 'Xu hướng tăng.',
          '_aimt_subject': subject, '_aimt_subject_valid': True}
    assert window._ai_allowed(ai)
    other = dict(window.primary(), name='Other account', symbol='EURUSD')
    plan = window.pack_plan(payload, 'BUY', 4)['plans']['SCALP']
    assert window.trade_account(other, 'BUY', payload, plan, True) is None
    window.cfg['pa']['use_ai_levels'] = True
    window.ai = None
    routed = []
    monkeypatch.setattr(window, 'panel_run', lambda *args, **kwargs: routed.append(args))
    window.panel_order('BUY')
    assert not routed
    public_page(window)
    assert not window._ai_allowed(ai)
    assert not window._analysis_allowed(payload)
    assert not window.apply_external_ai(ai)


def test_old_symbol_live_update_does_not_run_blocking_terminal_rpc_on_gui(window, monkeypatch):
    previous = window.last_live
    stale = dict(previous, sym='PREVIOUS_SYMBOL')

    def forbidden(*_args, **_kwargs):
        raise AssertionError('GUI must not resolve symbols through broker IPC')

    monkeypatch.setattr(window.broker, 'session', forbidden)
    window.on_live(stale)
    assert window.last_live is previous


def test_research_chart_card_survives_primary_refresh_and_public_image_has_explicit_subject(window, monkeypatch):
    page = public_page(window)
    assert 'CHƯA CÓ DỮ LIỆU' in window.card.text()
    window.render_card()
    assert 'CHƯA CÓ DỮ LIỆU' in window.card.text()
    snapshot = type('PublicSnapshot', (), {'instrument': page.instrument, 'timeframe': 'M1'})()
    page.bundle = {'snapshot': snapshot, 'original': {'overlay': {}}}
    monkeypatch.setattr(legacy, 'render_png', lambda *_args, **_kwargs: b'research-chart')
    assert window.chat_png() == b'research-chart'
    assert window._ai_image_subject['symbol'] == 'BTCUSDT'
    assert not window._ai_image_subject['execution_eligible']
    assert window._ai_image_subject['mode'] == 'live'
    page.bundle = None


def test_unchanged_primary_selection_preserves_generation_and_stale_primary_never_labels_quant_current(window):
    generation = window._analysis_generation
    window.market_selection_changed()
    assert window._analysis_generation == generation
    with window._scope_lock:
        window._execution_state['live_received_ms'] = int(time.time() * 1000) - 100000
    window.market_workspace._primary_quant = {'signal': 'BUY', 'summary': 'Old quant signal'}
    window.market_workspace._primary_ai = {'signal': 'BUY', 'conclusion': 'Old AI conclusion',
        'received_at_ms': int(time.time() * 1000) - 200000}
    window.render_card()
    assert 'Định lượng:</b> DỮ LIỆU CŨ' in window.card.text()
    assert 'AI · DỮ LIỆU CŨ' in window.card.text()
    assert 'Định lượng:</b> BUY' not in window.card.text()
