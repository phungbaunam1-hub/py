from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time

import pytest
from PyQt6 import QtCore, QtWidgets

from aimt_market.research import ResearchDialog


_APPLICATION = None


@pytest.fixture(scope='module')
def app():
    global _APPLICATION
    _APPLICATION = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APPLICATION


class _Signals(QtCore.QObject):
    result = QtCore.pyqtSignal(str, str, object)


class _Hub:
    def __init__(self, execute=True):
        self.signals = _Signals()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.execute = execute
        self.calls = []
        self.thread = None

    def submit(self, kind, token, function):
        self.calls.append((kind, token))
        if not self.execute:
            return Future()

        def work():
            self.thread = threading.get_ident()
            try:
                value = {'ok': True, 'data': function()}
            except Exception as error:
                value = {'ok': False, 'error': str(error)}
            self.signals.result.emit(kind, token, value)
        return self.pool.submit(work)

    def shutdown(self):
        self.pool.shutdown(wait=True)


def _candles(count=300):
    return [{'time': i * 60, 'open': 100 + i * .1, 'high': 100.3 + i * .1,
             'low': 99.7 + i * .1, 'close': 100 + i * .1, 'closed': True}
            for i in range(count)]


def _wait(app, predicate):
    start = time.monotonic()
    while not predicate():
        assert time.monotonic() - start < 5, 'Dialog worker did not complete'
        app.processEvents()
        time.sleep(.005)
    app.processEvents()


def test_research_runs_worker_on_owned_cache_and_renders_oos_not_train_as_result(app):
    hub = _Hub()
    cached = _candles()
    dialog = ResearchDialog(None, hub, cached, 'BTC/USDT', 'M1')
    dialog.show()
    try:
        # Later cache mutation must not alter the candle snapshot owned by the dialog.
        cached.clear()
        dialog.run_button.click()
        assert not dialog.run_button.isEnabled()
        _wait(app, lambda: dialog.report is not None)
        assert hub.thread != threading.get_ident()
        assert hub.calls[0][0] == 'research'
        assert dialog.report['closed_candle_count'] == 300
        assert dialog.report['status'] == 'complete'
        assert dialog.folds.rowCount() == 2
        assert 'OUT-OF-SAMPLE' in dialog.metrics.text()
        assert 'không annualize' in dialog.metrics.text()
        assert dialog.curve.points and dialog.run_button.isEnabled()
        assert 'GIẢ ĐỊNH' in dialog.notes.toPlainText()
    finally:
        dialog.close()
        hub.shutdown()


def test_insufficient_cache_shows_no_fake_zero_trade_success(app):
    hub = _Hub()
    dialog = ResearchDialog(None, hub, _candles(299), 'EUR/USD', 'M5')
    try:
        dialog.run_research()
        _wait(app, lambda: dialog.report is not None)
        assert dialog.report['status'] == 'insufficient'
        assert 'Chưa đủ dữ liệu' in dialog.status.text()
        assert 'Không có kết quả ngoài mẫu' in dialog.metrics.text()
        assert dialog.folds.rowCount() == 0 and not dialog.curve.points
    finally:
        dialog.close()
        hub.shutdown()


def test_wrong_tokens_and_closed_dialog_ignore_late_worker_results(app):
    hub = _Hub(execute=False)
    dialog = ResearchDialog(None, hub, _candles(), 'TEST', 'M1')
    try:
        dialog.run_research()
        token = hub.calls[-1][1]
        hub.signals.result.emit('chart', token, {'ok': True, 'data': {}})
        hub.signals.result.emit('research', 'old-token', {'ok': True, 'data': {}})
        app.processEvents()
        assert dialog.report is None
        dialog.close()
        hub.signals.result.emit('research', token, {'ok': True, 'data': {}})
        app.processEvents()
        assert dialog.report is None and dialog._closed
    finally:
        dialog.close()
        hub.shutdown()


def test_closed_hub_does_not_leave_run_button_stuck(app):
    hub = _Hub(execute=False)
    hub.submit = lambda *_: None
    dialog = ResearchDialog(None, hub, _candles(), 'TEST', 'M1')
    try:
        dialog.run_research()
        assert dialog.run_button.isEnabled()
        assert 'đã đóng' in dialog.status.text()
    finally:
        dialog.close()
        hub.shutdown()
