"""Instrument and request guards around the preserved MT5 application.

The GUI publishes plain Python state. Worker guards never inspect a Qt widget.
Research charts cannot route broker entries; closing and managing existing orders
remain independent of the selected research chart.
"""
import copy
from contextlib import contextmanager, nullcontext
import hashlib
import html
import json
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
        self._order_dispatcher = None
        self._dispatch_local = threading.local()
        self._dispatch_handles = {}
        self._dispatch_terminal_ids = set()
        self._uncertain_terminals = {}
        self._active_native_entries = {}

    def _init_order_dispatcher(self):
        from aimt_orderflow.async_dispatch import AsyncOrderDispatcher
        reference = weakref.ref(self)

        def finished(result):
            owner = reference()
            if owner is not None and not owner._scope_snapshot().get('closed'):
                if result.state == 'unknown':
                    owner._latch_uncertain_terminal(result.terminal_key, result.error)
                owner.order_dispatch_changed.emit(result)

        self._order_dispatcher = AsyncOrderDispatcher(max_workers=4, max_pending=128,
            max_per_key=32, history_limit=1024, on_result=finished)
        saved = self.cfg.get('async_uncertain_terminals') or {}
        if isinstance(saved, dict):
            with self._scope_lock:
                self._uncertain_terminals = {key: {'at_ms': int(value.get('at_ms') or 0)
                    if isinstance(value.get('at_ms'), (int, float)) and math.isfinite(value['at_ms']) else 0,
                    'reason': 'native_outcome_unknown_manual_reconciliation_required'}
                    for key, value in list(saved.items()) if isinstance(key, str) and isinstance(value, dict)}
            for key in self._uncertain_terminals:
                self._order_dispatcher.pause_entries(key)
        from PyQt6 import QtCore
        self.order_dispatch_changed.connect(self._on_order_dispatch_changed, QtCore.Qt.ConnectionType.QueuedConnection)

    def _on_order_dispatch_changed(self, event):
        if self._scope_snapshot().get('closed'):
            return
        if isinstance(event, dict):
            if event.get('kind') == 'latch':
                self._persist_uncertain_terminal_state()
                label = getattr(self, 'order_dispatch_status', None)
                if label is not None:
                    label.setText('MT5 · chưa rõ kết quả; khóa lệnh mới')
                self._sync_entry_buttons()
                return
            request_id, state = event['request_id'], event['state']
            error = ''
        else:
            request_id, state, error = event.request_id, event.state, event.error
        short_id = self._task_label(request_id)
        if request_id in self._dispatch_terminal_ids and state == 'queued':
            return
        if state not in ('queued', 'running'):
            self._dispatch_terminal_ids.add(request_id)
            with self._scope_lock:
                self._dispatch_handles.pop(request_id, None)
            self.log(f'[MT5 task {short_id}] {state}' + (': ' + str(error)[:800] if error else ''))
            value = getattr(event, 'value', None)
            if isinstance(value, dict):
                receipt = value.get('last_native_receipt')
                if isinstance(receipt, dict):
                    self.log(f'[MT5 task {short_id}] broker: {receipt.get("state", "unavailable")} · '
                        f'retcode={receipt.get("retcode")} · order={receipt.get("order")} · deal={receipt.get("deal")}')
        if len(self._dispatch_terminal_ids) > 2048:
            self._dispatch_terminal_ids = {request_id}
        label = getattr(self, 'order_dispatch_status', None)
        if label is not None:
            label.setText(f'MT5 #{short_id} · {state}')

    @staticmethod
    def _task_label(request_id):
        prefix, separator, counter = str(request_id).rpartition(':')
        return prefix[:4] + ':' + counter if separator else str(request_id)[-8:]

    def _cancel_entry_tasks(self, reason):
        if self._order_dispatcher is not None:
            self._order_dispatcher.cancel_pending(reason, predicate=lambda metadata: bool(metadata.get('entry')))

    def _terminal_key(self, account):
        key_for = getattr(self.broker, 'key_for', None)
        return str(key_for(account)) if callable(key_for) else 'demo-serialized'

    def _latch_uncertain_terminal(self, terminal_key, reason):
        with self._scope_lock:
            first = terminal_key not in self._uncertain_terminals
            self._uncertain_terminals[terminal_key] = {'at_ms': _now_ms(), 'reason': str(reason)[:800]}
        if self._order_dispatcher is not None:
            self._order_dispatcher.pause_entries(terminal_key)
            self._order_dispatcher.cancel_pending('Kết quả gửi lệnh trước chưa rõ; không tự gửi lệnh mới.',
                predicate=lambda metadata: bool(metadata.get('entry')) and metadata.get('terminal_key') == terminal_key)
        if first:
            self.log('MT5 CHƯA RÕ KẾT QUẢ: đã khóa lệnh mới trên terminal này. Kiểm tra lệnh trong MT5; '
                     'chỉ gõ ASYNC RESUME sau khi đối chiếu. Không tự gửi lại lệnh trước.')
            self.order_dispatch_changed.emit({'kind': 'latch'})

    def _persist_uncertain_terminal_state(self):
        with self._scope_lock:
            saved = {key: {'at_ms': int(value.get('at_ms') or 0),
                          'reason': 'native_outcome_unknown_manual_reconciliation_required'}
                     for key, value in self._uncertain_terminals.items()}
        self.cfg['async_uncertain_terminals'] = saved
        self._scope_legacy.save_cfg(self.cfg)

    def _preserve_outstanding_native_entries(self):
        # Called on the GUI thread before shutdown; an in-flight send has no final ack yet.
        with self._scope_lock:
            for key, count in self._active_native_entries.items():
                if count:
                    self._uncertain_terminals[key] = {'at_ms': _now_ms(), 'reason': 'closed_with_send_in_flight'}
        self._persist_uncertain_terminal_state()

    @staticmethod
    def _account_revision(account, *, include_risk=True):
        keys = ('name', 'path', 'login', 'server', 'password', 'symbol')
        if include_risk:
            keys += ('enabled', 'auto', 'max_lot', 'max_loss_pct', 'loss_mode', 'copy_on', 'copy_from', 'copy_ratio', 'copy_invert')
        fields = {key: account.get(key) for key in keys}
        return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode('utf-8')).hexdigest()

    def _order_configuration_revision(self):
        fields = {key: self.cfg.get(key) for key in ('risk_mode', 'risk_pct', 'risk_usd', 'lot_mode', 'lot_fixed',
            'max_lot', 'max_spread', 'magic', 'deviation', 'exec_plan', 'ai_levels', 'panel_target', 'panel_scope', 'pa')}
        return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode('utf-8')).hexdigest()

    def _order_guard(self, captured, account, *, entry, automatic=False, analysis=None, ai=None):
        reference = weakref.ref(self)
        account_name, revision = account.get('name'), self._account_revision(account, include_risk=entry)
        terminal_key = self._terminal_key(account)
        captured_broker = self.broker
        configuration = self._order_configuration_revision()
        submitted = _now_ms()

        def guard():
            owner = reference()
            if owner is None:
                return False, 'Cửa sổ không còn hoạt động.'
            current = owner._scope_snapshot()
            if current.get('closed'):
                return False, 'Ứng dụng đã đóng.'
            if _now_ms() - submitted > (30000 if entry else 120000):
                return False, 'Tác vụ đã hết thời gian chờ; không tự gửi lại.'
            selected = next((a for a in list(owner.cfg.get('accounts', ())) if a.get('name') == account_name), None)
            if selected is None or owner._account_revision(selected, include_risk=entry) != revision:
                return False, 'Tài khoản hoặc cấu hình kết nối đã thay đổi.'
            if owner.broker is not captured_broker or owner._terminal_key(selected) != terminal_key:
                return False, 'Terminal thực thi đã thay đổi khi đang chờ.'
            if not entry:
                return True, ''
            with owner._scope_lock:
                uncertain = terminal_key in owner._uncertain_terminals
            if owner._order_dispatcher is not None:
                uncertain = uncertain or owner._order_dispatcher.entries_paused(terminal_key)
            if uncertain:
                return False, 'Terminal có kết quả gửi lệnh chưa rõ; cần đối chiếu MT5 trước ASYNC RESUME.'
            if not current.get('primary_active') or not selected.get('enabled'):
                return False, 'Chart nghiên cứu hoặc tài khoản đã tắt; không mở lệnh.'
            if any(captured.get(key) != current.get(key) for key in
                    ('source', 'market', 'mode', 'primary_name', 'symbol', 'timeframe', 'analysis_generation')):
                return False, 'Ngữ cảnh lệnh đã thay đổi khi đang chờ.'
            if owner._order_configuration_revision() != configuration:
                return False, 'Tham số lệnh/rủi ro đã thay đổi khi đang chờ.'
            if automatic and (not owner.cfg.get('auto_trade') or not selected.get('auto')):
                return False, 'Auto đã tắt trước khi gửi lệnh.'
            if analysis is not None and not owner._analysis_allowed(analysis):
                return False, 'Phân tích dùng cho lệnh đã hết hiệu lực.'
            if ai is not None and not owner._ai_allowed(ai, account=selected):
                return False, 'Ngữ cảnh AI dùng cho lệnh đã hết hiệu lực.'
            return True, ''

        return guard

    @staticmethod
    def _guard_result(guard):
        result = guard()
        return result if isinstance(result, tuple) else (bool(result), '')

    @contextmanager
    def _order_guard_context(self, guard):
        previous = getattr(self._dispatch_local, 'entry_guard', None)
        self._dispatch_local.entry_guard = guard
        bridge = getattr(self.broker, 'order_send_guard', None)
        try:
            with bridge(guard) if callable(bridge) else nullcontext():
                yield
        finally:
            self._dispatch_local.entry_guard = previous

    def _submit_order_task(self, account, operation, *, guard, metadata):
        from aimt_orderflow.async_dispatch import DispatchError
        terminal_key = self._terminal_key(account)
        metadata = dict(metadata, terminal_key=terminal_key)
        try:
            handle = self._order_dispatcher.submit(terminal_key, operation, guard=guard, metadata=metadata)
        except DispatchError as error:
            self.log('Hàng đợi MT5 từ chối tác vụ: ' + str(error))
            return None
        with self._scope_lock:
            if not handle.done():
                self._dispatch_handles[handle.request_id] = handle
        self.order_dispatch_changed.emit({'request_id': handle.request_id, 'state': 'queued'})
        return handle

    @contextmanager
    def _capture_native_receipts(self):
        previous = getattr(self._dispatch_local, 'receipts', None)
        captured = []
        self._dispatch_local.receipts = captured
        try:
            yield captured
        finally:
            self._dispatch_local.receipts = previous

    @staticmethod
    def _operation_value(value, receipts):
        return {'broker_value': value, 'native_receipts': receipts[-32:],
                'native_receipt_count': len(receipts), 'native_receipts_omitted': max(0, len(receipts) - 32),
                'last_native_receipt': receipts[-1] if receipts else None}

    def exec_command(self, text):
        command = str(text).strip().upper()
        if command in ('ASYNC', 'ASYNC STATUS'):
            snapshot = self._order_dispatcher.snapshot() if self._order_dispatcher is not None else {}
            self.say(f"MT5 queue: queued {snapshot.get('queued', 0)}, running {snapshot.get('running', 0)}, "
                     f"terminal chưa rõ kết quả {len(self._uncertain_terminals)}. ASYNC RESUME chỉ sau khi kiểm tra MT5.")
            return snapshot
        if command == 'ASYNC RESUME':
            with self._scope_lock:
                count = len(self._uncertain_terminals)
                self._uncertain_terminals.clear()
            if self._order_dispatcher is not None:
                self._order_dispatcher.resume_entries()
            self._persist_uncertain_terminal_state()
            self._sync_entry_buttons()
            self.say(f'Đã mở lại {count} terminal sau đối chiếu thủ công; lệnh cũ không được gửi lại.')
            return
        return super().exec_command(text)

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
            self._cancel_entry_tasks('Đã đổi tài khoản/symbol/khung thời gian; huỷ lệnh mới còn chờ.')
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
            self._cancel_entry_tasks('Đã đổi chart; huỷ lệnh mới còn chờ.')
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
        primary = next((a for a in list(getattr(self, 'cfg', {}).get('accounts', ()))
                        if a.get('name') == state.get('primary_name')), None)
        with self._scope_lock:
            uncertain = primary is not None and self._terminal_key(primary) in self._uncertain_terminals
        if primary is not None and self._order_dispatcher is not None:
            uncertain = uncertain or self._order_dispatcher.entries_paused(self._terminal_key(primary))
        if uncertain:
            if notify:
                self.log('Lệnh mới bị khóa do kết quả gửi trước chưa rõ. Đối chiếu MT5 rồi mới dùng ASYNC RESUME.')
            return False
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
        original_session = self.broker.session

        @contextmanager
        def scoped_session(account):
            window = owner()
            if window is None:
                yield False
                return
            existing = getattr(window._dispatch_local, 'entry_guard', None)
            guard = existing or window._order_guard(window._scope_snapshot(), dict(account), entry=True)
            previous = getattr(window._dispatch_local, 'account', None)
            window._dispatch_local.account = dict(account)
            try:
                with window._order_guard_context(guard):
                    with original_session(account) as connected:
                        yield connected
            finally:
                window._dispatch_local.account = previous

        self.broker.session = scoped_session
        for name in ('order', 'pending'):
            original = getattr(self.broker, name)

            def guarded(*args, _original=original, **kwargs):
                window = owner()
                if window is None or not window._entries_allowed():
                    return False, 'Đang ở chart nghiên cứu hoặc ứng dụng đã đóng; không gửi lệnh MT5.'
                guard = getattr(window._dispatch_local, 'entry_guard', None)
                if guard is not None:
                    valid, reason = window._guard_result(guard)
                    if not valid:
                        return False, reason
                account = getattr(window._dispatch_local, 'account', None)
                terminal_key = window._terminal_key(account) if account else None
                if terminal_key is not None:
                    with window._scope_lock:
                        window._active_native_entries[terminal_key] = window._active_native_entries.get(terminal_key, 0) + 1
                try:
                    value = _original(*args, **kwargs)
                except Exception as error:
                    from aimt_orderflow.async_dispatch import IndeterminateExecutionError
                    if isinstance(error, (IndeterminateExecutionError, TimeoutError, EOFError, OSError)):
                        if account:
                            window._latch_uncertain_terminal(window._terminal_key(account), str(error))
                    raise
                finally:
                    if terminal_key is not None:
                        with window._scope_lock:
                            window._active_native_entries[terminal_key] -= 1
                receipt_method = getattr(window.broker, 'trade_receipt', None)
                if callable(receipt_method):
                    try:
                        receipt = receipt_method()
                    except Exception as error:
                        receipt = {'state': 'receipt_unavailable', 'error_type': type(error).__name__,
                                   'broker_call_returned': True}
                    receipts = getattr(window._dispatch_local, 'receipts', None)
                    if receipts is not None and isinstance(receipt, dict):
                        receipts.append(copy.deepcopy(receipt))
                return value

            setattr(self.broker, name, guarded)
        reverse = self.ops.reverse_all

        def guarded_reverse(*args, **kwargs):
            window = owner()
            if window is None or not window._entries_allowed():
                return 'Reverse bị chặn trên chart nghiên cứu; vị thế hiện tại được giữ nguyên.'
            guard = getattr(window._dispatch_local, 'entry_guard', None)
            if guard is not None and not window._guard_result(guard)[0]:
                return 'Reverse bị chặn vì ngữ cảnh lệnh đã thay đổi; vị thế được giữ nguyên.'
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
        self._cancel_entry_tasks('Phân tích mới đã thay ngữ cảnh; huỷ lệnh mới còn chờ.')
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
        if self._order_dispatcher is None:
            with self.broker.session(account) as connected:
                if not connected or self.broker.resolve(self.acc_symbol(account)) != payload.get('sym'):
                    self.log(f"[{account.get('name')}] Không chuyển mức giá/kế hoạch sang symbol khác phân tích.")
                    return None
            return super().trade_account(account, side, payload, plan, manual)
        selected, wanted = dict(account), self.acc_symbol(account)
        captured = self._scope_snapshot()
        ai = copy.deepcopy(self.ai) if self.ai else None
        guard = self._order_guard(captured, selected, entry=True, automatic=not manual, analysis=payload, ai=ai)

        def operation():
            with self._capture_native_receipts() as receipts:
                with self._order_guard_context(guard):
                    with self.broker.session(selected) as connected:
                        if not connected:
                            raise RuntimeError('Không kết nối được terminal MT5.')
                        if self.broker.resolve(wanted) != payload.get('sym'):
                            self.log(f"[{selected.get('name')}] Không chuyển mức giá/kế hoạch sang symbol khác phân tích.")
                            return self._operation_value(None, receipts)
                        value = super(ExecutionScopeMixin, self).trade_account(selected, side, payload, plan, manual)
                        return self._operation_value(value, receipts)

        handle = self._submit_order_task(selected, operation, guard=guard,
            metadata={'entry': True, 'automatic': not manual, 'action': side, 'account_name': selected.get('name'),
                      'symbol': payload.get('sym'), 'timeframe': captured.get('timeframe'),
                      'generation': captured.get('analysis_generation'), 'mode': captured.get('mode')})
        if handle is None:
            return None
        result = handle.result()
        return result.value.get('broker_value') if result.state == 'completed' and isinstance(result.value, dict) else None

    def panel_run(self, fn, order=False):
        if order and not self._entries_allowed():
            return
        if self._order_dispatcher is None:
            return super().panel_run(fn, order)
        accounts = self.enabled_accounts() if self.cfg.get('panel_target') == 'ALL' else [self.primary()]
        accounts = [dict(account) for account in accounts if account]
        if not accounts:
            self.log('Panel: chưa có tài khoản đang bật.')
            return []
        captured, handles = self._scope_snapshot(), []
        ai = copy.deepcopy(self.ai) if order and self.cfg.get('pa', {}).get('use_ai_levels') and self.ai else None
        for account in accounts:
            guard = self._order_guard(captured, account, entry=order, ai=ai)
            entry_guard = self._order_guard(captured, account, entry=True, ai=ai)
            wanted = self.acc_symbol(account)

            def operation(selected=account, requested=wanted, send_guard=entry_guard):
                with self._capture_native_receipts() as receipts:
                    with self._order_guard_context(send_guard):
                        with self.broker.session(selected) as connected:
                            if not connected:
                                raise RuntimeError('Không kết nối được terminal MT5.')
                            symbol = self.broker.resolve(requested)
                            message = fn(self.ops, symbol)
                            if message:
                                self.log(f"[Panel/{selected['name']}] " + str(message))
                            return self._operation_value(message, receipts)

            handle = self._submit_order_task(account, operation, guard=guard,
                metadata={'entry': bool(order), 'automatic': False, 'action': 'panel-entry' if order else 'panel-manage',
                          'account_name': account.get('name'), 'symbol': wanted,
                          'timeframe': captured.get('timeframe'), 'generation': captured.get('analysis_generation'),
                          'mode': captured.get('mode')})
            if handle is not None:
                handles.append(handle)
        return handles

    def set_auto_trade(self, enabled):
        result = super().set_auto_trade(enabled)
        self._publish_execution_scope()
        if not self.cfg.get('auto_trade') and self._order_dispatcher is not None:
            self._order_dispatcher.cancel_pending('Auto đã tắt; huỷ lệnh Auto còn chờ.',
                predicate=lambda metadata: bool(metadata.get('automatic')))
        return result

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
