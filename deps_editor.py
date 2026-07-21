#!/usr/bin/env python3
"""
GUI-редактор dependencies.json (PyQt6).

Запуск:
  .venv\\Scripts\\python.exe deps_editor.py
  .venv\\Scripts\\python.exe deps_editor.py path\\to\\dependencies.json
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QSettings, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QKeySequence, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyleFactory,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

DEFAULT_PATH = Path(__file__).resolve().parent / "dependencies.json"
THEME_SETTINGS_ORG = "ollama_faker"
THEME_SETTINGS_APP = "deps_editor"

SECTION_ORDER = [
    "field_mappings",
    "endpoint_rules",
    "interface_rules",
    "interface_lifecycle",
    "synthetic_bind_fields",
    "mock_data",
    "field_couplings",
    "reserved_values",
]


# =============================================================================
# JSON helpers
# =============================================================================
def dumps(obj: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, indent=2)


def loads_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    return json.loads(text)


def csv_to_list(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def list_to_csv(values: list | None) -> str:
    if not values:
        return ""
    return ", ".join(str(v) for v in values)


def normalize_values_list(text: str) -> list[Any]:
    """'a,b,c' или JSON-массив → list. Числа сохраняются если весь токен — int."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("Ожидался JSON-массив")
        return data
    out: list[Any] = []
    for part in csv_to_list(text):
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            out.append(int(part))
        else:
            try:
                if "." in part:
                    out.append(float(part))
                else:
                    out.append(part)
            except ValueError:
                out.append(part)
    return out


# =============================================================================
# Themes & UI helpers
# =============================================================================
def _qss_common() -> str:
    return """
    * { font-size: 13px; }
    QToolTip {
        padding: 6px 10px;
        border-radius: 6px;
        border: 1px solid palette(mid);
        font-size: 12px;
    }
    QMainWindow, QDialog { }
    QTabWidget::pane {
        border: 1px solid palette(mid);
        border-radius: 8px;
        top: -1px;
        padding: 6px;
    }
    QTabBar::tab {
        padding: 8px 14px;
        margin-right: 2px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        border: 1px solid transparent;
        min-width: 72px;
    }
    QTabBar::tab:hover { }
    QTabBar::tab:selected {
        font-weight: 600;
        border: 1px solid palette(mid);
        border-bottom-color: transparent;
    }
    QGroupBox {
        font-weight: 600;
        border: 1px solid palette(mid);
        border-radius: 8px;
        margin-top: 12px;
        padding: 12px 10px 10px 10px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
    }
    QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTextEdit {
        border: 1px solid palette(mid);
        border-radius: 6px;
        padding: 5px 8px;
        selection-background-color: palette(highlight);
        selection-color: palette(highlighted-text);
    }
    QPlainTextEdit, QListWidget {
        padding: 4px;
    }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus {
        border: 1px solid palette(highlight);
    }
    QComboBox::drop-down {
        border: none;
        width: 22px;
    }
    QPushButton, QToolButton {
        border: 1px solid palette(mid);
        border-radius: 6px;
        padding: 6px 12px;
        min-height: 22px;
    }
    QPushButton:hover, QToolButton:hover { }
    QPushButton:pressed, QToolButton:pressed { }
    QPushButton:disabled, QToolButton:disabled { color: palette(mid); }
    QPushButton#PrimaryButton {
        font-weight: 600;
        border: none;
        padding: 8px 16px;
    }
    QPushButton#DangerButton {
        border: none;
        padding: 6px 12px;
    }
    QPushButton#IconButton, QToolButton#IconButton {
        padding: 4px;
        min-width: 32px;
        max-width: 40px;
        font-weight: 600;
    }
    QListWidget::item {
        padding: 6px 8px;
        border-radius: 4px;
        margin: 1px 2px;
    }
    QListWidget::item:selected { }
    QScrollBar:vertical {
        width: 10px;
        margin: 2px;
        background: transparent;
    }
    QScrollBar::handle:vertical {
        border-radius: 4px;
        min-height: 24px;
    }
    QScrollBar:horizontal {
        height: 10px;
        margin: 2px;
        background: transparent;
    }
    QScrollBar::handle:horizontal {
        border-radius: 4px;
        min-width: 24px;
    }
    QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
    QStatusBar { padding: 2px 8px; }
    QMenuBar { padding: 2px 4px; }
    QMenuBar::item { padding: 6px 10px; border-radius: 4px; }
    QMenu {
        border: 1px solid palette(mid);
        border-radius: 8px;
        padding: 4px;
    }
    QMenu::item { padding: 6px 28px 6px 12px; border-radius: 4px; }
    QSplitter::handle { width: 3px; height: 3px; }
    QLabel#HintLabel {
        font-size: 12px;
    }
    QPlainTextEdit#PreviewPane, QPlainTextEdit#JsonEditor, QPlainTextEdit#HelpPane {
        font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    }
    """


def theme_stylesheet(theme: str) -> str:
    """Полный QSS для light / dark."""
    if theme == "dark":
        colors = """
        QWidget {
            background-color: #1a1d23;
            color: #e8eaed;
        }
        QMainWindow, QDialog, QTabWidget::pane, QScrollArea, QScrollArea > QWidget > QWidget {
            background-color: #1a1d23;
            color: #e8eaed;
        }
        QToolTip {
            background-color: #2b303b;
            color: #e8eaed;
            border: 1px solid #3d4450;
        }
        QTabWidget::pane { background: #21252b; border-color: #3d4450; }
        QTabBar::tab {
            background: #1a1d23;
            color: #9aa0a6;
            border-color: transparent;
        }
        QTabBar::tab:hover { background: #2b303b; color: #e8eaed; }
        QTabBar::tab:selected {
            background: #21252b;
            color: #7dd3c0;
            border-color: #3d4450;
        }
        QGroupBox { background: #21252b; border-color: #3d4450; color: #e8eaed; }
        QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTextEdit {
            background-color: #16181d;
            color: #e8eaed;
            border-color: #3d4450;
        }
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus {
            border-color: #3d9b8a;
        }
        QComboBox QAbstractItemView {
            background-color: #21252b;
            color: #e8eaed;
            selection-background-color: #2a6f64;
        }
        QPushButton, QToolButton {
            background-color: #2b303b;
            color: #e8eaed;
            border-color: #3d4450;
        }
        QPushButton:hover, QToolButton:hover {
            background-color: #363c48;
            border-color: #5a6370;
        }
        QPushButton:pressed, QToolButton:pressed {
            background-color: #1e2229;
        }
        QPushButton#PrimaryButton {
            background-color: #2a9d8f;
            color: #ffffff;
        }
        QPushButton#PrimaryButton:hover { background-color: #34b3a3; }
        QPushButton#PrimaryButton:pressed { background-color: #21867a; }
        QPushButton#DangerButton {
            background-color: #c45c5c;
            color: #ffffff;
        }
        QPushButton#DangerButton:hover { background-color: #d46a6a; }
        QPushButton#IconButton, QToolButton#IconButton {
            background-color: #2b303b;
        }
        QListWidget { background-color: #16181d; border-color: #3d4450; }
        QListWidget::item:hover { background: #2b303b; }
        QListWidget::item:selected {
            background: #2a6f64;
            color: #ffffff;
        }
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
            background: #3d4450;
        }
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
            background: #5a6370;
        }
        QStatusBar {
            background: #14161a;
            color: #9aa0a6;
            border-top: 1px solid #3d4450;
        }
        QMenuBar {
            background: #14161a;
            color: #e8eaed;
            border-bottom: 1px solid #3d4450;
        }
        QMenuBar::item:selected { background: #2b303b; }
        QMenu {
            background: #21252b;
            color: #e8eaed;
            border-color: #3d4450;
        }
        QMenu::item:selected { background: #2a6f64; }
        QSplitter::handle { background: #3d4450; }
        QLabel#HintLabel { color: #9aa0a6; }
        QCheckBox { color: #e8eaed; spacing: 8px; }
        QCheckBox::indicator {
            width: 16px; height: 16px;
            border-radius: 3px;
            border: 1px solid #3d4450;
            background: #16181d;
        }
        QCheckBox::indicator:checked {
            background: #2a9d8f;
            border-color: #2a9d8f;
        }
        QHeaderView::section {
            background: #21252b;
            color: #e8eaed;
            border: 1px solid #3d4450;
            padding: 4px;
        }
        QPlainTextEdit#PreviewPane {
            background-color: #12141a;
            color: #b8e0d2;
            border-color: #3d4450;
        }
        QPlainTextEdit#JsonEditor {
            background-color: #12141a;
            color: #d4d8de;
        }
        QPlainTextEdit#HelpPane {
            background-color: #16181d;
            color: #c5c9ce;
        }
        """
    else:
        colors = """
        QWidget {
            background-color: #f4f6f8;
            color: #1f2933;
        }
        QMainWindow, QDialog, QTabWidget::pane, QScrollArea, QScrollArea > QWidget > QWidget {
            background-color: #f4f6f8;
            color: #1f2933;
        }
        QToolTip {
            background-color: #ffffff;
            color: #1f2933;
            border: 1px solid #c5ced6;
        }
        QTabWidget::pane { background: #ffffff; border-color: #c5ced6; }
        QTabBar::tab {
            background: #e8eef2;
            color: #5a6570;
            border-color: transparent;
        }
        QTabBar::tab:hover { background: #dde5eb; color: #1f2933; }
        QTabBar::tab:selected {
            background: #ffffff;
            color: #0f766e;
            border-color: #c5ced6;
        }
        QGroupBox { background: #ffffff; border-color: #c5ced6; color: #1f2933; }
        QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTextEdit {
            background-color: #ffffff;
            color: #1f2933;
            border-color: #c5ced6;
        }
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus {
            border-color: #0f766e;
        }
        QComboBox QAbstractItemView {
            background-color: #ffffff;
            color: #1f2933;
            selection-background-color: #99f6e4;
            selection-color: #134e4a;
        }
        QPushButton, QToolButton {
            background-color: #ffffff;
            color: #1f2933;
            border-color: #c5ced6;
        }
        QPushButton:hover, QToolButton:hover {
            background-color: #e8eef2;
            border-color: #9aabba;
        }
        QPushButton:pressed, QToolButton:pressed {
            background-color: #dce4ea;
        }
        QPushButton#PrimaryButton {
            background-color: #0f766e;
            color: #ffffff;
        }
        QPushButton#PrimaryButton:hover { background-color: #0d9488; }
        QPushButton#PrimaryButton:pressed { background-color: #115e59; }
        QPushButton#DangerButton {
            background-color: #b91c1c;
            color: #ffffff;
        }
        QPushButton#DangerButton:hover { background-color: #dc2626; }
        QPushButton#IconButton, QToolButton#IconButton {
            background-color: #ffffff;
        }
        QListWidget { background-color: #ffffff; border-color: #c5ced6; }
        QListWidget::item:hover { background: #e8eef2; }
        QListWidget::item:selected {
            background: #99f6e4;
            color: #134e4a;
        }
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
            background: #c5ced6;
        }
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
            background: #9aabba;
        }
        QStatusBar {
            background: #e8eef2;
            color: #5a6570;
            border-top: 1px solid #c5ced6;
        }
        QMenuBar {
            background: #ffffff;
            color: #1f2933;
            border-bottom: 1px solid #c5ced6;
        }
        QMenuBar::item:selected { background: #e8eef2; }
        QMenu {
            background: #ffffff;
            color: #1f2933;
            border-color: #c5ced6;
        }
        QMenu::item:selected { background: #ccfbf1; color: #134e4a; }
        QSplitter::handle { background: #c5ced6; }
        QLabel#HintLabel { color: #5a6570; }
        QCheckBox { color: #1f2933; spacing: 8px; }
        QCheckBox::indicator {
            width: 16px; height: 16px;
            border-radius: 3px;
            border: 1px solid #c5ced6;
            background: #ffffff;
        }
        QCheckBox::indicator:checked {
            background: #0f766e;
            border-color: #0f766e;
        }
        QHeaderView::section {
            background: #e8eef2;
            color: #1f2933;
            border: 1px solid #c5ced6;
            padding: 4px;
        }
        QPlainTextEdit#PreviewPane {
            background-color: #f0fdf9;
            color: #134e4a;
            border-color: #99f6e4;
        }
        QPlainTextEdit#JsonEditor {
            background-color: #fafbfc;
            color: #1f2933;
        }
        QPlainTextEdit#HelpPane {
            background-color: #ffffff;
            color: #374151;
        }
        """
    return _qss_common() + colors


def apply_app_theme(app: QApplication, theme: str) -> None:
    theme = "dark" if theme == "dark" else "light"
    if "Fusion" in QStyleFactory.keys():
        app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(theme_stylesheet(theme))
    pal = QPalette()
    if theme == "dark":
        bg = QColor("#1a1d23")
        fg = QColor("#e8eaed")
        base = QColor("#16181d")
        accent = QColor("#2a9d8f")
        mid = QColor("#3d4450")
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Base, base)
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#21252b"))
        pal.setColor(QPalette.ColorRole.Text, fg)
        pal.setColor(QPalette.ColorRole.Button, QColor("#2b303b"))
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        pal.setColor(QPalette.ColorRole.Highlight, accent)
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2b303b"))
        pal.setColor(QPalette.ColorRole.ToolTipText, fg)
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#9aa0a6"))
        pal.setColor(QPalette.ColorRole.Mid, mid)
    else:
        bg = QColor("#f4f6f8")
        fg = QColor("#1f2933")
        base = QColor("#ffffff")
        accent = QColor("#0f766e")
        mid = QColor("#c5ced6")
        pal.setColor(QPalette.ColorRole.Window, bg)
        pal.setColor(QPalette.ColorRole.WindowText, fg)
        pal.setColor(QPalette.ColorRole.Base, base)
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#e8eef2"))
        pal.setColor(QPalette.ColorRole.Text, fg)
        pal.setColor(QPalette.ColorRole.Button, base)
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        pal.setColor(QPalette.ColorRole.Highlight, accent)
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ColorRole.ToolTipBase, base)
        pal.setColor(QPalette.ColorRole.ToolTipText, fg)
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#5a6570"))
        pal.setColor(QPalette.ColorRole.Mid, mid)
    app.setPalette(pal)
    settings = QSettings(THEME_SETTINGS_ORG, THEME_SETTINGS_APP)
    settings.setValue("theme", theme)


def load_saved_theme() -> str:
    settings = QSettings(THEME_SETTINGS_ORG, THEME_SETTINGS_APP)
    val = settings.value("theme", "light")
    return "dark" if str(val) == "dark" else "light"


def make_button(
    text: str,
    tip: str,
    *,
    slot=None,
    role: str = "default",
    fixed_width: int | None = None,
) -> QPushButton:
    """Кнопка с обязательной подсказкой. role: default | primary | danger | icon."""
    btn = QPushButton(text)
    btn.setToolTip(tip)
    btn.setStatusTip(tip)
    if role == "primary":
        btn.setObjectName("PrimaryButton")
    elif role == "danger":
        btn.setObjectName("DangerButton")
    elif role == "icon":
        btn.setObjectName("IconButton")
    if fixed_width is not None:
        btn.setFixedWidth(fixed_width)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


def hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("HintLabel")
    lab.setWordWrap(True)
    return lab


# =============================================================================
# Base widgets
# =============================================================================
class JsonEditor(QPlainTextEdit):
    """Редактор произвольного JSON с подсветкой ошибки."""

    changed = pyqtSignal()

    def __init__(self, parent=None, *, min_h: int = 120):
        super().__init__(parent)
        self.setObjectName("JsonEditor")
        self.setFont(QFont("Consolas", 10))
        self.setMinimumHeight(min_h)
        self.setPlaceholderText("{}")
        self.setToolTip("Редактор JSON. Неверный синтаксис помешает сохранить правило.")
        self.textChanged.connect(self.changed.emit)

    def set_data(self, data: Any) -> None:
        self.blockSignals(True)
        self.setPlainText(dumps(data) if data is not None else "{}")
        self.blockSignals(False)

    def get_data(self) -> Any:
        return loads_json(self.toPlainText())

    def try_get_data(self) -> tuple[bool, Any, str]:
        try:
            return True, self.get_data(), ""
        except json.JSONDecodeError as exc:
            return False, None, str(exc)


class PreviewPane(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PreviewPane")
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.setPlaceholderText("Здесь будет JSON, который попадёт в dependencies.json")
        self.setToolTip("Превью: JSON, который будет записан в dependencies.json для этого правила")

    def show_obj(self, obj: Any, title: str = "") -> None:
        header = f"// {title}\n" if title else ""
        self.setPlainText(header + dumps(obj))


def labeled(text: str, widget: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lab = QLabel(text)
    lab.setMinimumWidth(140)
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    return box


# =============================================================================
# Lifecycle step editor
# =============================================================================
class LifecycleStepEditor(QWidget):
    """Один setup/teardown шаг: endpoint, method, payload, extract…"""

    changed = pyqtSignal()

    def __init__(self, parent=None, *, title: str = "Шаг"):
        super().__init__(parent)
        self._title = title
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        self.mode = QComboBox()
        self.mode.addItem("Полный шаг (endpoint + payload)", "full")
        self.mode.addItem("Короткий путь (строка create/delete)", "string")
        self.mode.currentIndexChanged.connect(self._on_mode)
        self.mode.currentIndexChanged.connect(self.changed.emit)
        root.addWidget(labeled("Тип шага:", self.mode))

        self.string_path = QLineEdit()
        self.string_path.setPlaceholderText("/interfaces/vlan/add")
        self.string_path.textChanged.connect(self.changed.emit)
        self.string_row = labeled("Путь эндпоинта:", self.string_path)
        root.addWidget(self.string_row)

        self.full_box = QGroupBox(title)
        form = QFormLayout(self.full_box)

        self.endpoint = QLineEdit()
        self.endpoint.setPlaceholderText("/acl/acl_ipv4")
        self.endpoint.textChanged.connect(self.changed.emit)
        form.addRow("endpoint *", self.endpoint)

        self.method = QComboBox()
        self.method.addItems(["POST", "GET", "PUT", "PATCH", "DELETE"])
        self.method.currentIndexChanged.connect(self.changed.emit)
        form.addRow("method", self.method)

        self.payload = JsonEditor(min_h=140)
        self.payload.setPlaceholderText('{\n  "action": { "delete": { "acl_name": "{{acl_name}}" } }\n}')
        self.payload.changed.connect(self.changed.emit)
        form.addRow("payload (JSON)", self.payload)

        self.expected_status = QSpinBox()
        self.expected_status.setRange(0, 599)
        self.expected_status.setSpecialValueText("— (не задавать)")
        self.expected_status.setValue(0)
        self.expected_status.valueChanged.connect(self.changed.emit)
        form.addRow("expected_status", self.expected_status)

        self.note = QLineEdit()
        self.note.textChanged.connect(self.changed.emit)
        form.addRow("note", self.note)

        self.extract_to = QLineEdit()
        self.extract_to.setPlaceholderText("created_acl_name")
        self.extract_to.textChanged.connect(self.changed.emit)
        form.addRow("extract_to_variable", self.extract_to)

        self.response_extract = QLineEdit()
        self.response_extract.setPlaceholderText("data.acl_name")
        self.response_extract.textChanged.connect(self.changed.emit)
        form.addRow("response_extract", self.response_extract)

        self.setup_priority = QSpinBox()
        self.setup_priority.setRange(-1, 9999)
        self.setup_priority.setSpecialValueText("—")
        self.setup_priority.setValue(-1)
        self.setup_priority.valueChanged.connect(self.changed.emit)
        form.addRow("setup_priority", self.setup_priority)

        tip = hint_label(
            "Плейсхолдеры в payload: {{field_name}}, {{vid}}, {{created_…}}. "
            "Они подставятся раннером из main_test / mock_data."
        )
        form.addRow(tip)

        root.addWidget(self.full_box)
        self._on_mode()

    def _on_mode(self) -> None:
        is_string = self.mode.currentData() == "string"
        self.string_row.setVisible(is_string)
        self.full_box.setVisible(not is_string)

    def set_data(self, data: Any) -> None:
        self.blockSignals(True)
        if isinstance(data, str):
            self.mode.setCurrentIndex(1)
            self.string_path.setText(data)
        elif isinstance(data, dict):
            self.mode.setCurrentIndex(0)
            self.endpoint.setText(str(data.get("endpoint", "")))
            method = str(data.get("method", "POST")).upper()
            idx = self.method.findText(method)
            self.method.setCurrentIndex(max(0, idx))
            self.payload.set_data(data.get("payload", {}))
            es = data.get("expected_status")
            self.expected_status.setValue(int(es) if isinstance(es, int) else 0)
            self.note.setText(str(data.get("note", "") or ""))
            self.extract_to.setText(str(data.get("extract_to_variable", "") or ""))
            self.response_extract.setText(str(data.get("response_extract", "") or ""))
            sp = data.get("setup_priority")
            self.setup_priority.setValue(int(sp) if isinstance(sp, int) else -1)
        else:
            self.mode.setCurrentIndex(0)
            self.endpoint.clear()
            self.payload.set_data({})
        self.blockSignals(False)
        self._on_mode()

    def get_data(self) -> Any:
        if self.mode.currentData() == "string":
            path = self.string_path.text().strip()
            if not path:
                raise ValueError(f"{self._title}: пустой путь эндпоинта")
            return path

        endpoint = self.endpoint.text().strip()
        if not endpoint:
            raise ValueError(f"{self._title}: endpoint обязателен")
        ok, payload, err = self.payload.try_get_data()
        if not ok:
            raise ValueError(f"{self._title}: payload JSON — {err}")
        if not isinstance(payload, dict):
            raise ValueError(f"{self._title}: payload должен быть объектом {{…}}")

        step: dict[str, Any] = {
            "endpoint": endpoint,
            "method": self.method.currentText(),
            "payload": payload,
        }
        if self.expected_status.value() > 0:
            step["expected_status"] = self.expected_status.value()
        if self.note.text().strip():
            step["note"] = self.note.text().strip()
        if self.extract_to.text().strip():
            step["extract_to_variable"] = self.extract_to.text().strip()
        if self.response_extract.text().strip():
            step["response_extract"] = self.response_extract.text().strip()
        if self.setup_priority.value() >= 0:
            step["setup_priority"] = self.setup_priority.value()
        return step


class LifecyclePhaseEditor(QWidget):
    """Список шагов setup или teardown (0..N) + кнопки."""

    changed = pyqtSignal()

    def __init__(self, parent=None, *, phase: str = "setup"):
        super().__init__(parent)
        self.phase = phase
        self._steps: list[Any] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load_current)
        bar_btns = QVBoxLayout()
        for text, tip, slot, role in (
            ("+ Добавить шаг", "Добавить новый шаг lifecycle в конец списка", self._add, "default"),
            ("Дублировать", "Скопировать выбранный шаг сразу под ним", self._dup, "default"),
            ("Удалить", "Удалить выбранный шаг", self._del, "danger"),
            ("↑", "Поднять шаг выше (раньше в порядке выполнения)", self._up, "icon"),
            ("↓", "Опустить шаг ниже", self._down, "icon"),
        ):
            bar_btns.addWidget(make_button(text, tip, slot=slot, role=role))
        bar_btns.addStretch(1)
        bar.addWidget(self.list, 1)
        bar.addLayout(bar_btns)
        root.addLayout(bar, 1)

        self.editor = LifecycleStepEditor(title=phase)
        self.editor.changed.connect(self._save_current)
        root.addWidget(self.editor, 2)

        self._updating = False

    def set_data(self, data: Any) -> None:
        self._updating = True
        if data is None:
            self._steps = []
        elif isinstance(data, list):
            self._steps = copy.deepcopy(data)
        else:
            self._steps = [copy.deepcopy(data)]
        self._refresh_list()
        self._updating = False
        if self._steps:
            self.list.setCurrentRow(0)
        else:
            self.editor.set_data({})

    def get_data(self) -> Any | None:
        # Не эмитим changed: иначе parent.get_data → снова сюда (бесконечная рекурсия).
        self._save_current(emit=False)
        if not self._steps:
            return None
        if len(self._steps) == 1:
            return copy.deepcopy(self._steps[0])
        return copy.deepcopy(self._steps)

    def _refresh_list(self) -> None:
        self.list.blockSignals(True)
        row = self.list.currentRow()
        self.list.clear()
        for i, step in enumerate(self._steps):
            if isinstance(step, str):
                label = step
            elif isinstance(step, dict):
                label = f"{step.get('method', 'POST')} {step.get('endpoint', '?')}"
            else:
                label = str(step)
            self.list.addItem(f"{i + 1}. {label}")
        self.list.blockSignals(False)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    def _load_current(self, row: int) -> None:
        if self._updating or row < 0 or row >= len(self._steps):
            return
        self._updating = True
        self.editor.set_data(self._steps[row])
        self._updating = False

    def _save_current(self, *, emit: bool = True) -> None:
        if self._updating:
            return
        row = self.list.currentRow()
        if row < 0 or row >= len(self._steps):
            return
        try:
            self._steps[row] = self.editor.get_data()
        except ValueError:
            return
        self._refresh_list()
        self.list.setCurrentRow(row)
        if emit:
            self.changed.emit()

    def _add(self) -> None:
        self._save_current()
        self._steps.append({
            "endpoint": "/",
            "method": "POST",
            "payload": {},
        })
        self._refresh_list()
        self.list.setCurrentRow(len(self._steps) - 1)
        self.changed.emit()

    def _dup(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self._save_current()
        self._steps.insert(row + 1, copy.deepcopy(self._steps[row]))
        self._refresh_list()
        self.list.setCurrentRow(row + 1)
        self.changed.emit()

    def _del(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        del self._steps[row]
        self._refresh_list()
        if self._steps:
            self.list.setCurrentRow(min(row, len(self._steps) - 1))
        else:
            self.editor.set_data({})
        self.changed.emit()

    def _up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self._save_current()
        self._steps[row - 1], self._steps[row] = self._steps[row], self._steps[row - 1]
        self._refresh_list()
        self.list.setCurrentRow(row - 1)
        self.changed.emit()

    def _down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._steps) - 1:
            return
        self._save_current()
        self._steps[row + 1], self._steps[row] = self._steps[row], self._steps[row + 1]
        self._refresh_list()
        self.list.setCurrentRow(row + 1)
        self.changed.emit()


# =============================================================================
# Generic keyed-list + editor pattern
# =============================================================================
class KeyListPanel(QWidget):
    """Слева список ключей, справа редактор. Сигнал selection_changed(key)."""

    selection_changed = pyqtSignal(str)
    keys_changed = pyqtSignal()

    def __init__(self, parent=None, *, key_label: str = "Имя"):
        super().__init__(parent)
        self.key_label = key_label
        self._keys: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self.selection_changed.emit)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, tip, slot in (
            ("+", f"Добавить новый элемент ({self.key_label})", self.add_key),
            ("✎", f"Переименовать выбранный {self.key_label.lower()}", self.rename_key),
            ("−", f"Удалить выбранный {self.key_label.lower()}", self.remove_key),
        ):
            role = "danger" if text == "−" else "icon"
            row.addWidget(make_button(text, tip, slot=slot, role=role, fixed_width=36))
        lay.addLayout(row)

    def set_keys(self, keys: list[str]) -> None:
        current = self.list.currentItem().text() if self.list.currentItem() else ""
        self._keys = list(keys)
        self.list.blockSignals(True)
        self.list.clear()
        for k in self._keys:
            self.list.addItem(k)
        self.list.blockSignals(False)
        if current in self._keys:
            items = self.list.findItems(current, Qt.MatchFlag.MatchExactly)
            if items:
                self.list.setCurrentItem(items[0])
        elif self._keys:
            self.list.setCurrentRow(0)

    def current_key(self) -> str:
        item = self.list.currentItem()
        return item.text() if item else ""

    def add_key(self) -> None:
        name, ok = QInputDialog.getText(self, "Новый элемент", f"{self.key_label}:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._keys:
            QMessageBox.warning(self, "Дубликат", f"Уже есть: {name}")
            return
        self._keys.append(name)
        self.set_keys(self._keys)
        items = self.list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])
        self.keys_changed.emit()

    def rename_key(self) -> None:
        old = self.current_key()
        if not old:
            return
        name, ok = QInputDialog.getText(self, "Переименовать", f"{self.key_label}:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        if name in self._keys:
            QMessageBox.warning(self, "Дубликат", f"Уже есть: {name}")
            return
        idx = self._keys.index(old)
        self._keys[idx] = name
        self.set_keys(self._keys)
        items = self.list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])
        self.keys_changed.emit()
        # rename signal via selection — parent must remap data
        self.selection_changed.emit(name)

    def remove_key(self) -> None:
        key = self.current_key()
        if not key:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить {key}?") != QMessageBox.StandardButton.Yes:
            return
        self._keys.remove(key)
        self.set_keys(self._keys)
        self.keys_changed.emit()


def make_scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    return scroll


# =============================================================================
# Field mappings tab
# =============================================================================
class FieldMappingEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key = ""
        self._old_key = ""
        root = QVBoxLayout(self)

        self.name = QLineEdit()
        self.name.setPlaceholderText("acl_name / vrf_name / zone_name …")
        self.name.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("Имя поля в payload:", self.name))

        flags = QHBoxLayout()
        self.optional = QCheckBox("optional (пропуск если пусто)")
        self.optional.stateChanged.connect(self.changed.emit)
        flags.addWidget(self.optional)
        root.addLayout(flags)

        self.requirements = QLineEdit()
        self.requirements.setPlaceholderText("setup, teardown  (через запятую или пусто)")
        self.requirements.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("requirements:", self.requirements))

        self.skip_targets = QLineEdit()
        self.skip_targets.setPlaceholderText("/dns/…/add, /dns/…/slave/*")
        self.skip_targets.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("skip_targets:", self.skip_targets))

        self.bind_fields = QLineEdit()
        self.bind_fields.setPlaceholderText("ip_addr, vid")
        self.bind_fields.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("bind_fields:", self.bind_fields))

        row = QHBoxLayout()
        self.teardown_priority = QSpinBox()
        self.teardown_priority.setRange(-1, 9999)
        self.teardown_priority.setSpecialValueText("—")
        self.teardown_priority.setValue(-1)
        self.teardown_priority.valueChanged.connect(self.changed.emit)
        self.setup_priority = QSpinBox()
        self.setup_priority.setRange(-1, 9999)
        self.setup_priority.setSpecialValueText("—")
        self.setup_priority.setValue(-1)
        self.setup_priority.valueChanged.connect(self.changed.emit)
        row.addWidget(QLabel("teardown_priority"))
        row.addWidget(self.teardown_priority)
        row.addWidget(QLabel("setup_priority"))
        row.addWidget(self.setup_priority)
        root.addLayout(row)

        self.setup = LifecyclePhaseEditor(phase="setup")
        self.setup.changed.connect(self.changed.emit)
        self.teardown = LifecyclePhaseEditor(phase="teardown")
        self.teardown.changed.connect(self.changed.emit)

        tabs = QTabWidget()
        tabs.addTab(self.setup, "Setup (создать зависимость)")
        tabs.addTab(self.teardown, "Teardown (удалить)")
        root.addWidget(tabs, 1)

        hint = hint_label(
            "Поле ищется в payload по имени (на любой вложенности). "
            "Setup/teardown — шаги, которые раннер добавит в сценарий."
        )
        root.addWidget(hint)

    def set_data(self, key: str, data: dict) -> None:
        self._old_key = key
        self._key = key
        data = data or {}
        self.name.setText(key)
        self.optional.setChecked(bool(data.get("optional")))
        self.requirements.setText(list_to_csv(data.get("requirements") or []))
        self.skip_targets.setText(list_to_csv(data.get("skip_targets") or []))
        self.bind_fields.setText(list_to_csv(data.get("bind_fields") or []))
        tp = data.get("teardown_priority")
        self.teardown_priority.setValue(int(tp) if isinstance(tp, int) else -1)
        sp = data.get("setup_priority")
        self.setup_priority.setValue(int(sp) if isinstance(sp, int) else -1)
        # support create/delete aliases
        setup = data.get("setup", data.get("create"))
        teardown = data.get("teardown", data.get("delete"))
        self.setup.set_data(setup)
        self.teardown.set_data(teardown)

    def get_data(self) -> tuple[str, dict]:
        key = self.name.text().strip()
        if not key:
            raise ValueError("Имя поля обязательно")
        cfg: dict[str, Any] = {}
        if self.optional.isChecked():
            cfg["optional"] = True
        req = csv_to_list(self.requirements.text())
        if req:
            cfg["requirements"] = req
        skip = csv_to_list(self.skip_targets.text())
        if skip:
            cfg["skip_targets"] = skip
        bind = csv_to_list(self.bind_fields.text())
        if bind:
            cfg["bind_fields"] = bind
        if self.teardown_priority.value() >= 0:
            cfg["teardown_priority"] = self.teardown_priority.value()
        if self.setup_priority.value() >= 0:
            cfg["setup_priority"] = self.setup_priority.value()
        setup = self.setup.get_data()
        teardown = self.teardown.get_data()
        if setup is not None:
            cfg["setup"] = setup
        if teardown is not None:
            cfg["teardown"] = teardown
        if "setup" not in cfg and "teardown" not in cfg:
            raise ValueError("Нужен хотя бы setup или teardown")
        return key, cfg


class FieldMappingsTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: dict[str, Any] = {}
        self._loading = False
        split = QSplitter()
        self.keys = KeyListPanel(key_label="Имя поля")
        self.keys.selection_changed.connect(self._on_select)
        self.keys.keys_changed.connect(self._on_keys_meta)
        self.editor = FieldMappingEditor()
        self.editor.changed.connect(self._on_edit)
        self.preview = PreviewPane()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(make_scroll(self.editor), 3)
        rl.addWidget(QLabel("JSON этого правила:"))
        rl.addWidget(self.preview, 2)
        split.addWidget(self.keys)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        apply_btn = make_button(
            "Применить изменения текущего правила",
            "Записать форму в память редактора (ещё не в файл). Затем Ctrl+S — сохранить на диск.",
            slot=self.apply_current,
            role="primary",
        )
        lay.addWidget(apply_btn)

    def set_section(self, data: dict | None) -> None:
        self._loading = True
        self._data = copy.deepcopy(data or {})
        self.keys.set_keys(sorted(self._data.keys()))
        self._loading = False
        self._on_select(self.keys.current_key())

    def get_section(self) -> dict:
        self.apply_current(silent=True)
        return copy.deepcopy(self._data)

    def apply_current(self, silent: bool = False) -> None:
        if self._loading:
            return
        old = self.keys.current_key()
        if not old and not self.editor.name.text().strip():
            return
        try:
            new_key, cfg = self.editor.get_data()
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "Ошибка", str(exc))
            return
        if old and old in self._data and old != new_key:
            del self._data[old]
        self._data[new_key] = cfg
        self.keys.set_keys(sorted(self._data.keys()))
        items = self.keys.list.findItems(new_key, Qt.MatchFlag.MatchExactly)
        if items:
            self.keys.list.setCurrentItem(items[0])
        self.preview.show_obj({new_key: cfg}, title=new_key)
        self.dirty.emit()

    def _on_select(self, key: str) -> None:
        if self._loading:
            return
        if key and key in self._data:
            self._loading = True
            self.editor.set_data(key, self._data[key])
            self.preview.show_obj({key: self._data[key]}, title=key)
            self._loading = False

    def _on_edit(self) -> None:
        if self._loading:
            return
        try:
            key, cfg = self.editor.get_data()
            self.preview.show_obj({key: cfg}, title="черновик (нажми Применить)")
        except ValueError as exc:
            self.preview.setPlainText(f"// ошибка: {exc}")

    def _on_keys_meta(self) -> None:
        # sync deletions from list panel
        listed = set(self.keys._keys)
        for gone in [k for k in list(self._data) if k not in listed]:
            # only if user deleted via − and key was known
            if gone not in listed:
                self._data.pop(gone, None)
        # additions: empty stub
        for key in listed:
            if key not in self._data:
                self._data[key] = {
                    "setup": {"endpoint": "/", "method": "POST", "payload": {}},
                }
        self.dirty.emit()
        self._on_select(self.keys.current_key())


# =============================================================================
# Endpoint rules tab
# =============================================================================
class EndpointRuleEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)

        self.path = QLineEdit()
        self.path.setPlaceholderText("/acl/filter/filter_ipv4")
        self.path.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("Эндпоинт (ключ):", self.path))

        self.bind_fields = QLineEdit()
        self.bind_fields.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("bind_fields:", self.bind_fields))

        self.lifecycle_key_field = QLineEdit()
        self.lifecycle_key_field.setPlaceholderText("entry_type  (опционально)")
        self.lifecycle_key_field.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("lifecycle_key_field:", self.lifecycle_key_field))

        self.requirements = QLineEdit()
        self.requirements.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("requirements:", self.requirements))

        self.teardown_priority = QSpinBox()
        self.teardown_priority.setRange(-1, 9999)
        self.teardown_priority.setSpecialValueText("—")
        self.teardown_priority.setValue(-1)
        self.teardown_priority.valueChanged.connect(self.changed.emit)
        root.addWidget(labeled("teardown_priority:", self.teardown_priority))

        self.top_setup = LifecyclePhaseEditor(phase="setup")
        self.top_setup.changed.connect(self.changed.emit)
        self.top_teardown = LifecyclePhaseEditor(phase="teardown")
        self.top_teardown.changed.connect(self.changed.emit)

        top_tabs = QTabWidget()
        top_tabs.addTab(self.top_setup, "Top-level setup")
        top_tabs.addTab(self.top_teardown, "Top-level teardown")
        root.addWidget(top_tabs, 1)

        root.addWidget(QLabel("Блоки по action / ключу (add, delete, a, mx …):"))
        blocks_row = QHBoxLayout()
        self.block_list = QListWidget()
        self.block_list.currentTextChanged.connect(self._load_block)
        blocks_row.addWidget(self.block_list, 1)
        bb = QVBoxLayout()
        bb.addWidget(make_button(
            "+", "Добавить блок action/ключа (add, delete, a, mx…)",
            slot=self._add_block, role="icon", fixed_width=36,
        ))
        bb.addWidget(make_button(
            "−", "Удалить выбранный блок",
            slot=self._del_block, role="danger", fixed_width=36,
        ))
        bb.addStretch(1)
        blocks_row.addLayout(bb)
        root.addLayout(blocks_row)

        self.block_setup = LifecyclePhaseEditor(phase="setup")
        self.block_setup.changed.connect(self._save_block)
        self.block_teardown = LifecyclePhaseEditor(phase="teardown")
        self.block_teardown.changed.connect(self._save_block)
        bt = QTabWidget()
        bt.addTab(self.block_setup, "Block setup")
        bt.addTab(self.block_teardown, "Block teardown")
        root.addWidget(bt, 1)

        self._blocks: dict[str, dict] = {}
        self._loading = False
        self._meta_keys = {
            "bind_fields", "teardown_priority", "lifecycle_key_field",
            "requirements", "setup", "teardown",
        }

    def set_data(self, key: str, data: dict) -> None:
        self._loading = True
        data = data or {}
        self.path.setText(key)
        self.bind_fields.setText(list_to_csv(data.get("bind_fields") or []))
        self.lifecycle_key_field.setText(str(data.get("lifecycle_key_field", "") or ""))
        self.requirements.setText(list_to_csv(data.get("requirements") or []))
        tp = data.get("teardown_priority")
        self.teardown_priority.setValue(int(tp) if isinstance(tp, int) else -1)
        self.top_setup.set_data(data.get("setup"))
        self.top_teardown.set_data(data.get("teardown"))
        self._blocks = {}
        for k, v in data.items():
            if k in self._meta_keys:
                continue
            if isinstance(v, dict) and ("setup" in v or "teardown" in v):
                self._blocks[k] = copy.deepcopy(v)
        self.block_list.clear()
        for k in sorted(self._blocks):
            self.block_list.addItem(k)
        self._loading = False
        if self._blocks:
            self.block_list.setCurrentRow(0)
        else:
            self.block_setup.set_data(None)
            self.block_teardown.set_data(None)

    def get_data(self) -> tuple[str, dict]:
        self._save_block(emit=False)
        key = self.path.text().strip()
        if not key:
            raise ValueError("Путь эндпоинта обязателен")
        cfg: dict[str, Any] = {}
        bind = csv_to_list(self.bind_fields.text())
        if bind:
            cfg["bind_fields"] = bind
        lkf = self.lifecycle_key_field.text().strip()
        if lkf:
            cfg["lifecycle_key_field"] = lkf
        req = csv_to_list(self.requirements.text())
        if req:
            cfg["requirements"] = req
        if self.teardown_priority.value() >= 0:
            cfg["teardown_priority"] = self.teardown_priority.value()
        setup = self.top_setup.get_data()
        teardown = self.top_teardown.get_data()
        if setup is not None:
            cfg["setup"] = setup
        if teardown is not None:
            cfg["teardown"] = teardown
        for name, block in self._blocks.items():
            cfg[name] = block
        return key, cfg

    def _add_block(self) -> None:
        name, ok = QInputDialog.getText(self, "Блок", "Имя (add / delete / a / mx …):")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._meta_keys:
            QMessageBox.warning(self, "Имя", "Это зарезервированное meta-поле")
            return
        self._blocks[name] = {}
        self.block_list.addItem(name)
        items = self.block_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.block_list.setCurrentItem(items[0])
        self.changed.emit()

    def _del_block(self) -> None:
        item = self.block_list.currentItem()
        if not item:
            return
        name = item.text()
        self._blocks.pop(name, None)
        self.block_list.takeItem(self.block_list.row(item))
        self.changed.emit()

    def _load_block(self, name: str) -> None:
        if self._loading or not name:
            return
        block = self._blocks.get(name, {})
        self._loading = True
        self.block_setup.set_data(block.get("setup"))
        self.block_teardown.set_data(block.get("teardown"))
        self._loading = False

    def _save_block(self, *, emit: bool = True) -> None:
        if self._loading:
            return
        item = self.block_list.currentItem()
        if not item:
            return
        name = item.text()
        block: dict[str, Any] = {}
        setup = self.block_setup.get_data()
        teardown = self.block_teardown.get_data()
        if setup is not None:
            block["setup"] = setup
        if teardown is not None:
            block["teardown"] = teardown
        self._blocks[name] = block
        if emit:
            self.changed.emit()


class EndpointRulesTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: dict[str, Any] = {}
        self._loading = False
        split = QSplitter()
        self.keys = KeyListPanel(key_label="Эндпоинт")
        self.keys.selection_changed.connect(self._on_select)
        self.keys.keys_changed.connect(self._sync_keys)
        self.editor = EndpointRuleEditor()
        self.editor.changed.connect(self._preview)
        self.preview = PreviewPane()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(make_scroll(self.editor), 3)
        rl.addWidget(QLabel("JSON:"))
        rl.addWidget(self.preview, 2)
        split.addWidget(self.keys)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        btn = make_button(
            "Применить текущее правило",
            "Записать форму endpoint rule в память редактора. Ctrl+S — в файл.",
            slot=self.apply_current,
            role="primary",
        )
        lay.addWidget(btn)

    def set_section(self, data: dict | None) -> None:
        self._loading = True
        self._data = copy.deepcopy(data or {})
        self.keys.set_keys(sorted(self._data.keys()))
        self._loading = False
        self._on_select(self.keys.current_key())

    def get_section(self) -> dict:
        self.apply_current(silent=True)
        return copy.deepcopy(self._data)

    def apply_current(self, silent: bool = False) -> None:
        old = self.keys.current_key()
        try:
            new_key, cfg = self.editor.get_data()
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "Ошибка", str(exc))
            return
        if old and old in self._data and old != new_key:
            del self._data[old]
        self._data[new_key] = cfg
        self.keys.set_keys(sorted(self._data.keys()))
        items = self.keys.list.findItems(new_key, Qt.MatchFlag.MatchExactly)
        if items:
            self.keys.list.setCurrentItem(items[0])
        self.preview.show_obj({new_key: cfg})
        self.dirty.emit()

    def _on_select(self, key: str) -> None:
        if self._loading or not key or key not in self._data:
            return
        self._loading = True
        self.editor.set_data(key, self._data[key])
        self.preview.show_obj({key: self._data[key]})
        self._loading = False

    def _preview(self) -> None:
        try:
            k, c = self.editor.get_data()
            self.preview.show_obj({k: c}, title="черновик")
        except ValueError as exc:
            self.preview.setPlainText(f"// {exc}")

    def _sync_keys(self) -> None:
        listed = set(self.keys._keys)
        for gone in [k for k in list(self._data) if k not in listed]:
            self._data.pop(gone, None)
        for key in listed:
            if key not in self._data:
                self._data[key] = {}
        self.dirty.emit()


# =============================================================================
# Interface rules tab
# =============================================================================
class InterfaceRuleItemEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)

        self.match_mode = QComboBox()
        self.match_mode.addItem("по prefix (bond, vlan, tunnel…)", "prefix")
        self.match_mode.addItem("по pattern (regex)", "pattern")
        self.match_mode.currentIndexChanged.connect(self.changed.emit)
        root.addWidget(labeled("Как матчить:", self.match_mode))

        self.prefix = QLineEdit()
        self.prefix.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("prefix:", self.prefix))

        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("^vlan[0-9]+$")
        self.pattern.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("pattern:", self.pattern))

        self.env = QLineEdit()
        self.env.setPlaceholderText("DEVICE_VLAN_IFNAMES")
        self.env.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("env (.env):", self.env))

        self.allowed = QLineEdit()
        self.allowed.setPlaceholderText("vlan100, vlan200")
        self.allowed.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("allowed:", self.allowed))

        flags = QHBoxLayout()
        self.physical = QCheckBox("physical (не создавать)")
        self.setup_defer = QCheckBox("setup_defer (bond после slave)")
        self.physical.stateChanged.connect(self.changed.emit)
        self.setup_defer.stateChanged.connect(self.changed.emit)
        flags.addWidget(self.physical)
        flags.addWidget(self.setup_defer)
        root.addLayout(flags)

        self.bind_fields = QLineEdit()
        self.bind_fields.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("bind_fields:", self.bind_fields))

        self.requirements = QLineEdit()
        self.requirements.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("requirements:", self.requirements))

        self.teardown_priority = QSpinBox()
        self.teardown_priority.setRange(-1, 9999)
        self.teardown_priority.setSpecialValueText("—")
        self.teardown_priority.setValue(-1)
        self.teardown_priority.valueChanged.connect(self.changed.emit)
        root.addWidget(labeled("teardown_priority:", self.teardown_priority))

        self.setup = LifecyclePhaseEditor(phase="setup")
        self.setup.changed.connect(self.changed.emit)
        self.teardown = LifecyclePhaseEditor(phase="teardown")
        self.teardown.changed.connect(self.changed.emit)
        tabs = QTabWidget()
        tabs.addTab(self.setup, "Setup / create")
        tabs.addTab(self.teardown, "Teardown / delete")
        root.addWidget(tabs, 1)

    def set_data(self, data: dict) -> None:
        data = data or {}
        if data.get("pattern"):
            self.match_mode.setCurrentIndex(1)
            self.pattern.setText(str(data.get("pattern", "")))
            self.prefix.clear()
        else:
            self.match_mode.setCurrentIndex(0)
            self.prefix.setText(str(data.get("prefix", "") or ""))
            self.pattern.clear()
        self.env.setText(str(data.get("env", "") or ""))
        self.allowed.setText(list_to_csv(data.get("allowed") or []))
        self.physical.setChecked(bool(data.get("physical")))
        self.setup_defer.setChecked(bool(data.get("setup_defer")))
        self.bind_fields.setText(list_to_csv(data.get("bind_fields") or []))
        self.requirements.setText(list_to_csv(data.get("requirements") or []))
        tp = data.get("teardown_priority")
        self.teardown_priority.setValue(int(tp) if isinstance(tp, int) else -1)
        setup = data.get("setup", data.get("create"))
        teardown = data.get("teardown", data.get("delete"))
        self.setup.set_data(setup)
        self.teardown.set_data(teardown)

    def get_data(self) -> dict:
        cfg: dict[str, Any] = {}
        if self.match_mode.currentData() == "pattern":
            pat = self.pattern.text().strip()
            if not pat:
                raise ValueError("pattern обязателен")
            cfg["pattern"] = pat
        else:
            pref = self.prefix.text().strip()
            if not pref:
                raise ValueError("prefix обязателен")
            cfg["prefix"] = pref
        if self.env.text().strip():
            cfg["env"] = self.env.text().strip()
        allowed = csv_to_list(self.allowed.text())
        if allowed:
            cfg["allowed"] = allowed
        if self.physical.isChecked():
            cfg["physical"] = True
        if self.setup_defer.isChecked():
            cfg["setup_defer"] = True
        bind = csv_to_list(self.bind_fields.text())
        if bind:
            cfg["bind_fields"] = bind
        req = csv_to_list(self.requirements.text())
        if req:
            cfg["requirements"] = req
        if self.teardown_priority.value() >= 0:
            cfg["teardown_priority"] = self.teardown_priority.value()
        setup = self.setup.get_data()
        teardown = self.teardown.get_data()
        if setup is not None:
            cfg["setup"] = setup
        if teardown is not None:
            cfg["teardown"] = teardown
        return cfg


class InterfaceRulesTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._field_key = "ifname"
        self._rules: list[dict] = []
        self._loading = False

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.field_key = QLineEdit("ifname")
        self.field_key.textChanged.connect(self.dirty.emit)
        top.addWidget(QLabel("Ключ поля (обычно ifname):"))
        top.addWidget(self.field_key, 1)
        lay.addLayout(top)

        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load_row)
        ll.addWidget(self.list)
        row = QHBoxLayout()
        for text, tip, slot, role in (
            ("+", "Добавить правило интерфейса (prefix/pattern)", self._add, "icon"),
            ("−", "Удалить выбранное правило", self._del, "danger"),
            ("↑", "Поднять правило выше (раньше матчится)", self._up, "icon"),
            ("↓", "Опустить правило ниже", self._down, "icon"),
        ):
            row.addWidget(make_button(text, tip, slot=slot, role=role, fixed_width=36))
        ll.addLayout(row)

        self.editor = InterfaceRuleItemEditor()
        self.editor.changed.connect(self._preview)
        self.preview = PreviewPane()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(make_scroll(self.editor), 3)
        rl.addWidget(self.preview, 2)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        lay.addWidget(split)
        btn = make_button(
            "Применить текущее правило ifname",
            "Записать выбранное interface rule в память. Ctrl+S — сохранить файл.",
            slot=self.apply_current,
            role="primary",
        )
        lay.addWidget(btn)

    def set_section(self, data: dict | None) -> None:
        data = data or {}
        # take first key or ifname
        if data:
            self._field_key = next(iter(data.keys()))
        else:
            self._field_key = "ifname"
        self.field_key.setText(self._field_key)
        block = data.get(self._field_key) or {}
        self._rules = copy.deepcopy(block.get("rules") or [])
        # legacy prefixes → skip for simplicity or convert
        self._refresh()
        if self._rules:
            self.list.setCurrentRow(0)

    def get_section(self) -> dict:
        self.apply_current(silent=True)
        key = self.field_key.text().strip() or "ifname"
        return {key: {"rules": copy.deepcopy(self._rules)}}

    def _refresh(self) -> None:
        self.list.blockSignals(True)
        row = self.list.currentRow()
        self.list.clear()
        for i, rule in enumerate(self._rules):
            label = rule.get("prefix") or rule.get("pattern") or f"rule_{i}"
            self.list.addItem(f"{i + 1}. {label}")
        self.list.blockSignals(False)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    def _load_row(self, row: int) -> None:
        if row < 0 or row >= len(self._rules):
            return
        self._loading = True
        self.editor.set_data(self._rules[row])
        self.preview.show_obj(self._rules[row])
        self._loading = False

    def apply_current(self, silent: bool = False) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        try:
            self._rules[row] = self.editor.get_data()
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "Ошибка", str(exc))
            return
        self._refresh()
        self.list.setCurrentRow(row)
        self.preview.show_obj(self._rules[row])
        self.dirty.emit()

    def _preview(self) -> None:
        if self._loading:
            return
        try:
            self.preview.show_obj(self.editor.get_data(), title="черновик")
        except ValueError as exc:
            self.preview.setPlainText(f"// {exc}")

    def _add(self) -> None:
        self._rules.append({"prefix": "new", "setup": {"endpoint": "/", "method": "POST", "payload": {}}})
        self._refresh()
        self.list.setCurrentRow(len(self._rules) - 1)
        self.dirty.emit()

    def _del(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        del self._rules[row]
        self._refresh()
        self.dirty.emit()

    def _up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self.apply_current(silent=True)
        self._rules[row - 1], self._rules[row] = self._rules[row], self._rules[row - 1]
        self._refresh()
        self.list.setCurrentRow(row - 1)
        self.dirty.emit()

    def _down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._rules) - 1:
            return
        self.apply_current(silent=True)
        self._rules[row + 1], self._rules[row] = self._rules[row], self._rules[row + 1]
        self._refresh()
        self.list.setCurrentRow(row + 1)
        self.dirty.emit()


# =============================================================================
# Simple map tabs: mock_data, reserved, synthetic, interface_lifecycle
# =============================================================================
class MapListTab(QWidget):
    """Редактор dict[str, list] — mock by_field / reserved / synthetic."""

    dirty = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        key_label: str = "Ключ",
        value_label: str = "Значения (через запятую или JSON-массив)",
        section_root: str | None = None,
        nested_key: str | None = None,
        hint: str = "",
    ):
        super().__init__(parent)
        self.section_root = section_root
        self.nested_key = nested_key
        self._map: dict[str, Any] = {}
        self._loading = False

        lay = QVBoxLayout(self)
        if hint:
            lay.addWidget(hint_label(hint))

        split = QSplitter()
        self.keys = KeyListPanel(key_label=key_label)
        self.keys.selection_changed.connect(self._on_select)
        self.keys.keys_changed.connect(self._sync)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.key_edit = QLineEdit()
        self.key_edit.textChanged.connect(self._draft)
        rl.addWidget(labeled(key_label + ":", self.key_edit))
        self.values = QPlainTextEdit()
        self.values.setPlaceholderText(value_label)
        self.values.setFont(QFont("Consolas", 10))
        self.values.setToolTip(value_label)
        self.values.textChanged.connect(self._draft)
        rl.addWidget(QLabel(value_label))
        rl.addWidget(self.values, 1)
        self.preview = PreviewPane()
        rl.addWidget(self.preview, 1)
        btn = make_button(
            "Применить",
            "Записать ключ и значения в память редактора. Ctrl+S — в файл.",
            slot=self.apply_current,
            role="primary",
        )
        rl.addWidget(btn)

        split.addWidget(self.keys)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)

    def set_map(self, data: dict | None) -> None:
        self._loading = True
        self._map = copy.deepcopy(data or {})
        self.keys.set_keys(sorted(self._map.keys(), key=str))
        self._loading = False
        self._on_select(self.keys.current_key())

    def get_map(self) -> dict:
        self.apply_current(silent=True)
        return copy.deepcopy(self._map)

    def apply_current(self, silent: bool = False) -> None:
        old = self.keys.current_key()
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            values = normalize_values_list(self.values.toPlainText())
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "Ошибка", str(exc))
            return
        if old and old in self._map and old != key:
            del self._map[old]
        self._map[key] = values
        self.keys.set_keys(sorted(self._map.keys(), key=str))
        items = self.keys.list.findItems(key, Qt.MatchFlag.MatchExactly)
        if items:
            self.keys.list.setCurrentItem(items[0])
        self.preview.show_obj({key: values})
        self.dirty.emit()

    def _on_select(self, key: str) -> None:
        if self._loading or not key or key not in self._map:
            return
        self._loading = True
        self.key_edit.setText(key)
        val = self._map[key]
        if isinstance(val, list):
            self.values.setPlainText(list_to_csv(val))
        else:
            self.values.setPlainText(str(val))
        self.preview.show_obj({key: val})
        self._loading = False

    def _draft(self) -> None:
        if self._loading:
            return
        try:
            key = self.key_edit.text().strip() or "?"
            values = normalize_values_list(self.values.toPlainText())
            self.preview.show_obj({key: values}, title="черновик")
        except Exception as exc:
            self.preview.setPlainText(f"// {exc}")

    def _sync(self) -> None:
        listed = set(self.keys._keys)
        for gone in [k for k in list(self._map) if k not in listed]:
            self._map.pop(gone, None)
        for key in listed:
            if key not in self._map:
                self._map[key] = []
        self.dirty.emit()


class MockDataTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.by_field = MapListTab(
            key_label="Имя поля",
            value_label="Значения mock (csv или JSON-массив)",
            hint="by_field имеет приоритет над by_schema. Пример: igmp_dest_addr → 224.0.0.1, 239.1.1.1",
        )
        self.by_schema = MapListTab(
            key_label="Имя схемы OpenAPI",
            value_label="Значения (csv или JSON-массив)",
            hint="by_schema: IP_ADDR, FILTER_PORT_LISTING, … — матч по pattern компонента",
        )
        self.by_field.dirty.connect(self.dirty.emit)
        self.by_schema.dirty.connect(self.dirty.emit)
        self.tabs.addTab(self.by_field, "by_field")
        self.tabs.addTab(self.by_schema, "by_schema")
        lay.addWidget(self.tabs)

    def set_section(self, data: dict | None) -> None:
        data = data or {}
        self.by_field.set_map(data.get("by_field") or {})
        self.by_schema.set_map(data.get("by_schema") or {})

    def get_section(self) -> dict:
        out: dict[str, Any] = {}
        bf = self.by_field.get_map()
        bs = self.by_schema.get_map()
        if bf:
            out["by_field"] = bf
        if bs:
            out["by_schema"] = bs
        return out


class InterfaceLifecycleTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QFormLayout(self)
        self.enabled = QCheckBox("enabled")
        self.enabled.setChecked(True)
        self.enabled.stateChanged.connect(self.dirty.emit)
        self.rules_key = QLineEdit("ifname")
        self.rules_key.textChanged.connect(self.dirty.emit)
        self.schema_components = QPlainTextEdit()
        self.schema_components.setPlaceholderText("IFNAME\ninterfaces_vlan_ifname\n…")
        self.schema_components.textChanged.connect(self.dirty.emit)
        self.exclude_fields = QLineEdit()
        self.exclude_fields.textChanged.connect(self.dirty.emit)
        lay.addRow(self.enabled)
        lay.addRow("rules_key", self.rules_key)
        lay.addRow("schema_components (по строке)", self.schema_components)
        lay.addRow("exclude_fields", self.exclude_fields)
        tip = hint_label("Какие OpenAPI $ref считать именем интерфейса и гонять через interface_rules.")
        lay.addRow(tip)

    def set_section(self, data: dict | None) -> None:
        data = data or {}
        self.enabled.setChecked(data.get("enabled", True) is not False)
        self.rules_key.setText(str(data.get("rules_key", "ifname")))
        comps = data.get("schema_components") or []
        self.schema_components.setPlainText("\n".join(str(c) for c in comps))
        self.exclude_fields.setText(list_to_csv(data.get("exclude_fields") or []))

    def get_section(self) -> dict:
        comps = [ln.strip() for ln in self.schema_components.toPlainText().splitlines() if ln.strip()]
        out: dict[str, Any] = {
            "rules_key": self.rules_key.text().strip() or "ifname",
            "schema_components": comps,
        }
        if not self.enabled.isChecked():
            out["enabled"] = False
        excl = csv_to_list(self.exclude_fields.text())
        if excl:
            out["exclude_fields"] = excl
        return out


class SyntheticBindTab(MapListTab):
    def __init__(self, parent=None):
        super().__init__(
            parent,
            key_label="Эндпоинт",
            value_label="Поля через запятую (acl_name, time…)",
            hint="Для delete/modify без ID в payload — взять поля из mock_data.by_field и повесить field_mappings.",
        )

    def set_section(self, data: dict | None) -> None:
        self.set_map(data)

    def get_section(self) -> dict:
        return self.get_map()


class ReservedValuesTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        self.map = MapListTab(
            key_label="Имя поля",
            value_label="Запрещённые значения",
            hint="reserved_values.by_field — генератор не будет использовать эти значения (vlan1, vid 603…).",
        )
        self.map.dirty.connect(self.dirty.emit)
        lay.addWidget(self.map)

    def set_section(self, data: dict | None) -> None:
        data = data or {}
        self.map.set_map(data.get("by_field") or {})

    def get_section(self) -> dict:
        bf = self.map.get_map()
        return {"by_field": bf} if bf else {"by_field": {}}


# =============================================================================
# Field couplings tab
# =============================================================================
class FieldCouplingEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)

        self.endpoints = QLineEdit()
        self.endpoints.setPlaceholderText("/acl/acl_ipv4  или пусто = все")
        self.endpoints.textChanged.connect(self.changed.emit)
        root.addWidget(labeled("endpoints:", self.endpoints))

        when_box = QGroupBox("when (условие)")
        wf = QFormLayout(when_box)
        self.when_path = QLineEdit()
        self.when_path.setPlaceholderText("action.add.rule.icmp")
        self.when_path.textChanged.connect(self.changed.emit)
        self.when_mode = QComboBox()
        self.when_mode.addItem("present: true (поле есть)", "present")
        self.when_mode.addItem("in: список значений", "in")
        self.when_mode.currentIndexChanged.connect(self.changed.emit)
        self.when_in = QLineEdit()
        self.when_in.setPlaceholderText("gretap, gre  (для режима in)")
        self.when_in.textChanged.connect(self.changed.emit)
        wf.addRow("path *", self.when_path)
        wf.addRow("режим", self.when_mode)
        wf.addRow("in values", self.when_in)
        root.addWidget(when_box)

        self.only_if_missing = QCheckBox("only_if_missing (ensure только если поля ещё нет)")
        self.only_if_missing.setChecked(True)
        self.only_if_missing.stateChanged.connect(self.changed.emit)
        root.addWidget(self.only_if_missing)

        root.addWidget(QLabel("ensure — что выставить (JSON-объект путей):"))
        tip = hint_label(
            'Формат:\n'
            '{\n'
            '  "action.add.rule.protocol": { "value": { "protocol_name": "icmp" } },\n'
            '  "settings.dst_address": { "from_mock": "dst_address" },\n'
            '  "x.y": { "values": ["a", "b"] }\n'
            '}'
        )
        tip.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(tip)
        self.ensure = JsonEditor(min_h=160)
        self.ensure.changed.connect(self.changed.emit)
        root.addWidget(self.ensure, 1)

        self.remove = QPlainTextEdit()
        self.remove.setPlaceholderText("action.add.rule.tcp_flags\naction.add.rule.sourceports")
        self.remove.setMaximumHeight(100)
        self.remove.textChanged.connect(self.changed.emit)
        root.addWidget(QLabel("remove — пути удалить (по одному на строку):"))
        root.addWidget(self.remove)

    def set_data(self, data: dict) -> None:
        data = data or {}
        self.endpoints.setText(list_to_csv(data.get("endpoints") or []))
        when = data.get("when") or {}
        self.when_path.setText(str(when.get("path", "") or ""))
        if when.get("present") is True:
            self.when_mode.setCurrentIndex(0)
            self.when_in.clear()
        else:
            self.when_mode.setCurrentIndex(1)
            self.when_in.setText(list_to_csv(when.get("in") or []))
        self.only_if_missing.setChecked(bool(data.get("only_if_missing", True)))
        self.ensure.set_data(data.get("ensure") or {})
        rem = data.get("remove") or []
        self.remove.setPlainText("\n".join(str(x) for x in rem))

    def get_data(self) -> dict:
        path = self.when_path.text().strip()
        if not path:
            raise ValueError("when.path обязателен")
        when: dict[str, Any] = {"path": path}
        if self.when_mode.currentData() == "present":
            when["present"] = True
        else:
            values = normalize_values_list(self.when_in.text())
            if not values:
                raise ValueError("when.in: нужен хотя бы один элемент")
            when["in"] = values

        ok, ensure, err = self.ensure.try_get_data()
        if not ok:
            raise ValueError(f"ensure JSON: {err}")
        if ensure is None:
            ensure = {}
        if ensure and not isinstance(ensure, dict):
            raise ValueError("ensure должен быть объектом")

        remove = [ln.strip() for ln in self.remove.toPlainText().splitlines() if ln.strip()]
        if not ensure and not remove:
            raise ValueError("Нужен ensure и/или remove")

        cfg: dict[str, Any] = {"when": when}
        eps = csv_to_list(self.endpoints.text())
        if eps:
            cfg["endpoints"] = eps
        if ensure:
            cfg["ensure"] = ensure
        if remove:
            cfg["remove"] = remove
        cfg["only_if_missing"] = self.only_if_missing.isChecked()
        return cfg


class FieldCouplingsTab(QWidget):
    dirty = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules: list[dict] = []
        self._loading = False
        lay = QVBoxLayout(self)
        lay.addWidget(hint_label(
            "Couplings правят payload после генерации: при условии when — ensure поля и/или remove пути. "
            "Порядок правил = приоритет."
        ))

        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load)
        ll.addWidget(self.list)
        row = QHBoxLayout()
        for text, tip, slot, role in (
            ("+", "Добавить coupling-правило", self._add, "icon"),
            ("−", "Удалить выбранное правило", self._del, "danger"),
            ("↑", "Поднять правило выше (выше приоритет)", self._up, "icon"),
            ("↓", "Опустить правило ниже", self._down, "icon"),
        ):
            row.addWidget(make_button(text, tip, slot=slot, role=role, fixed_width=36))
        ll.addLayout(row)

        self.editor = FieldCouplingEditor()
        self.editor.changed.connect(self._preview)
        self.preview = PreviewPane()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(make_scroll(self.editor), 3)
        rl.addWidget(self.preview, 2)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        lay.addWidget(split)
        lay.addWidget(make_button(
            "Применить текущее coupling-правило",
            "Записать форму coupling в память. Ctrl+S — сохранить dependencies.json.",
            slot=self.apply_current,
            role="primary",
        ))

    def set_section(self, data: list | None) -> None:
        self._rules = copy.deepcopy(data or [])
        self._refresh()
        if self._rules:
            self.list.setCurrentRow(0)

    def get_section(self) -> list:
        self.apply_current(silent=True)
        return copy.deepcopy(self._rules)

    def _refresh(self) -> None:
        self.list.blockSignals(True)
        row = self.list.currentRow()
        self.list.clear()
        for i, rule in enumerate(self._rules):
            when = (rule.get("when") or {}).get("path", "?")
            eps = ",".join(rule.get("endpoints") or ["*"])
            self.list.addItem(f"{i + 1}. [{eps}] when {when}")
        self.list.blockSignals(False)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    def _load(self, row: int) -> None:
        if row < 0 or row >= len(self._rules):
            return
        self._loading = True
        self.editor.set_data(self._rules[row])
        self.preview.show_obj(self._rules[row])
        self._loading = False

    def apply_current(self, silent: bool = False) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        try:
            self._rules[row] = self.editor.get_data()
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "Ошибка", str(exc))
            return
        self._refresh()
        self.list.setCurrentRow(row)
        self.preview.show_obj(self._rules[row])
        self.dirty.emit()

    def _preview(self) -> None:
        if self._loading:
            return
        try:
            self.preview.show_obj(self.editor.get_data(), title="черновик")
        except ValueError as exc:
            self.preview.setPlainText(f"// {exc}")

    def _add(self) -> None:
        self._rules.append({
            "when": {"path": "action.add.rule.icmp", "present": True},
            "ensure": {"action.add.rule.protocol": {"value": {"protocol_name": "icmp"}}},
            "only_if_missing": False,
        })
        self._refresh()
        self.list.setCurrentRow(len(self._rules) - 1)
        self.dirty.emit()

    def _del(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        del self._rules[row]
        self._refresh()
        self.dirty.emit()

    def _up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self.apply_current(silent=True)
        self._rules[row - 1], self._rules[row] = self._rules[row], self._rules[row - 1]
        self._refresh()
        self.list.setCurrentRow(row - 1)
        self.dirty.emit()

    def _down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._rules) - 1:
            return
        self.apply_current(silent=True)
        self._rules[row + 1], self._rules[row] = self._rules[row], self._rules[row + 1]
        self._refresh()
        self.list.setCurrentRow(row + 1)
        self.dirty.emit()


# =============================================================================
# Main window
# =============================================================================
class DependenciesEditor(QMainWindow):
    def __init__(self, path: Path | None = None):
        super().__init__()
        self.path = Path(path) if path else DEFAULT_PATH
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._theme = load_saved_theme()
        self.setWindowTitle(f"Dependencies Editor — {self.path.name}")
        self.resize(1280, 840)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(False)
        self.tab_field_mappings = FieldMappingsTab()
        self.tab_endpoint_rules = EndpointRulesTab()
        self.tab_interface_rules = InterfaceRulesTab()
        self.tab_interface_lifecycle = InterfaceLifecycleTab()
        self.tab_synthetic = SyntheticBindTab()
        self.tab_mock = MockDataTab()
        self.tab_couplings = FieldCouplingsTab()
        self.tab_reserved = ReservedValuesTab()

        tab_tips = [
            (self.tab_field_mappings, "1. Field mappings",
             "Зависимости по имени поля в payload (setup/teardown)"),
            (self.tab_endpoint_rules, "2. Endpoint rules",
             "Lifecycle, привязанный к тестируемому URL"),
            (self.tab_interface_rules, "3. Interface rules",
             "Создание/удаление интерфейсов по prefix или pattern"),
            (self.tab_interface_lifecycle, "4. Interface lifecycle",
             "Какие OpenAPI-схемы считать ifname"),
            (self.tab_synthetic, "5. Synthetic bind",
             "Поля для delete/modify из mock_data"),
            (self.tab_mock, "6. Mock data",
             "Фиксированные значения вместо случайного JSF"),
            (self.tab_couplings, "7. Field couplings",
             "when → ensure / remove после генерации payload"),
            (self.tab_reserved, "8. Reserved values",
             "Значения, которые генератор никогда не использует"),
        ]
        for widget, title, tip in tab_tips:
            idx = self.tabs.addTab(widget, title)
            self.tabs.setTabToolTip(idx, tip)

        for tab in (
            self.tab_field_mappings, self.tab_endpoint_rules, self.tab_interface_rules,
            self.tab_interface_lifecycle, self.tab_synthetic, self.tab_mock,
            self.tab_couplings, self.tab_reserved,
        ):
            tab.dirty.connect(self._mark_dirty)

        help_w = QPlainTextEdit()
        help_w.setObjectName("HelpPane")
        help_w.setReadOnly(True)
        help_w.setPlainText(self._help_text())
        help_w.setToolTip("Краткая справка по секциям редактора")
        self.tabs.addTab(help_w, "Справка")
        self.tabs.setTabToolTip(self.tabs.count() - 1, "Справка по использованию")

        self.setCentralWidget(self.tabs)
        self._build_menu()
        self.statusBar().showMessage(str(self.path))
        self.load_file(self.path)

    def _help_text(self) -> str:
        return """\
РЕДАКТОР dependencies.json

Как пользоваться:
1. Выбери вкладку секции.
2. В списке слева — правила; + / − / ✎ — добавить, удалить, переименовать.
3. Справа — форма. Внизу справа — JSON, который уйдёт в файл.
4. «Применить…» → в память редактора, затем Ctrl+S → на диск.

Тема: меню «Вид» → Светлая / Тёмная (сохраняется между запусками).
Наведи курсор на кнопку — появится подсказка.

Секции:
• Field mappings — поле в payload → setup/teardown.
• Endpoint rules — lifecycle по URL (+ блоки add/delete/a/mx).
• Interface rules — create/delete интерфейсов по prefix/pattern.
• Interface lifecycle — какие OpenAPI-схемы = ifname.
• Synthetic bind — delete без ID: поля из mock.
• Mock data — фиксированные значения вместо JSF.
• Field couplings — when → ensure/remove.
• Reserved values — запрет vlan1 / vid 603 и т.п.

Плейсхолдеры: {{acl_name}}, {{vid}}, {{item}}…
"""

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Файл")
        file_menu.setToolTipsVisible(True)
        file_acts = [
            ("Открыть…", "Открыть другой dependencies.json", QKeySequence.StandardKey.Open, self.open_file),
            ("Сохранить", "Записать все секции в текущий файл", QKeySequence.StandardKey.Save, self.save_file),
            ("Сохранить как…", "Сохранить копию под новым именем", QKeySequence.StandardKey.SaveAs, self.save_file_as),
            ("Перезагрузить с диска", "Отбросить несохранённые правки и заново прочитать файл",
             QKeySequence.StandardKey.Refresh, self.reload_file),
            ("Проверить сборку JSON", "Собрать JSON из всех вкладок и проверить валидность",
             None, self.validate_all),
        ]
        for title, tip, shortcut, slot in file_acts:
            act = QAction(title, self)
            act.setToolTip(tip)
            act.setStatusTip(tip)
            if shortcut:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            file_menu.addAction(act)

        view_menu = self.menuBar().addMenu("Вид")
        view_menu.setToolTipsVisible(True)
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self._act_light = QAction("Светлая тема", self)
        self._act_light.setCheckable(True)
        self._act_light.setToolTip("Светлый интерфейс (teal / slate)")
        self._act_light.setStatusTip("Включить светлую тему")
        self._act_dark = QAction("Тёмная тема", self)
        self._act_dark.setCheckable(True)
        self._act_dark.setToolTip("Тёмный интерфейс для работы вечером")
        self._act_dark.setStatusTip("Включить тёмную тему")
        theme_group.addAction(self._act_light)
        theme_group.addAction(self._act_dark)
        view_menu.addAction(self._act_light)
        view_menu.addAction(self._act_dark)
        if self._theme == "dark":
            self._act_dark.setChecked(True)
        else:
            self._act_light.setChecked(True)
        self._act_light.triggered.connect(lambda: self.set_theme("light"))
        self._act_dark.triggered.connect(lambda: self.set_theme("dark"))

        toggle = QAction("Переключить тему", self)
        toggle.setShortcut(QKeySequence("Ctrl+T"))
        toggle.setToolTip("Быстро переключить светлую ↔ тёмную (Ctrl+T)")
        toggle.setStatusTip("Переключить тему")
        toggle.triggered.connect(self.toggle_theme)
        view_menu.addSeparator()
        view_menu.addAction(toggle)

    def set_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if app is None:
            return
        self._theme = "dark" if theme == "dark" else "light"
        apply_app_theme(app, self._theme)
        if self._theme == "dark":
            self._act_dark.setChecked(True)
        else:
            self._act_light.setChecked(True)
        self.statusBar().showMessage(
            f"Тема: {'тёмная' if self._theme == 'dark' else 'светлая'} | {self.path}",
            4000,
        )

    def toggle_theme(self) -> None:
        self.set_theme("light" if self._theme == "dark" else "dark")

    def _mark_dirty(self) -> None:
        self._dirty = True
        title = f"Dependencies Editor — {self.path.name} *"
        self.setWindowTitle(title)

    def _collect(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        # preserve unknown top-level keys
        for key, val in self._data.items():
            if key not in SECTION_ORDER:
                out[key] = copy.deepcopy(val)

        out["field_mappings"] = self.tab_field_mappings.get_section()
        out["endpoint_rules"] = self.tab_endpoint_rules.get_section()
        out["interface_rules"] = self.tab_interface_rules.get_section()
        out["interface_lifecycle"] = self.tab_interface_lifecycle.get_section()
        out["synthetic_bind_fields"] = self.tab_synthetic.get_section()
        out["mock_data"] = self.tab_mock.get_section()
        out["field_couplings"] = self.tab_couplings.get_section()
        out["reserved_values"] = self.tab_reserved.get_section()
        return out

    def load_file(self, path: Path) -> None:
        if not path.is_file():
            QMessageBox.warning(self, "Файл", f"Не найден: {path}")
            self._data = {k: {} if k != "field_couplings" else [] for k in SECTION_ORDER}
            self._data["field_couplings"] = []
            self._push_to_tabs()
            return
        with open(path, encoding="utf-8") as f:
            self._data = json.load(f)
        if not isinstance(self._data, dict):
            raise ValueError("Корень dependencies.json должен быть объектом")
        self.path = path
        self._push_to_tabs()
        self._dirty = False
        self.setWindowTitle(f"Dependencies Editor — {self.path.name}")
        self.statusBar().showMessage(f"Загружено: {path} | секций: {len(self._data)}")

    def _push_to_tabs(self) -> None:
        self.tab_field_mappings.set_section(self._data.get("field_mappings"))
        self.tab_endpoint_rules.set_section(self._data.get("endpoint_rules"))
        self.tab_interface_rules.set_section(self._data.get("interface_rules"))
        self.tab_interface_lifecycle.set_section(self._data.get("interface_lifecycle"))
        self.tab_synthetic.set_section(self._data.get("synthetic_bind_fields"))
        self.tab_mock.set_section(self._data.get("mock_data"))
        self.tab_couplings.set_section(self._data.get("field_couplings"))
        self.tab_reserved.set_section(self._data.get("reserved_values"))

    def open_file(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть dependencies.json", str(self.path.parent), "JSON (*.json)"
        )
        if path:
            self.load_file(Path(path))

    def save_file(self) -> None:
        try:
            data = self._collect()
            # validate round-trip
            json.dumps(data)
        except Exception as exc:
            QMessageBox.critical(self, "Не сохранено", str(exc))
            return
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        self._data = data
        self._dirty = False
        self.setWindowTitle(f"Dependencies Editor — {self.path.name}")
        self.statusBar().showMessage(f"Сохранено: {self.path}")

    def save_file_as(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", str(self.path), "JSON (*.json)"
        )
        if path:
            self.path = Path(path)
            self.save_file()

    def reload_file(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Перезагрузить",
                "Есть несохранённые изменения. Перезагрузить с диска?",
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.load_file(self.path)

    def validate_all(self) -> None:
        try:
            data = self._collect()
            text = dumps(data)
            json.loads(text)
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return
        fm = len(data.get("field_mappings") or {})
        er = len(data.get("endpoint_rules") or {})
        fc = len(data.get("field_couplings") or [])
        QMessageBox.information(
            self, "OK",
            f"JSON валиден.\n"
            f"field_mappings: {fm}\n"
            f"endpoint_rules: {er}\n"
            f"field_couplings: {fc}\n"
            f"Размер: {len(text)} байт",
        )

    def closeEvent(self, event) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Выход",
                "Сохранить изменения перед выходом?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Save:
                self.save_file()
                event.accept()
            elif ans == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    app = QApplication(argv)
    app.setApplicationName("Dependencies Editor")
    app.setOrganizationName(THEME_SETTINGS_ORG)
    app.setStyle(QStyleFactory.create("Fusion") or app.style())
    apply_app_theme(app, load_saved_theme())
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    win = DependenciesEditor(path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
