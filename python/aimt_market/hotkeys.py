"""User-controlled B/S confirmation; no broker access in this module."""
from PyQt6 import QtCore, QtWidgets


def install_confirmation(window, legacy):
    window.cfg.setdefault('hotkey_confirm', True)
    checkbox = QtWidgets.QCheckBox('Q xác nhận lệnh')
    checkbox.setChecked(bool(window.cfg['hotkey_confirm']))
    checkbox.setToolTip('Bật: B/S chuẩn bị lệnh, Q xác nhận trong 4 giây. Bỏ chọn: B/S gửi ngay. '
                        'Phím giữ lặp không tạo thêm lệnh; khi gõ văn bản không kích hoạt hotkey.')
    row = window.centralWidget().layout().itemAt(2).layout()
    row.insertWidget(row.indexOf(window.b_hk) + 1, checkbox)

    def changed(checked):
        window.armed = None
        window.cfg['hotkey_confirm'] = bool(checked)
        legacy.save_cfg(window.cfg)
        window.say('Hotkey B/S: ' + ('cần Q xác nhận.' if checked else 'gửi trực tiếp khi bấm, không cần Q.'))

    checkbox.toggled.connect(changed)
    window.confirm_hotkeys = checkbox
    window.cb_acc.currentTextChanged.connect(lambda _: setattr(window, 'armed', None))
    window.cb_sym.currentTextChanged.connect(lambda _: setattr(window, 'armed', None))


def repeated_trade_key(window, event):
    if event.type() != QtCore.QEvent.Type.KeyPress or not event.isAutoRepeat() or not window.cfg.get('hotkeys'):
        return False
    focus = QtWidgets.QApplication.focusWidget()
    if isinstance(focus, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox)):
        return False
    if isinstance(focus, QtWidgets.QComboBox) and focus.isEditable():
        return False
    active = QtWidgets.QApplication.activeWindow()
    if active is not window and not (active and window.isAncestorOf(active)):
        return False
    return (event.text() or '').upper() in set('BSQC VZ123TERPLUJIKDGX'.replace(' ', ''))
