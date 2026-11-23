"""ui/suspension_dialog.py -- the desktop's runaway-suspension controls
(AGENT-RUNAWAY-SUSPEND-01, Gatling Slice C1b).

The one place a suspension store is initialized, repaired, recovered or
resumed. Every button mints a fresh local suspension-control envelope at the
moment of the click (core.operator_ingress.local_suspension_control); no
text, Telegram message or model output can reach these controls.

  * No store yet: the owner chooses Enable protection (first launch with runaway
    suspension here -- an explicit confirmation, because a missing store
    cannot be told apart from a deleted one) or Recover (Lumina ran with it
    before, or the owner isn't sure: the owner scope starts suspended).
    With no trace at all, Recover needs its own acknowledgement that
    earlier state and outcomes are unknown; it restores nothing and every
    channel also starts suspended until reviewed.
  * A faulted store: Repair identity (non-destructive, when only the
    identity pin is missing or unreadable) or Recover (destructive:
    evidence first, other runtimes quiesced or acknowledged, new
    incarnation).
  * Active incidents: each is shown with its time, trigger, evidence counts
    and what was in flight; Resume clears exactly the incident on screen.
    Resume waits while earlier work is still running; work whose process
    died is "outcome unknown" and needs the acknowledgement checkbox.
  * Recover from this restore (C1-S2): on a restored copy waiting for recovery
    the owner sets up a new owner PIN (never the original installation's),
    reviews what the restore receipt says, and recovers with the PIN and two
    acknowledgements. Plain Recover is not offered there, and on any restored
    copy channels can't be allowed to start unsuspended: each is resumed on
    its own.
  * Wire seat (W-B2): a restored copy is Wire-locked (core/wire_seat_gate.py).
    "Allow this copy to use the Wire seat" is offered only for that lock,
    only after the owner ticks that the original installation's status is
    unknown, and only once safe recovery of the restored copy is confirmed.
    It starts nothing and turns nothing on.

The selector shown here is for this desktop display (and, from Slice C3,
the owner's verified Telegram /resume). It is never logged.
"""
from __future__ import annotations

import time
import os
import stat
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QMessageBox,
)

from core import operator_ingress, suspension as S, suspension_runtime as R
from core import suspension_permissions as P
from core import wire_seat_gate as G


# ── C1-S2 owner texts (module constants so tests can lint them) ──
ARCHIVE_TITLE_TEXT = "Recover from this restore"
ARCHIVE_INTRO_TEXT = (
    "This data folder was restored from a backup. Your chats, memories and other data are already "
    "here and stay readable whatever you choose below. Recovering from this restore starts a new "
    "safety identity for this copy: every channel starts suspended for your review, and nothing "
    "is turned on automatically.")
PIN_SETUP_TEXT = (
    "Set an owner PIN for this restored copy. You never need the original installation's PIN: "
    "a PIN is never put in a backup. It is stored hashed in this copy's settings. It is also the "
    "PIN that unlocks sensitive tools on a non-owner channel such as Discord: you send it to "
    "Lumina in that conversation and she passes it to the PIN check. Send it only to unlock "
    "those tools and only where no one else can read it, never in a shared channel or in "
    "passing: a PIN sent in a chat stays in that chat's history. It can't be changed or reset "
    "in this version. If you forget it before "
    "recovering, restore the backup again into a new folder: nothing from the backup is lost. "
    "After recovery it only guards those non-owner tools; your data and your own use of Lumina "
    "never need it. Use at least 6 characters; a 6-digit number is fine. Credentials and your PIN "
    "are not in an Agent Backup: enter credentials again after recovery.")
PIN_UNREADABLE_TEXT = (
    "This copy's settings file can't be read, so an owner PIN can't be set or checked safely. "
    "Your backup is unaffected: restore it again into a new folder.")
ARCHIVE_ACK_RUNTIMES_TEXT = "No other Lumina process is using this folder, or it may be ignored."
ARCHIVE_ACK_PRIOR_TEXT = (
    "The backup is a past point in time. Anything that happened on the original installation "
    "since then is unknown, and every channel starts suspended for my review.")
ARCHIVE_ACK_NO_LINK_TEXT = (
    "This copy's restore receipt can't be matched to its restore record. It will recover safely, "
    "but it will never be able to use the Wire seat. Restoring the backup again into a new folder "
    "is the way to get a copy that can.")
RESTORED_CHANNELS_TEXT = (
    "This is a restored copy: every channel first seen starts suspended. Resume each one on its own.")
RECORD_STATUS_TEXT = {
    "kept_as_inactive_history": ("The restore receipt says the backup's safety record was kept. It is "
                                 "checked again before use and can only add restrictions."),
    "not_restored_owner_skipped": "The restore receipt says no safety record was kept (skipped at restore).",
    "not_restored_record_unavailable": ("The restore receipt says no safety record was kept (the "
                                        "backup's record was damaged)."),
    None: "This backup's format carries no safety record.",
}


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return label


def _acknowledgement(explanation, caption):
    """Keep the meaning readable; native checkbox captions don't wrap."""
    group = QWidget()
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(_label(explanation))
    box = QCheckBox(caption)
    box.setAccessibleDescription(explanation)
    layout.addWidget(box)
    return group, box


class SuspensionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Runaway suspension")
        self.setMinimumWidth(420)
        available = self.screen().availableGeometry()
        self.resize(min(640, available.width() - 40), min(560, available.height() - 60))
        # Keep unchecked choices visible against Lumina's dark application theme.
        self.setStyleSheet("""
            QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {
                border: 1px solid #9ba7b9; background: transparent;
            }
            QRadioButton::indicator:unchecked { border-radius: 6px; }
            QPushButton:disabled { color: palette(mid); }
        """)
        self._views = ()
        self._blocked_runtimes = ()
        self._permission_pending = False
        self._permission_preview = None

        layout = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        content = QVBoxLayout(body)
        content.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll)
        self.state_label = _label()
        content.addWidget(self.state_label)
        content.addWidget(_label("Data folder — check this is your existing Lumina folder:"))
        self.data_folder_label = _label()
        self.data_folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.data_folder_label.setStyleSheet("font-weight: bold;")
        content.addWidget(self.data_folder_label)

        self.permission_group = QWidget()
        permissions = QVBoxLayout(self.permission_group)
        permissions.setContentsMargins(0, 0, 0, 0)
        self.permission_label = _label()
        self.permission_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        permissions.addWidget(self.permission_label)
        self.permissions_fix_btn = QPushButton("Fix folder permissions")
        self.permissions_fix_btn.clicked.connect(self._fix_folder_permissions)
        permissions.addWidget(self.permissions_fix_btn)
        self.permissions_not_now_btn = QPushButton("Not now")
        self.permissions_not_now_btn.clicked.connect(self.reject)
        permissions.addWidget(self.permissions_not_now_btn)
        explanation = QLabel('<a style="color: #7dcfff;" href="' + (Path(__file__).resolve().parents[1] / "docs/upgrading.md").as_uri()
                             + '#folder-permissions">Why does this folder need different permissions?</a>')
        explanation.setOpenExternalLinks(True)
        explanation.setWordWrap(True)
        permissions.addWidget(explanation)
        content.addWidget(self.permission_group)

        self.setup_choices = QWidget()
        choices = QVBoxLayout(self.setup_choices)
        choices.setContentsMargins(0, 0, 0, 0)
        choices.addWidget(_label("Has runaway-task protection been set up for this data folder before?"))
        self.first_time_choice = QRadioButton("No, this is the first time.")
        self.previous_use_choice = QRadioButton("Yes, or I'm not sure.")
        self.choice_group = QButtonGroup(self)
        for choice in (self.first_time_choice, self.previous_use_choice):
            self.choice_group.addButton(choice)
            choices.addWidget(choice)
            choice.toggled.connect(self._choice_changed)
        choices.addWidget(_label("Choose an option, then confirm below. Nothing changes until you click the action button."))
        content.addWidget(self.setup_choices)

        self.incidents = QListWidget()
        self.incidents.currentRowChanged.connect(self._show_incident)
        self.incidents.setMaximumHeight(160)
        content.addWidget(self.incidents)
        self.detail_label = _label()
        content.addWidget(self.detail_label)

        self.first_run_group, self.first_run_confirm = _acknowledgement(
            "Enable protection only if this data folder has never used this safety feature. "
            "Your existing chats, memories, Skills and preferences will not be reset.",
            "I confirm this is the first setup.")
        self.first_run_confirm.toggled.connect(lambda on: self._exclusive(self.first_run_confirm, on))
        content.addWidget(self.first_run_group)
        self.unknown_prior_group, self.unknown_prior_state = _acknowledgement(
            "Earlier safety records, other Lumina processes and work outcomes are unknown. "
            "Recovery starts a new safety record with my work and every channel paused for review. "
            "No earlier safety records can be restored.",
            "I accept this paused start.")
        self.unknown_prior_state.toggled.connect(
            lambda on: self._exclusive(self.unknown_prior_state, on))
        content.addWidget(self.unknown_prior_group)
        self.ack_unknown_group, self.ack_unknown = _acknowledgement(
            "Work whose process stopped may or may not have finished (outcome unknown).",
            "I accept the unknown outcome.")
        content.addWidget(self.ack_unknown_group)
        self.ack_runtimes_group, self.ack_runtimes = _acknowledgement(
            "Other Lumina processes listed above are stopped or may be ignored.",
            "I acknowledge these processes.")
        content.addWidget(self.ack_runtimes_group)
        self.unknown_scopes_group, self.unknown_scopes_clear = _acknowledgement(
            "After recovery, let channels whose state can't be read start unsuspended. "
            "Leave this unchecked to keep unknown channels paused for review.",
            "Allow an unpaused start.")
        content.addWidget(self.unknown_scopes_group)
        self.recovery_note = _label(
            "Safe recovery rebuilds safety protection. It is not Agent Restore and does not recover "
            "chats or memories. Stop other Lumina processes using this folder before recovery.")
        content.addWidget(self.recovery_note)

        # C1-S2: Recover from this restore (restored copy awaiting recovery).
        self.archive_group = QWidget()
        archive = QVBoxLayout(self.archive_group)
        archive.setContentsMargins(0, 0, 0, 0)
        archive_title = _label(ARCHIVE_TITLE_TEXT)
        archive_title.setStyleSheet("font-weight: bold;")
        archive.addWidget(archive_title)
        archive.addWidget(_label(ARCHIVE_INTRO_TEXT))
        self.archive_review_label = _label()
        archive.addWidget(self.archive_review_label)
        self.pin_setup_group = QWidget()
        pin_setup = QVBoxLayout(self.pin_setup_group)
        pin_setup.setContentsMargins(0, 0, 0, 0)
        pin_setup.addWidget(_label(PIN_SETUP_TEXT))
        self.pin_entry = QLineEdit()
        self.pin_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_entry.setPlaceholderText("New owner PIN")
        self.pin_confirm_entry = QLineEdit()
        self.pin_confirm_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_confirm_entry.setPlaceholderText("Repeat the PIN")
        self.set_pin_btn = QPushButton("Set owner PIN")
        self.set_pin_btn.clicked.connect(self._set_owner_pin)
        for widget in (self.pin_entry, self.pin_confirm_entry, self.set_pin_btn):
            pin_setup.addWidget(widget)
        archive.addWidget(self.pin_setup_group)
        self.pin_unreadable_label = _label(PIN_UNREADABLE_TEXT)
        archive.addWidget(self.pin_unreadable_label)
        self.archive_pin_entry = QLineEdit()
        self.archive_pin_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.archive_pin_entry.setPlaceholderText("Owner PIN")
        archive.addWidget(self.archive_pin_entry)
        self.archive_ack_runtimes_group, self.archive_ack_runtimes = _acknowledgement(
            ARCHIVE_ACK_RUNTIMES_TEXT, "I confirm.")
        self.archive_ack_prior_group, self.archive_ack_prior = _acknowledgement(
            ARCHIVE_ACK_PRIOR_TEXT, "I accept this paused start.")
        self.archive_ack_no_link_group, self.archive_ack_no_link = _acknowledgement(
            ARCHIVE_ACK_NO_LINK_TEXT, "I accept that this copy can't use the Wire seat.")
        for group in (self.archive_ack_runtimes_group, self.archive_ack_prior_group,
                      self.archive_ack_no_link_group):
            archive.addWidget(group)
        for box in (self.archive_ack_runtimes, self.archive_ack_prior, self.archive_ack_no_link):
            box.toggled.connect(lambda _on: self._sync_buttons())
        self.archive_pin_entry.textChanged.connect(lambda _text: self._sync_buttons())
        self.archive_recover_btn = QPushButton(ARCHIVE_TITLE_TEXT)
        self.archive_recover_btn.clicked.connect(self._recover_from_archive)
        archive.addWidget(self.archive_recover_btn)
        content.addWidget(self.archive_group)
        self._archive_inputs = None
        self._archive_awaiting = False
        self._pin_state = "unset"
        self.restored_channels_label = _label(RESTORED_CHANNELS_TEXT)
        content.addWidget(self.restored_channels_label)
        self._restored = False

        # W-B2: the Wire seat of a restored copy (owner-local release only).
        self.wire_group = QWidget()
        wire = QVBoxLayout(self.wire_group)
        wire.setContentsMargins(0, 0, 0, 0)
        wire_title = _label("Wire seat")
        wire_title.setStyleSheet("font-weight: bold;")
        wire.addWidget(wire_title)
        self.wire_status_label = _label()
        wire.addWidget(self.wire_status_label)
        self.wire_body_label = _label(G.RELEASE_BODY_TEXT)
        wire.addWidget(self.wire_body_label)
        self.wire_ack_group, self.wire_ack = _acknowledgement(G.RELEASE_ACK_TEXT, G.RELEASE_ACK_CAPTION)
        self.wire_ack_label = self.wire_ack_group.layout().itemAt(0).widget()
        self.wire_ack.toggled.connect(lambda _on: self._sync_wire_button())
        wire.addWidget(self.wire_ack_group)
        self.wire_blocker_label = _label()
        wire.addWidget(self.wire_blocker_label)
        self.wire_release_btn = QPushButton(G.RELEASE_BUTTON_TEXT)
        self.wire_release_btn.clicked.connect(self._release_wire_seat)
        wire.addWidget(self.wire_release_btn)
        content.addWidget(self.wire_group)
        self._wire_verdict = None
        self._wire_blocker = None
        guide = QLabel('<a style="color: #7dcfff;" href="' + (Path(__file__).resolve().parents[1] / "docs/upgrading.md").as_uri()
                       + '">Upgrade and troubleshooting guide</a> (docs/upgrading.md)')
        guide.setWordWrap(True)
        guide.setOpenExternalLinks(True)
        content.addWidget(guide)
        self.result_label = _label()
        content.addWidget(self.result_label)
        self.technical_toggle = QPushButton("Technical details")
        self.technical_toggle.setCheckable(True)
        content.addWidget(self.technical_toggle)
        self.technical_details = _label()
        self.technical_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.technical_details.hide()
        self.technical_toggle.toggled.connect(self.technical_details.setVisible)
        content.addWidget(self.technical_details)

        buttons = QVBoxLayout()
        self.setup_action_btn = QPushButton("Continue")
        self.setup_action_btn.clicked.connect(self._setup_action)
        self.repair_btn = QPushButton("Repair identity")
        self.repair_btn.clicked.connect(self._repair)
        self.recover_btn = QPushButton("Review safe recovery")
        self.recover_btn.clicked.connect(self._recover)
        self.resume_btn = QPushButton("Resume selected")
        self.resume_btn.clicked.connect(self._resume)
        self.ack_scopes_btn = QPushButton("Unknown channels may start unsuspended")
        self.ack_scopes_btn.clicked.connect(self._acknowledge_unknown_scopes)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        for button in (self.setup_action_btn, self.repair_btn, self.recover_btn, self.resume_btn,
                       self.ack_scopes_btn, self.close_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.refresh()

    # -- state ---------------------------------------------------------------

    def refresh(self) -> None:
        status = R.status()
        self._status = status
        if (status.state is R.State.FAULT and status.fault is S.Fault.INSECURE
                and not self._permission_pending and self._memory_permissions_problem(status)):
            self._offer_folder_permissions(f"C1 probe: {status.fault.value}")
        self.data_folder_label.setText(status.data_dir or "No data folder selected — close and check your launch settings.")
        self.incidents.clear()
        self._views = ()
        if status.state is R.State.ACTIVE:
            try:
                self._views = R.operator_view()
            except Exception as exc:
                self.result_label.setText(f"Could not read incidents: {type(exc).__name__}")
            for view in self._views:
                QListWidgetItem(f"{view.scope} · since {_when(view.created_at)} · {view.trigger}",
                                self.incidents)
            if self._views:
                self.incidents.setCurrentRow(0)
                head = (f"⛔ {len(self._views)} suspended scope(s). New turns, tools and "
                        "provider requests in a suspended scope are refused until resumed.")
            else:
                head = "✅ Runaway suspension is active and nothing is suspended."
        elif status.state is R.State.UNINITIALIZED:
            if status.probe_status is S.StoreStatus.ABSENT_NO_TRACE:
                head = ("Set up Lumina's safety protection\n\n"
                        "This update adds protection against runaway AI tasks. Your chats, memories, "
                        "Skills and preferences will not be reset. Work stays paused until safety "
                        "protection is ready.")
            else:
                head = ("The runaway-suspension store is missing but traces of an earlier one "
                        f"remain ({', '.join(status.traces)}). Choose Recover. Nothing is "
                        "admitted until then.")
        elif status.state is R.State.FAULT:
            fault = status.fault.value if status.fault else "unknown"
            head = (f"The runaway-suspension store failed its checks ({fault}), so nothing is "
                    "admitted. Repair identity keeps everything when only the identity pin is "
                    "damaged; Recover preserves evidence and rebuilds the store.")
        else:
            head = "This process is not participating in runaway suspension."
        if self._permission_pending:
            head = "Safety setup is paused until this folder's permissions are reviewed."
        local = f" Stopped in this process (unconfirmed): {', '.join(status.local_stops)}." \
            if status.local_stops else ""
        self.state_label.setText(head + local)
        self.incidents.setVisible(bool(self._views))
        self.detail_label.setVisible(bool(self._views))
        if not self._views:
            self.detail_label.setText("")
        self._refresh_archive_recovery()
        self._refresh_wire_seat()
        self._sync_buttons()
        self._fit_setup()

    def _refresh_archive_recovery(self) -> None:
        """C1-S2: what this copy needs. Read-only; the backend re-checks all."""
        data_dir = self._status.data_dir
        try:
            self._restored = bool(data_dir) and G.is_restored_root(data_dir)
        except Exception:
            self._restored = True       # unknown counts as restored
        self._archive_awaiting = R.awaiting_archive_recovery()
        self._archive_inputs = None
        if not self._archive_awaiting:
            return
        from core import agent_backup_restore as restore_engine
        from core import pin_gate
        self._pin_state = pin_gate.owner_pin_state()
        try:
            self._archive_inputs = restore_engine.archive_recovery_inputs(data_dir)
        except Exception as exc:
            self._archive_inputs = None
            self._technical_error(f"Restore receipt: {type(exc).__name__}")
        inputs = self._archive_inputs
        if inputs is not None and inputs.receipt_paired:
            lines = [f"Restore {inputs.restore_id[:8]} · backup {inputs.archive_sha256[:12]}",
                     RECORD_STATUS_TEXT.get(inputs.receipt_record_status,
                                            "The restore receipt's safety record entry can't be read; "
                                            "it will not be used.")]
        else:
            lines = ["The restore receipt can't be matched to this copy's restore record."]
        self.archive_review_label.setText("\n".join(lines))

    def _refresh_wire_seat(self) -> None:
        """W-B2: show this data folder's Wire seat state. The release is
        offered only for a restored copy waiting for the owner's decision;
        every other lock is shown read-only with its own remedy."""
        verdict = G.process_seat_verdict()
        self._wire_verdict = verdict
        self._wire_blocker = None
        self.wire_ack.setChecked(False)  # never carried across a refresh
        offer = verdict.reason == G.RECOVERED_UNRELEASED
        if verdict.reason == G.RELEASED:
            text = G.RELEASED_STATUS_TEXT
        elif offer:
            restores = "; ".join(f"restored {_when(at)}, restore {rid[:8]}"
                                 for rid, _sha, at in verdict.unreleased)
            text = f"{G.STATUS_TEXT[G.RECOVERED_UNRELEASED]} ({restores})"
            self._wire_blocker = R.wire_release_blocker(
                tuple((rid, sha) for rid, sha, _at in verdict.unreleased))
        else:
            text = G.describe(verdict)
            if verdict.error_class:
                detail = f"Wire seat check: {verdict.error_class}"
                if detail not in self.technical_details.text():
                    self._technical_error(detail)
        self.wire_group.setVisible(verdict.reason != G.NO_TRACE)
        self.wire_status_label.setText(text)
        self.wire_body_label.setVisible(offer)
        self.wire_ack_group.setVisible(offer)
        self.wire_release_btn.setVisible(offer)
        self.wire_blocker_label.setText(self._wire_blocker.text if self._wire_blocker else "")
        self.wire_blocker_label.setVisible(offer and self._wire_blocker is not None)
        self._sync_wire_button()

    def _sync_wire_button(self) -> None:
        verdict = self._wire_verdict
        self.wire_release_btn.setEnabled(
            verdict is not None and verdict.reason == G.RECOVERED_UNRELEASED
            and self._wire_blocker is None and self.wire_ack.isChecked())

    @staticmethod
    def _memory_permissions_problem(status):
        try:
            info = os.lstat(Path(status.data_dir) / S.MEMORY_DIRNAME)
            return (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or bool(info.st_mode & 0o022))
        except (OSError, TypeError, AttributeError):
            return False

    def _technical_error(self, detail):
        earlier = self.technical_details.text()
        self.technical_details.setText((earlier + "\n" if earlier else "") + detail)

    def _offer_folder_permissions(self, detail):
        self._permission_pending = True
        self._permission_preview = None
        self._technical_error(detail)
        path = Path(self._status.data_dir) / S.MEMORY_DIRNAME
        explanation = ("Other people may be able to change files in your memory folder. "
                       "Lumina keeps work paused to protect its safety records.\n\n"
                       f"Affected folder: {path}\n")
        try:
            info = os.lstat(path)
            mode = stat.S_IMODE(info.st_mode)
            proposed = f"{mode & ~0o022:04o}" if stat.S_ISDIR(info.st_mode) else "unavailable (not an ordinary directory)"
            explanation += f"Current mode: {mode:04o}\nProposed mode: {proposed}\n\n"
        except OSError as exc:
            explanation += "Current/proposed modes: unavailable; the folder could not be inspected safely.\n\n"
            self._technical_error(f"{type(exc).__name__}: {exc}")
        try:
            preview = P.inspect(path)
            if not preview.mode & 0o022:
                raise P.PermissionRepairRefused("No group/other write bits are present; stop and review the error with support.")
            self._permission_preview = preview
            explanation += ("This removes only group and other write access to this folder. "
                            "Files, subfolders, ownership and other permission bits stay as they are. "
                            "If you intentionally share write access, choose Not now and ask the folder owner "
                            "for a reviewed plan. Do not use sudo or change data folders to bypass this check.")
        except (S.SuspensionError, OSError) as exc:
            explanation += ("Lumina cannot safely repair this folder. It must be an ordinary folder "
                            "owned by your OS user, with no symlinks in its path. Stop and ask the folder "
                            "owner or support to review its ownership, sharing and launch path. "
                            "Do not use sudo, delete safety files or switch data folders to bypass this check.")
            self._technical_error(f"{type(exc).__name__}: {exc}")
        self.permission_label.setText(explanation)
        self.permissions_fix_btn.setEnabled(self._permission_preview is not None)
        self.first_run_confirm.setChecked(False)
        self.unknown_prior_state.setChecked(False)

    def _permission_failure(self, exc):
        if isinstance(exc, S.StoreFault) and exc.fault is S.Fault.INSECURE and exc.detail.startswith("memory directory"):
            self._offer_folder_permissions(f"{type(exc).__name__}: {exc}")
            self.result_label.setText("Review the folder permissions above before continuing safety setup.")
            return True
        return False

    def _fix_folder_permissions(self):
        preview = self._permission_preview
        if preview is None or not self._permission_pending:
            return
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Confirm folder permission repair")
        confirmation.setIcon(QMessageBox.Question)
        confirmation.setTextFormat(Qt.PlainText)
        confirmation.setText(
            "Confirm that this is your private memory folder and write access is NOT intentionally shared.\n\n"
            f"Folder: {preview.path}\nMode: {preview.mode:04o} → {preview.proposed_mode:04o}\n\n"
            "Change only this folder's group/other write bits? No files or subfolders will be changed. "
            "This does not initialize, recover or resume safety protection.")
        confirmation.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        confirmation.setDefaultButton(QMessageBox.No)
        if confirmation.exec() != QMessageBox.Yes:
            return
        try:
            R.repair_folder_permissions(preview, ingress=operator_ingress.local_suspension_control("folder_permissions"))
        except (S.SuspensionError, OSError) as exc:
            self._technical_error(f"{type(exc).__name__}: {exc}")
            self.permissions_fix_btn.setEnabled(False)
            self._permission_preview = None
            self.result_label.setText("Repair could not be safely completed and verified. Permissions may have changed. "
                                      "Stop and ask the folder owner or support to review the path and Technical details.")
        else:
            self._permission_pending = False
            self._permission_preview = None
            self.first_run_confirm.setChecked(False)
            self.unknown_prior_state.setChecked(False)
            self.result_label.setText("Folder permissions repaired and verified. Review your original safety setup choice "
                                      "and confirm it again. Nothing was initialized, recovered or resumed.")
        self.refresh()

    def _fit_setup(self) -> None:
        if self._absent_clean():
            # Fit the first-run decision to its wrapped content, keeping scrolling
            # available when the screen is short. No empty incident space remains.
            self.layout().activate()
            frame = 2 * self.scroll.frameWidth()
            height = self.scroll.widget().layout().heightForWidth(self.scroll.width() - frame)
            margins = self.layout().contentsMargins()
            actions = self.layout().itemAt(1).sizeHint().height()
            height += frame + margins.top() + margins.bottom() + self.layout().spacing() + actions
            self.resize(self.width(), min(height, self.screen().availableGeometry().height() - 60))
        elif self._views:
            self.resize(self.width(), min(640, self.screen().availableGeometry().height() - 60))

    def _sync_buttons(self) -> None:
        status = self._status
        absent_clean = self._absent_clean()
        if not absent_clean:
            self.choice_group.setExclusive(False)
            for choice in (self.first_time_choice, self.previous_use_choice):
                choice.blockSignals(True)
                choice.setChecked(False)
                choice.blockSignals(False)
            self.choice_group.setExclusive(True)
            for box in (self.first_run_confirm, self.unknown_prior_state):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        self.setup_choices.setVisible(absent_clean)
        first = absent_clean and self.first_time_choice.isChecked()
        uncertain = absent_clean and self.previous_use_choice.isChecked()
        self.first_run_group.setVisible(first)
        self.unknown_prior_group.setVisible(uncertain)
        self.setup_action_btn.setVisible(absent_clean)
        self.setup_action_btn.setText("Enable protection" if first else
                                      "Review safe recovery" if uncertain else "Continue")
        self.setup_action_btn.setEnabled((first and self.first_run_confirm.isChecked()) or
                                         (uncertain and self.unknown_prior_state.isChecked()))
        self.repair_btn.setVisible(status.state is R.State.FAULT and status.fault in (
            S.Fault.IDENTITY_MISSING, S.Fault.IDENTITY_INVALID))
        recovering = status.state in (R.State.UNINITIALIZED, R.State.FAULT)
        self.recover_btn.setVisible(recovering and not absent_clean)
        self.recover_btn.setEnabled(recovering and not absent_clean)
        # With no trace there is no old registry or scope table to acknowledge:
        # the no-trace acknowledgement covers both, and channels always start
        # suspended.
        self.ack_runtimes_group.setVisible(recovering and not absent_clean)
        self.unknown_scopes_group.setVisible(recovering and not absent_clean)
        self.recovery_note.setVisible(recovering)
        # C1-S2 (D1-2, TA2-05): a restored copy awaiting recovery offers only
        # Recover from this restore; the PIN setup exists only there.
        awaiting = self._archive_awaiting
        paired = self._archive_inputs is not None and self._archive_inputs.receipt_paired
        self.archive_group.setVisible(awaiting)
        self.pin_setup_group.setVisible(awaiting and self._pin_state == "unset")
        self.pin_unreadable_label.setVisible(awaiting and self._pin_state not in ("set", "unset"))
        self.archive_pin_entry.setVisible(awaiting and self._pin_state == "set")
        for group in (self.archive_ack_runtimes_group, self.archive_ack_prior_group):
            group.setVisible(awaiting and self._pin_state == "set")
        self.archive_ack_no_link_group.setVisible(awaiting and self._pin_state == "set" and not paired)
        self.archive_recover_btn.setVisible(awaiting and self._pin_state == "set")
        self.archive_recover_btn.setEnabled(
            awaiting and self._pin_state == "set" and bool(self.archive_pin_entry.text())
            and self.archive_ack_runtimes.isChecked() and self.archive_ack_prior.isChecked()
            and (paired or self.archive_ack_no_link.isChecked()))
        if awaiting:
            for widget in (self.recover_btn, self.ack_runtimes_group, self.unknown_scopes_group):
                widget.hide()
        # A2-4: on any restored copy, channels are never allowed an unpaused
        # start; the backend refuses it too, this only explains it.
        if self._restored:
            self.unknown_scopes_group.hide()
        self.restored_channels_label.setVisible(self._restored)
        self.resume_btn.setVisible(status.state is R.State.ACTIVE)
        self.resume_btn.setEnabled(bool(self._views) and self.incidents.currentRow() >= 0)
        self.ack_unknown_group.setVisible(status.state is R.State.ACTIVE and bool(self._views))
        self.ack_scopes_btn.setVisible(status.state is R.State.ACTIVE and not self._restored and any(
            v.trigger == S.RECOVERY_TRIGGER for v in self._views))
        self.result_label.setVisible(bool(self.result_label.text()))
        self.permission_group.setVisible(self._permission_pending)
        self.technical_toggle.setVisible(bool(self.technical_details.text()))
        if self._permission_pending:
            for widget in (self.setup_choices, self.first_run_group, self.unknown_prior_group,
                           self.setup_action_btn, self.repair_btn, self.recover_btn,
                           self.ack_runtimes_group, self.unknown_scopes_group, self.recovery_note):
                widget.hide()

    def _choice_changed(self) -> None:
        # Choosing a different path never carries its earlier acknowledgement.
        if not self.first_time_choice.isChecked():
            self.first_run_confirm.setChecked(False)
        if not self.previous_use_choice.isChecked():
            self.unknown_prior_state.setChecked(False)
        if hasattr(self, "_status"):
            self._sync_buttons()
            self._fit_setup()

    def _setup_action(self) -> None:
        if self.first_time_choice.isChecked():
            self._initialize()
        elif self.previous_use_choice.isChecked():
            self._recover()

    def _exclusive(self, box: QCheckBox, on: bool) -> None:
        """"First launch" and "can't confirm a first launch" contradict each
        other: ticking one clears the other."""
        if on:
            other = self.unknown_prior_state if box is self.first_run_confirm else self.first_run_confirm
            other.setChecked(False)
        self._sync_buttons()

    def _absent_clean(self) -> bool:
        return (self._status.state is R.State.UNINITIALIZED
                and self._status.probe_status is S.StoreStatus.ABSENT_NO_TRACE)

    def _selected(self):
        row = self.incidents.currentRow()
        return self._views[row] if 0 <= row < len(self._views) else None

    def _show_incident(self, _row: int) -> None:
        view = self._selected()
        if view is None:
            self.detail_label.setText("")
            self._sync_buttons()
            return
        evidence = ", ".join(f"{k}={v}" for k, v in sorted(view.evidence.items())) or "none"
        lines = [f"Scope: {view.scope}", f"Since: {_when(view.created_at)}",
                 f"Trigger: {view.trigger}", f"Evidence: {evidence}",
                 f"In flight when it latched: {view.in_flight_at_trip} (they may still have finished)",
                 f"Selector: {view.selector}"]
        try:
            report = R.lease_report(view.scope)
        except Exception:
            report = None
        if report is not None:
            if report.live:
                lines.append("Still running: " + ", ".join(f"{l.kind}:{l.name}" for l in report.live))
            if report.orphaned:
                lines.append("Outcome unknown (process stopped): "
                             + ", ".join(f"{l.kind}:{l.name}" for l in report.orphaned))
        if view.requires_ack:
            lines.append("This incident came from store recovery: acknowledge unknown outcomes to resume.")
        self.detail_label.setText("\n".join(lines))
        self._sync_buttons()

    # -- actions (each mints a fresh local envelope at the click) -------------

    def _initialize(self) -> None:
        if not self.first_time_choice.isChecked() or not self.first_run_confirm.isChecked():
            return
        try:
            R.initialize(ingress=operator_ingress.local_suspension_control("initialize"))
            self.result_label.setText("Initialized. Runaway suspension is now active.")
        except S.SuspensionError as exc:
            if not self._permission_failure(exc):
                self.result_label.setText(f"Not initialized: {exc}")
        self.first_run_confirm.setChecked(False)
        self.refresh()

    def _repair(self) -> None:
        try:
            R.repair_identity(ingress=operator_ingress.local_suspension_control("repair_identity"))
            self.result_label.setText("Identity repaired; every suspension was kept.")
        except S.SuspensionError as exc:
            if not self._permission_failure(exc):
                self.result_label.setText(f"Repair not possible: {exc}")
        self.refresh()

    def _recover(self) -> None:
        no_trace = self._absent_clean()
        if no_trace and (not self.previous_use_choice.isChecked() or not self.unknown_prior_state.isChecked()):
            return
        ack_runtimes = self.ack_runtimes.isChecked() and not no_trace
        acked = set(self._blocked_runtimes) if ack_runtimes else set()
        try:
            outcome = R.recover(
                ingress=operator_ingress.local_suspension_control("recover"),
                acknowledged_runtimes=acked,
                acknowledge_unreadable_runtimes=ack_runtimes,
                acknowledge_unknown_scopes=self.unknown_scopes_clear.isChecked() and not no_trace,
                acknowledge_unknown_prior_state=no_trace and self.unknown_prior_state.isChecked())
        except S.RecoveryBlocked as exc:
            self._blocked_runtimes = tuple(exc.runtimes)
            listed = f" ({len(exc.runtimes)} process(es))" if exc.runtimes else ""
            self.result_label.setText(f"Recovery waiting: {exc.reason}{listed}. Nothing was changed "
                                      "beyond preserving evidence.")
            self.refresh()
            return
        except S.SuspensionError as exc:
            if not self._permission_failure(exc):
                self.result_label.setText(f"Recovery failed: {exc}")
            self.refresh()
            return
        self._blocked_runtimes = ()
        self.unknown_prior_state.setChecked(False)
        scopes = ", ".join(outcome.suspended_scopes) or "none"
        if not outcome.evidence_survived:
            self.result_label.setText(
                "New safety record started; nothing was restored. No earlier safety records "
                "survived, so earlier suspensions and the outcome of earlier work are unknown. "
                f"Receipt kept in {outcome.evidence_dir}. Suspended for your review: {scopes}; "
                "every channel also starts suspended until you review it.")
        else:
            self.result_label.setText(
                f"Recovered. Evidence kept in {outcome.evidence_dir}. Suspended for your review: {scopes}.")
        self.refresh()

    def _clear_pin_entries(self) -> None:
        for entry in (self.pin_entry, self.pin_confirm_entry, self.archive_pin_entry):
            entry.clear()

    def _set_owner_pin(self) -> None:
        pin, confirm = self.pin_entry.text(), self.pin_confirm_entry.text()
        self._clear_pin_entries()      # PS-5: cleared on every outcome
        try:
            R.set_owner_pin(ingress=operator_ingress.local_suspension_control("owner_pin_setup"),
                            pin=pin, confirm=confirm)
        except R.OwnerPinRefused as exc:
            self.result_label.setText(f"PIN not set: {exc.text}")
        except Exception as exc:
            self.result_label.setText("PIN not set: Lumina couldn't save the PIN. Nothing was changed.")
            self._technical_error(f"Owner PIN setup: {type(exc).__name__}")
        else:
            self.result_label.setText("Owner PIN set. Enter it below to recover from this restore.")
        del pin, confirm
        self.refresh()

    def _recover_from_archive(self) -> None:
        pin = self.archive_pin_entry.text()
        self._clear_pin_entries()
        inputs = self._archive_inputs
        if inputs is None or not self._archive_awaiting:
            self.result_label.setText("Nothing was changed: review this copy again.")
            self.refresh()
            return
        try:
            outcome = R.recover_from_archive(
                ingress=operator_ingress.local_suspension_control("recover_from_archive"),
                pin=pin, inputs=inputs,
                acknowledge_unreadable_runtimes=self.archive_ack_runtimes.isChecked(),
                acknowledge_prior_state_since_archive=self.archive_ack_prior.isChecked(),
                acknowledge_no_lineage=self.archive_ack_no_link.isChecked())
        except R.RecoverFromArchiveRefused as exc:
            self.result_label.setText(f"Not recovered: {exc.text}")
        except S.RecoveryBlocked as exc:
            self.result_label.setText(f"Not recovered: {exc.reason}. Nothing was changed.")
        except S.SuspensionError as exc:
            if not self._permission_failure(exc):
                self.result_label.setText("Not recovered: the recovery could not be completed. "
                                          "Review Suspension controls again.")
                self._technical_error(f"Recover from this restore: {type(exc).__name__}")
        except Exception as exc:          # TR1-01: never a silent failure in the slot
            self.result_label.setText("Not recovered: the recovery could not be completed. "
                                      "Review Suspension controls again.")
            self._technical_error(f"Recover from this restore: {type(exc).__name__}")
        else:
            link = ("" if outcome.lineage_written else
                    " This copy has no confirmed link to its restore, so it can't use the Wire seat.")
            self.result_label.setText(
                "Safety protection is running again with a new identity. Your own work and every "
                "channel start suspended: review and resume each one. Full recovery is not proven "
                "yet: also re-pair Chrome Companion, enter credentials again (they are not in an "
                "Agent Backup), and decide about the Wire seat." + link)
        del pin
        for box in (self.archive_ack_runtimes, self.archive_ack_prior, self.archive_ack_no_link):
            box.setChecked(False)
        self.refresh()

    def _resume(self) -> None:
        view = self._selected()
        if view is None:
            return
        try:
            outcome = R.resume(view, ingress=operator_ingress.local_suspension_control("resume"),
                               ack_unknown=self.ack_unknown.isChecked())
        except (S.SuspensionError, R.SuspensionRefused) as exc:
            self.result_label.setText(f"Not resumed: {exc}")
            self.refresh()
            return
        result = outcome.result
        if result is S.ResumeResult.RESUMED:
            text = f"Resumed {view.scope}. Work from before the suspension stays stopped."
        elif result is S.ResumeResult.DRAIN_PENDING:
            text = (f"Not yet: {len(outcome.live_leases)} earlier dispatch(es) are still running. "
                    "Try again when they finish.")
        elif result is S.ResumeResult.UNKNOWN_NEEDS_ACK:
            text = ("Not yet: some earlier work's process stopped, so its outcome is unknown. "
                    "Tick the acknowledgement and resume again.")
        else:
            text = f"Not resumed ({result.value}). The incident may have changed; review it again."
        self.ack_unknown.setChecked(False)
        self.result_label.setText(text)
        self.refresh()

    def _release_wire_seat(self) -> None:
        verdict = self._wire_verdict
        if verdict is None or verdict.reason != G.RECOVERED_UNRELEASED or not self.wire_ack.isChecked():
            return
        shown = G.acknowledgement_sha256(self.wire_body_label.text(), self.wire_ack_label.text(),
                                         self.wire_ack.text())
        try:
            R.release_wire_seat(
                ingress=operator_ingress.local_suspension_control("wire_seat_release"),
                expected=tuple((rid, sha) for rid, sha, _at in verdict.unreleased),
                acknowledged_source_unverified=self.wire_ack.isChecked(),
                acknowledgement_sha256=shown)
        except R.WireReleaseRefused as exc:
            self.result_label.setText(f"Wire seat not allowed: {exc.text}")
        except Exception as exc:
            self.result_label.setText("Wire seat not allowed: the release could not be completed. "
                                      "The Wire stays locked.")
            self._technical_error(f"Wire seat release: {type(exc).__name__}")
        else:
            self.result_label.setText(G.RELEASE_DONE_TEXT)
        self.wire_ack.setChecked(False)
        self.refresh()

    def _acknowledge_unknown_scopes(self) -> None:
        try:
            R.acknowledge_unknown_scopes(
                ingress=operator_ingress.local_suspension_control("acknowledge_unknown_scopes"))
            self.result_label.setText("Channels first seen from now on start unsuspended.")
        except S.SuspensionError as exc:
            self.result_label.setText(f"Not changed: {exc}")
        self.refresh()
