"""Painting helpers for the additive dock. Original chart interaction remains intact."""
import bisect
from datetime import datetime, timezone
import math
from PyQt6 import QtCore, QtGui, QtWidgets


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


class FlowCanvas(QtWidgets.QWidget):
    def __init__(self, heatmap=False):
        super().__init__()
        self.heatmap = heatmap
        self.snapshot, self.history = None, ()
        self.visible = 90
        self.setMinimumSize(320, 190)
        self.setToolTip("Lăn chuột để zoom nến. Heatmap là depth lấy mẫu trong phiên, không phải lịch sử tải từ broker.")

    def set_data(self, snapshot, history):
        self.snapshot, self.history = snapshot, tuple(history)
        self.update()

    def wheelEvent(self, event):
        self.visible = min(240, max(20, self.visible + (-10 if event.angleDelta().y() > 0 else 10)))
        self.update()
        event.accept()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#070c15"))
        painter.setPen(QtGui.QColor("#9eacc4"))
        if not self.snapshot:
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Chọn nguồn rồi bấm Kết nối Order Flow")
        elif self.heatmap:
            self._paint_heatmap(painter)
        else:
            self._paint_candles(painter)

    def _paint_candles(self, painter):
        candles = self.snapshot.candles[-self.visible:]
        if not candles:
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                             "Chưa có trade prints với volume thật. Quotes không tạo big trade.")
            return
        plot = QtCore.QRectF(10, 24, max(1, self.width() - 94), max(1, self.height() - 55))
        low, high = min(c.low for c in candles), max(c.high for c in candles)
        pad = max((high - low) * .1, abs(high) * .00001, .01)
        low, high = low - pad, high + pad
        width = plot.width() / max(15, len(candles))
        y = lambda price: plot.bottom() - (price - low) / (high - low) * plot.height()
        x = lambda index: plot.left() + (index + .5) * width
        for i in range(5):
            price = low + (high - low) * i / 4
            yy = y(price)
            painter.setPen(QtGui.QColor("#1c2638"))
            painter.drawLine(QtCore.QPointF(plot.left(), yy), QtCore.QPointF(plot.right(), yy))
            painter.setPen(QtGui.QColor("#9eacc4"))
            painter.drawText(QtCore.QPointF(plot.right() + 5, yy + 4), f"{price:.5f}".rstrip('0').rstrip('.'))
        painter.save()
        painter.setClipRect(plot)
        for i, candle in enumerate(candles):
            color = QtGui.QColor("#2ecc91" if candle.close >= candle.open else "#ff627d")
            painter.setPen(QtGui.QPen(color, 1))
            painter.drawLine(QtCore.QPointF(x(i), y(candle.high)), QtCore.QPointF(x(i), y(candle.low)))
            painter.fillRect(QtCore.QRectF(x(i) - width * .3, min(y(candle.open), y(candle.close)),
                max(1, width * .6), max(1, abs(y(candle.open) - y(candle.close)))), color)
        times = [c.time_ms for c in candles]
        draw_bubbles(painter, self.snapshot.big_trades, times, x, y, times[0], plot)
        painter.restore()
        painter.setPen(QtGui.QColor("#9eacc4"))
        painter.drawText(10, 16, f"Nến từ prints · Δ {self.snapshot.delta:+.2f} · UTC")
        for i in range(0, len(candles), max(1, len(candles) // 5)):
            painter.drawText(QtCore.QPointF(x(i), self.height() - 8), utc(candles[i].time_ms)[:8])

    def _paint_heatmap(self, painter):
        history = self.history[-160:]
        if not history or not history[-1][1]:
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Chưa có displayed depth / DOM.")
            return
        latest = history[-1][1]
        reference = sum(level.price for level in latest) / len(latest)
        selected = sorted({level.price for _, levels in history for level in levels},
                          key=lambda price: abs(price - reference))[:50]
        prices = sorted(selected, reverse=True)
        rows = {price: row for row, price in enumerate(prices)}
        maximum = max((level.size for _, levels in history for level in levels if level.price in rows), default=1)
        plot = QtCore.QRectF(10, 26, max(1, self.width() - 94), max(1, self.height() - 58))
        dx, dy = plot.width() / len(history), plot.height() / max(1, len(prices))
        for i, (_, levels) in enumerate(history):
            for level in levels:
                if level.price not in rows:
                    continue
                alpha = int(20 + 235 * math.sqrt(level.size / maximum))
                color = QtGui.QColor(28, 206, 170, alpha) if level.side == "Bid" else QtGui.QColor(255, 167, 56, alpha)
                painter.fillRect(QtCore.QRectF(plot.left() + i * dx, plot.top() + rows[level.price] * dy,
                                               max(dx, .5), max(dy, .5)), color)
        painter.setPen(QtGui.QColor("#9eacc4"))
        painter.drawText(10, 16, "Depth lấy mẫu trong phiên · Bid xanh / Ask vàng")
        for i in range(0, len(prices), max(1, len(prices) // 7)):
            painter.drawText(QtCore.QPointF(plot.right() + 4, plot.top() + (i + .7) * dy), f"{prices[i]:.4f}")
        painter.drawText(10, self.height() - 8, utc(history[0][0])[:8])
        painter.drawText(self.width() - 150, self.height() - 8, utc(history[-1][0])[:8] + " UTC")


def draw_bubbles(painter, trades, times, x, y, first_ms, plot):
    visible = [trade for trade in trades[:1500] if trade.time_ms >= first_ms]
    maximum = max((trade.size for trade in visible), default=1)
    for trade in visible:
        index = bisect.bisect_right(times, trade.time_ms) - 1
        if index < 0:
            continue
        center = QtCore.QPointF(x(index), y(trade.price))
        if not plot.contains(center):
            continue
        radius = 4 + 12 * math.sqrt(trade.size / maximum)
        color = QtGui.QColor("#31dfb1" if trade.side == "Buy" else "#ff667d" if trade.side == "Sell" else "#b8bfd0")
        color.setAlpha(65)
        painter.setBrush(color)
        color.setAlpha(190)
        painter.setPen(QtGui.QPen(color, 1))
        painter.drawEllipse(center, radius, radius)


def install_chart_overlay(legacy):
    if getattr(legacy.ChartWidget, "_orderflow_extended", False):
        return

    class BubbleChart(legacy.ChartWidget):
        _orderflow_extended = True
        orderflow_dock = None

        def paintEvent(self, event):
            super().paintEvent(event)
            dock, geo = self.orderflow_dock, self._geo
            if not dock or not dock.overlay.isChecked() or dock.source.currentIndex() != 1 or not geo or self.bars is None:
                return
            if self.title.split(',', 1)[0] != dock.active_symbol:
                return
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            plot = QtCore.QRectF(geo['L'], geo['T'], geo['R'] - geo['L'], geo['B'] - geo['T'])
            painter.setClipRect(plot)
            times = [int(t) * 1000 for t in self.bars.t]
            x = lambda i: geo['L'] + (i - geo['left'] + .5) * geo['w']
            y = lambda price: geo['T'] + (geo['hi'] - price) / (geo['hi'] - geo['lo']) * (geo['B'] - geo['T'])
            first = max(0, int(geo['left']))
            draw_bubbles(painter, dock.last_snapshot.big_trades, times, x, y, times[first], plot)

    legacy.ChartWidget = BubbleChart
