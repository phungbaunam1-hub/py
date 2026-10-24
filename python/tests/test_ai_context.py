from datetime import datetime, timezone
import gc
import json
from types import SimpleNamespace
import weakref
import time

import pytest
from PyQt6 import QtCore, QtWidgets

from aimt_market.ai import (BEGIN, END, SUBJECT, SUBJECT_END, build_ai_context,
    context_text, install_ai_hooks, parse_conclusion, publish_orderflow_context, sanitize, subject_prompt)


def decoded_context(messages):
    text = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END, start)
    return json.loads(text[start:end])


class CheckBox:
    def __init__(self): self.checked, self.enabled, self.label = False, True, 'optional'
    def isChecked(self): return self.checked
    def isEnabled(self): return self.enabled
    def text(self): return self.label
    def setChecked(self, value): self.checked = value
    def setEnabled(self, value): self.enabled = value
    def setText(self, value): self.label = value


class Window:
    def __init__(self):
        self.cfg = {'symbol': 'XAUUSD', 'tf': 'M5', 'risk_mode': 'Cân bằng', 'risk_pct': 1,
            'auto_trade': False, 'gemini_key': 'MY_PRIVATE_API_VALUE', 'pa': {'sl_pts': 1000},
            'llm': {'openai': {'key': 'MY_OPENAI_SECRET', 'base': 'https://api.example/v1?token=ABC'}},
            'accounts': [{'name': 'A', 'enabled': True, 'password': 'PASSWORD_A'},
                         {'name': 'B', 'enabled': True, 'password': 'PASSWORD_B', 'symbol': 'EURUSD'}]}
        self.broker = SimpleNamespace(demo=True)
        self.payload = {'sym': 'XAUUSD', 'bar_id': 1234, 'bid': 2000, 'ask': 2001,
                        'atr': 3, 'results': [], 'bars': None}
        self.last_live = {'name': 'A', 'sym': 'XAUUSD', 'bid': 2000, 'ask': 2001,
            'pos': [{'ticket': 1, 'symbol': 'XAUUSD', 'type': 'BUY', 'volume': .1,
                     'open': 1990, 'sl': 1985, 'tp': 2010, 'profit': 1}],
            'orders': [], 'acc': {'balance': 1000, 'equity': 1001}}
        self.snaps = {'B': {'acc': {'balance': 2000, 'equity': 2002},
            'positions': [{'ticket': 2, 'symbol': 'EURUSD', 'type': 'SELL', 'volume': .2,
                           'open': 1.2, 'sl': 1.3, 'tp': 1.1, 'profit': 2}]}}
        self.plan = {'side': 'WAIT', 'score': 0, 'plans': {}}
        self.news = [(datetime(2026, 10, 23, tzinfo=timezone.utc),
                      'Headline: MY_PRIVATE_API_VALUE https://name:PASSWORD_A@example.com/news?api_key=MY_OPENAI_SECRET', 0)]
        self.ai, self.halt, self.infos = None, {}, {}
        self.chat = SimpleNamespace(c_ctx=CheckBox())
        self.market_workspace = SimpleNamespace(ai_snapshot=lambda: {
            'active_chart_id': 'btc', 'charts': [{'chart_id': 'btc', 'source': 'Binance',
                'symbol': 'BTCUSDT', 'market': 'perpetual', 'mode': 'public', 'timeframe': 'M1',
                'candles': [{'time': n * 60, 'open': 100, 'high': 102, 'low': 99,
                             'close': 101, 'volume': 30} for n in range(350)],
                'trades': [], 'book': {'bids': [], 'asks': []},
                'derivatives': {'funding_rate': .0001, 'open_interest': 200,
                                'source': 'Binance futures', 'timestamp_ms': 1234}}]})
        self.conclusions = []

    def chat_context(self): return 'old context'
    def ai_conclusion_changed(self, data): self.conclusions.append(data)


def legacy_module(captured, reply=None):
    def provider(cfg, name, system, messages, png=None, json_mode=False, log=None):
        context = decoded_context(messages)
        captured.append({'system': system, 'messages': messages, 'png': png,
                         'json_mode': json_mode, 'context': context})
        if reply is not None:
            return reply(context)
        subject = context['request_subject']
        return json.dumps({'signal': 'SIDEWAYS', 'confidence': 50, 'summary': 'Giá đang đi ngang.',
            'request_id': subject['request_id'], 'symbol': subject.get('symbol'),
            'source': subject.get('source')})

    def parse(text):
        data = json.loads(text)
        signal = str(data.get('signal', 'WAIT')).upper()
        data['signal'] = signal if signal in ('BUY', 'SELL', 'WAIT') else 'WAIT'
        try: data['confidence'] = max(0, min(100, float(data.get('confidence', 0))))
        except (TypeError, ValueError): data['confidence'] = 0
        return data

    return SimpleNamespace(llm_call=provider, build_prompt=lambda *_: 'old prompt', parse_ai=parse)


def test_context_includes_all_enabled_accounts_risk_and_derivatives_without_credentials():
    window = Window()
    context = build_ai_context(window)
    assert context['execution_subject']['symbol'] == 'XAUUSD'
    assert [a['name'] for a in context['accounts']] == ['A', 'B']
    assert context['accounts'][0]['positions']['items'][0]['ticket'] == 1
    assert context['accounts'][1]['positions']['items'][0]['ticket'] == 2
    assert context['risk']['risk_pct'] == 1
    chart = context['market_workspace']['charts']['items'][0]
    assert chart['candles']['included'] == 300 and chart['candles']['omitted'] == 50
    assert chart['derivatives']['source'] == 'Binance futures'
    encoded = context_text(context)
    for secret in ('MY_PRIVATE_API_VALUE', 'MY_OPENAI_SECRET', 'PASSWORD_A', 'PASSWORD_B', 'name:'):
        assert secret not in encoded
    assert '[REDACTED]' in encoded
    assert context['transport']['serialized_bytes_utf8'] > context['transport']['serialized_characters']


def test_all_llm_routes_receive_fresh_single_context_even_if_chat_checkbox_is_false():
    captured, window = [], Window()
    module = legacy_module(captured)
    original = module.llm_call
    uninstall = install_ai_hooks(module, window)
    try:
        assert window.chat.c_ctx.checked and not window.chat.c_ctx.enabled
        for purpose in ('chat', 'manage positions', 'entry', 'automatic analysis'):
            module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': purpose}], png=b'image')
        assert len(captured) == 4
        assert all(c['messages'][-1]['content'].count(BEGIN) == 1 for c in captured)
        assert all(len(c['context']['accounts']) == 2 for c in captured)
        assert all(c['png'] == b'image' for c in captured)
        assert all('180 từ' in c['system'] for c in captured)
        window.last_live['bid'] = 2010
        window.payload['bid'] = 2010
        module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': window.chat_context() + '\nquestion'}])
        assert captured[-1]['context']['execution_market']['quote']['bid'] == 2010
        assert captured[-1]['messages'][-1]['content'].count(BEGIN) == 1
        assert captured[-1]['context']['request_subject']['symbol'] == 'BTCUSDT'
        assert not captured[-1]['context']['request_subject']['execution_eligible']
    finally:
        uninstall()
    assert module.llm_call is original
    assert window.chat_context() == 'old context'
    assert not window.chat.c_ctx.checked and window.chat.c_ctx.enabled


def test_execution_signal_keeps_explicit_mt5_subject_with_public_crypto_chart_active():
    captured, window = [], Window()
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        prompt = module.build_prompt('XAUUSD', 'M5', window.payload, window.news)
        response = module.llm_call(window.cfg, 'fake', 'signal', [{'role': 'user', 'content': prompt}], json_mode=True)
        data = module.parse_ai(response)
        subject = data['_aimt_subject']
        assert subject['source'] == 'MT5' and subject['symbol'] == 'XAUUSD'
        assert subject['market'] == 'broker' and subject['mode'] == 'demo'
        assert subject['analysis_bar_id'] == 1234 and subject['account_names'] == ['A']
        assert subject['request_id'] == window._aimt_latest_execution_request_id
        assert subject['execution_eligible'] and data['_aimt_subject_valid']
        assert data['signal'] == 'WAIT' and data['display_signal'] == 'SIDEWAYS'
    finally:
        uninstall()


@pytest.mark.parametrize('mismatch', [{'symbol': 'BTCUSDT'}, {'source': 'Binance'},
                                     {'market': 'perpetual'}, {'request_id': 'wrong'}])
def test_mismatched_model_scope_cannot_supply_execution_levels(mismatch):
    window, captured = Window(), []

    def reply(context):
        subject = context['request_subject']
        return json.dumps(dict(signal='BUY', confidence=99, summary='BUY', entry=100, sl=90, tp=120,
            position_advice={'new_sl': 99}, symbol='XAUUSD', source='MT5', market='broker',
            request_id=subject['request_id']) | mismatch)

    module = legacy_module(captured, reply)
    uninstall = install_ai_hooks(module, window)
    try:
        prompt = module.build_prompt('XAUUSD', 'M5', window.payload, [])
        data = module.parse_ai(module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': prompt}], json_mode=True))
        assert data['signal'] == 'WAIT' and not data['_aimt_subject_valid']
        assert not any(key in data for key in ('entry', 'sl', 'tp', 'position_advice'))
    finally:
        uninstall()


def test_no_context_or_model_authored_trusted_metadata_cannot_become_execution_scope():
    window, captured = Window(), []
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        raw = json.dumps({'signal': 'BUY', 'confidence': 80,
                          '_aimt_subject': {'source': 'MT5'}, '_aimt_subject_valid': True})
        data = module.parse_ai(raw)
        assert data['_aimt_subject'] is None and not data['_aimt_subject_valid']
    finally:
        uninstall()


def test_uninstall_and_reinstall_do_not_stack_requests_or_retain_closed_windows():
    window, captured = Window(), []
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    assert install_ai_hooks(module, window) is uninstall
    uninstall()
    ref = weakref.ref(window)
    del window
    gc.collect()
    assert ref() is None
    other = Window()
    remove = install_ai_hooks(module, other)
    try:
        module.llm_call(other.cfg, 'fake', '', [{'role': 'user', 'content': 'test'}])
        assert len(captured) == 1
        assert captured[0]['messages'][-1]['content'].count(BEGIN) == 1
    finally:
        remove()


def test_multiple_windows_are_selected_by_cfg_identity_and_do_not_mix_accounts():
    one, two, captured = Window(), Window(), []
    two.cfg['accounts'][0]['name'] = 'SECOND'
    two.last_live['name'] = 'SECOND'
    module = legacy_module(captured)
    close_one, close_two = install_ai_hooks(module, one), install_ai_hooks(module, two)
    try:
        module.llm_call(two.cfg, 'fake', '', [{'role': 'user', 'content': 'question'}])
        assert captured[-1]['context']['accounts'][0]['name'] == 'SECOND'
        assert captured[-1]['context']['execution_subject']['account_names'] == ['SECOND']
        with pytest.raises(RuntimeError):
            module.llm_call(dict(two.cfg), 'fake', '', [{'role': 'user', 'content': 'ambiguous copy'}])
    finally:
        close_one()
        close_two()


def test_nonfinite_values_and_nested_secrets_cannot_escape_json():
    result = sanitize({'price': float('nan'), 'qty': float('inf'), 'nested': {'password': 'hide',
        'authorization': 'Bearer hide', 'x': 'https://user:pw@site/path?token=abc&symbol=BTC#secret'}})
    text = json.dumps(result, allow_nan=False)
    assert result['price'] is None and result['qty'] is None
    assert all(secret not in text for secret in ('hide', 'user:pw', 'abc', '#secret'))


def test_context_budget_discloses_reduced_market_rows_and_keeps_positions_and_risk():
    window = Window()
    many = [{'chart_id': str(i), 'source': 'OKX', 'symbol': 'BTC-USDT', 'market': 'spot',
        'candles': [{'time': j * 60, 'open': 100, 'high': 102, 'low': 99, 'close': 100,
                     'volume': 100, 'note': 'x' * 120} for j in range(400)],
        'trades': [{'price': 100, 'size': 1, 'side': 'Buy', 'detail': 'y' * 100} for _ in range(400)],
        'book': {'bids': [[100, 1]] * 300, 'asks': [[101, 1]] * 300}} for i in range(8)]
    window.market_workspace = SimpleNamespace(ai_snapshot=lambda: {'active_chart_id': '0', 'charts': many})
    context = build_ai_context(window)
    assert len(context_text(context)) < 180100
    assert context['risk']['risk_pct'] == 1
    assert context['accounts'][0]['positions']['items'][0]['ticket'] == 1
    charts = context['market_workspace']['charts']['items']
    assert charts[0]['candles']['total'] == 400
    assert any(chart['candles']['omitted'] > 0 for chart in charts)
    assert context['transport']['positions_and_risk_not_reduced_for_market_history']
    assert all('omitted' in chart['trades'] for chart in charts)


@pytest.mark.parametrize('text,signal', [
    ('{"signal":"WAIT","summary":"Đi ngang"}', 'SIDEWAYS'),
    ('{"signal":"SELL","summary":"Xu hướng giảm","risks":"Spread"}', 'SELL'),
    ('**Tín hiệu: BUY**\nKết luận: xu hướng tăng.', 'BUY'),
    ('Chưa đủ dữ liệu để kết luận.', 'UNAVAILABLE'),
    ('BUY / SELL / SIDEWAYS là các lựa chọn.', 'UNAVAILABLE')])
def test_conclusion_display_is_explicit_without_inventing_missing_signals(text, signal):
    result = parse_conclusion(text)
    assert result['signal'] == signal
    assert 'entry' not in result and 'confidence' not in result


@pytest.fixture(scope='session')
def ai_qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_ai_web_button_uses_full_context_without_legacy_analysis_and_conclusion_is_queued(ai_qt_app):
    class Web(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.messages = []
            self.button = QtWidgets.QPushButton('① Chép PROMPT', self)
            self.button.clicked.connect(self.copy_prompt)
        def copy_prompt(self): self.messages.append('old')
        def msg(self, text): self.messages.append(text)

    window, captured = Window(), []
    window.payload = None
    window.web = Web()
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        window.web.button.click()
        text = ai_qt_app.clipboard().text()
        assert BEGIN in text and 'BTCUSDT' in text and 'MY_PRIVATE_API_VALUE' not in text
        assert window.web.messages[-1].startswith('Đã chép đầy đủ snapshot')
        context = json.loads(text.split(BEGIN, 1)[1].split(END, 1)[0])
        assert context['request_subject']['symbol'] == 'BTCUSDT'
        assert not context['request_subject']['execution_eligible']
        reply = module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': 'test'}])
        assert not window.conclusions  # Qt delivery is queued, even from the GUI caller.
        ai_qt_app.processEvents()
        assert window.conclusions[-1]['signal'] == 'SIDEWAYS'
        assert 'request_subject' in window.conclusions[-1]
    finally:
        uninstall()
        window.web.close()


class ClipboardWeb(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.button = QtWidgets.QPushButton('① Chép PROMPT', self)
        self.button.clicked.connect(self.copy_prompt)

    def copy_prompt(self):
        pass

    def msg(self, _text):
        pass


def web_context(app, window):
    window.web.button.click()
    text = app.clipboard().text()
    return json.loads(text.split(BEGIN, 1)[1].split(END, 1)[0])


def test_web_execution_resolver_is_bound_to_primary_chart_nonce_and_current_analysis(ai_qt_app):
    window, captured = Window(), []
    window._analysis_generation = 12
    active = {'active_chart_id': 'primary', 'charts': []}
    window.market_workspace = SimpleNamespace(ai_snapshot=lambda: dict(active))
    window.ai_web_subject = lambda: dict(build_ai_context(window)['execution_subject'], chart_id='primary')
    window.web = ClipboardWeb()
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        subject = web_context(ai_qt_app, window)['request_subject']
        reply = json.dumps({'request_id': subject['request_id'], 'source': 'MT5',
                            'symbol': 'XAUUSD', 'signal': 'BUY', 'entry': 2000, 'sl': 1990, 'tp': 2020})
        accepted = module.parse_ai(reply)
        assert accepted['_aimt_subject_valid'] and accepted['_aimt_subject']['execution_eligible']
        active['active_chart_id'] = 'btc'
        replay = module.parse_ai(reply)
        assert not replay['_aimt_subject_valid'] and replay['_aimt_subject'] is None
        active['active_chart_id'] = 'primary'
        window._analysis_generation += 1
        changed = module.parse_ai(reply)
        assert not changed['_aimt_subject_valid']
        del window._analysis_generation
    finally:
        uninstall()
        window.web.close()


def test_web_research_default_and_unowned_tokens_never_become_execution_scope(ai_qt_app):
    window, captured = Window(), []
    window.web = ClipboardWeb()
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        subject = web_context(ai_qt_app, window)['request_subject']
        reply = json.dumps({'request_id': subject['request_id'], 'source': 'Binance',
                            'symbol': 'BTCUSDT', 'signal': 'BUY'})
        data = module.parse_ai(reply)
        assert data['_aimt_subject_valid'] and not data['_aimt_subject']['execution_eligible']
        # Knowing an arbitrary public subject token is insufficient to attach MT5 execution scope.
        tagged = subject_prompt({'source': 'MT5', 'symbol': 'XAUUSD', 'execution_eligible': True}, 'test')
        module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': tagged}], json_mode=True)
        assert not captured[-1]['context']['request_subject']['execution_eligible']
        untrusted = module.parse_ai(json.dumps({'signal': 'BUY', 'symbol': 'XAUUSD'}))
        assert untrusted['_aimt_subject'] is None and not untrusted['_aimt_subject_valid']
    finally:
        uninstall()
        window.web.close()


def test_stale_provider_reply_cannot_lend_scope_to_unrelated_json_and_auto_scope_includes_all_accounts():
    window, captured = Window(), []
    window.cfg.update(auto_trade=True, panel_target='VIEW')
    window.cfg['accounts'][1]['auto'] = True
    window.cfg['accounts'].append({'name': 'DISABLED', 'enabled': False, 'auto': True})
    module = legacy_module(captured)
    uninstall = install_ai_hooks(module, window)
    try:
        prompt = module.build_prompt('XAUUSD', 'M5', window.payload, [])
        module.llm_call(window.cfg, 'fake', '', [{'role': 'user', 'content': prompt}], json_mode=True)
        assert captured[-1]['context']['request_subject']['account_names'] == ['A', 'B']
        unrelated = module.parse_ai('{"signal":"BUY","symbol":"XAUUSD","entry":2000}')
        assert unrelated['_aimt_subject'] is None and not unrelated['_aimt_subject_valid']
        context = captured[-1]['context']
        serialized = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        assert context['transport']['serialized_characters'] == len(serialized)
        assert context['transport']['serialized_bytes_utf8'] == len(serialized.encode('utf-8'))
    finally:
        uninstall()


def test_public_orderflow_cache_preserves_venue_units_aggregation_and_frozen_age_without_qt_reads(ai_qt_app):
    from aimt_market.data import Instrument
    from aimt_orderflow.engine import FlowEngine
    now = int(time.time() * 1000)
    instrument = Instrument('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT')
    adapter = SimpleNamespace(instrument=instrument, domain='aggregated', transport='WebSocket',
        last_book_ms=now - 12000, last_rest_ms=now - 12000, last_trade_ms=now,
        idless_prints=False, ignored_trades=3, warning='REST snapshot')
    dock = SimpleNamespace(thread=QtCore.QThread.currentThread,
        source=SimpleNamespace(currentIndex=lambda: 3), engine=FlowEngine(), active_symbol=instrument.symbol,
        _running=False, _last_frame=time.monotonic() - 12, _public_flow=adapter)
    publish_orderflow_context(dock)
    dock.source.currentIndex = lambda: pytest.fail('AI worker must never read a Qt control')
    dock.engine.snapshot = lambda: pytest.fail('The immutable published snapshot must be used')
    window = Window()
    window.orderflow = dock
    context = build_ai_context(window)['orderflow']
    assert context['source'] == 'OKX' and context['source'] != 'DEMO'
    assert context['market'] == 'perpetual' and context['volume_unit'] == 'contracts'
    assert context['aggregated_approximation'] and context['trade_domain'] == 'aggregated'
    assert context['frozen'] and context['stale'] and context['book_age_seconds'] >= 12
    assert context['book_snapshot_only'] and not context['individual_limit_orders_observed']
