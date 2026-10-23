"""Run the preserved original application with an optional Order Flow dock."""
import multiprocessing


def main():
    import argparse
    import os
    from pathlib import Path
    import sys

    parser = argparse.ArgumentParser(description="AI MT Trading gốc + Order Flow bổ sung")
    parser.add_argument('--demo', action='store_true', help='Ép broker gốc chạy mô phỏng; không kết nối MT5.')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if getattr(sys, 'frozen', False) and not os.environ.get('AIMT_CONFIG'):
        directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AI_MT_Trading'
        directory.mkdir(parents=True, exist_ok=True)
        os.environ['AIMT_CONFIG'] = str(directory / 'ai_mt_trading_config.json')
    from PyQt6 import QtWidgets
    from original import AI_MT_Trading as legacy
    from aimt_orderflow.application import create_window

    if args.demo or args.smoke_test:
        load = legacy.load_cfg

        def demo_config():
            cfg = load()
            cfg['force_demo'] = True
            if args.smoke_test:
                cfg.update(use_ai=False, auto_trade=False, news_feeds=[])
            return cfg

        legacy.load_cfg = demo_config
    app = QtWidgets.QApplication(sys.argv[:1])
    window = create_window(legacy)
    window.show()
    if args.smoke_test:
        _run_smoke(app, window)
    return app.exec()


def _run_smoke(app, window):
    """Exercise the actual packaged GUI on Windows without connecting a broker."""
    import json
    import os
    from pathlib import Path
    import time
    import sys
    from PyQt6 import QtCore
    from original import AI_MT_Trading as legacy

    report_path = Path(os.environ.get('AIMT_SMOKE_REPORT', 'aimt-smoke-report.json'))
    start = time.monotonic()
    window.resize(1500, 950)
    window.start_analysis(True)
    window.orderflow.start_feed()

    def check():
        ready = (window.chart.bars is not None and window.payload is not None and not window.busy
                 and window.orderflow.last_snapshot.big_trades)
        if not ready and time.monotonic() - start < 15:
            QtCore.QTimer.singleShot(50, check)
            return
        report = {
            'ok': bool(ready), 'broker_demo': bool(window.broker.demo),
            'original_tabs': [window.tabs.tabText(i) for i in range(window.tabs.count())],
            'analysis_results': len(window.payload['results']) if window.payload else 0,
            'has_panel_chat_web': bool(window.panel and window.chat and window.web),
            'orderflow_trades': len(window.orderflow.last_snapshot.big_trades),
            'orderflow_levels': len(window.orderflow.last_snapshot.book),
            'mt5_module_available': bool(legacy.HAS_MT5),
            'webengine_available': bool(legacy.HAS_WEB),
        }
        report['ok'] = (report['ok'] and report['broker_demo'] and report['analysis_results'] == 8
                        and report['has_panel_chat_web'] and report['orderflow_levels'] > 0)
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            helper = Path(QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.LibraryExecutablesPath)) / 'QtWebEngineProcess.exe'
            report['webengine_helper_exists'] = helper.is_file()
            report['ok'] = (report['ok'] and report['mt5_module_available']
                            and report['webengine_available'] and report['webengine_helper_exists'])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(report_path.with_suffix('.png')))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        app.exit(0 if report['ok'] else 1)

    QtCore.QTimer.singleShot(100, check)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
