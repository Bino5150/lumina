from PySide6.QtWidgets import (
    QWidget, QLabel, QTextEdit, QLineEdit, QPushButton, QSpinBox, QComboBox,
    QTableWidget, QAbstractItemView, QScrollArea
)
from PySide6.QtGui import QPixmap, QPainter, QPainterPath
from PySide6.QtCore import Qt, QTimer

import os as _os
import shiboken6
# 2026-08-14 arrow-visibility fix: the QSS "zero-size box + one colored
# border side" triangle trick (standard on the web) does NOT reliably
# render as a triangle for Qt's ::down-arrow/::up-arrow subcontrols -- Qt's
# style engine pre-allocates that subcontrol's geometry itself and paints
# whatever region it reserved, so the result was a plain filled rectangle,
# not a chevron (caught by an actual offscreen render, not assumed from the
# CSS). Real small PNG icons, same as every other Qt dark-theme project
# (QDarkStyleSheet etc.) uses for this exact subcontrol, is the reliable
# fix. Icons live in assets/icons/ -- ASSETS_DIR-relative like every other
# bundled asset in this codebase (see config.ASSETS_DIR).
_ICONS_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__)))), "assets", "icons")

def _icon_url(name: str) -> str:
    # Qt QSS requires forward slashes in url() regardless of platform.
    return _os.path.join(_ICONS_DIR, name).replace(_os.sep, "/")


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
    # :pressed is declared after :hover in each block so it wins the cascade
    # when a button is pressed while still under the cursor (both selectors
    # match at that instant; Qt's stylesheet engine resolves equal-specificity
    # conflicts by source order, same as CSS) -- otherwise a real finger-down
    # click would render as plain hover, giving no distinct press feedback.
    # Colors reuse the existing palette only (accent_glow/accent_dim/bg_deep),
    # no new design-system tokens; padding/border-width are unchanged from
    # the released/hover rules so pressing never shifts layout geometry.
    if accent:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['accent']};color:{c['bg_deep']};border:none;
            border-radius:7px;padding:8px 18px;font-size:12px;font-weight:bold;}}
            QPushButton:hover{{background:#33ecff;}}
            QPushButton:pressed{{background:{c['accent_dim']};}}
        """)
    elif danger:
        btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{c['danger']};border:1px solid {c['danger']}44;
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{background:{c['danger']}22;border-color:{c['danger']};}}
            QPushButton:pressed{{background:{c['danger']};color:{c['bg_deep']};border-color:{c['danger']};}}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['bg_card']};color:{c['text_primary']};border:1px solid {c['border']};
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{border-color:{c['accent_dim']};color:{c['accent']};}}
            QPushButton:pressed{{background:{c['accent_glow']};border-color:{c['accent']};color:{c['accent']};}}
        """)
    return btn

class ButtonFeedback:
    """Shared success/failure text feedback for a synchronous Settings
    Save/Apply button (Patch 3A.3B). TTSTab already solved this for its
    async single-flight case (_schedule_feedback_reset's generation-guarded
    QTimer); this is the same idea generalized for the plain "click ->
    action already ran synchronously -> show the truthful outcome" case
    used everywhere else in Settings, so each tab doesn't reinvent its own
    QTimer bookkeeping.

    success() shows text then reverts to the button's idle text after
    HOLD_MS. failure() shows text and does NOT revert on its own -- it
    stays until the next success()/failure() call replaces it, so a real
    error can't quietly vanish before anyone reads it. Both bump a
    per-instance generation counter, so a delayed revert scheduled by an
    older call can never stomp state set by a newer one. The delayed
    revert also checks shiboken6.isValid(btn) before touching it, since a
    rebuildable panel (PersonasTab's right panel, rebuilt on every persona
    selection) can delete the button out from under a pending timer.
    """

    HOLD_MS = 1750

    def __init__(self, btn: QPushButton, idle_text: str = None):
        self.btn = btn
        self.idle_text = btn.text() if idle_text is None else idle_text
        self._generation = 0

    def success(self, text: str = "✓ Saved", hold_ms: int = None):
        self._generation += 1
        generation = self._generation
        self.btn.setText(text)
        QTimer.singleShot(
            self.HOLD_MS if hold_ms is None else hold_ms,
            lambda: self._revert(generation),
        )

    def failure(self, text: str = "✗ Failed"):
        self._generation += 1
        self.btn.setText(text)

    def _revert(self, generation: int):
        if generation != self._generation:
            return
        if not shiboken6.isValid(self.btn):
            return
        self.btn.setText(self.idle_text)


def safe_error_detail(e: Exception) -> str:
    """Exception TYPE name only (e.g. "PermissionError", "OSError") --
    deliberately never str(e). For credential-storage failure paths, the
    exception message body is untrusted: a buggy or malicious secret-store
    implementation could embed the credential value that failed to store
    directly in its message (see 3A.3B Correction 2). The type name alone
    still carries real operator value without ever risking leaking one."""
    return type(e).__name__


def _spin(val: int, lo: int, hi: int, step: int, c: dict) -> QSpinBox:
    # Arrow visibility fix (2026-08-14): the old stylesheet never touched the
    # up-button/down-button subcontrols at all, so they fell back to whatever
    # the platform style draws by default -- on this dark palette that's
    # frequently a near-invisible dark-on-dark glyph. Explicit button
    # backgrounds make the clickable region itself visible, and real chevron
    # icons (see _icon_url() above) make the arrows visible -- verified by
    # an actual offscreen render, not assumed from the stylesheet text.
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(val)
    s.setFixedHeight(36)
    up_icon = _icon_url("chevron_up.png")
    down_icon = _icon_url("chevron_down.png")
    s.setStyleSheet(f"""
        QSpinBox{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:4px 8px;font-size:12px;}}
        QSpinBox::up-button, QSpinBox::down-button{{
            subcontrol-origin:border;width:18px;background:{c['bg_card']};
            border-left:1px solid {c['border']};
        }}
        QSpinBox::up-button{{subcontrol-position:top right;border-top-right-radius:6px;}}
        QSpinBox::down-button{{subcontrol-position:bottom right;border-bottom-right-radius:6px;}}
        QSpinBox::up-button:hover, QSpinBox::down-button:hover{{background:{c['accent_glow']};}}
        QSpinBox::up-arrow{{image:url({up_icon});}}
        QSpinBox::down-arrow{{image:url({down_icon});}}
    """)
    return s

def _combo(c: dict) -> QComboBox:
    """Shared QComboBox factory -- same rationale as _spin() above. Every
    call site in this package (and the app-wide stylesheet in main_window.py)
    had independently reinvented `QComboBox::drop-down{border:none;...}` with
    no `::down-arrow` rule at all, so the dropdown affordance was invisible
    everywhere at once, not just on the one field Bino happened to notice
    (S60-era pattern already named in the deep audit: one root cause copied
    into N call sites). Fixed once here; callers just add/populate items and
    connect signals same as before -- this only owns sizing/styling, matching
    the _le/_btn/_spin convention of "factory returns a styled widget, caller
    wires behavior.\""""
    # NOTE: a QComboBox:hover::down-arrow{image:...} state-swap rule was
    # tried here (matching the spinbox hover-highlight) and pulled during
    # verification -- it caused Qt to paint a second, oversized, mispositioned
    # copy of the arrow (offscreen-render-confirmed, not just suspected).
    # The static arrow below renders correctly and consistently; the border
    # hover-highlight on the field itself is enough affordance on its own.
    cb = QComboBox()
    cb.setFixedHeight(36)
    down_icon = _icon_url("chevron_down.png")
    cb.setStyleSheet(f"""
        QComboBox{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
        QComboBox:hover{{border-color:{c['accent_dim']};}}
        QComboBox::drop-down{{
            subcontrol-origin:padding;subcontrol-position:top right;width:22px;
            border-left:1px solid {c['border']};background:{c['bg_card']};
            border-top-right-radius:6px;border-bottom-right-radius:6px;
        }}
        QComboBox::down-arrow{{image:url({down_icon});}}
        QComboBox QAbstractItemView{{background:{c['bg_card']};color:{c['text_primary']};
        border:1px solid {c['border']};selection-background-color:{c['accent_glow']};
        selection-color:{c['accent']};outline:none;}}
    """)
    return cb

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
