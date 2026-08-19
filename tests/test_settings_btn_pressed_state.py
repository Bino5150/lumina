"""
Patch 3A.3A Part A / R1 — proves _btn()'s QPushButton:pressed rule is real,
visible feedback, not just text present in a stylesheet string.

Real offscreen QApplication, real QPushButton from the shared _btn()
factory, real QTest.mousePress/mouseRelease -- grabs the widget's actual
rendered pixels in the idle, pressed (mouse held down), and released states
and asserts pressed differs from idle, and release restores the original
render exactly. Covers all three _btn() variants (normal, accent, danger).

Run headless from repo root:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_settings_btn_pressed_state.py -v
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.main_window import COLORS
from ui.settings._widgets import _btn


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module", autouse=True)
def _drain_preexisting_stray_qtimers(qapp):
    # Not related to this patch: tests/test_emergency_stop_ui.py's
    # _window_fake() gives LuminaWindow a chat_widget stub with no `.input`
    # attribute, then drives a latched-emergency-stop path that schedules
    # QTimer.singleShot(0, lambda: self.chat_widget.input.setPlainText(...))
    # (ui/main_window.py, out of this patch's scope) as a real side effect
    # and never pumps the event loop afterward. It sat harmlessly pending
    # because nothing earlier in the suite ever called
    # processEvents()/qWaitForWindowExposed() -- this file is the first to
    # need a genuine offscreen render, so it's the one that would otherwise
    # inherit that stray timer's AttributeError on its first pump. Absorb it
    # here once, before this file's own assertions run, so a pre-existing,
    # out-of-scope test-isolation gap elsewhere can't fail this file.
    try:
        QApplication.processEvents()
    except Exception:
        pass
    yield


def _render_bytes(widget):
    # grab() performs a synchronous paint of current widget state (isDown(),
    # etc.) -- it doesn't depend on the event loop having processed a
    # deferred paint event, so this reflects exactly what's on screen right
    # now, not a stale or pending frame.
    image = widget.grab().toImage()
    return bytes(image.constBits())


_VARIANTS = [
    pytest.param({}, id="normal"),
    pytest.param({"accent": True}, id="accent"),
    pytest.param({"danger": True}, id="danger"),
]


@pytest.mark.parametrize("kwargs", _VARIANTS)
def test_btn_pressed_render_differs_from_idle_and_release_restores_it(qapp, kwargs):
    btn = _btn("Test", COLORS, **kwargs)
    try:
        btn.resize(140, 36)
        btn.show()
        QTest.qWaitForWindowExposed(btn)

        idle_bytes = _render_bytes(btn)

        center = btn.rect().center()
        QTest.mousePress(btn, Qt.LeftButton, pos=center)
        assert btn.isDown(), "QTest mouse press did not register as a real Qt press"
        pressed_bytes = _render_bytes(btn)

        QTest.mouseRelease(btn, Qt.LeftButton, pos=center)
        assert not btn.isDown()
        released_bytes = _render_bytes(btn)

        assert pressed_bytes != idle_bytes, (
            f"{kwargs}: pressed render is pixel-identical to idle render -- "
            "no visible press feedback"
        )
        assert released_bytes == idle_bytes, (
            f"{kwargs}: released render does not match idle render -- "
            "release did not restore expected appearance"
        )
    finally:
        btn.hide()
        btn.deleteLater()


@pytest.mark.parametrize("kwargs", _VARIANTS)
def test_btn_pressed_stylesheet_has_no_geometry_changing_rule(kwargs):
    # Belt-and-suspenders: the pressed block must not introduce a
    # padding/border-width/font-size override that would shift layout size
    # relative to the released/hover rules (checked directly against the
    # released rule's declared values, not merely "did we type the word").
    btn = _btn("Test", COLORS, **kwargs)
    css = btn.styleSheet()
    assert "QPushButton:pressed" in css

    import re

    def _block(selector):
        m = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
        assert m, f"no {selector} block found"
        return m.group(1)

    base = _block("QPushButton")
    pressed = _block("QPushButton:pressed")

    def _prop(block, name):
        m = re.search(name + r":([^;]+);", block)
        return m.group(1).strip() if m else None

    for prop in ("padding", "border-radius", "font-size"):
        base_val, pressed_val = _prop(base, prop), _prop(pressed, prop)
        if pressed_val is not None:
            assert pressed_val == base_val, (
                f"{kwargs}: pressed overrides {prop} ({pressed_val!r} vs "
                f"released {base_val!r}) -- geometry-jumping hack"
            )
    btn.deleteLater()
