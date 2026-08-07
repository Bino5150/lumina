from PySide6.QtWidgets import (
    QWidget, QLabel, QTextEdit, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QAbstractItemView, QScrollArea
)
from PySide6.QtGui import QPixmap, QPainter, QPainterPath
from PySide6.QtCore import Qt


# ── Style helpers ──────────────────────────────────────────────────────────────

def _sec(text: str, c: dict) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{c['accent']};font-size:10px;font-weight:bold;letter-spacing:2px;padding:10px 0 4px 0;background:transparent;")
    return lbl

def _lbl(text: str, c: dict) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{c['text_muted']};font-size:12px;background:transparent;")
    return lbl

def _te(default: str, c: dict, single: bool = False, height: int = None) -> QTextEdit:
    te = QTextEdit()
    te.setPlainText(default)
    if single:
        te.setFixedHeight(36)
        te.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    elif height:
        te.setFixedHeight(height)
    te.setStyleSheet(f"""
        QTextEdit{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:5px 10px;font-size:12px;}}
        QTextEdit:focus{{border:1px solid {c['border_accent']};}}
    """)
    return te

def _le(default: str, c: dict) -> QLineEdit:
    le = QLineEdit(default)
    le.setFixedHeight(36)
    le.setStyleSheet(f"""
        QLineEdit{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:5px 10px;font-size:12px;}}
        QLineEdit:focus{{border:1px solid {c['border_accent']};}}
    """)
    return le

def _btn(text: str, c: dict, accent: bool = False, danger: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    if accent:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['accent']};color:{c['bg_deep']};border:none;
            border-radius:7px;padding:8px 18px;font-size:12px;font-weight:bold;}}
            QPushButton:hover{{background:#33ecff;}}
        """)
    elif danger:
        btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{c['danger']};border:1px solid {c['danger']}44;
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{background:{c['danger']}22;border-color:{c['danger']};}}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['bg_card']};color:{c['text_primary']};border:1px solid {c['border']};
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{border-color:{c['accent_dim']};color:{c['accent']};}}
        """)
    return btn

def _spin(val: int, lo: int, hi: int, step: int, c: dict) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(val)
    s.setFixedHeight(36)
    s.setStyleSheet(f"""
        QSpinBox{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:4px 8px;font-size:12px;}}
    """)
    return s

def _table(cols: list, c: dict) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setAlternatingRowColors(False)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.setStyleSheet(f"""
        QTableWidget{{background:{c['bg_card']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:8px;gridline-color:{c['border']};font-size:12px;}}
        QTableWidget::item{{padding:6px 10px;border:none;}}
        QTableWidget::item:selected{{background:{c['accent_glow']};color:{c['accent']};}}
        QHeaderView::section{{background:{c['bg_panel']};color:{c['text_muted']};
        border:none;border-bottom:1px solid {c['border']};padding:6px 10px;font-size:11px;font-weight:bold;}}
    """)
    return t

def make_round_pixmap(path: str, size: int) -> QPixmap:
    src = QPixmap(path).scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, src)
    p.end()
    return out

def _scroll_wrap(widget: QWidget, c: dict) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setStyleSheet(f"QScrollArea{{background:{c['bg_deep']};border:none;}}")
    sa.setWidget(widget)
    return sa
