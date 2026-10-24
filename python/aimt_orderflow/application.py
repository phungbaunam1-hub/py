from PyQt6 import QtCore, QtWidgets
from .dock import OrderFlowDock
from .mt5_bridge import install
from .widgets import install_chart_overlay
from aimt_market.ai import install_ai_hooks, publish_orderflow_context
from aimt_market.execution import ExecutionScopeMixin
from aimt_market.hotkeys import install_confirmation
from aimt_market.workspace import MarketWorkspace


def create_window(legacy, *, client_factory=None, stream_factory=None):
    install(legacy)
    install_chart_overlay(legacy)

    class EnhancedMain(ExecutionScopeMixin, legacy.Main):
        order_dispatch_changed = QtCore.pyqtSignal(object)

        def __init__(self):
            self._init_execution_scope(legacy)
            super().__init__()
            self.market_workspace = MarketWorkspace(self, legacy, client_factory, stream_factory)
            left = self.hs.widget(1)
            primary_chart = left.replaceWidget(0, self.market_workspace)
            self.market_workspace.attach_primary_chart(primary_chart)
            self.orderflow = OrderFlowDock(self, legacy)
            self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.orderflow)
            self.orderflow.hide()  # Original startup layout and active Panel tab are preserved.
            self.chart.orderflow_dock = self.orderflow
            self.orderflow_button = QtWidgets.QPushButton("Order Flow")
            self.orderflow_button.setToolTip("Mở dock bổ sung: big trades, DOM, wall, sweep, heatmap. Ctrl+Shift+O")
            self.orderflow_button.setShortcut("Ctrl+Shift+O")
            self.orderflow_button.clicked.connect(lambda: self.show_dock(self.orderflow))
            self.centralWidget().layout().itemAt(2).layout().addWidget(self.orderflow_button)
            self.research_button = QtWidgets.QPushButton('Kiểm chứng')
            self.research_button.setToolTip('So sánh phương pháp trên nến đã đóng, phí và walk-forward ngoài mẫu.')
            self.research_button.clicked.connect(lambda: self.market_workspace.open_research())
            self.centralWidget().layout().itemAt(2).layout().addWidget(self.research_button)
            self.order_dispatch_status = QtWidgets.QLabel('MT5 · hàng đợi sẵn sàng')
            self.order_dispatch_status.setToolTip('Hàng đợi Python có giới hạn; completed chỉ là tác vụ đã trả kết quả. '
                'Khớp toàn bộ/một phần/đã nhận lệnh theo xác nhận native MT5 trong Nhật ký. '
                'Đây không phải CTrade.SetAsyncMode của MQL5; kết quả unknown không tự gửi lại. '
                'Khóa unknown được lưu qua lần khởi động; kiểm tra MT5 rồi gõ ASYNC RESUME để mở lại.')
            self.centralWidget().layout().itemAt(2).layout().addWidget(self.order_dispatch_status)
            self._init_order_dispatcher()
            self._scope_ready = True
            self._selection_changed()
            for control in (self.cb_acc, self.cb_sym, self.cb_tf):
                control.currentTextChanged.connect(self._selection_changed)
            self.market_workspace.tabs.currentChanged.connect(self.market_selection_changed)
            install_confirmation(self, legacy)
            self._remove_ai_hooks = install_ai_hooks(legacy, self)
            self.orderflow._refresh_timer.timeout.connect(lambda: publish_orderflow_context(self.orderflow))
            self.orderflow.source.currentIndexChanged.connect(lambda *_: publish_orderflow_context(self.orderflow))
            self.orderflow.connect_button.clicked.connect(lambda *_: publish_orderflow_context(self.orderflow))
            self.orderflow.stop_button.clicked.connect(lambda *_: publish_orderflow_context(self.orderflow))
            publish_orderflow_context(self.orderflow)
            if callable(getattr(self.orderflow, 'market_updated', None)):
                def market_updated(page, trades=None):
                    self.orderflow.market_updated(page, trades)
                    publish_orderflow_context(self.orderflow)
                self.market_orderflow_updated = market_updated

        def chat_png(self):
            page = self.market_workspace.active_page
            if page is not None and page.bundle is not None:
                snapshot = page.bundle['snapshot']
                self._ai_image_subject = {'chart_id': page.id, 'source': snapshot.instrument.provider,
                    'market': snapshot.instrument.market, 'symbol': snapshot.instrument.symbol,
                    'timeframe': snapshot.timeframe, 'mode': getattr(snapshot, 'mode', 'live'), 'execution_eligible': False}
                return legacy.render_png(page.chart.bars, page.bundle['original']['overlay'])
            self._ai_image_subject = self.ai_web_subject()
            return super().chat_png()

        def closeEvent(self, event):
            with self._scope_lock:
                self._execution_state.update(closed=True, primary_active=False)
            self._preserve_outstanding_native_entries()
            self._order_dispatcher.shutdown(wait=False, cancel_pending=True)
            self._remove_ai_hooks()
            self.market_workspace.shutdown()
            self.orderflow.shutdown()
            self.chart.orderflow_dock = None
            for timer in (self.timer, self.dock_timer, self.embed_timer):
                timer.stop()
            QtWidgets.QApplication.instance().removeEventFilter(self)
            super().closeEvent(event)

    return EnhancedMain()
