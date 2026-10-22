"""Synthetic installations only: permission repair and current desktop status."""
import os
import stat

import pytest

from core import operator_ingress as oi, suspension as S, suspension_runtime as R
from test_agent_runaway_suspend_01_c1b_qt import qapp, window, _make_window


def _memory(tmp_path, mode=0o2777):
    folder = tmp_path / "installation" / "memory"
    folder.mkdir(parents=True)
    folder.chmod(mode)
    return folder


def test_removes_only_write_bits_and_preserves_every_file(tmp_path):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    child = folder / "history"
    child.write_bytes(b"keep this history")
    child.chmod(0o664)
    before = child.stat()
    preview = P.inspect(folder)
    assert preview.mode == 0o2777 and preview.proposed_mode == 0o2755
    P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2755
    assert child.stat() == before
    assert child.read_bytes() == b"keep this history"


@pytest.mark.parametrize("replacement", ["directory", "symlink", "parent_symlink", "mode"])
def test_substitution_or_mode_change_is_refused(tmp_path, replacement):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    preview = P.inspect(folder)
    target = tmp_path / "target"
    target.mkdir(mode=0o777)
    target.chmod(0o777)
    if replacement == "parent_symlink":
        folder.parent.rename(tmp_path / "old-parent")
        folder.parent.symlink_to(target, target_is_directory=True)
    elif replacement == "mode":
        folder.chmod(0o775)
    else:
        folder.rename(folder.with_name("old-memory"))
        if replacement == "directory":
            folder.mkdir(mode=0o777)
        else:
            folder.symlink_to(target, target_is_directory=True)
    with pytest.raises((P.PermissionRepairRefused, OSError)):
        P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o777


def test_wrong_owner_and_non_directories_are_refused(tmp_path, monkeypatch):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(P.os, "getuid", lambda: folder.stat().st_uid + 1)
        with pytest.raises(P.PermissionRepairRefused, match="own"):
            P.inspect(folder)
    file = tmp_path / "file"
    file.touch()
    link = tmp_path / "link"
    link.symlink_to(folder, target_is_directory=True)
    for path in (file, link):
        with pytest.raises((P.PermissionRepairRefused, OSError)):
            P.inspect(path)


def test_owner_is_rechecked_after_preview(tmp_path, monkeypatch):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    preview = P.inspect(folder)
    monkeypatch.setattr(P.os, "getuid", lambda: folder.stat().st_uid + 1)
    with pytest.raises(P.PermissionRepairRefused):
        P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2777


def test_post_change_verification_and_permission_failure(tmp_path, monkeypatch):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    preview = P.inspect(folder)
    with monkeypatch.context() as patch:
        patch.setattr(P.os, "fchmod", lambda *a: None)
        with pytest.raises(P.PermissionRepairRefused, match="verif"):
            P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))
    def denied(*args):
        raise PermissionError("synthetic denied")
    monkeypatch.setattr(P.os, "fchmod", denied)
    with pytest.raises(PermissionError):
        P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))


def test_permission_repair_requires_owner_ingress(tmp_path):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    with pytest.raises(S.NotAuthorized):
        P.repair(P.inspect(folder), ingress=None)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2777


def test_dialog_cancel_repair_then_explicit_setup(qapp, tmp_path, monkeypatch):
    from ui.suspension_dialog import SuspensionDialog
    from PySide6.QtWidgets import QMessageBox
    folder = _memory(tmp_path)
    (folder / "history").write_text("kept")
    R.start(folder.parent, role="desktop", heartbeat=False)
    dialog = SuspensionDialog()
    try:
        dialog.first_time_choice.setChecked(True)
        dialog.first_run_confirm.setChecked(True)
        dialog.setup_action_btn.click()
        assert "Other people" in dialog.permission_label.text()
        assert str(folder) in dialog.permission_label.text()
        assert "2777" in dialog.permission_label.text() and "2755" in dialog.permission_label.text()
        assert "insecure_permissions" in dialog.technical_details.text()
        dialog.permissions_not_now_btn.click()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o2777
        from PySide6.QtCore import Qt
        confirmations = []
        def decline(box):
            assert box.textFormat() == Qt.PlainText
            assert box.standardButton(box.defaultButton()) == QMessageBox.No
            assert str(folder) in box.text()
            assert "NOT intentionally shared" in box.text()
            confirmations.append(box.text())
            return QMessageBox.No
        monkeypatch.setattr(QMessageBox, "exec", decline)
        dialog.permissions_fix_btn.click()
        assert len(confirmations) == 1
        assert stat.S_IMODE(folder.stat().st_mode) == 0o2777
        monkeypatch.setattr(QMessageBox, "exec", lambda *a, **k: QMessageBox.Yes)
        dialog.permissions_fix_btn.click()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o2755
        assert (folder / "history").read_text() == "kept"
        assert not (folder / S.STORE_NAME).exists()
        assert not dialog.first_run_confirm.isChecked()
        assert not dialog.setup_action_btn.isEnabled()
        dialog.first_time_choice.setChecked(True)
        dialog.first_run_confirm.setChecked(True)
        dialog.setup_action_btn.click()
        assert R.status().state is R.State.ACTIVE
    finally:
        dialog.close()


def test_startup_status_updates_after_initialization_and_controls_reopen(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    folder = _memory(tmp_path, 0o700)
    R.start(folder.parent, role="desktop", heartbeat=False)
    win = _make_window(tmp_path, monkeypatch)
    try:
        QApplication.processEvents()
        assert "isn't ready" in win.safety_banner.status_label.text()
        assert not any("isn't ready" in text for text in win._notices)
        dialog = win._suspension_dialog
        dialog.first_time_choice.setChecked(True)
        dialog.first_run_confirm.setChecked(True)
        dialog.setup_action_btn.click()
        QApplication.processEvents()
        assert "active" in win.safety_banner.status_label.text()
        assert "isn't ready" not in win.safety_banner.status_label.text()
        dialog.close()
        win.safety_banner.controls_btn.click()
        assert win._suspension_dialog.isVisible()
        assert win.safety_banner.controls_btn.text() == "Open safety controls"
    finally:
        if win._suspension_dialog:
            win._suspension_dialog.close()
        win.close()


def test_banner_tracks_trip_resume_fault_and_local_stops(window):
    from PySide6.QtWidgets import QApplication
    R.trip(S.Trigger.BREAKER_TRIPPED, {"calls": 20})
    QApplication.processEvents()
    assert "1 scope(s)" in window.safety_banner.status_label.text()
    R.resume(R.operator_view()[0], ingress=oi.local_suspension_control("resume"))
    QApplication.processEvents()
    assert "nothing is suspended" in window.safety_banner.status_label.text()
    from ui.safety_banner import SafetyBanner
    banner = SafetyBanner()
    banner.refresh(R.RuntimeStatus(R.State.ACTIVE, "desktop", "/fake", None, None, (), ("owner",), 0))
    assert "unconfirmed" in banner.status_label.text()
    banner.refresh(R.RuntimeStatus(R.State.FAULT, "desktop", "/fake", None, S.Fault.CORRUPT, (), (), 0))
    assert "isn't ready" in banner.status_label.text()


def test_late_swap_never_chmods_the_substitute(tmp_path, monkeypatch):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    preview = P.inspect(folder)
    old = folder.with_name("original")
    real = os.fchmod
    def swap(fd, mode):
        folder.rename(old)
        folder.mkdir(mode=0o777)
        folder.chmod(0o777)
        real(fd, mode)
    monkeypatch.setattr(P.os, "fchmod", swap)
    with pytest.raises(P.PermissionRepairRefused, match="verif"):
        P.repair(preview, ingress=oi.local_suspension_control("folder_permissions"))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o777
    assert stat.S_IMODE(old.stat().st_mode) == 0o2755


def test_runtime_refuses_wrong_role_and_changed_data_folder(tmp_path):
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    preview = P.inspect(folder)
    R.start(folder.parent, role="cli", heartbeat=False)
    with pytest.raises(S.NotAuthorized):
        R.repair_folder_permissions(preview, ingress=oi.local_suspension_control("folder_permissions"))
    other = tmp_path / "other"
    R.start(other, role="desktop", heartbeat=False)
    with pytest.raises(P.PermissionRepairRefused):
        R.repair_folder_permissions(preview, ingress=oi.local_suspension_control("folder_permissions"))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2777


def test_existing_suspension_and_safety_files_survive_repair(qapp, tmp_path, monkeypatch):
    from ui.suspension_dialog import SuspensionDialog
    from PySide6.QtWidgets import QMessageBox
    folder = _memory(tmp_path, 0o700)
    with S.initialize(folder.parent, ingress=oi.local_suspension_control("initialize")) as store:
        store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED, evidence={"calls": 20})
        incident = store.operator_view()[0]
    before = {name: (folder / name).read_bytes() for name in (S.STORE_NAME, S.IDENTITY_NAME)}
    folder.chmod(0o775)
    R.start(folder.parent, role="desktop", heartbeat=False)
    assert R.status().state is R.State.FAULT
    dialog = SuspensionDialog()
    try:
        assert dialog._permission_pending
        monkeypatch.setattr(QMessageBox, "exec", lambda *a, **k: QMessageBox.Yes)
        dialog.permissions_fix_btn.click()
        assert R.status().state is R.State.ACTIVE
        assert R.operator_view()[0].suspension_id == incident.suspension_id
        with pytest.raises(R.SuspensionRefused):
            R.capture(S.OWNER_SCOPE)
        assert before == {name: (folder / name).read_bytes() for name in before}
    finally:
        dialog.close()


def test_directory_refusal_guidance_and_unrelated_file_error(qapp, tmp_path, monkeypatch):
    from ui.suspension_dialog import SuspensionDialog
    from core import suspension_permissions as P
    folder = _memory(tmp_path)
    R.start(folder.parent, role="desktop", heartbeat=False)
    dialog = SuspensionDialog()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(P.os, "getuid", lambda: folder.stat().st_uid + 1)
            dialog.first_time_choice.setChecked(True)
            dialog.first_run_confirm.setChecked(True)
            dialog.setup_action_btn.click()
        assert not dialog.permissions_fix_btn.isEnabled()
        assert "ask the folder owner" in dialog.permission_label.text()
        assert "2777" in dialog.permission_label.text()
        dialog.technical_toggle.click()
        assert dialog.technical_details.isVisibleTo(dialog)
        assert "insecure_permissions" in dialog.technical_details.text()
    finally:
        dialog.close()
    folder.chmod(0o700)
    dialog = SuspensionDialog()
    try:
        assert not dialog._permission_failure(S.StoreFault(S.Fault.INSECURE, "soul identity must be private"))
        assert not dialog._permission_pending
    finally:
        dialog.close()


def test_repair_after_uncertain_choice_still_requires_recovery_acknowledgement(qapp, tmp_path, monkeypatch):
    from ui.suspension_dialog import SuspensionDialog
    from PySide6.QtWidgets import QMessageBox
    folder = _memory(tmp_path)
    R.start(folder.parent, role="desktop", heartbeat=False)
    dialog = SuspensionDialog()
    try:
        dialog.previous_use_choice.setChecked(True)
        dialog.unknown_prior_state.setChecked(True)
        dialog.setup_action_btn.click()
        assert dialog._permission_pending
        monkeypatch.setattr(QMessageBox, "exec", lambda *a, **k: QMessageBox.Yes)
        dialog.permissions_fix_btn.click()
        assert dialog.previous_use_choice.isChecked()
        assert not dialog.unknown_prior_state.isChecked()
        assert not (folder / S.STORE_NAME).exists()
        assert not dialog.setup_action_btn.isEnabled()
        dialog.unknown_prior_state.setChecked(True)
        dialog.setup_action_btn.click()
        assert R.operator_view()[0].trigger == S.RECOVERY_TRIGGER
        with pytest.raises(R.SuspensionRefused):
            R.capture(S.OWNER_SCOPE)
    finally:
        dialog.close()
