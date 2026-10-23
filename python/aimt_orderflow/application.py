from PyQt6 import QtCore, QtWidgets
from .dock import OrderFlowDock
from .mt5_bridge import install
from .widgets import install_chart_overlay


def create_window(legacy):
    install(legacy)
    install_chart_overlay(legacy)

    class EnhancedMain(legacy.Main):
        def __init__(self):
            super().__init__()
            self.orderflow = OrderFlowDock(self, legacy)
            self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.orderflow)
            self.orderflow.hide()  # Original startup layout and active Panel tab are preserved.
            self.chart.orderflow_dock = self.orderflow
            self.orderflow_button = QtWidgets.QPushButton("Order Flow")
            self.orderflow_button.setToolTip("Mở dock bổ sung: big trades, DOM, wall, sweep, heatmap. Ctrl+Shift+O")
            self.orderflow_button.setShortcut("Ctrl+Shift+O")
            self.orderflow_button.clicked.connect(lambda: self.show_dock(self.orderflow))
            self.centralWidget().layout().itemAt(2).layout().addWidget(self.orderflow_button)

        def closeEvent(self, event):
            self.orderflow.shutdown()
            self.chart.orderflow_dock = None
            for timer in (self.timer, self.dock_timer, self.embed_timer):
                timer.stop()
            QtWidgets.QApplication.instance().removeEventFilter(self)
            super().closeEvent(event)

    return EnhancedMain()
