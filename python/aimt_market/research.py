"""Asynchronous, read-only walk-forward research over an owned cached candle snapshot."""
import copy
from datetime import datetime, timezone
import json
import math
import uuid

from PyQt6 import QtCore, QtGui, QtWidgets

from .backtest import walk_forward_report


def _date(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%d/%m %H:%M')
    except (ValueError, TypeError, OverflowError, OSError):
        return '—'


def _value(value, suffix='', signed=False):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return '—'
    if not math.isfinite(number):
        return '—'
    return (f'{number:+.2f}' if signed else f'{number:.2f}') + suffix


class _EquityCurve(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(140)
        self.points = []

    def set_points(self, points):
        self.points = [(float(point['time']), float(point['equity'])) for point in points
                       if math.isfinite(float(point['time'])) and math.isfinite(float(point['equity']))]
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor('#0b111c'))
        painter.setPen(QtGui.QColor('#b0bdcf'))
        if not self.points:
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, 'Chưa có đường equity ngoài mẫu')
            return
        plot = QtCore.QRectF(12, 24, max(1, self.width() - 98), max(1, self.height() - 48))
        first, last = self.points[0][0], self.points[-1][0]
        lower = min(10_000, min(value for _, value in self.points))
        upper = max(10_000, max(value for _, value in self.points))
        padding = max((upper - lower) * .1, abs(upper) * .001, .01)
        lower, upper = lower - padding, upper + padding
        x = lambda stamp: plot.left() + (stamp - first) / max(1, last - first) * plot.width()
        y = lambda value: plot.bottom() - (value - lower) / (upper - lower) * plot.height()
        for index in range(4):
            value = lower + (upper - lower) * index / 3
            painter.setPen(QtGui.QColor('#263147'))
            painter.drawLine(QtCore.QPointF(plot.left(), y(value)), QtCore.QPointF(plot.right(), y(value)))
            painter.setPen(QtGui.QColor('#9aaec8'))
            painter.drawText(QtCore.QPointF(plot.right() + 4, y(value) + 4), f'{value:,.1f}')
        path = QtGui.QPainterPath()
        for index, (stamp, value) in enumerate(self.points):
            if index == 0:
                path.moveTo(x(stamp), y(value))
            else:
                path.lineTo(x(stamp), y(value))
        painter.setPen(QtGui.QPen(QtGui.QColor('#38d4ad'), 1.8))
        painter.drawPath(path)
        painter.setPen(QtGui.QColor('#b0bdcf'))
        painter.drawText(12, 15, 'Equity ngoài mẫu sau phí · vốn mô phỏng 10.000 · không leverage')
        painter.drawText(12, self.height() - 6, _date(first) + ' UTC')
        painter.drawText(max(12, self.width() - 160), self.height() - 6, _date(last) + ' UTC')


class ResearchDialog(QtWidgets.QDialog):
    """Run CPU-only research via the shared worker hub; never fetch data or place orders."""
    def __init__(self, parent, hub, candles, symbol, timeframe):
        super().__init__(parent)
        self.hub = hub
        self._candles = copy.deepcopy(list(candles or ()))
        self._token = None
        self._future = None
        self._closed = False
        self.report = None
        self.setWindowTitle('Kiểm chứng phương pháp · ngoài mẫu')
        self.resize(1020, 720)
        self.setMinimumSize(800, 580)
        layout = QtWidgets.QVBoxLayout(self)
        self.header = QtWidgets.QLabel(f'{symbol} · {timeframe} · {len(self._candles)} nến cache')
        self.header.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.header.setWordWrap(True)
        layout.addWidget(self.header)
        description = QtWidgets.QLabel('Chỉ nghiên cứu lịch sử đã cache. Không gọi mạng, gửi lệnh hoặc tự thay cấu hình giao dịch.')
        description.setWordWrap(True)
        layout.addWidget(description)

        controls = QtWidgets.QHBoxLayout()
        self.family = QtWidgets.QComboBox()
        self.family.addItem('Xu hướng · EMA crossover', 'trend')
        self.family.addItem('Breakout · biên Donchian trước nến', 'breakout')
        self.family.addItem('Mean reversion · z-score', 'mean_reversion')
        self.fee = QtWidgets.QDoubleSpinBox()
        self.slippage = QtWidgets.QDoubleSpinBox()
        for spin, default in ((self.fee, 5), (self.slippage, 2)):
            spin.setRange(0, 1000)
            spin.setDecimals(2)
            spin.setSingleStep(.5)
            spin.setValue(default)
            spin.setSuffix(' bps')
            spin.setMaximumWidth(115)
        self.run_button = QtWidgets.QPushButton('Chạy walk-forward')
        controls.addWidget(self.family, 2)
        controls.addWidget(QtWidgets.QLabel('Phí mỗi chiều'))
        controls.addWidget(self.fee)
        controls.addWidget(QtWidgets.QLabel('Slippage mỗi chiều'))
        controls.addWidget(self.slippage)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        windows = QtWidgets.QHBoxLayout()
        self.train = QtWidgets.QSpinBox()
        self.train.setRange(60, 5000)
        self.train.setValue(150)
        self.test = QtWidgets.QSpinBox()
        self.test.setRange(10, 2000)
        self.test.setValue(60)
        windows.addWidget(QtWidgets.QLabel('Nến train'))
        windows.addWidget(self.train)
        windows.addWidget(QtWidgets.QLabel('Nến test/fold'))
        windows.addWidget(self.test)
        windows.addWidget(QtWidgets.QLabel('Long/flat · tối thiểu 300 nến đóng · signal close → next open'), 1)
        layout.addLayout(windows)
        self.status = QtWidgets.QLabel('Chọn họ phương pháp và chi phí giả định, rồi bấm Chạy.')
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.metrics = QtWidgets.QLabel('Chưa có kết quả ngoài mẫu.')
        self.metrics.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.metrics.setWordWrap(True)
        self.metrics.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.metrics)
        self.curve = _EquityCurve()
        layout.addWidget(self.curve)
        tabs = QtWidgets.QTabWidget()
        self.folds = QtWidgets.QTableWidget(0, 8)
        self.folds.setHorizontalHeaderLabels(['Fold / UTC', 'Tham số từ train', 'Train net', 'Test net',
                                             'Test DD', 'Trades', 'Chi phí test', 'Mốc test'])
        self.folds.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.folds.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.folds.setAlternatingRowColors(True)
        self.folds.verticalHeader().hide()
        self.folds.horizontalHeader().setStretchLastSection(True)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlainText('Kết quả lịch sử không phải đảm bảo lợi nhuận. Chi phí được tính cả lúc vào và thoát vị thế.')
        tabs.addTab(self.folds, 'Các fold ngoài mẫu')
        tabs.addTab(self.notes, 'Giả định / giới hạn')
        layout.addWidget(tabs, 1)
        self.close_button = QtWidgets.QPushButton('Đóng')
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.run_button.clicked.connect(self.run_research)
        self.hub.signals.result.connect(self._completed)
        self.finished.connect(self._detach)

    def _set_busy(self, busy):
        for widget in (self.run_button, self.family, self.fee, self.slippage, self.train, self.test):
            widget.setEnabled(not busy)

    def run_research(self):
        if self._closed or not self.run_button.isEnabled():
            return
        self._token = token = uuid.uuid4().hex
        options = {'family': self.family.currentData(), 'fee_bps': self.fee.value(),
                   'slippage_bps': self.slippage.value(), 'train_bars': self.train.value(),
                   'test_bars': self.test.value()}
        cached = copy.deepcopy(self._candles)
        self._set_busy(True)
        self.report = None
        self.folds.setRowCount(0)
        self.curve.set_points([])
        self.metrics.setText('Đang tính; chưa có kết quả mới.')
        self.status.setText('Đang chạy trên worker: chọn tham số chỉ bằng train, rồi kiểm chứng trên test chưa dùng để chọn.')
        self._future = self.hub.submit('research', token, lambda: walk_forward_report(cached, **options))
        if self._future is None:
            self._set_busy(False)
            self.status.setText('Bộ xử lý đã đóng; chưa chạy kiểm chứng.')

    def _completed(self, kind, token, result):
        if self._closed or kind != 'research' or token != self._token:
            return
        self._set_busy(False)
        self._future = None
        if not result.get('ok'):
            self.status.setText('Chưa có kết quả: ' + str(result.get('error', 'Lỗi worker.')))
            return
        self.report = report = result['data']
        self.notes.setPlainText('GIẢ ĐỊNH\n' + '\n'.join('• ' + text for text in report.get('assumptions', [])) +
                               '\n\nGIỚI HẠN\n' + '\n'.join('• ' + text for text in report.get('warnings', [])))
        if report.get('status') != 'complete':
            self.status.setText(f"Chưa đủ dữ liệu: {report.get('closed_candle_count', 0)} nến đóng hợp lệ; "
                                f"cần ít nhất {report.get('required_candle_count', 300)}.")
            self.metrics.setText('Không có kết quả ngoài mẫu. Thêm lịch sử thay vì coi 0 giao dịch là một kiểm chứng đạt.')
            return
        stats = report['combined_test_metrics']
        self.status.setText(f"Hoàn tất {len(report['windows'])} fold ngoài mẫu · grid nhỏ theo train · "
                            f"{report.get('unused_tail_bars', 0)} nến cuối chưa đủ fold bị bỏ.")
        self.metrics.setText(
            'OUT-OF-SAMPLE  Net ' + _value(stats['net_return_pct'], '%', True) +
            '  |  Gross ' + _value(stats['gross_return_pct'], '%', True) +
            '  |  Max drawdown ' + _value(stats['max_drawdown_pct'], '%') + '\n' +
            f"Round trips {stats['trade_count']}  |  Chi phí {_value(stats['costs'])} / vốn 10.000 giả định  |  "
            'Sharpe/bar ' + _value(stats['sharpe_per_bar']) + ' (không annualize)')
        self.curve.set_points(report['equity_curve'])
        self.folds.setRowCount(len(report['windows']))
        for row, window in enumerate(report['windows']):
            training, testing = window['train_metrics'], window['test_metrics']
            values = [str(window['fold']), json.dumps(window['chosen_parameters'], ensure_ascii=False, separators=(',', ':')),
                      _value(training['net_return_pct'], '%', True), _value(testing['net_return_pct'], '%', True),
                      _value(testing['max_drawdown_pct'], '%'), str(testing['trade_count']), _value(testing['costs']),
                      _date(window['test_start_time']) + ' → ' + _date(window['test_end_time'])]
            tip = 'Train UTC ' + _date(window['train_start_time']) + ' → ' + _date(window['train_end_time'])
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(tip)
                self.folds.setItem(row, column, item)
        self.folds.resizeColumnsToContents()

    def _detach(self, *_):
        if self._closed:
            return
        self._closed = True
        self._token = None
        if self._future is not None:
            self._future.cancel()
        try:
            self.hub.signals.result.disconnect(self._completed)
        except (TypeError, RuntimeError):
            pass

    def closeEvent(self, event):
        self._detach()
        super().closeEvent(event)
