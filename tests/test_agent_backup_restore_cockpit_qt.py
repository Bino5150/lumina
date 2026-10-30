"""AGENT-BACKUP-RESTORE-UI-01: the real Settings cockpit, real workers, real engine.

Disposable archives, destinations and install folders only; the file dialogs
are replaced with fixed paths. Missing Qt fails (never skips) when
LUMINA_REQUIRE_QT=1, as in CI's qt-security-guards job.
"""
import json
import os
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton)

import core.agent_backup_restore as ar
import core.agent_backup_restore_cockpit as cp
import ui.settings.restore_cockpit as rc
from test_agent_backup_restore_02 import (  # noqa: F401  (fixtures)
    entries, leftovers, repack, source, target, v1, v2, _killed_restore)

GATE_SECONDS = 30


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dlg(app, source, monkeypatch):
    # `source` first: its fixture calls monkeypatch.undo(), which would wipe the patch below.
    from ui.main_window import COLORS
    def confirm(self, title, text):
        self.confirmations.append((title, text))
        return self.answer
    # Class-level: the real _confirm is a modal QMessageBox.exec() that would hang a test.
    monkeypatch.setattr(rc.RestoreCockpitDialog, "_confirm", confirm)
    d = rc.RestoreCockpitDialog(SimpleNamespace(owner=True), COLORS)
    d.confirmations = []
    d.answer = True
    d.show()
    yield d
    rc._wait_for_workers()
    for _ in range(50):
        app.processEvents()
    d._shutdown()
    d.close()
    d.deleteLater()


def settle(app, dlg=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if (dlg is None or not dlg.busy) and not rc.operation_running():
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("workers did not settle")


def choose_archive(app, dlg, path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    dlg.choose_archive_btn.click()
    settle(app, dlg)


def set_folders(dlg, data_dir, base):
    dlg.data_dir_edit.setText(str(data_dir))
    dlg.install_dir_edit.setText(str(base))


def combo(dlg, unit):
    widget = dlg.findChild(QComboBox, "choice_" + unit)
    assert widget is not None, unit
    return widget


def choose(dlg, unit, value):
    box = combo(dlg, unit)
    box.setCurrentIndex(box.findData(value))


def preview(app, dlg):
    dlg.preview_btn.click()
    settle(app, dlg)


def ready(app, dlg, archive, target, monkeypatch):
    choose_archive(app, dlg, archive, monkeypatch)
    set_folders(dlg, *target)
    preview(app, dlg)
    assert dlg.session.plan is not None and dlg.session.plan["status"] == "ready"


def recover(app, dlg):
    dlg.approve_check.setChecked(True)
    assert dlg.recover_btn.isEnabled()
    dlg.recover_btn.click()
    settle(app, dlg)


def gate(monkeypatch, module, name):
    """Replace module.name with a function that blocks until released."""
    release, entered, real = threading.Event(), threading.Event(), getattr(module, name)
    seen = {}

    def blocked(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        entered.set()
        assert release.wait(GATE_SECONDS)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, name, blocked)
    return release, entered, seen


def test_the_real_confirmation_defaults_to_cancel_and_shows_plain_text(app, monkeypatch):
    from ui.main_window import COLORS
    d = rc.RestoreCockpitDialog(SimpleNamespace(owner=True), COLORS)
    seen = {}

    def fake_exec(box):                      # nobody clicks: the dialog is dismissed
        seen.update(default=box.defaultButton().text(), escape=box.escapeButton().text(),
                    fmt=box.textFormat(), text=box.text())
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert d._confirm("t", "<b>destination</b>") is False
    assert seen["default"] == "Cancel" and seen["escape"] == "Cancel" and seen["fmt"] == Qt.PlainText
    assert "<b>destination</b>" in seen["text"]
    d._shutdown()
    d.close()


# --------------------------------------------------------------------------
# Entry point and labels
# --------------------------------------------------------------------------

@pytest.fixture
def memory_tab(app, monkeypatch):
    from ui.main_window import COLORS
    from ui.settings.memory_tab import MemoryTab
    monkeypatch.setattr(MemoryTab, "_load", lambda self: None)
    tab = MemoryTab(SimpleNamespace(owner=True), COLORS)
    yield tab
    tab.close()


def test_memory_tab_offers_a_clearly_labelled_isolated_recovery_and_nothing_stronger(memory_tab):
    button = memory_tab.findChild(QPushButton, "isolated_archive_recovery")
    assert button is not None and "Isolated" in button.text()
    tip = button.toolTip()
    assert "NEW folder" in tip and "Does not change the running Lumina" in tip and "Verified Agent Backup" in tip
    texts = " | ".join(b.text() for b in memory_tab.findChildren(QPushButton))
    for banned in ("Restore Current", "Restore Lumina", "Activate", "Replace", "Switch to"):
        assert banned not in texts
    assert "Backup Memory (Legacy)" in texts and "Verified Agent Backup" in texts


def test_only_the_owner_can_open_the_cockpit(memory_tab, monkeypatch):
    opened, messages = [], []
    monkeypatch.setattr(rc, "open_cockpit", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: messages.append((title, text)))
    memory_tab.agent.owner = False
    memory_tab.isolated_recovery_btn.click()
    assert not opened and messages and "owner" in messages[0][1].lower()
    memory_tab.agent.owner = True
    memory_tab.isolated_recovery_btn.click()
    assert len(opened) == 1


def test_a_second_cockpit_is_refused_while_a_recovery_step_runs(app, memory_tab, monkeypatch):
    warnings, created = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append((title, text)))
    monkeypatch.setattr(rc.RestoreCockpitDialog, "exec", lambda self: created.append(self) or 0)
    monkeypatch.setattr(rc, "mutating_operation_running", lambda: True)
    assert rc.open_cockpit(memory_tab.agent, memory_tab.c, memory_tab) is None
    assert warnings and not created
    monkeypatch.setattr(rc, "mutating_operation_running", lambda: False)
    shown = rc.open_cockpit(memory_tab.agent, memory_tab.c, memory_tab)
    assert shown is not None and len(created) == 1
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # the closed dialog is not kept as a child
    assert not [c for c in memory_tab.children() if isinstance(c, rc.RestoreCockpitDialog)]


def test_initial_state_is_inert_and_truthful(dlg):
    assert not dlg.preview_btn.isEnabled() and not dlg.recover_btn.isEnabled()
    assert not dlg.approve_check.isEnabled() and not dlg.reinspect_btn.isEnabled()
    assert dlg.banner.text() == cp.BANNER and "NEW folder" in cp.BANNER
    assert "not production disaster recovery" in cp.BANNER and "cannot produce a Verified Agent Backup" in cp.BANNER
    everything = " | ".join([b.text() for b in dlg.findChildren(QPushButton)]
                            + [c.text() for c in dlg.findChildren(QCheckBox)]
                            + [l.text() for l in dlg.findChildren(QLabel)])
    for banned in ("Restore Current", "Restore Lumina", "Activate", "Replace the", "Switch to", "Overwrite"):
        assert banned not in everything, banned
    for label in dlg.findChildren(QLabel):
        if label.text():
            assert label.textFormat() == Qt.PlainText, label.text()


def test_invisible_and_reordering_characters_in_an_archive_name_never_reach_the_views(app, dlg, v2, monkeypatch):
    odd = v2.with_name("back\u061cup\u3164\U000e0041\u202e.zip")
    shutil.copyfile(v2, odd)
    choose_archive(app, dlg, odd, monkeypatch)
    assert dlg.session.intake is not None, dlg.status_label.text()
    shown = dlg.archive_view.toPlainText()
    assert "back?up???.zip" in shown
    for ch in shown:
        assert ch == "\n" or (unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")
                              and ch not in "\u3164\u115f\u1160\uffa0"), ascii(ch)


# --------------------------------------------------------------------------
# The whole journey through the widgets
# --------------------------------------------------------------------------

def test_inspect_preview_approve_recover_verify_end_to_end(app, dlg, v2, target, monkeypatch):
    import hashlib
    data_dir, base = target
    choose_archive(app, dlg, v2, monkeypatch)
    text = dlg.archive_view.toPlainText()
    assert hashlib.sha256(v2.read_bytes()).hexdigest() in text and "Incomplete State Export recovery" in text
    assert "Verified Agent Backup is unavailable" in text
    rows = {dlg.units_table.item(r, 0).text() for r in range(dlg.units_table.rowCount())}
    assert {"primary_database", "preferences", "skills", "personas"} <= rows
    assert combo(dlg, "primary_database").currentData() == "recover"
    assert not dlg.preview_btn.isEnabled() and "data folder" in dlg.preview_btn.toolTip()
    set_folders(dlg, data_dir, base)
    assert dlg.preview_btn.isEnabled() and not data_dir.exists()
    preview(app, dlg)
    plan = dlg.session.plan
    assert plan["plan_digest"] in dlg.plan_view.toPlainText() and "nothing has been written" in dlg.plan_view.toPlainText()
    assert not data_dir.exists() and not leftovers(data_dir)
    assert not dlg.recover_btn.isEnabled() and dlg.approve_check.isEnabled()
    assert plan["plan_digest"][:16] in dlg.approve_check.text()
    dlg.approve_check.setChecked(True)
    assert dlg.recover_btn.isEnabled() and dlg.session.approved_digest == plan["plan_digest"]
    dlg.recover_btn.click()
    assert dlg.busy and not dlg.recover_btn.isEnabled() and not dlg.close_btn.isEnabled()
    settle(app, dlg)
    out = dlg.result_view.toPlainText()
    assert "ISOLATED RECOVERY PUBLISHED AND VERIFIED" in out and "[FAIL]" not in out and "[PASS]" in out
    assert "has not been activated" in out and "Verified Agent Backup: not available" in out
    assert (data_dir / "memory/lumina.db").is_file()
    receipt = data_dir / "restore_history" / plan["restore_id"] / "receipt.json"
    assert receipt.is_file() and str(receipt) in out
    # The approval was consumed: the same plan cannot run again.
    assert not dlg.approve_check.isChecked() and not dlg.recover_btn.isEnabled() and dlg.session.plan is None
    assert dlg.close_btn.isEnabled() and dlg.copy_receipt_btn.isEnabled()


def test_an_enter_key_in_a_path_field_never_activates_a_button(app, dlg, v2, target, monkeypatch):
    from PySide6.QtTest import QTest
    ready(app, dlg, v2, target, monkeypatch)
    dlg.approve_check.setChecked(True)
    assert dlg.recover_btn.isEnabled()
    for field in (dlg.data_dir_edit, dlg.install_dir_edit):
        field.setFocus()
        QTest.keyClick(field, Qt.Key_Return)
        QTest.keyClick(field, Qt.Key_Enter)
    app.processEvents()
    assert not dlg.busy and not rc.operation_running() and not target[0].exists()
    assert all(not b.autoDefault() and not b.isDefault() for b in dlg.findChildren(QPushButton))
    assert dlg.session.can_execute          # untouched: only an explicit click starts a recovery


def test_typed_folders_get_tilde_expanded_and_trimmed_in_the_preview(app, dlg, v2, target, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install = tmp_path / "fresh"
    install.rename(tmp_path / "fresh_unused") if install.exists() else None
    target[1].rename(tmp_path / "fresh")
    (tmp_path / "newdata_parent").mkdir()
    choose_archive(app, dlg, v2, monkeypatch)
    dlg.data_dir_edit.setText("  ~/newdata_parent/recovered  ")
    dlg.install_dir_edit.setText("~/fresh\n")
    preview(app, dlg)
    plan = dlg.session.plan
    assert plan is not None
    assert plan["destination"]["data_dir"] == str(tmp_path / "newdata_parent/recovered")
    assert plan["destination"]["install_dir"] == str(tmp_path / "fresh")
    assert str(tmp_path / "newdata_parent/recovered") in dlg.plan_view.toPlainText()


def test_cancelled_file_dialogs_change_nothing(app, dlg, v2, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    for button in (dlg.choose_archive_btn, dlg.choose_parent_btn, dlg.choose_install_btn, dlg.verify_folder_btn):
        button.click()
    settle(app, dlg)
    assert dlg.session.archive_path is None and not dlg.data_dir_edit.text() and not dlg.install_dir_edit.text()
    assert not rc.operation_running()


def test_choosing_a_parent_proposes_a_new_folder_name_and_never_creates_it(app, dlg, tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    dlg.choose_parent_btn.click()
    proposed = Path(dlg.data_dir_edit.text())
    assert proposed.parent == tmp_path and proposed.name.startswith("lumina-recovered-") and not proposed.exists()


@pytest.mark.parametrize("change", ["choice", "data_dir", "install_dir", "archive"])
def test_any_change_after_preview_invalidates_the_plan_and_the_approval(change, app, dlg, v2, v1, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    dlg.approve_check.setChecked(True)
    assert dlg.recover_btn.isEnabled()
    if change == "choice":
        choose(dlg, "skills", "skip")
    elif change == "data_dir":
        dlg.data_dir_edit.setText(dlg.data_dir_edit.text() + "2")
    elif change == "install_dir":
        dlg.install_dir_edit.setText(dlg.install_dir_edit.text() + "2")
    else:
        choose_archive(app, dlg, v1, monkeypatch)
    assert dlg.session.plan is None and dlg.session.approved_digest is None
    assert not dlg.approve_check.isChecked() and not dlg.approve_check.isEnabled() and not dlg.recover_btn.isEnabled()
    assert dlg.plan_view.toPlainText() == "" and "Preview" in dlg.preview_status.text() or change == "archive"
    assert not target[0].exists()


def test_a_noop_edit_keeps_the_plan(app, dlg, v2, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    dlg.approve_check.setChecked(True)
    dlg.data_dir_edit.setText(dlg.data_dir_edit.text())
    choose(dlg, "skills", "recover")
    assert dlg.session.can_execute and dlg.plan_view.toPlainText()


def test_repeated_clicks_run_each_step_once(app, dlg, v2, target, monkeypatch):
    counts = {"inspect": 0, "plan": 0, "restore": 0}
    for module, name, key in ((cp.ri, "intake_archive", "inspect"), (ar, "restore_isolated", "restore")):
        real = getattr(module, name)

        def counted(*a, _real=real, _key=key, **k):
            counts[_key] += 1
            return _real(*a, **k)
        monkeypatch.setattr(module, name, counted)
    real_build = cp.build_plan
    monkeypatch.setattr(cp, "build_plan", lambda *a: counts.__setitem__("plan", counts["plan"] + 1) or real_build(*a))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(v2), ""))
    dlg.choose_archive_btn.click()
    dlg.choose_archive_btn.click()      # while busy: ignored
    dlg.reinspect_btn.click()
    settle(app, dlg)
    assert counts["inspect"] == 1
    set_folders(dlg, *target)
    dlg.preview_btn.click()
    dlg.preview_btn.click()
    settle(app, dlg)
    assert counts["plan"] == 1
    dlg.approve_check.setChecked(True)
    dlg.recover_btn.click()
    dlg.recover_btn.click()
    dlg._start_execute()                # even the slot itself refuses a second run
    settle(app, dlg)
    assert counts["restore"] == 1 and (target[0] / "memory/lumina.db").is_file()


def test_archive_changed_between_preview_and_execute_forces_a_new_inspection(app, dlg, v2, target, monkeypatch):
    data_dir, base = target
    ready(app, dlg, v2, target, monkeypatch)
    repack(v2, [(n, d + b" " if n == "overlay/personas/lumina.json" else d) for n, d in entries(v2)])
    recover(app, dlg)
    out = dlg.result_view.toPlainText()
    assert "plan stale" in out.lower() and "Nothing was written" in out and "SHA-256" in out
    assert not data_dir.exists() and not leftovers(data_dir)
    assert dlg.session.plan is None and not dlg.recover_btn.isEnabled() and not dlg.approve_check.isChecked()
    assert dlg.plan_view.toPlainText() == "" and "Preview again" in dlg.preview_status.text()
    # The inspection is forgotten: the table described an archive that no longer exists.
    assert dlg.session.intake is None and dlg.units_table.rowCount() == 0 and not dlg.preview_btn.isEnabled()
    assert "THIS ARCHIVE CHANGED" in dlg.archive_view.toPlainText() and dlg.reinspect_btn.isEnabled()
    # No silent retry and no adjusted selection: after inspecting again the damaged archive starts on Skip.
    dlg.reinspect_btn.click()
    settle(app, dlg)
    assert "Selective Recovery" in dlg.archive_view.toPlainText()
    assert all(combo(dlg, u).currentData() == "skip" for u in ("primary_database", "skills"))
    assert not dlg.preview_btn.isEnabled() and not data_dir.exists()
    choose(dlg, "primary_database", "recover")
    preview(app, dlg)
    assert dlg.session.plan is not None and dlg.session.plan["restore_class"] == ar.SELECTIVE


@pytest.mark.parametrize("how", ["different_units", "same_units_new_bytes"])
def test_a_preview_for_a_swapped_archive_is_dropped_and_forces_reinspection(how, app, dlg, v2, v1, source, target,
                                                                           tmp_path, monkeypatch):
    import contextlib
    import shutil
    import sqlite3
    from test_agent_backup_restore_02 import _build_v2
    choose_archive(app, dlg, v2, monkeypatch)
    set_folders(dlg, *target)
    if how == "different_units":
        shutil.copy(v1, v2)
    else:
        with contextlib.closing(sqlite3.connect(source[0] / "memory/lumina.db")) as conn, conn:
            conn.execute("INSERT INTO memories (label, content, created_at) VALUES ('later', 'added after', 't')")
        shutil.copy(_build_v2(source, tmp_path / "archives" / "v2b.zip"), v2)
    preview(app, dlg)
    assert dlg.session.plan is None and dlg.session.intake is None and dlg.units_table.rowCount() == 0
    shown = dlg.plan_view.toPlainText()
    assert ("plan stale" in shown.lower() and "SHA-256" in shown) or "did not match" in shown
    assert not dlg.preview_btn.isEnabled() and not dlg.recover_btn.isEnabled() and not target[0].exists()
    assert "THIS ARCHIVE CHANGED" in dlg.archive_view.toPlainText() or "Inspect the archive again" in dlg.status_label.text()


def test_selective_archive_requires_a_deliberate_choice(app, dlg, v2, target, monkeypatch):
    repack(v2, [(n, b'{"name": "TAMPERED"}' if n == "overlay/personas/lumina.json" else d) for n, d in entries(v2)])
    choose_archive(app, dlg, v2, monkeypatch)
    text = dlg.archive_view.toPlainText()
    assert "Selective Recovery" in text and "FAILED" in text and "NOT verified" in text
    table = dlg.units_table
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "personas")
    assert "SHA-256" in table.item(row, 2).text() and table.item(row, 3).text() == "Not recoverable"
    assert dlg.findChild(QComboBox, "choice_personas") is None
    assert all(combo(dlg, u).currentData() == "skip" for u in ("primary_database", "skills"))
    set_folders(dlg, *target)
    assert not dlg.preview_btn.isEnabled() and "Nothing is selected" in dlg.preview_btn.toolTip()
    choose(dlg, "primary_database", "recover")
    choose(dlg, "skills", "recover")
    preview(app, dlg)
    plan = dlg.session.plan
    assert plan["restore_class"] == ar.SELECTIVE and "Selective Recovery" in dlg.plan_view.toPlainText()
    recover(app, dlg)
    assert "ISOLATED RECOVERY PUBLISHED AND VERIFIED" in dlg.result_view.toPlainText()
    assert (target[0] / "memory/lumina.db").is_file() and not (target[1] / "personas/lumina.json").exists()


@pytest.mark.parametrize("bad", ["production_home", "not_empty", "missing_parent", "missing_install"])
def test_unsafe_destinations_are_explained_and_cannot_proceed(bad, app, dlg, v2, target, tmp_path, monkeypatch):
    data_dir, base = target
    code = {"production_home": "PRODUCTION_ROOT", "not_empty": "DESTINATION_NOT_EMPTY",
            "missing_parent": "DESTINATION_PARENT_UNSAFE", "missing_install": "INSTALL_FOLDER_UNSAFE"}[bad]
    if bad == "production_home":
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home/.local/share/lumina").mkdir(parents=True)
        data_dir = tmp_path / "home/.local/share/lumina/restored"
    elif bad == "not_empty":
        data_dir.mkdir()
        (data_dir / "keep").write_text("x")
    elif bad == "missing_parent":
        data_dir = tmp_path / "nowhere" / "data"
    else:
        base = tmp_path / "no_install"
    choose_archive(app, dlg, v2, monkeypatch)
    set_folders(dlg, data_dir, base)
    preview(app, dlg)
    assert dlg.session.plan is None and not dlg.recover_btn.isEnabled() and not dlg.approve_check.isEnabled()
    assert "unsafe destination" in dlg.plan_view.toPlainText().lower() and "Nothing was written" in dlg.status_label.text()
    assert code in cp.REFUSAL_GUIDE
    if bad == "not_empty":
        assert (data_dir / "keep").read_text() == "x"


def test_install_conflicts_need_a_decision_then_keep_the_owners_file(app, dlg, v2, target, monkeypatch):
    data_dir, base = target
    (base / "personas/lumina.json").write_text('{"name": "Destination"}')
    choose_archive(app, dlg, v2, monkeypatch)
    set_folders(dlg, *target)
    preview(app, dlg)
    assert dlg.session.plan["status"] == "needs_owner_conflict_choice"
    assert "NEEDS YOUR DECISION" in dlg.plan_view.toPlainText() and "personas/lumina.json" in dlg.plan_view.toPlainText()
    assert not dlg.approve_check.isEnabled() and not dlg.recover_btn.isEnabled()
    choose(dlg, "personas", "recover_keep_destination")
    assert dlg.session.plan is None            # the choice invalidated the old plan
    preview(app, dlg)
    assert dlg.session.plan["status"] == "ready"
    recover(app, dlg)
    assert (base / "personas/lumina.json").read_text() == '{"name": "Destination"}'
    assert "Install files kept as they were" in dlg.result_view.toPlainText()


# --------------------------------------------------------------------------
# Interrupted recovery
# --------------------------------------------------------------------------

def test_interrupted_recovery_is_named_confirmed_and_rolled_back(app, dlg, v2, target, tmp_path, monkeypatch):
    data_dir, base = target
    _killed_restore(v2, data_dir, base, "install_file_created", tmp_path)
    choose_archive(app, dlg, v2, monkeypatch)
    set_folders(dlg, *target)
    preview(app, dlg)
    assert "interrupted restore pending" in dlg.plan_view.toPlainText().lower()
    assert "Check for interrupted recovery" in dlg.plan_view.toPlainText() and leftovers(data_dir)
    dlg.answer = False
    dlg.interrupted_btn.click()
    settle(app, dlg)
    assert dlg.confirmations and "journal proves it created" in dlg.confirmations[-1][1] and leftovers(data_dir)
    dlg.answer = True
    dlg.interrupted_btn.click()
    settle(app, dlg)
    assert "Rolled back an interrupted recovery" in dlg.result_view.toPlainText() and not leftovers(data_dir)
    assert not dlg.quarantine_btn.isVisible()
    preview(app, dlg)
    assert dlg.session.plan is not None


def test_unrecognised_staging_needs_a_second_explicit_action(app, dlg, v2, target, monkeypatch):
    data_dir, base = target
    rogue = data_dir.parent / f".{data_dir.name}.restore-{'e' * 32}.staging"
    rogue.mkdir(mode=0o770)
    (rogue / "keep.txt").write_text("not ours")
    rogue.chmod(0o770)
    set_folders(dlg, *target)
    assert not dlg.quarantine_btn.isVisible()
    dlg.interrupted_btn.click()
    settle(app, dlg)
    assert "LEFT ALONE" in dlg.result_view.toPlainText() and rogue.exists() and dlg.quarantine_btn.isVisible()
    dlg.answer = False
    dlg.quarantine_btn.click()
    settle(app, dlg)
    assert rogue.exists()
    dlg.answer = True
    dlg.quarantine_btn.click()
    settle(app, dlg)
    assert not rogue.exists() and "Moved aside, never deleted" in dlg.result_view.toPlainText()
    (moved,) = leftovers(data_dir)
    assert (data_dir.parent / moved / "keep.txt").read_text() == "not ours"


def test_interrupted_check_refuses_production_and_needs_the_owner(app, dlg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home/.local/share/lumina-release").mkdir(parents=True)
    dlg.data_dir_edit.setText(str(tmp_path / "home/.local/share/lumina-release"))
    dlg.interrupted_btn.click()
    settle(app, dlg)
    assert "unsafe destination" in dlg.result_view.toPlainText().lower()
    dlg.agent.owner = False
    before = len(dlg.confirmations)
    dlg.interrupted_btn.click()
    settle(app, dlg)
    assert len(dlg.confirmations) == before and "owner" in dlg.status_label.text().lower()


# --------------------------------------------------------------------------
# Threads, dialog lifecycle, close behaviour
# --------------------------------------------------------------------------

def test_engine_work_runs_off_the_gui_thread(app, dlg, v2, target, monkeypatch):
    threads = {}
    for name in ("inspect_archive", "build_plan", "execute_recovery"):
        real = getattr(cp, name)

        def spy(*a, _real=real, _name=name):
            threads[_name] = threading.current_thread()
            return _real(*a)
        monkeypatch.setattr(cp, name, spy)
    ready(app, dlg, v2, target, monkeypatch)
    recover(app, dlg)
    assert set(threads) == {"inspect_archive", "build_plan", "execute_recovery"}
    assert all(t is not threading.main_thread() for t in threads.values())


def test_the_dialog_cannot_close_while_a_recovery_runs_and_the_app_tracks_the_worker(app, dlg, v2, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    release, entered, _seen = gate(monkeypatch, cp, "execute_recovery")
    dlg.approve_check.setChecked(True)
    dlg.recover_btn.click()
    assert entered.wait(GATE_SECONDS)
    assert rc.mutating_operation_running() and len(rc._WORKERS) == 1
    assert not dlg.close_btn.isEnabled() and "cannot be cancelled" in dlg.close_btn.toolTip()
    dlg.close_btn.click()
    assert not dlg.close() and dlg.isVisible()
    dlg.reject()
    assert dlg.isVisible() and "cannot be cancelled" in dlg.status_label.text()
    assert not dlg.preview_btn.isEnabled() and not dlg.choose_archive_btn.isEnabled()
    dlg.recover_btn.click()                                   # a second click starts nothing
    assert len(rc._WORKERS) == 1
    release.set()
    settle(app, dlg)
    assert dlg.close_btn.isEnabled() and not rc.mutating_operation_running()
    assert "ISOLATED RECOVERY PUBLISHED AND VERIFIED" in dlg.result_view.toPlainText()
    assert dlg.close() and not dlg.isVisible()


def test_close_stays_refused_until_this_dialog_has_taken_its_workers_result(app, dlg):
    # The application registry can drop a worker on the worker's own thread before the GUI
    # thread has processed that worker's result; the dialog must still refuse to close then.
    assert not rc.mutating_operation_running()
    dlg._worker = SimpleNamespace(mutating=True)
    app.processEvents()
    assert dlg.close_blocked and not dlg.close() and dlg.isVisible()
    dlg.reject()
    assert dlg.isVisible() and "cannot be cancelled" in dlg.status_label.text()
    dlg._refresh()
    assert not dlg.close_btn.isEnabled()
    dlg._worker = SimpleNamespace(mutating=False)             # a read-only step may be left behind
    assert not dlg.close_blocked
    dlg._worker = None


def test_application_quit_waits_for_a_running_recovery(app, dlg, v2, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    release, entered, _seen = gate(monkeypatch, cp, "execute_recovery")
    dlg.approve_check.setChecked(True)
    dlg.recover_btn.click()
    assert entered.wait(GATE_SECONDS) and app.property(rc._SHUTDOWN_PROPERTY) is True
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    rc._wait_for_workers()                                   # what aboutToQuit runs
    assert time.monotonic() - started >= 0.2
    settle(app, dlg)
    assert (target[0] / "memory/lumina.db").is_file()


def test_closing_during_a_read_only_step_is_safe_and_its_result_is_never_shown(app, dlg, v2, monkeypatch):
    release, entered, _seen = gate(monkeypatch, cp, "inspect_archive")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(v2), ""))
    dlg.choose_archive_btn.click()
    assert entered.wait(GATE_SECONDS) and not rc.mutating_operation_running() and rc.operation_running()
    assert dlg.close() and not dlg.isVisible()               # read-only: closing is allowed
    assert len(rc._WORKERS) == 1                             # still tracked by the application
    release.set()
    settle(app)
    assert not rc._WORKERS and dlg.archive_view.toPlainText() == "" and dlg.session.intake is None


def test_a_worker_that_dies_is_reported_without_its_text(app, dlg, v2, monkeypatch):
    monkeypatch.setattr(cp, "inspect_archive", lambda p: (_ for _ in ()).throw(SystemExit("<b>SECRET-FROM-ARCHIVE</b>")))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(v2), ""))
    dlg.choose_archive_btn.click()
    settle(app, dlg)
    text = dlg.archive_view.toPlainText()
    assert "unexpected" in text.lower() and "SECRET-FROM-ARCHIVE" not in text + dlg.status_label.text()
    assert not rc.operation_running() and dlg.choose_archive_btn.isEnabled()


def test_archive_derived_markup_is_shown_as_plain_text(app, dlg, v2, monkeypatch):
    real = cp.inspect_archive("/nonexistent")
    assert not real["ok"]
    good = cp.inspect_archive(str(v2))
    good["view"]["warnings"] = ["<b>BOLD</b><img src='file:///etc/passwd'>"]
    good["view"]["units"][0]["label"] = "<i>unit</i>"
    monkeypatch.setattr(cp, "inspect_archive", lambda p: good)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(v2), ""))
    dlg.choose_archive_btn.click()
    settle(app, dlg)
    assert "<b>BOLD</b><img src='file:///etc/passwd'>" in dlg.archive_view.toPlainText()
    assert dlg.units_table.item(0, 0).text() == "<i>unit</i>"
    assert not dlg.archive_view.document().isEmpty() and "<" in dlg.archive_view.toPlainText()


def test_a_non_owner_cannot_run_a_recovery_even_with_an_approved_plan(app, dlg, v2, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    dlg.approve_check.setChecked(True)
    dlg.agent.owner = False
    dlg.recover_btn.click()
    settle(app, dlg)
    assert not target[0].exists() and "owner" in dlg.status_label.text().lower()
    assert dlg.session.can_execute      # nothing consumed: the owner can still proceed
    dlg.agent.owner = True
    dlg.recover_btn.click()
    settle(app, dlg)
    assert (target[0] / "memory/lumina.db").is_file()


def test_failures_after_staging_are_shown_with_the_engines_rollback(app, dlg, v2, target, monkeypatch):
    import errno
    ready(app, dlg, v2, target, monkeypatch)

    def full(stage):
        if stage == "data_staged":
            raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ar, "_checkpoint", full)
    recover(app, dlg)
    out = dlg.result_view.toPlainText()
    assert "restore failed" in out.lower() and "Destination untouched: yes" in out and "Staging folder removed: yes" in out
    assert not target[0].exists() and not leftovers(target[0]) and dlg.session.plan is None


def test_published_but_unverified_is_a_loud_failure_never_a_success(app, dlg, v2, target, monkeypatch):
    ready(app, dlg, v2, target, monkeypatch)
    real = ar.verify_restored_root
    monkeypatch.setattr(ar, "verify_restored_root", lambda d, r: {**real(d, r), "passed": False})
    recover(app, dlg)
    out = dlg.result_view.toPlainText()
    assert "VERIFICATION FAILED" in out.upper() and "do not use this folder" in out.lower()
    assert "ISOLATED RECOVERY PUBLISHED AND VERIFIED" not in out
    assert "PUBLISHED BUT NOT VERIFIED" in dlg.status_label.text()


# --------------------------------------------------------------------------
# Receipt and re-verification
# --------------------------------------------------------------------------

def test_receipt_actions_copy_view_and_save_without_overwriting(app, dlg, v2, target, tmp_path, monkeypatch):
    data_dir, _base = target
    ready(app, dlg, v2, target, monkeypatch)
    recover(app, dlg)
    receipt = Path(dlg._result["receipt_path"])
    dlg.copy_receipt_btn.click()
    assert QApplication.clipboard().text() == str(receipt)
    viewed = []
    monkeypatch.setattr(QDialogProxy := rc.QDialog, "exec", lambda self: viewed.append(self.findChild(QPlainTextEdit, "receipt_text").toPlainText()) or 0)
    dlg.view_receipt_btn.click()
    assert viewed and json.loads(viewed[0])["restore_class"] == ar.INCOMPLETE and viewed[0].isascii()
    copy = tmp_path / "keep" / "receipt-copy.json"
    copy.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(copy), ""))
    dlg.save_receipt_btn.click()
    assert copy.read_bytes() == receipt.read_bytes() and (copy.stat().st_mode & 0o777) == 0o600
    copy.write_text("PRIOR")
    dlg.save_receipt_btn.click()
    assert copy.read_text() == "PRIOR" and "never overwrites" in dlg.status_label.text()
    link = tmp_path / "keep" / "link.json"
    link.symlink_to(tmp_path / "victim.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(link), ""))
    dlg.save_receipt_btn.click()
    assert not (tmp_path / "victim.json").exists()


def test_verify_a_recovered_folder_and_detect_later_damage(app, dlg, v2, target, monkeypatch):
    data_dir, _base = target
    ready(app, dlg, v2, target, monkeypatch)
    recover(app, dlg)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(data_dir))
    dlg.verify_folder_btn.click()
    settle(app, dlg)
    assert "RECOVERED FOLDER RE-VERIFIED" in dlg.result_view.toPlainText() and "cannot prove the receipt" in dlg.result_view.toPlainText()
    (data_dir / "memory/prefs.json").write_text('{"telegram_remote_access": true}')
    dlg.verify_folder_btn.click()
    settle(app, dlg)
    out = dlg.result_view.toPlainText()
    assert "[FAIL] Every recovered data file" in out and "RE-VERIFIED" not in out
    assert "Verification failed" in dlg.status_label.text()


def test_verify_folder_on_a_plain_folder_says_there_is_no_receipt(app, dlg, tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "plain"))
    dlg.verify_folder_btn.click()
    settle(app, dlg)
    assert "No recovery receipt found" in dlg.result_view.toPlainText()
    assert not dlg.copy_receipt_btn.isEnabled()
