from collections import deque
from dataclasses import asdict
import time
from PyQt6 import QtCore, QtGui, QtWidgets

from .engine import FlowEngine, Settings
from .feeds import BrokerFeed, DemoFeed, FeedSignals
from .widgets import FlowCanvas, utc


class OrderFlowDock(QtWidgets.QDockWidget):
    """An optional dock; never replaces the original chart, panel, AI or account tabs."""
    def __init__(self, app, legacy):
        super().__init__("Order Flow · phần bổ sung", app)
        self.app, self.legacy = app, legacy
        self.setObjectName("OrderFlowDock")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        try:
            settings = Settings(**app.cfg.get("orderflow", {}))
            settings.validate()
        except (ValueError, TypeError):
            settings = Settings()
        self.engine = FlowEngine(settings)
        self.last_snapshot = self.engine.snapshot()
        self.history = deque(maxlen=160)
        self.worker = self.demo = None
        self.active_symbol = ""
        self._generation = 0
        self._running = False
        self._shutting_down = False
        self._last_frame = None
        self.signals = FeedSignals(self)
        self.signals.frame.connect(self._receive)
        self.signals.status.connect(self._on_status)
        root = QtWidgets.QWidget()
        self.setWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        row = QtWidgets.QHBoxLayout()
        self.source = QtWidgets.QComboBox()
        self.source.addItems(["DEMO order flow (dữ liệu giả)", "MT5 đang chọn · trades/DOM", "Rithmic plugin · chưa có SDK"])
        self.connect_button = QtWidgets.QPushButton("Kết nối Order Flow")
        self.stop_button = QtWidgets.QPushButton("Ngắt")
        self.stop_button.setEnabled(False)
        self.overlay = QtWidgets.QCheckBox("Bubble trên chart gốc (chỉ MT5)")
        self.overlay.setChecked(True)
        row.addWidget(self.source, 2)
        row.addWidget(self.connect_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.overlay)
        layout.addLayout(row)
        self.status = QtWidgets.QLabel("Chưa kết nối order flow. Các chức năng của ứng dụng gốc vẫn giữ nguyên.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        filters = QtWidgets.QHBoxLayout()
        self.inputs = {}
        for key, label, high, decimals in (
            ("min_trade", "Big trade ≥", 1e9, 3), ("min_wall", "Wall ≥", 1e9, 3),
            ("sweep_volume", "Sweep volume ≥", 1e9, 3), ("sweep_levels", "Mức giá", 1000, 0),
            ("sweep_ms", "Cửa sổ ms", 60000, 0), ("candle_seconds", "Nến giây", 86400, 0)):
            spin = QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox()
            spin.setRange(0 if decimals else 2 if key == 'sweep_levels' else 1, high if decimals else int(high))
            if decimals:
                spin.setDecimals(decimals)
            spin.setValue(getattr(settings, key))
            spin.setMaximumWidth(110)
            self.inputs[key] = spin
            filters.addWidget(QtWidgets.QLabel(label))
            filters.addWidget(spin)
        self.apply_button = QtWidgets.QPushButton("Áp dụng")
        filters.addWidget(self.apply_button)
        layout.addLayout(filters)
        self.summary = QtWidgets.QLabel("Bid — | Ask — | Δ 0 | Chưa có trade prints")
        self.summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        charts = QtWidgets.QTabWidget()
        self.chart = FlowCanvas()
        self.heatmap = FlowCanvas(heatmap=True)
        charts.addTab(self.chart, "Nến + Big trades")
        charts.addTab(self.heatmap, "Heatmap trong phiên")
        self.tables = QtWidgets.QTabWidget()
        self.dom = self._table(["Phía", "Giá", "Displayed size"])
        self.tape = self._table(["UTC", "Aggressor", "Giá", "Size"])
        self.events = self._table(["UTC", "Tín hiệu", "Giá", "Size"])
        self.tables.addTab(self.dom, "DOM / Wall")
        self.tables.addTab(self.tape, "Big trades")
        self.tables.addTab(self.events, "Sweep / Wall")
        split.addWidget(charts)
        split.addWidget(self.tables)
        split.setSizes([700, 400])
        layout.addWidget(split, 1)
        note = QtWidgets.QLabel("Ngưỡng dùng đơn vị volume của nguồn dữ liệu. Marketable limit cũng tạo trade prints; "
            "wall là tổng displayed size theo giá, không phải một lệnh. Không phải công thức MZpack độc quyền. "
            "Dock này không gửi lệnh.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa4bb;font-size:10px")
        layout.addWidget(note)
        self.connect_button.clicked.connect(self.start_feed)
        self.stop_button.clicked.connect(self.stop_feed)
        self.apply_button.clicked.connect(self.apply_settings)
        self.overlay.toggled.connect(lambda _: self.app.chart.update())
        self.source.currentIndexChanged.connect(self._selection_changed)
        self.app.cb_sym.currentTextChanged.connect(self._selection_changed)
        self.app.cb_acc.currentTextChanged.connect(self._selection_changed)
        self._demo_timer = QtCore.QTimer(self)
        self._demo_timer.setInterval(150)
        self._demo_timer.timeout.connect(self._demo_step)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start()

    @staticmethod
    def _table(headers):
        table = QtWidgets.QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().hide()
        table.setAlternatingRowColors(True)
        return table

    def apply_settings(self):
        try:
            settings = Settings(**{key: spin.value() for key, spin in self.inputs.items()})
            self.engine.update_settings(settings)
            self.app.cfg['orderflow'] = asdict(settings)
            self.legacy.save_cfg(self.app.cfg)
            self.app.log("Order Flow: đã áp dụng ngưỡng. Đổi timeframe chỉ xóa nến của dock, không đổi chart/phân tích gốc.")
            self.refresh()
            return True
        except ValueError as error:
            self.status.setText(str(error))
            return False

    def start_feed(self):
        if self._shutting_down or self._running:
            return
        if self.worker and self.worker.is_alive():
            self.status.setText("Đang dừng tác vụ MT5 trước; chờ một chút rồi kết nối lại.")
            return
        if not self.apply_settings():
            return
        self.stop_feed()
        self.engine.reset()
        self.history.clear()
        self.last_snapshot = self.engine.snapshot()
        self._quote_text, self._last_frame = 'Bid — | Ask —', None
        self.active_symbol = self.app.acc_symbol(self.app.primary()) if self.app.primary() else self.app.cfg['symbol']
        mode = self.source.currentIndex()
        if mode == 2:
            self.status.setText("Rithmic chưa khả dụng: thiếu SDK/adapter plugin chính thức. Không mở kết nối và không tự chuyển qua demo.")
            self.app.log(self.status.text())
            self.refresh()
            return
        if mode == 1 and self.app.broker.demo:
            self.status.setText("Ứng dụng gốc hiện đang mô phỏng MT5. Hãy cấu hình terminal MT5 thật hoặc chọn DEMO order flow riêng.")
            self.refresh()
            return
        self._running = True
        self.connect_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.source.setEnabled(False)
        if mode == 0:
            self.demo = DemoFeed(self.active_symbol)
            now_ms = int(time.time() * 1000)
            for i in range(300):
                self._receive(self._generation, self.demo.frame(now_ms - (300 - i) * 1000))
            self._demo_timer.start()
        else:
            self.worker = BrokerFeed(self.app.broker, self.app.primary(), self.active_symbol, self.signals, self._generation)
            self.worker.start()
        self.refresh()

    def _demo_step(self):
        if self._running and self.demo:
            self._receive(self._generation, self.demo.frame())

    def _receive(self, generation, frame):
        if generation != self._generation or not self._running or self._shutting_down:
            return
        try:
            self.active_symbol = frame.get('symbol', self.active_symbol)
            for trade in frame['trades']:
                self.engine.trade(trade)
            accepted = self.engine.depth(frame['time_ms'], frame['levels'])
            snapshot = self.engine.snapshot()
            if accepted:
                bids = [level for level in snapshot.book if level.side == 'Bid'][:30]
                asks = [level for level in snapshot.book if level.side == 'Ask'][:30]
                self.history.append((frame['time_ms'], tuple(bids + asks)))
            self._last_frame = time.monotonic()
            prefix = "DEMO" if self.source.currentIndex() == 0 else f"MT5 · {self.active_symbol}"
            self.status.setText(prefix + " · " + (frame.get('warning') or "Đang nhận dữ liệu từ terminal. Không phải Rithmic."))
            bid, ask = frame.get('bid'), frame.get('ask')
            self._quote_text = f"Bid {bid:g}" if bid is not None else "Bid —"
            self._quote_text += f" | Ask {ask:g}" if ask is not None else " | Ask —"
        except (ValueError, KeyError, TypeError) as error:
            self.stop_feed()
            self.status.setText(f"Dữ liệu order flow không hợp lệ; đã dừng: {error}")
        finally:
            if self.worker:
                self.worker.acknowledge()

    def _on_status(self, generation, text):
        if generation != self._generation or self._shutting_down:
            return
        if text.startswith("Lỗi"):
            self.stop_feed()
        self.status.setText(text)

    def _selection_changed(self, *_):
        if self._shutting_down:
            return
        was_running = self._running
        self.stop_feed()
        self.engine.reset()
        self.history.clear()
        self._last_frame = None
        self._quote_text = "Bid — | Ask —"
        self.refresh()
        if was_running:
            self.status.setText("Đã đổi tài khoản/symbol/nguồn: kết nối lại Order Flow để bắt đầu phiên mới.")

    def stop_feed(self):
        self._generation += 1
        self._running = False
        self._demo_timer.stop()
        self.demo = None
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=.2)
        self.connect_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.source.setEnabled(True)
        self.status.setText("Đã ngắt order flow. Giữ ảnh chụp cuối; không ảnh hưởng kết nối/tác vụ gốc.")

    def refresh(self):
        if self._shutting_down:
            return
        snapshot = self.last_snapshot = self.engine.snapshot()
        self.chart.set_data(snapshot, self.history)
        self.heatmap.set_data(snapshot, self.history)
        self.app.chart.update()
        self.summary.setText(getattr(self, '_quote_text', 'Bid — | Ask —') +
            f" | Δ {snapshot.delta:+.3f} | {len(snapshot.candles)} nến | {len(snapshot.big_trades)} big prints giữ trong bộ nhớ")
        if self._running and self._last_frame and time.monotonic() - self._last_frame > 5:
            self.status.setText("Order flow chưa có frame mới >5 giây; dữ liệu hiển thị đã cũ. Không tự tạo dữ liệu thay thế.")
        book = [x for x in snapshot.book if x.side == 'Bid'][:30] + [x for x in snapshot.book if x.side == 'Ask'][:30]
        rows = [(x.side, f'{x.price:g}', f'{x.size:g}') for x in book]
        self._fill(self.dom, rows, [x.size >= self.engine.settings.min_wall for x in book])
        self._fill(self.tape, [(utc(t.time_ms), t.side, f'{t.price:g}', f'{t.size:g}') for t in snapshot.big_trades[:100]])
        self._fill(self.events, [(utc(s.time_ms), s.kind, f'{s.price:g}', f'{s.size:g}') for s in snapshot.signals[:100]],
                   tips=[s.detail for s in snapshot.signals[:100]])

    @staticmethod
    def _fill(table, rows, walls=(), tips=()):
        signature = (tuple(rows), tuple(walls), tuple(tips))
        if getattr(table, '_flow_signature', None) == signature:
            return
        table._flow_signature = signature
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if row < len(walls) and walls[row]:
                    item.setForeground(QtGui.QColor('#ffca76'))
                    item.setBackground(QtGui.QColor('#40301a'))
                if row < len(tips):
                    item.setToolTip(tips[row])
                table.setItem(row, column, item)
        table.setUpdatesEnabled(True)

    def shutdown(self):
        self.stop_feed()
        self._shutting_down = True
        self._refresh_timer.stop()
        if self.worker:
            self.worker.join(timeout=2)
