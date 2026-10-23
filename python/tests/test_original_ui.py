import copy
import hashlib
from pathlib import Path
import sys
import time

import pytest
from PyQt6 import QtWidgets
from aimt_orderflow.application import create_window
from original import AI_MT_Trading as legacy


@pytest.fixture(scope='module')
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    original_hook = sys.excepthook
    monkeypatch.setattr(legacy, 'CFG_PATH', str(tmp_path / 'config.json'))
    load = legacy.load_cfg

    def config():
        cfg = load()
        cfg.update(force_demo=True, use_ai=False, news_feeds=[])
        return cfg

    monkeypatch.setattr(legacy, 'load_cfg', config)
    monkeypatch.setattr(legacy, 'fetch_news', lambda _: [])
    view = create_window(legacy)
    view.resize(1500, 950)
    view.show()
    yield view
    view.close()
    view.feed.join(timeout=2)
    view.copy.join(timeout=2)
    qt_app.processEvents()
    sys.excepthook = original_hook


def wait_for(qt_app, predicate, seconds=10):
    start = time.monotonic()
    while not predicate():
        assert time.monotonic() - start < seconds, 'Timed out waiting for real application behavior'
        qt_app.processEvents()
        time.sleep(.01)
    qt_app.processEvents()


def test_original_file_is_exact_user_upload():
    path = Path(legacy.__file__)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == 'b6265fb39c54e0235926be87544c5cdf177ad7728e33d582ff0fdc3554fc0909'


def test_original_panels_accounts_chat_and_chart_are_preserved(window, qt_app):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['Phân tích', 'Tài khoản', 'Lệnh', 'Nhật ký']
    assert window.panel and window.chat and window.web and window.nodes
    assert window.cb_tf.count() == 7
    assert window.cb_plan.count() == 4
    assert len(legacy.ANALYZERS) == 8
    assert window.orderflow.isHidden()
    assert window.orderflow_button.text() == 'Order Flow'
    wait_for(qt_app, lambda: window.chart.bars is not None)
    assert window.chart.bars.n > 100
    # Exercise the original eight-method pipeline, not an addon imitation.
    window.start_analysis(True)
    wait_for(qt_app, lambda: window.payload is not None and not window.busy)
    assert len(window.payload['results']) == 8
    assert all(result.state != 'Lỗi' for result in window.payload['results'])
    assert window.plan is not None


def test_addon_demo_does_not_replace_ui_or_route_orders(window, qt_app):
    # Let the original account feed initialize its running equity peak before comparison.
    wait_for(qt_app, lambda: window.chart.bars is not None)
    original_cfg = copy.deepcopy({k: v for k, v in window.cfg.items() if k != 'orderflow'})
    panel = window.panel
    window.orderflow_button.click()
    qt_app.processEvents()
    assert not window.orderflow.isHidden()
    window.orderflow.start_feed()
    wait_for(qt_app, lambda: window.orderflow.tape.rowCount() > 0 and window.orderflow.dom.rowCount() > 0)
    assert window.orderflow.last_snapshot.candles
    assert window.orderflow.last_snapshot.signals
    assert 'DEMO' in window.orderflow.status.text()
    assert window.panel is panel
    assert {k: v for k, v in window.cfg.items() if k != 'orderflow'} == original_cfg
    with window.broker.session(window.primary()):
        assert window.broker.positions() == [] and window.broker.orders() == []
    window.orderflow.stop_feed()
    before = window.orderflow.engine.snapshot()
    end = time.monotonic() + .35
    while time.monotonic() < end:
        qt_app.processEvents()
        time.sleep(.01)
    assert window.orderflow.engine.snapshot() == before


def test_source_change_resets_tape_and_missing_rithmic_does_not_fallback(window, qt_app):
    dock = window.orderflow
    dock.start_feed()
    assert dock.engine.snapshot().big_trades
    dock.stop_feed()
    dock.source.setCurrentIndex(2)
    dock.start_feed()
    assert 'SDK' in dock.status.text()
    assert not dock.engine.snapshot().big_trades
    assert not dock._running
    assert dock.demo is None and dock.worker is None
    dock.source.setCurrentIndex(1)
    dock.start_feed()
    assert 'mô phỏng MT5' in dock.status.text()
    assert not dock._running


def test_filters_persist_separately_and_chart_interactions_still_exist(window):
    dock = window.orderflow
    dock.inputs['min_trade'].setValue(100)
    assert dock.apply_settings()
    assert window.cfg['orderflow']['min_trade'] == 100
    assert legacy.load_cfg()['orderflow']['min_trade'] == 100
    assert all(callable(getattr(window.chart, name)) for name in
               ('mousePressEvent', 'mouseMoveEvent', 'wheelEvent', 'keyPressEvent', 'set_overlay', 'set_trades'))
