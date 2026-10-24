"""Instrument and request guards around the preserved MT5 application.

The GUI publishes plain Python state. Worker guards never inspect a Qt widget.
Research charts cannot route broker entries; closing and managing existing orders
remain independent of the selected research chart.
"""
import copy
import html
import math
import threading
import time
import weakref

from .ai import parse_conclusion


def _now_ms():
    return int(time.time() * 1000)


def execution_accounts(cfg, primary_name):
    enabled = [a for a in cfg.get('accounts', ()) if a.get('enabled')]
    if cfg.get('auto_trade'):
        return list(dict.fromkeys(([primary_name] if primary_name else []) +
                                 [a['name'] for a in enabled if a.get('auto')]))
    if cfg.get('panel_target') == 'ALL':
        return [a['name'] for a in enabled]
    return [primary_name] if primary_name else []


def validate_ai_scope(ai, state, *, account=None, now_ms=None):
    """Validate trusted hook metadata, never model-authored instrument claims."""
    now = _now_ms() if now_ms is None else now_ms
    if not isinstance(ai, dict) or not ai.get('_aimt_subject_valid'):
        return False, 'AI thiếu ngữ cảnh yêu cầu đã xác thực.'
    subject = ai.get('_aimt_subject') or {}
    if not subject.get('execution_eligible'):
        return False, 'Nhận định chart nghiên cứu không được dùng cho lệnh MT5.'
    if subject.get('chart_id') != 'mt5-execution':
        return False, 'AI thuộc chart khác.'
    if state.get('closed'):
        return False, 'Cửa sổ đã đóng.'
    for key in ('source', 'market', 'mode', 'symbol', 'timeframe', 'analysis_bar_id', 'analysis_generation'):
        if subject.get(key) is None or subject.get(key) != state.get(key):
            return False, f'Ngữ cảnh AI đã thay đổi ({key}).'
    if set(subject.get('account_names', ())) != set(state.get('account_names', ())):
        return False, 'Phạm vi tài khoản của AI đã thay đổi.'
    valid_ids = {item for item in state.get('request_ids', ()) if item}
    if not subject.get('request_id') or subject['request_id'] not in valid_ids:
        return False, 'AI không khớp mã yêu cầu hiện tại.'
    stamp = subject.get('requested_at_ms')
    if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= 180000:
        return False, 'Kết quả AI đã quá 180 giây hoặc sai thời gian.'
    if not state.get('live_received_ms') or not 0 <= now - state['live_received_ms'] <= 90000:
        return False, 'Giá MT5 thiếu hoặc đã quá 90 giây.'
    if account is not None:
        if not account.get('enabled') or account.get('name') not in subject.get('account_names', ()):
            return False, 'Tài khoản không thuộc yêu cầu AI này.'
    return True, ''


class ExecutionScopeMixin:
    """Mixin placed before the unchanged legacy.Main in the inheritance chain."""

    def _init_execution_scope(self, legacy):
        self._scope_legacy = legacy
        self._scope_lock = threading.RLock()
        self._execution_state = {'primary_active': False, 'closed': False}
        self._analysis_generation = 0
        self._analysis_request = None
        self._scope_ready = False
        self._scope_fingerprint = None
        self._broker_scope_installed = False
        self._market_last_live_ms = 0
        self._resolved_aliases = {}
        self._market_active_chart = 'mt5-execution'

    def _scope_snapshot(self):
        with self._scope_lock:
            state = copy.deepcopy(self._execution_state)
        # Nonces are plain Python attributes, updated before provider calls return.
        state['request_ids'] = [getattr(self, '_aimt_latest_execution_request_id', None),
                                getattr(self, '_aimt_latest_web_request_id', None)]
        return state

    def _publish_execution_scope(self):
        if not self._scope_ready:
            return
        primary = self.primary() or {}
        live, payload = self.last_live or {}, self.payload or {}
        wanted = self.acc_symbol(primary)
        alias_key = (primary.get('name'), wanted, self.cfg.get('tf'))
        with self._scope_lock:
            expected_symbol = self._resolved_aliases.get(alias_key, wanted)
        matching_live = (live.get('name') == primary.get('name') and live.get('tf') == self.cfg.get('tf')
                         and live.get('sym') == expected_symbol)
        workspace = getattr(self, 'market_workspace', None)
        state = {'source': 'MT5', 'market': 'broker', 'mode': 'demo' if self.broker.demo else 'live',
            'chart_id': 'mt5-execution', 'primary_active': not workspace or workspace.execution_allowed(),
            'primary_name': primary.get('name'), 'symbol': live.get('sym') if matching_live else self.acc_symbol(primary),
            'timeframe': self.cfg.get('tf'), 'account_names': execution_accounts(self.cfg, primary.get('name')),
            'analysis_bar_id': payload.get('bar_id'), 'analysis_generation': self._analysis_generation,
            'live_received_ms': self._market_last_live_ms if matching_live else 0,
            'closed': self._execution_state.get('closed', False)}
        with self._scope_lock:
            self._execution_state = state

    def _selection_changed(self, *_):
        if not self._scope_ready:
            return
        primary = self.primary() or {}
        fingerprint = (primary.get('name'), self.acc_symbol(primary), self.cfg.get('tf'),
            tuple((a.get('name'), a.get('enabled'), a.get('auto'), a.get('symbol'), a.get('path'),
                   a.get('login'), a.get('server')) for a in self.cfg.get('accounts', ())))
        if fingerprint != self._scope_fingerprint:
            self._scope_fingerprint = fingerprint
            self._analysis_generation += 1
            self.payload = self.plan = self.ai = self.last_ai = None
            self.armed = None
            self._market_last_live_ms = 0
            self._resolved_aliases = {}
            self._aimt_latest_execution_request_id = self._aimt_latest_web_request_id = None
            workspace = getattr(self, 'market_workspace', None)
            if workspace is not None:
                workspace._primary_ai = None
            self.render_card()
            self._resolve_primary_alias(primary, fingerprint)
        self._publish_execution_scope()
        self._sync_entry_buttons()

    def _resolve_primary_alias(self, primary, fingerprint):
        # Terminal symbol lookup is an IPC operation and must stay off the GUI thread.
        if not primary:
            return
        broker, account = self.broker, dict(primary)
        wanted, timeframe = self.acc_symbol(primary), self.cfg.get('tf')
        reference = weakref.ref(self)

        def resolve():
            try:
                with broker.session(account) as connected:
                    resolved = broker.resolve(wanted) if connected else None
                owner = reference()
                if owner is None or not resolved:
                    return
                with owner._scope_lock:
                    if not owner._execution_state.get('closed') and owner._scope_fingerprint == fingerprint:
                        owner._resolved_aliases[(account.get('name'), wanted, timeframe)] = resolved
            except Exception as error:
                owner = reference()
                if owner is not None and not owner._execution_state.get('closed'):
                    owner.log('Chưa xác nhận được alias symbol MT5: ' + type(error).__name__)

        threading.Thread(target=resolve, daemon=True).start()

    def market_selection_changed(self, *_):
        self.armed = None
        workspace = getattr(self, 'market_workspace', None)
        chart_id = workspace.active_page.id if workspace is not None and workspace.active_page is not None else 'mt5-execution'
        if chart_id != self._market_active_chart:
            self._market_active_chart = chart_id
            self._analysis_generation += 1
            self.ai = self.last_ai = None
            self._aimt_latest_execution_request_id = self._aimt_latest_web_request_id = None
            if workspace is not None:
                workspace._primary_ai = None
        self._publish_execution_scope()
        self._sync_entry_buttons()

    def _sync_entry_buttons(self):
        from PyQt6 import QtWidgets
        allowed = self._entries_allowed(notify=False)
        buttons = [self.b_buy, self.b_sell]
        panel = getattr(self, 'panel', None)
        if panel is not None:
            entry_names = {'BUY', 'SELL', 'Buy Stop', 'Sell Stop', 'Buy Limit', 'Sell Limit', 'Reverse'}
            buttons += [button for button in panel.findChildren(QtWidgets.QPushButton) if button.text() in entry_names]
        for button in buttons:
            if not hasattr(button, '_aimt_primary_tooltip'):
                button._aimt_primary_tooltip = button.toolTip()
            button.setEnabled(allowed)
            button.setToolTip(button._aimt_primary_tooltip if allowed else
                'Chọn tab MT5 · thực thi để mở lệnh; tab này chỉ dùng nghiên cứu thị trường.')

    def _entries_allowed(self, *, notify=True):
        state = self._scope_snapshot()
        allowed = state.get('primary_active') and not state.get('closed')
        if not allowed and notify:
            self.log('Lệnh mới bị chặn: chọn tab MT5 · thực thi và kiểm tra tài khoản/symbol trên thanh công cụ.')
        return bool(allowed)

    def _ai_allowed(self, ai, *, account=None, notify=False):
        valid, reason = validate_ai_scope(ai, self._scope_snapshot(), account=account)
        if not valid and notify:
            self.log('Không dùng AI cho lệnh: ' + reason)
        return valid

    def _analysis_allowed(self, payload):
        state = self._scope_snapshot()
        if not self._entries_allowed() or not payload:
            return False
        valid = (payload.get('_aimt_analysis_generation') == state.get('analysis_generation') and
                 payload.get('_aimt_account_name') == state.get('primary_name') and
                 payload.get('sym') == state.get('symbol') and payload.get('tf') == state.get('timeframe') and
                 payload.get('bar_id') == state.get('analysis_bar_id') and
                 0 <= _now_ms() - payload.get('_aimt_analyzed_at_ms', 0) <= 180000 and
                 0 <= _now_ms() - state.get('live_received_ms', 0) <= 90000)
        if not valid:
            self.log('Phân tích MT5 đã cũ hoặc đổi tài khoản/symbol/khung/chart; bấm Phân tích lại trước khi vào lệnh.')
        return valid

    def _install_broker_scope(self):
        if self._broker_scope_installed or not hasattr(self, 'broker') or not hasattr(self, 'ops'):
            return
        self._broker_scope_installed = True
        owner = weakref.ref(self)
        for name in ('order', 'pending'):
            original = getattr(self.broker, name)

            def guarded(*args, _original=original, **kwargs):
                window = owner()
                if window is None or not window._entries_allowed():
                    return False, 'Đang ở chart nghiên cứu hoặc ứng dụng đã đóng; không gửi lệnh MT5.'
                return _original(*args, **kwargs)

            setattr(self.broker, name, guarded)
        reverse = self.ops.reverse_all

        def guarded_reverse(*args, **kwargs):
            window = owner()
            if window is None or not window._entries_allowed():
                return 'Reverse bị chặn trên chart nghiên cứu; vị thế hiện tại được giữ nguyên.'
            return reverse(*args, **kwargs)

        self.ops.reverse_all = guarded_reverse

    def enabled_accounts(self):
        self._install_broker_scope()
        return super().enabled_accounts()

    def refresh_accounts(self):
        result = super().refresh_accounts()
        self._selection_changed()
        return result

    def start_analysis(self, manual):
        workspace = getattr(self, 'market_workspace', None)
        if workspace is not None and not workspace.execution_allowed():
            if manual:
                workspace.ask_ai()
            return
        if self.busy or not self.enabled_accounts():
            return super().start_analysis(manual)
        self._analysis_generation += 1
        self.ai = self.last_ai = None
        if workspace is not None:
            workspace._primary_ai = None
        self._aimt_latest_execution_request_id = self._aimt_latest_web_request_id = None
        self._publish_execution_scope()
        state = self._scope_snapshot()
        self._analysis_request = {key: state.get(key) for key in
            ('primary_name', 'symbol', 'timeframe', 'analysis_generation')}
        return super().start_analysis(manual)

    def on_local(self, payload):
        request, state = self._analysis_request or {}, self._scope_snapshot()
        if (not self._entries_allowed(notify=False) or any(request.get(key) != state.get(key) for key in
                ('primary_name', 'symbol', 'timeframe', 'analysis_generation')) or payload.get('sym') != state.get('symbol')):
            self.log('Bỏ kết quả phân tích cũ sau khi đổi ngữ cảnh; phân tích lại chart MT5 hiện tại.')
            self.bus.finished.emit()
            return
        payload.update(tf=request['timeframe'], _aimt_analysis_generation=request['analysis_generation'],
                       _aimt_account_name=request['primary_name'], _aimt_analyzed_at_ms=_now_ms())
        self.payload = payload
        self._publish_execution_scope()
        result = super().on_local(payload)
        self.market_workspace.update_cache()
        return result

    def on_live(self, live):
        if not self._scope_ready:
            return super().on_live(live)
        primary = self.primary() or {}
        if live.get('name') != primary.get('name') or live.get('tf') != self.cfg.get('tf'):
            return
        # A queued feed update for a previous symbol must not re-enable its old scope.
        wanted = self.acc_symbol(primary)
        with self._scope_lock:
            expected = self._resolved_aliases.get((primary.get('name'), wanted, self.cfg.get('tf')), wanted)
        if live.get('sym') != expected:
            return
        self._market_last_live_ms = _now_ms()
        result = super().on_live(live)
        self._publish_execution_scope()
        return result

    def on_ai(self, result):
        if result.get('ok') and not self._ai_allowed(result.get('data'), notify=True):
            self.bus.finished.emit()
            return
        return super().on_ai(result)

    def finalize(self, ai, ai_failed):
        if not self.payload:
            self.bus.finished.emit()
            return
        if ai and not self._ai_allowed(ai, notify=True):
            self.ai = None
            self.plan = self.make_plan(self.payload, None)
            self.auto_msg = 'AI không khớp ngữ cảnh hiện tại; chu kỳ này không vào lệnh.'
            self.render_card(True)
            self.bus.finished.emit()
            return
        if ai:
            workspace = getattr(self, 'market_workspace', None)
            if workspace is not None:
                workspace._primary_ai = dict(parse_conclusion(ai), request_subject=ai.get('_aimt_subject'),
                    received_at_ms=_now_ms(), subject_valid=True)
        result = super().finalize(ai, ai_failed)
        if ai:
            conclusion = parse_conclusion(ai)
            lines = ['TÍN HIỆU: ' + conclusion['signal'], 'KẾT LUẬN: ' + conclusion['conclusion']]
            if conclusion['reason']:
                lines.append('CĂN CỨ: ' + conclusion['reason'])
            if conclusion['risk']:
                lines.append('RỦI RO: ' + conclusion['risk'])
            words = '\n'.join(lines).split()
            self.txt_ai.setPlainText('\n'.join(lines) if len(words) <= 180 else ' '.join(words[:180]) + ' …')
        return result

    def make_plan(self, payload, ai):
        return super().make_plan(payload, ai if ai is None or self._ai_allowed(ai) else None)

    def pack_plan(self, payload, side, score, ai=None, rng=None, note=''):
        return super().pack_plan(payload, side, score, ai if ai is None or self._ai_allowed(ai) else None, rng, note)

    def apply_external_ai(self, data):
        self._publish_execution_scope()
        if not self._entries_allowed() or not self._ai_allowed(data, notify=True):
            self.say('Kết quả AI Web không khớp yêu cầu MT5 hiện tại; không áp dụng cho lệnh.')
            return False
        return super().apply_external_ai(data)

    def ai_web_subject(self):
        self._publish_execution_scope()
        state = self._scope_snapshot()
        if not self._entries_allowed(notify=False) or not self.payload or not self._analysis_allowed(self.payload):
            return None
        subject = {key: state.get(key) for key in ('source', 'market', 'mode', 'symbol', 'timeframe',
            'analysis_bar_id', 'analysis_generation', 'account_names', 'chart_id')}
        subject['execution_eligible'] = True
        return subject

    def manual(self, side):
        if self._entries_allowed() and self._analysis_allowed(self.payload):
            return super().manual(side)

    def arm(self, side):
        if not self._entries_allowed():
            self.armed = None
            return
        if self.cfg.get('hotkey_confirm', True):
            return super().arm(side)
        self.armed = None
        return self.exec_command(side)

    def eventFilter(self, obj, event):
        from .hotkeys import repeated_trade_key
        if repeated_trade_key(self, event):
            return True
        return super().eventFilter(obj, event)

    def job_trade(self, side, payload, manual):
        if not self._analysis_allowed(payload):
            self.bus.finished.emit()
            return
        return super().job_trade(side, payload, manual)

    def trade_account(self, account, side, payload, plan, manual):
        if not self._analysis_allowed(payload):
            return None
        if plan.get('src') == 'AI' and not self._ai_allowed(self.ai, account=account, notify=True):
            return None
        with self.broker.session(account) as connected:
            if not connected or self.broker.resolve(self.acc_symbol(account)) != payload.get('sym'):
                self.log(f"[{account.get('name')}] Không chuyển mức giá/kế hoạch sang symbol khác phân tích.")
                return None
        return super().trade_account(account, side, payload, plan, manual)

    def panel_run(self, fn, order=False):
        if order and not self._entries_allowed():
            return
        return super().panel_run(fn, order)

    def panel_order(self, side, lot_override=None):
        if not self._entries_allowed():
            return
        if not self.cfg.get('pa', {}).get('use_ai_levels'):
            return super().panel_order(side, lot_override)
        self._publish_execution_scope()
        if not self._ai_allowed(self.ai, notify=True):
            return
        pa, plan, live, ai = self.cfg['pa'], self.plan, self.last_live, self.ai
        if not plan or plan.get('side') != side or not live:
            self.log('SL/TP AI chưa có kế hoạch cùng chiều hiện tại; không gửi lệnh.')
            return
        targets = self.enabled_accounts() if self.cfg.get('panel_target') == 'ALL' else [self.primary()]
        if any(not self._ai_allowed(ai, account=a, notify=True) for a in targets if a):
            return
        scalp = plan.get('plans', {}).get('SCALP') or {}
        if scalp.get('src') != 'AI':
            self.log('Bật SL/TP tự động theo AI và lấy kết quả hợp lệ trước khi dùng mức AI cho panel.')
            return
        legacy = self._scope_legacy

        def send(ops, symbol):
            if symbol != ai['_aimt_subject']['symbol'] or not self._ai_allowed(ai):
                return 'Symbol/nguồn/tuổi kết quả AI đã thay đổi; không gửi lệnh.'
            meta = ops.br.meta(symbol)
            lot = float(lot_override) if lot_override else self.panel_lot(meta,
                (ops.br.account() or {}).get('balance', 0), pa['sl_pts'])
            lot = legacy.norm_lot(meta, lot) if lot > 0 else 0
            if self.cfg['max_lot']:
                lot = min(lot, self.cfg['max_lot'])
            if lot <= 0:
                return 'Không tính được lot hợp lệ.'
            reference = live['ask'] if side == 'BUY' else live['bid']
            _, stop, target = legacy.plan_prices(scalp, reference, meta['digits'])
            return ops.market(symbol, side, lot, pa['sl_pts'], pa['tp_pts'], stop, target)

        return self.panel_run(send, order=True)

    def panel_pending(self, kind, side):
        if self._entries_allowed():
            return super().panel_pending(kind, side)

    def job_ai_adjust(self, payload, ai):
        try:
            return self._job_ai_adjust_scoped(payload, ai)
        except Exception as error:
            self.log('AI chỉnh SL/TP không hoàn thành: ' + type(error).__name__)

    def _job_ai_adjust_scoped(self, payload, ai):
        if not self._ai_allowed(ai, notify=True):
            return
        signal = ai.get('signal')
        if signal not in ('BUY', 'SELL'):
            return
        advice = ai.get('position_advice') or {}

        def number(value):
            try:
                result = float(value)
                return result if math.isfinite(result) and result > 0 else None
            except (TypeError, ValueError):
                return None

        new_stop = number(advice.get('new_sl')) or number(ai.get('sl'))
        new_target = number(advice.get('new_tp')) or number(ai.get('tp'))
        atr, broker = payload['atr'], self.broker
        for account in [a for a in self.enabled_accounts() if a.get('auto')]:
            if not self._ai_allowed(ai, account=account, notify=True):
                continue
            with broker.session(account) as connected:
                if not connected:
                    continue
                symbol = broker.resolve(self.acc_symbol(account))
                if symbol != ai['_aimt_subject']['symbol']:
                    self.log(f"[{account['name']}] AI không quản lý mức giá của symbol khác.")
                    continue
                meta, (bid, ask) = broker.meta(symbol), broker.tick(symbol)
                minimum = meta['stops_min']
                for position in broker.positions(symbol):
                    if (position['magic'] != int(self.cfg['magic']) or position['type'] != signal or
                            not self._ai_allowed(ai, account=account)):
                        continue
                    buy, stop, target = signal == 'BUY', position['sl'], position['tp']
                    changes = []
                    if new_stop:
                        tighter = ((buy and new_stop < bid - minimum and (not stop or new_stop > stop + .1 * atr)) or
                                   (not buy and new_stop > ask + minimum and (not stop or new_stop < stop - .1 * atr)))
                        if tighter:
                            stop = round(new_stop, meta['digits'])
                            changes.append('siết SL')
                    if new_target:
                        appropriate = (new_target > bid + max(minimum, .5 * atr) if buy else
                                       new_target < ask - max(minimum, .5 * atr))
                        if appropriate and abs(new_target - target) >= .1 * atr:
                            target = round(new_target, meta['digits'])
                            changes.append('TP')
                    if changes and broker.modify_pos(position['ticket'], symbol, stop, target):
                        self.log(f"[{account['name']}] AI chỉnh #{position['ticket']}: {', '.join(changes)}")

    def ai_conclusion_changed(self, result):
        workspace = getattr(self, 'market_workspace', None)
        subject = result.get('request_subject') or {}
        if workspace is None or not result.get('subject_valid'):
            return
        if subject.get('chart_id') == 'mt5-execution':
            state = self._scope_snapshot()
            if (subject.get('symbol') != state.get('symbol') or
                    subject.get('timeframe') != state.get('timeframe') or
                    (subject.get('analysis_generation') is not None and
                     subject.get('analysis_generation') != state.get('analysis_generation'))):
                return
            workspace._primary_ai = dict(result)
            if workspace.execution_allowed():
                self.render_card()

    def render_card(self, ai_failed=False):
        workspace = getattr(self, 'market_workspace', None)
        if workspace is not None and workspace.active_page is not None:
            return workspace.present_active()
        result = super().render_card(ai_failed)
        # Keep the original plans and all eight methods; clarify the visible neutral label.
        text = self.card.text().replace('>WAIT<', '>SIDEWAYS<')
        state, now = self._scope_snapshot(), _now_ms()
        stale = not state.get('live_received_ms') or now - state['live_received_ms'] > 90000
        if stale:
            for label in ('BUY', 'SELL', 'SIDEWAYS'):
                text = text.replace('>' + label + '</div>', '>DỮ LIỆU CŨ</div>', 1)
            text += '<br><b>DỮ LIỆU CŨ:</b> chưa có giá MT5 hợp lệ trong 90 giây; làm mới trước khi dùng tín hiệu.'
        summary = getattr(workspace, '_primary_ai', None) if workspace else None
        if summary:
            age = max(0, (now - summary.get('received_at_ms', now)) // 1000)
            label = 'DỮ LIỆU CŨ' if stale or age > 180 else summary.get('signal', 'UNAVAILABLE')
            text += '<br><b>AI · ' + html.escape(label) + f' · {age}s trước</b><br>' + html.escape(summary.get('conclusion', '')[:600])
        quant = getattr(workspace, '_primary_quant', None) if workspace else None
        if quant:
            text += '<br><b>Định lượng:</b> ' + ('DỮ LIỆU CŨ · làm mới giá trước khi đọc tín hiệu.' if stale else
                html.escape(quant.get('signal', '')) + ' · ' + html.escape(quant.get('summary', '')[:300]))
        self.card.setText(text)
        return result
