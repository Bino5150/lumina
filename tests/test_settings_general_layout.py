"""Focused responsive-layout regression for Settings > General."""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from ui.main_window import COLORS
from ui.settings.general_tab import GeneralTab


def test_general_tab_content_tracks_viewport_when_resized():
    """Long help text must wrap instead of establishing a wider panel."""
    app = QApplication.instance() or QApplication([])
    agent = SimpleNamespace(
        owner=True,
        current_persona=None,
        tts=None,
        registry=None,
        llm=None,
    )
    tab = GeneralTab(agent, COLORS)
    scroll = tab.findChild(QScrollArea)
    content = scroll.widget()
    right_gutter = content.layout().contentsMargins().right()

    tab.show()
    for width in (960, 800, 1100, 900):
        tab.resize(width, 700)
        app.processEvents()

        assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        assert scroll.verticalScrollBar().isVisible()
        assert content.width() == scroll.viewport().width()

        # Preserve the existing three-column row while keeping its last
        # control and the Save button inside the panel's right gutter.
        assert tab.ctx_spin.y() == tab.mem_spin.y() == tab.result_spin.y()
        assert tab.ctx_spin.x() < tab.mem_spin.x() < tab.result_spin.x()
        for widget in (tab.result_spin, tab.save_btn):
            right_edge = widget.mapTo(content, widget.rect().bottomRight()).x() + 1
            assert content.width() - right_edge >= right_gutter

    tab.close()
