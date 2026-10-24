"""Independent chart tabs, one explicit MT5 execution chart and a shared analysis view."""
import copy
from collections import deque
from dataclasses import replace
import html
import json
import threading
import time
import uuid
import weakref

from PyQt6 import QtCore, QtWidgets

from .analysis import bars_from_candles
from .data import CAPABILITIES, TIMEFRAMES
from .hub import MarketHub
from .quant import analyze_market
from .live import TradeMailbox, update_price_candles
from .stream import PublicTradeStream


class NewChartDialog(QtWidgets.QDialog):
    def __init__(self, workspace):
        super().__init__(workspace.window)
        self.workspace = workspace
        self.instrument = None
        self._items, self._token = [], ''
        self.setWindowTitle('Thêm chart · dữ liệu công khai / MT5')
        self.resize(620, 300)
        layout = QtWidgets.QFormLayout(self)
        self.provider = QtWidgets.QComboBox()
        self.provider.addItems(list(CAPABILITIES) + ['MT5'])
        self.market = QtWidgets.QComboBox()
        self.asset_filter = QtWidgets.QComboBox()
        self.asset_filter.addItems(['Tất cả cặp', 'Crypto', 'Forex / fiat'])
        self.symbol = QtWidgets.QComboBox()
        self.symbol.setEditable(True)
        self.symbol.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.symbol.completer().setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
        self.symbol.completer().setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        self.timeframe = QtWidgets.QComboBox()
        self.timeframe.addItems(list(TIMEFRAMES))
        self.futures = QtWidgets.QCheckBox('Kèm futures/perpetual cùng sàn, cùng base/quote khi có')
        self.futures.setChecked(True)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                                QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.retry = QtWidgets.QPushButton('Tải lại danh sách')
        for text, widget in [('Nguồn', self.provider), ('Thị trường', self.market), ('Lọc', self.asset_filter),
                             ('Tìm / chọn cặp', self.symbol), ('Khung nến', self.timeframe), ('', self.futures),
                             ('', self.status), ('', self.retry), ('', self.buttons)]:
            layout.addRow(text, widget)
        self.provider.currentTextChanged.connect(self._provider_changed)
        self.market.currentTextChanged.connect(self.reload)
        self.asset_filter.currentIndexChanged.connect(self._filter)
        self.retry.clicked.connect(self.reload)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        workspace.hub.signals.result.connect(self._result)
        self.finished.connect(self._finished)
        self._provider_changed()

    def _provider_changed(self, *_):
        self.market.blockSignals(True)
        self.market.clear()
        provider = self.provider.currentText()
        self.market.addItems(['broker'] if provider == 'MT5' else list(CAPABILITIES[provider]['markets']))
        self.market.blockSignals(False)
        self.reload()

    def reload(self, *_):
        provider, market = self.provider.currentText(), self.market.currentText()
        if not market:
            return
        self._token = uuid.uuid4().hex
        self._items = []
        self.symbol.clear()
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.status.setText('Đang lấy danh sách cặp đang hoạt động; không cần API key cho dữ liệu công khai…')
        account = dict(self.workspace.window.primary() or {})
        self.workspace.hub.submit('catalog', self._token,
            lambda: self.workspace.hub.catalog(provider, market, account), provider)

    def _result(self, kind, token, result):
        if kind != 'catalog' or token != self._token:
            return
        if not result['ok']:
            self.status.setText('Không tải được danh sách: ' + result['error'] +
                                ' Có thể do mạng, giới hạn API hoặc vùng truy cập. Có thể chọn nguồn khác.')
            return
        self._items = result['data']
        self._filter()

    def _filter(self, *_):
        choice = self.asset_filter.currentIndex()
        values = [item for item in self._items if choice == 0 or
                  (choice == 2 and item.kind == 'forex') or (choice == 1 and item.kind == 'crypto')]
        self.symbol.clear()
        for item in sorted(values, key=lambda item: (item.base not in ('BTC', 'XBT', 'ETH'), item.symbol)):
            self.symbol.addItem(f'{item.symbol} · {item.base}/{item.quote} · {item.kind}', item)
        self.status.setText(f'{len(values):,} cặp phù hợp / {len(self._items):,} cặp lấy được từ nguồn này. '
                            'Mỗi chart giữ nguyên nguồn và symbol riêng.')
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(bool(values))

    def _accept(self):
        text = self.symbol.currentText().split(' · ', 1)[0].strip()
        self.instrument = next((item for item in self._items if item.symbol.casefold() == text.casefold()), None)
        if self.instrument is None:
            self.status.setText('Hãy chọn một cặp trong danh sách thực tế của nguồn, không nhập symbol tự đoán.')
            return
        self.accept()

    def _finished(self, *_):
        self._token = ''
        try:
            self.workspace.hub.signals.result.disconnect(self._result)
        except (TypeError, RuntimeError):
            pass


class ChartPage(QtWidgets.QWidget):
    def __init__(self, workspace, instrument, timeframe, include_futures):
        super().__init__()
        self.workspace, self.instrument = workspace, instrument
        self.id = uuid.uuid4().hex
        self.include_futures = include_futures
        self.bundle = self.ai_result = None
        self.token, self.inflight, self.next_refresh = '', False, 0
        self.closed = False
        self.ai_token = ''
        self.stream_trades = deque(maxlen=200)
        self.stream_status = {'state': 'disconnected', 'message': 'Chưa mở trade stream.'}
        self.stream_dropped = 0
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(f'{instrument.provider} · {instrument.market} · {instrument.symbol}'))
        self.timeframe = QtWidgets.QComboBox()
        self.timeframe.addItems(list(TIMEFRAMES))
        self.timeframe.setCurrentText(timeframe)
        row.addWidget(self.timeframe)
        self.refresh_button = QtWidgets.QPushButton('Làm mới')
        self.ai_button = QtWidgets.QPushButton('AI nhận định')
        self.research_button = QtWidgets.QPushButton('Kiểm chứng')
        row.addWidget(self.refresh_button)
        row.addWidget(self.ai_button)
        row.addWidget(self.research_button)
        if instrument.provider == 'MT5':
            trade_button = QtWidgets.QPushButton('Dùng cho MT5 giao dịch')
            trade_button.clicked.connect(lambda: workspace.activate_mt5(self.instrument, self.timeframe.currentText()))
            row.addWidget(trade_button)
        row.addStretch()
        layout.addLayout(row)
        self.status = QtWidgets.QLabel('Chưa có dữ liệu. Chart này chưa gắn tài khoản thực thi.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.chart = workspace.legacy.ChartWidget()
        layout.addWidget(self.chart, 1)
        self.refresh_button.clicked.connect(lambda: workspace.load_page(self))
        self.ai_button.clicked.connect(lambda: workspace.ask_ai(self))
        self.research_button.clicked.connect(lambda: workspace.open_research(self))
        self.timeframe.currentTextChanged.connect(self._timeframe_changed)

    def _timeframe_changed(self, *_):
        self.token = ''
        self.ai_token = ''
        self.ai_button.setEnabled(True)
        self.inflight = False
        self.bundle = self.ai_result = None
        self.stream_trades.clear()
        self.workspace._stop_stream()
        self.chart.set_data(None, {})
        self.workspace.update_cache()
        self.workspace.load_page(self)


class MarketWorkspace(QtWidgets.QWidget):
    MAX_ADDITIONAL_CHARTS = 8

    def __init__(self, window, legacy, client_factory=None, stream_factory=None):
        super().__init__()
        self.window, self.legacy = window, legacy
        self.hub = MarketHub(legacy, window.broker, client_factory)
        self.hub.signals.result.connect(self._result)
        self._cache_lock = threading.RLock()
        self._ai_cache = {'active_chart_id': 'mt5-execution', 'charts': []}
        self._pages, self._tokens = {}, {}
        self._closed = False
        self._primary_quant, self._primary_quant_key = None, None
        self._primary_ai = None
        self._stream_factory = stream_factory or PublicTradeStream
        self._stream = self._stream_page = self._mailbox = None
        self._research_dialogs = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabPosition(QtWidgets.QTabWidget.TabPosition.South)
        self.tabs.setTabsClosable(True)
        self.primary_page = QtWidgets.QWidget()
        self._primary_layout = QtWidgets.QVBoxLayout(self.primary_page)
        self._primary_layout.setContentsMargins(0, 0, 0, 0)
        label = QtWidgets.QLabel('MT5 THỰC THI · tài khoản và symbol theo thanh công cụ phía trên. '
                                'Các tab + là chart nghiên cứu độc lập.')
        label.setWordWrap(True)
        self._primary_layout.addWidget(label)
        self.tabs.addTab(self.primary_page, 'MT5 · thực thi')
        self.plus_page = QtWidgets.QWidget()
        self.tabs.addTab(self.plus_page, '+')
        layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self._active_changed)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self._hide_close_buttons()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.live_timer = QtCore.QTimer(self)
        self.live_timer.setInterval(100)
        self.live_timer.timeout.connect(self._drain_stream)
        self.live_timer.start()

    def attach_primary_chart(self, chart):
        self._primary_layout.addWidget(chart, 1)

    def _hide_close_buttons(self):
        for page in (self.primary_page, self.plus_page):
            index = self.tabs.indexOf(page)
            for side in (QtWidgets.QTabBar.ButtonPosition.LeftSide, QtWidgets.QTabBar.ButtonPosition.RightSide):
                self.tabs.tabBar().setTabButton(index, side, None)

    @property
    def active_page(self):
        value = self.tabs.currentWidget()
        return value if isinstance(value, ChartPage) else None

    def execution_allowed(self):
        return not self._closed and self.tabs.currentWidget() is self.primary_page

    def _active_changed(self, *_):
        if self.tabs.currentWidget() is self.plus_page:
            self.tabs.setCurrentWidget(self.primary_page)
            QtCore.QTimer.singleShot(0, self.add_dialog)
            return
        self.window.armed = None
        self._stop_stream()
        self._start_stream()
        self.update_cache()
        self.present_active()
        callback = getattr(self.window, 'market_orderflow_updated', None)
        if callback:
            callback(self.active_page)

    def add_dialog(self):
        if self._closed:
            return
        if len(self._pages) >= self.MAX_ADDITIONAL_CHARTS:
            self.window.say('Tối đa 8 chart bổ sung để giữ bộ nhớ và hạn mức API. Đóng một tab để thêm chart mới.')
            return
        dialog = NewChartDialog(self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.add_chart(dialog.instrument, dialog.timeframe.currentText(), dialog.futures.isChecked())

    def add_chart(self, instrument, timeframe='M1', include_futures=True, load=True):
        if len(self._pages) >= self.MAX_ADDITIONAL_CHARTS:
            raise ValueError('Chart limit reached.')
        page = ChartPage(self, instrument, timeframe, include_futures)
        self._pages[page.id] = page
        self.tabs.insertTab(self.tabs.indexOf(self.plus_page), page,
                            f'{instrument.symbol} · {instrument.provider} · {timeframe}')
        self._hide_close_buttons()
        self.tabs.setCurrentWidget(page)
        if load:
            self.load_page(page)
        return page

    def load_page(self, page):
        if self._closed or page.closed or page.inflight:
            return
        page.token = token = uuid.uuid4().hex
        page.inflight = True
        page.status.setText('Đang tải snapshot công khai / terminal… Giá cũ chưa được coi là dữ liệu mới.')
        self._tokens[token] = weakref.ref(page)
        instrument, timeframe = page.instrument, page.timeframe.currentText()
        account = dict(self.window.primary() or {})
        include_futures = page.include_futures
        self.hub.submit('chart', token, lambda: self.hub.fetch(instrument, timeframe, account, include_futures),
                        instrument.provider if instrument.provider != 'MT5' else '')

    def _result(self, kind, token, result):
        if self._closed or kind not in ('chart', 'ai'):
            return
        reference = self._tokens.pop(token, None)
        page = reference() if reference else None
        if page is None or page.closed:
            return
        if kind == 'ai':
            if token != page.ai_token:
                return
            if result['ok']:
                page.ai_result = result['data']
                self.update_cache()
                self.present_active()
            else:
                self.window.log('AI: ' + result['error'])
                page.status.setText('AI không trả kết quả mới: ' + result['error'])
            page.ai_button.setEnabled(True)
            return
        if token != page.token:
            return
        page.inflight = False
        interval = 15 if self.active_page is page else 60
        if not result['ok']:
            page.next_refresh = time.monotonic() + max(interval, result.get('retry_after', 15))
            page.status.setText('Không có dữ liệu mới: ' + result['error'] + ' · Không tự chuyển sang demo.')
            self.update_cache()
            return
        page.bundle = bundle = result['data']
        snapshot = bundle['snapshot']
        if page.ai_result:
            page.ai_result['context_changed'] = self._closed_bar_time(snapshot) != page.ai_result.get('analysis_bar_id')
        page.instrument = snapshot.instrument
        bars = bars_from_candles(self.legacy, snapshot.candles)
        page.chart.title = f'{snapshot.instrument.symbol},{snapshot.timeframe} [{snapshot.instrument.provider} {snapshot.instrument.market}]'
        page.chart.set_data(bars, bundle['original']['overlay'])
        quote = snapshot.quote
        bid, ask = quote.get('bid'), quote.get('ask')
        if bid is not None:
            page.chart.set_live(bid, ask or bid)
        stamp = time.strftime('%H:%M:%S', time.gmtime(snapshot.fetched_at_ms / 1000))
        warnings = ' · '.join(snapshot.errors[:2])
        page.rest_status = (f'{snapshot.instrument.provider} {snapshot.instrument.market} · {stamp} UTC · '
            f'volume: {snapshot.instrument.volume_unit if snapshot.instrument.provider != "MT5" else "broker real volume / quote ticks"} · REST snapshot, không phải HFT · ' +
            ('MT5 mô phỏng · ' if getattr(snapshot, 'mode', '') == 'demo' else '') +
            'chưa gắn tài khoản thực thi' + (' · ' + warnings if warnings else ''))
        self._show_status(page)
        page.next_refresh = time.monotonic() + interval
        self.tabs.setTabText(self.tabs.indexOf(page), f'{page.instrument.symbol} · {page.instrument.provider} · {snapshot.timeframe}')
        self.update_cache()
        self.present_active()
        self._update_crypto_flow(page)
        self._start_stream()

    def _close_tab(self, index):
        page = self.tabs.widget(index)
        if not isinstance(page, ChartPage):
            return
        page.closed, page.token = True, ''
        if self._stream_page is page:
            self._stop_stream()
        self._pages.pop(page.id, None)
        self.tabs.removeTab(index)
        page.deleteLater()
        self.update_cache()

    def _tick(self):
        now = time.monotonic()
        for page in list(self._pages.values()):
            if not page.inflight and now >= page.next_refresh:
                self.load_page(page)
        self.update_cache()
        page = self.active_page
        if page and page.bundle:
            age_key = self._display_age_key(page)
            if getattr(page, '_displayed_age_key', None) != age_key:
                page._displayed_age_key = age_key
                self.present_active()

    @staticmethod
    def _closed_bar_time(snapshot):
        return next((bar['time'] for bar in reversed(snapshot.candles) if bar.get('closed') is True), None)

    @staticmethod
    def _display_age_key(page):
        now = time.time() * 1000
        ai = page.ai_result or {}
        return (now - page.bundle['snapshot'].fetched_at_ms > 90000,
                bool(ai.get('context_changed')) or now - ai.get('received_at_ms', 0) > 180000)

    def _primary_snapshot(self):
        live = self.window.last_live
        if not live or live.get('rates') is None:
            return None
        rates = live['rates'][-600:]
        now = int(time.time() * 1000)
        received = getattr(self.window, '_market_last_live_ms', 0)
        observed = min(now, received) if received else 0
        tf = live.get('tf', self.window.cfg.get('tf', 'M1'))
        names = rates.dtype.names or ()
        candles = []
        for row in rates:
            real = float(row['real_volume']) if 'real_volume' in names else 0
            candles.append({'time': int(row['time']), 'open': float(row['open']), 'high': float(row['high']),
                           'low': float(row['low']), 'close': float(row['close']),
                           'volume': real if real > 0 else float(row['tick_volume']), 'volume_is_real': real > 0,
                           'closed': (int(row['time']) + TIMEFRAMES.get(tf, 60)) * 1000 <= observed})
        key = (live.get('name'), bool(live.get('demo')), live.get('sym'), tf,
               hash(rates.tobytes()), tuple(bar['closed'] for bar in candles[-2:]))
        if key != self._primary_quant_key:
            self._primary_quant = analyze_market(candles, now_ms=observed)
            self._primary_quant_key = key
        payload = self.window.payload or {}
        original = [{'key': item.key, 'name': item.name, 'signal': {1: 'BUY', -1: 'SELL', 0: 'SIDEWAYS'}.get(item.sig),
                     'state': item.state, 'notes': list(item.notes)} for item in payload.get('results', [])]
        return {'chart_id': 'mt5-execution', 'source': 'MT5', 'symbol': live.get('sym'), 'timeframe': tf,
                'mode': 'demo' if live.get('demo') else 'live', 'fetched_at_ms': received,
                'candles': candles, 'quote': {'bid': live.get('bid'), 'ask': live.get('ask')},
                'volume_unit': 'broker_real_volume_or_quote_tick_count', 'quant': self._primary_quant,
                'original_analysis': original, 'execution_eligible': True,
                'data_quality': ['MT5 candle tick_volume không phải traded size khi thiếu real_volume.']}

    def update_cache(self):
        if self._closed:
            return
        charts = []
        primary = self._primary_snapshot()
        if primary:
            charts.append(primary)
        for page in self._pages.values():
            if not page.bundle:
                charts.append({'chart_id': page.id, 'source': page.instrument.provider,
                    'symbol': page.instrument.symbol, 'timeframe': page.timeframe.currentText(),
                    'status': 'unavailable', 'error': page.status.text(), 'execution_eligible': False})
                continue
            entry = page.bundle['snapshot'].as_dict()
            entry.update(chart_id=page.id, source=page.instrument.provider, symbol=page.instrument.symbol,
                         quant=copy.deepcopy(page.bundle['quant']), original_analysis=page.bundle['original']['serialized'],
                         related_futures=copy.deepcopy(page.bundle['related_futures']), execution_eligible=False,
                         ai_conclusion=copy.deepcopy(page.ai_result))
            entry['trade_stream'] = {'status': copy.deepcopy(page.stream_status),
                'trades': list(page.stream_trades), 'dropped_ui_prints': page.stream_dropped,
                'book_mode': 'REST snapshot', 'candle_volume_mode': 'REST authoritative; live prices only',
                'native_and_aggregate_not_combined': True}
            charts.append(entry)
        active = self.active_page.id if self.active_page else 'mt5-execution'
        with self._cache_lock:
            self._ai_cache = {'active_chart_id': active, 'charts': copy.deepcopy(charts),
                              'scope': 'charts_in_workspace_only', 'generated_at_ms': int(time.time() * 1000),
                              'execution_chart_id': 'mt5-execution', 'public_exchange_order_routing': False}

    def ai_snapshot(self):
        with self._cache_lock:
            value = copy.deepcopy(self._ai_cache)
        now = int(time.time() * 1000)
        for chart in value['charts']:
            stamp = chart.get('fetched_at_ms')
            chart['age_seconds'] = max(0, (now - stamp) / 1000) if stamp else None
            chart['stale'] = stamp is None or now - stamp > 90000
        return value

    def present_active(self):
        page = self.active_page
        if page is None:
            self.window.render_card()
            if self.window.payload:
                self.window.nodes.set_state(self.window.payload.get('results', []))
            return
        if page.bundle is None:
            self.window.card.setText('<b>CHƯA CÓ DỮ LIỆU</b><br>' + html.escape(page.status.text()))
            return
        bundle, snapshot = page.bundle, page.bundle['snapshot']
        quant = bundle['quant']
        signal = quant.get('signal', 'INSUFFICIENT_DATA')
        stale, ai_stale = self._display_age_key(page)
        if stale:
            signal = 'STALE'
        label = 'CHƯA ĐỦ DỮ LIỆU' if signal == 'INSUFFICIENT_DATA' else signal
        if stale:
            label = 'DỮ LIỆU CŨ · CẦN LÀM MỚI'
        color = {'BUY': '#37d67a', 'SELL': '#ff6b7a', 'SIDEWAYS': '#f5b942'}.get(signal, '#9aa4bb')
        ai = page.ai_result or {}
        conclusion = ai.get('conclusion') or ai.get('summary') or 'Chưa hỏi AI cho chart này.'
        ai_heading = 'Kết luận AI'
        if ai:
            stamp = time.strftime('%H:%M:%S UTC', time.gmtime(ai.get('received_at_ms', 0) / 1000))
            ai_heading += ' · ' + stamp + (' · NHẬN ĐỊNH CŨ' if ai_stale else ' · ' + str(ai.get('signal', 'UNAVAILABLE')))
        self.window.card.setText(f'<div style="font-size:25px;color:{color};font-weight:bold">{label}</div>'
            f'<b>{html.escape(snapshot.instrument.symbol)} · {html.escape(snapshot.instrument.provider)} · {snapshot.timeframe}</b><br>'
            + html.escape(quant.get('summary', '')) + '<br><br><b>' + html.escape(ai_heading) + '</b><br>' + html.escape(str(conclusion)[:1200]) +
            '<br><br><span style="color:#9aa4bb">Chart nghiên cứu. Chưa gắn tài khoản đặt lệnh crypto.</span>')
        self.window.nodes.title = f'{snapshot.instrument.provider} · {snapshot.instrument.symbol}'
        self.window.nodes.set_state(bundle['original']['results'], sub=signal + ' · nến đã đóng')
        rows = [f'<b>{html.escape(str(method.get("name", "")))}</b>: {html.escape(str(method.get("signal", "")))} · '
                + html.escape('; '.join(str(item) for item in method.get('observations', [])))
                for method in quant.get('methods', [])]
        self.window.txt_ai.setHtml('<b>Nhận định hợp nhất · định lượng + tám phương pháp gốc</b><br>' +
            html.escape(quant.get('summary', '')) + '<br><br>' + '<br>'.join(rows) +
            '<br><br><b>' + html.escape(ai_heading) + ':</b> ' + html.escape(str(conclusion)[:1200]))

    def ask_ai(self, page=None):
        page = page or self.active_page
        if page is None:
            self.window.start_analysis(True)
            return
        if not page.bundle:
            self.window.say('Cần tải dữ liệu chart thành công trước khi hỏi AI.')
            return
        snapshot = page.bundle['snapshot']
        if time.time() * 1000 - snapshot.fetched_at_ms > 90000:
            self.window.say('Dữ liệu chart đã cũ; làm mới trước khi hỏi AI.')
            return
        provider = self.window.cfg.get('ai_provider', 'gemini')
        if not self.legacy.prov_conf(self.window.cfg, provider).get('key'):
            self.window.say('Chưa có API key AI. Nhập tại Cài đặt → AI hoặc dùng AI Web với prompt đầy đủ.')
            return
        from .ai import parse_conclusion, subject_prompt
        cfg = copy.deepcopy(self.window.cfg)
        subject = {'source': f'{snapshot.instrument.provider}:{snapshot.instrument.market}',
                   'symbol': snapshot.instrument.symbol, 'timeframe': snapshot.timeframe,
                   'chart_id': page.id, 'mode': 'live' if snapshot.instrument.provider != 'MT5' else snapshot.mode,
                   'market': snapshot.instrument.market,
                   'execution_eligible': False, 'requested_at_ms': int(time.time() * 1000)}
        prompt = subject_prompt(subject, 'Nhận định thị trường chart này dựa trên toàn bộ bối cảnh được đính kèm. '
            'Trả JSON gồm signal BUY/SELL/SIDEWAYS, conclusion, reason, risk. Tối đa 180 từ; '
            'không đề xuất quản lý một lệnh ở nguồn hoặc symbol khác.', owner=self.window)
        token = uuid.uuid4().hex
        page.ai_token = token
        self._tokens[token] = weakref.ref(page)
        page.ai_button.setEnabled(False)
        self.update_cache()

        def request():
            text = self.legacy.llm_call(cfg, provider, self.legacy.SIGNAL_SYS,
                                       [{'role': 'user', 'content': prompt}], json_mode=True, log=self.window.log)
            result = parse_conclusion(text)
            result['subject'] = subject
            result['received_at_ms'] = int(time.time() * 1000)
            result['analysis_bar_id'] = self._closed_bar_time(snapshot)
            return result

        self.hub.submit('ai', token, request)

    def activate_mt5(self, instrument, timeframe):
        if instrument.provider != 'MT5':
            return
        self.window.cb_sym.setCurrentText(instrument.symbol)
        self.window.cb_tf.setCurrentText(timeframe)
        self.tabs.setCurrentWidget(self.primary_page)
        self.window.say(f'MT5 thực thi đã chọn {instrument.symbol} · {timeframe}; kiểm tra tài khoản trên thanh công cụ.')

    def _update_crypto_flow(self, page):
        # Hooked by the additive dock integration; primary MT5 order flow stays independent.
        callback = getattr(self.window, 'market_orderflow_updated', None)
        if callback and page is self.active_page:
            callback(page)

    def _stop_stream(self):
        stream, page = self._stream, self._stream_page
        self._stream = self._stream_page = self._mailbox = None
        if page is not None and not page.closed:
            page.stream_status = dict(page.stream_status, state='disconnected', streaming=False,
                                      message='Đã dừng: chỉ stream chart đang xem.')
            self._show_status(page)
        if stream:
            # Socket close/join can wait for the network; keep that off the GUI thread.
            threading.Thread(target=stream.stop, kwargs={'timeout': 2}, daemon=True,
                             name='PublicStream-stop').start()

    def _start_stream(self):
        page = self.active_page
        if self._closed or page is None or page.bundle is None or page.instrument.provider == 'MT5':
            return
        if self._stream_page is page:
            return
        self._stop_stream()
        if not PublicTradeStream.supports(page.instrument):
            page.stream_status = {'state': 'unavailable', 'message': 'Nguồn này dùng REST; chưa hỗ trợ stream.'}
            self._show_status(page)
            return
        mailbox = TradeMailbox()
        self._mailbox, self._stream_page = mailbox, page
        self._stream = self._stream_factory(page.instrument, mailbox.trades, mailbox.status)
        self._stream.start()

    def _show_status(self, page):
        page.status.setText(getattr(page, 'rest_status', 'Chưa có snapshot mới.') +
                            ' · Trade stream: ' + page.stream_status.get('message', 'chưa có trạng thái'))

    def _drain_stream(self):
        mailbox, page = self._mailbox, self._stream_page
        if not mailbox or page is None or page.closed or page is not self.active_page:
            return
        trades, status, dropped = mailbox.drain()
        if status:
            page.stream_status = status
        if dropped:
            page.stream_dropped += dropped
            page.stream_status = dict(page.stream_status, message=
                f'Quá tải hiển thị: bỏ {page.stream_dropped} prints; delta/sweep phiên không đầy đủ. Làm mới để lấy REST.')
        if status or dropped:
            self._show_status(page)
        if not trades or not page.bundle:
            return
        page.stream_trades.extend(trades)
        snapshot = page.bundle['snapshot']
        candles = update_price_candles(snapshot.candles, trades, snapshot.timeframe)
        quote = dict(snapshot.quote, last_trade=trades[-1]['price'],
                     last_trade_time_ms=trades[-1]['time_ms'])
        page.bundle['snapshot'] = replace(snapshot, candles=candles, quote=quote)
        page.chart.set_data(bars_from_candles(self.legacy, candles), page.bundle['original']['overlay'])
        callback = getattr(self.window, 'market_orderflow_updated', None)
        if callback:
            callback(page, trades)
        # The 1-second timer publishes a bounded, thread-safe AI snapshot.

    def open_research(self, page=None):
        from .research import ResearchDialog
        page = page or self.active_page
        if page and page.bundle:
            snapshot = page.bundle['snapshot']
            candles, symbol, tf = snapshot.candles, page.instrument.symbol, snapshot.timeframe
        elif page is None:
            snapshot = self._primary_snapshot() or {}
            candles, symbol, tf = snapshot.get('candles', []), snapshot.get('symbol', ''), snapshot.get('timeframe', '')
        else:
            self.window.say('Tải chart trước khi kiểm chứng phương pháp.')
            return
        dialog = ResearchDialog(self.window, self.hub, copy.deepcopy(candles), symbol, tf)
        self._research_dialogs.append(dialog)
        dialog.finished.connect(lambda *_: self._research_dialogs.remove(dialog) if dialog in self._research_dialogs else None)
        dialog.show()

    def shutdown(self):
        self._closed = True
        self.timer.stop()
        self.live_timer.stop()
        self._stop_stream()
        for dialog in list(self._research_dialogs):
            dialog.close()
        for page in self._pages.values():
            page.closed = True
        self.hub.shutdown()
