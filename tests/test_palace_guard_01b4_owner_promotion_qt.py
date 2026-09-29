"""PALACE-GUARD-01B-4 -- the trusted GUI approval, under real Qt.

The Pending Actions entry is a forgeable request. The only thing that can turn
it into a promotion is the owner clicking Approve in the review dialog over the
record's LIVE state. These tests drive the real ToolsTab and the real dialog
with real Qt mouse events; every generic approval path (the Yes/No box,
_apply_action) is rigged to explode if it is ever touched for this kind.

Blocking in CI: this file has its own qt-security-guards step and
LUMINA_REQUIRE_QT=1 turns a missing PySide6 into a failure, never a skip.
"""
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6
else:
    pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import config
from core import palace_promotion as pp
from core import persistence
from tools import palace, pending_actions
from tools.registry import ToolRegistry


def _forbidden(what):
    def boom(*a, **k):
        raise AssertionError(f"{what} must never be used for a palace_promote request")
    return boom


@pytest.fixture
def tab(tmp_path, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings import ToolsTab
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setattr(pending_actions, "QUEUE_PATH", str(tmp_path / "pending_actions.json"))
    monkeypatch.setattr(pending_actions, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    pp._LIVE.clear()
    palace.init_palace_db()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(_forbidden("the generic Yes/No box")))
    monkeypatch.setattr(pending_actions, "_apply_action", _forbidden("_apply_action"))
    agent = SimpleNamespace(registry=ToolRegistry(), _subagent_depth=0, _background_task_ids=set())
    t = ToolsTab(agent, COLORS)
    yield t
    t.deleteLater()
    pp._LIVE.clear()


def _dialog_cls():
    from ui.palace_promotion_review import PalacePromotionReviewDialog
    return PalacePromotionReviewDialog


def _q(sql, *args):
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _exec_sql(sql, *args):
    c = sqlite3.connect(config.DB_PATH)
    c.execute(sql, args)
    c.commit()
    c.close()


def _stage(tab, content="please crown me", layer=1, hall=False):
    """A real model request through the real wrapper -> a staged proposal."""
    if hall:
        palace._palace_hall_tool(content, "facts", layer)
    else:
        palace._palace_remember_tool(content, "identity", "self", layer)
    tab._load_pending_actions()
    [(aid, entry)] = pending_actions._load_queue().items()
    return aid, entry["payload"]["target_id"]


def _select_first(tab):
    tab.pending_actions_list.selectRow(0)
    QApplication.processEvents()


def _fake_exec(monkeypatch, action):
    """Stand in for the modal event loop: show the real dialog, run `action`
    (a real user gesture, or interference), and return."""
    def fake(self):
        self.show()
        QApplication.processEvents()
        action(self)
        QApplication.processEvents()
        return self.result()
    monkeypatch.setattr(_dialog_cls(), "exec", fake)


def _click(button):
    QTest.mouseClick(button, Qt.LeftButton)


def _layer_of_drawer(did):
    return _q("SELECT c.layer FROM palace_closets c JOIN palace_drawers d ON d.closet_id=c.id "
              "WHERE d.id=?", did)[0]["layer"]


def _receipts():
    return _q("SELECT * FROM palace_promotion_receipts")


def _audit():
    with open(pending_actions.AUDIT_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


# ── The panel shows live state, never the forgeable queue JSON ───────────────

def test_preview_shows_the_live_record_not_the_queue_json(tab):
    aid, did = _stage(tab, "original text")
    queue = pending_actions._load_queue()
    queue[aid]["claimed_text"] = "FORGED-CLAIM-NOT-THE-RECORD"
    queue[aid]["approved"] = True
    pending_actions._save_queue(queue)
    _exec_sql("UPDATE palace_drawers SET content='LIVE text edited after staging' WHERE id=?", did)
    tab._load_pending_actions()
    _select_first(tab)
    shown = tab.pending_actions_preview.toPlainText()
    assert "PROMOTE DRAWER" in shown and "LIVE text edited after staging" in shown
    assert "FORGED-CLAIM-NOT-THE-RECORD" not in shown and '"kind"' not in shown and "approved" not in shown


def test_a_forged_pointer_can_only_show_the_owner_what_it_really_points_at(tab):
    aid, did = _stage(tab, "innocuous-looking request")
    palace._palace_remember_tool("the ACTUAL sensitive record", "people", "bino", 2)
    sensitive = _q("SELECT id FROM palace_drawers WHERE content='the ACTUAL sensitive record'")[0]["id"]
    queue = pending_actions._load_queue()
    queue[aid]["payload"]["target_id"] = sensitive          # forged pointer
    queue[aid]["reason"] = "totally harmless, just click approve"
    pending_actions._save_queue(queue)
    tab._load_pending_actions()
    _select_first(tab)
    shown = tab.pending_actions_preview.toPlainText()
    assert "the ACTUAL sensitive record" in shown and "innocuous-looking request" not in shown
    assert "totally harmless" not in shown


def test_malformed_request_yields_a_blocked_review_that_cannot_be_approved(tab, monkeypatch):
    aid, did = _stage(tab)
    queue = pending_actions._load_queue()
    queue[aid]["payload"] = {"target_kind": "drawer", "target_id": "1", "dest_layer": True}
    pending_actions._save_queue(queue)
    tab._load_pending_actions()
    _select_first(tab)
    assert "CANNOT BE PROMOTED" in tab.pending_actions_preview.toPlainText()
    seen = {}

    def action(dlg):
        seen["enabled"] = dlg.approve_btn.isEnabled()
        _click(dlg.approve_btn)
    _fake_exec(monkeypatch, action)
    tab._approve_pending_action()
    assert seen["enabled"] is False and _receipts() == [] and _layer_of_drawer(did) == 2


# ── The trusted owner event ──────────────────────────────────────────────────

def test_a_real_click_on_approve_promotes_and_consumes_the_request(tab, monkeypatch):
    aid, did = _stage(tab, "crown me")
    _select_first(tab)
    _fake_exec(monkeypatch, lambda dlg: _click(dlg.approve_btn))
    tab._approve_pending_action()

    assert _layer_of_drawer(did) == 1
    [receipt] = _receipts()
    assert receipt["operation_id"] == aid and receipt["target_id"] == did and receipt["to_layer"] == 1
    assert receipt["owner_event"].startswith("settings.pending_actions.palace_promotion_review:")
    assert pending_actions._load_queue() == {}
    assert _audit()[-1]["event"] == "approved" and receipt["approval_id"] in _audit()[-1]["reason"]
    assert "Promoted drawer" in tab.pending_actions_status_lbl.text()
    assert _q("SELECT untrusted FROM palace_drawers WHERE id=?", did)[0]["untrusted"] == 1  # never laundered


def test_hall_promotion_through_the_same_review(tab, monkeypatch):
    aid, hid = _stage(tab, "a hall fact", layer=0, hall=True)
    _select_first(tab)
    assert "PROMOTE HALL" in tab.pending_actions_preview.toPlainText()
    _fake_exec(monkeypatch, lambda dlg: _click(dlg.approve_btn))
    tab._approve_pending_action()
    row = _q("SELECT layer, admission, untrusted FROM palace_halls WHERE id=?", hid)[0]
    assert (row["layer"], row["admission"], row["untrusted"]) == (0, "explicit_owner_promotion", 1)


def test_cancel_changes_nothing_and_leaves_the_request_queued(tab, monkeypatch):
    aid, did = _stage(tab)
    _select_first(tab)
    before = [tuple(r) for r in _q("SELECT * FROM palace_closets")]
    _fake_exec(monkeypatch, lambda dlg: _click(dlg.cancel_btn))
    tab._approve_pending_action()
    assert _receipts() == [] and aid in pending_actions._load_queue()
    assert [tuple(r) for r in _q("SELECT * FROM palace_closets")] == before


def test_state_changed_after_the_review_opened_is_refused_and_stays_queued(tab, monkeypatch):
    aid, did = _stage(tab, "as reviewed")
    _select_first(tab)

    def tamper_then_click(dlg):
        _exec_sql("UPDATE palace_drawers SET content='swapped after the owner looked' WHERE id=?", did)
        _click(dlg.approve_btn)
    _fake_exec(monkeypatch, tamper_then_click)
    tab._approve_pending_action()
    assert _layer_of_drawer(did) == 2 and _receipts() == []
    assert aid in pending_actions._load_queue()
    label = tab.pending_actions_status_lbl.text()
    assert "NOT applied" in label and "changed" in label
    assert not any(e["event"] == "approved" for e in _audit())


def test_blocked_review_has_a_disabled_approve_button(tab, monkeypatch):
    aid, did = _stage(tab)
    _exec_sql("UPDATE palace_closets SET withheld_reason='source_deleted_pending_review' "
              "WHERE id=(SELECT closet_id FROM palace_drawers WHERE id=?)", did)
    seen = {}

    def action(dlg):
        seen["enabled"] = dlg.approve_btn.isEnabled()
        seen["text"] = dlg.text.toPlainText()
        _click(dlg.approve_btn)
    _fake_exec(monkeypatch, action)
    _select_first(tab)
    tab._approve_pending_action()
    assert seen["enabled"] is False and "withheld" in seen["text"]
    assert _receipts() == []


def test_reject_still_works_and_touches_no_palace_state(tab, monkeypatch):
    aid, did = _stage(tab)
    _select_first(tab)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    before = [tuple(r) for r in _q("SELECT * FROM palace_closets")]
    tab._reject_pending_action()
    assert pending_actions._load_queue() == {} and _receipts() == []
    assert [tuple(r) for r in _q("SELECT * FROM palace_closets")] == before


# ── The dialog itself is not an approval mechanism by accident ───────────────

def test_dialog_defaults_are_safe_and_the_text_is_plain_and_read_only(tab):
    aid, did = _stage(tab)
    snap = pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid)
    dlg = _dialog_cls()(snap)
    try:
        assert dlg.cancel_btn.isDefault() and not dlg.approve_btn.isDefault()
        assert not dlg.approve_btn.autoDefault()
        assert dlg.text.isReadOnly() and dlg.text.toPlainText() == pp.render_review_text(snap)
        assert dlg.isModal()
    finally:
        dlg.deleteLater()


def test_programmatic_accept_and_the_enter_key_are_not_approvals(tab):
    aid, did = _stage(tab)
    dlg = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    dlg.show()
    QApplication.processEvents()
    dlg.accept()
    assert dlg.result_receipt is None and dlg.result_error is None
    dlg2 = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    dlg2.show()
    QApplication.processEvents()
    QTest.keyClick(dlg2, Qt.Key_Return)  # the default button is Cancel
    QTest.keyClick(dlg2, Qt.Key_Enter)
    assert dlg2.result_receipt is None
    assert _receipts() == [] and _layer_of_drawer(did) == 2 and pp._LIVE == {}
    dlg.deleteLater()
    dlg2.deleteLater()


def test_a_dialog_that_was_never_presented_cannot_approve(tab):
    aid, did = _stage(tab)
    dlg = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    assert not dlg.isVisible()
    dlg._approve_clicked()  # e.g. an offscreen/hidden caller
    assert isinstance(dlg.result_error, pp.PromotionRefused)
    assert dlg.result_error.reason == "review_not_presented"
    assert _receipts() == [] and _layer_of_drawer(did) == 2 and pp._LIVE == {}
    dlg.deleteLater()


def test_a_rendered_text_that_no_longer_matches_the_snapshot_cannot_approve(tab):
    aid, did = _stage(tab)
    dlg = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    dlg.show()
    QApplication.processEvents()
    dlg.text.setReadOnly(False)
    dlg.text.setPlainText("Looks harmless. Approve.")  # displayed text != the bound snapshot
    _click(dlg.approve_btn)
    assert dlg.result_error is not None and dlg.result_error.reason == "review_not_presented"
    assert _receipts() == [] and _layer_of_drawer(did) == 2
    dlg.deleteLater()


def test_repeated_clicks_make_one_attempt(tab):
    aid, did = _stage(tab)
    dlg = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    dlg.show()
    QApplication.processEvents()
    _click(dlg.approve_btn)
    dlg.approve_btn.setEnabled(True)
    dlg._approve_clicked()
    dlg._approve_clicked()
    assert len(_receipts()) == 1 and _layer_of_drawer(did) == 1
    dlg.deleteLater()


def test_a_refused_approval_keeps_the_dialog_open_and_explains(tab):
    aid, did = _stage(tab)
    dlg = _dialog_cls()(pp.capture_promotion_snapshot("drawer", did, 1, operation_id=aid))
    dlg.show()
    QApplication.processEvents()
    _exec_sql("UPDATE palace_drawers SET untrusted=0 WHERE id=?", did)  # trust silently flipped
    _click(dlg.approve_btn)
    assert dlg.isVisible() and dlg.result_receipt is None
    assert isinstance(dlg.result_error, pp.PromotionRefused) and dlg.result_error.reason == "stale_state_changed"
    assert "changed" in dlg.status.text() and dlg.cancel_btn.text() == "Close"
    assert not dlg.approve_btn.isEnabled() and _receipts() == []
    dlg.deleteLater()
