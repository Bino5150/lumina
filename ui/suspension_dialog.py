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

The selector shown here is for this desktop display (and, from Slice C3,
the owner's verified Telegram /resume). It is never logged.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core import operator_ingress, suspension as S, suspension_runtime as R


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
        guide = QLabel('<a href="' + (Path(__file__).resolve().parents[1] / "docs/upgrading.md").as_uri()
                       + '">Upgrade and troubleshooting guide</a> (docs/upgrading.md)')
        guide.setWordWrap(True)
        guide.setOpenExternalLinks(True)
        content.addWidget(guide)
        self.result_label = _label()
        content.addWidget(self.result_label)

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
        local = f" Stopped in this process (unconfirmed): {', '.join(status.local_stops)}." \
            if status.local_stops else ""
        self.state_label.setText(head + local)
        self.incidents.setVisible(bool(self._views))
        self.detail_label.setVisible(bool(self._views))
        if not self._views:
            self.detail_label.setText("")
        self._sync_buttons()
        self._fit_setup()

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
        self.resume_btn.setVisible(status.state is R.State.ACTIVE)
        self.resume_btn.setEnabled(bool(self._views) and self.incidents.currentRow() >= 0)
        self.ack_unknown_group.setVisible(status.state is R.State.ACTIVE and bool(self._views))
        self.ack_scopes_btn.setVisible(status.state is R.State.ACTIVE and any(
            v.trigger == S.RECOVERY_TRIGGER for v in self._views))
        self.result_label.setVisible(bool(self.result_label.text()))

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
            self.result_label.setText(f"Not initialized: {exc}")
        self.first_run_confirm.setChecked(False)
        self.refresh()

    def _repair(self) -> None:
        try:
            R.repair_identity(ingress=operator_ingress.local_suspension_control("repair_identity"))
            self.result_label.setText("Identity repaired; every suspension was kept.")
        except S.SuspensionError as exc:
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

    def _acknowledge_unknown_scopes(self) -> None:
        try:
            R.acknowledge_unknown_scopes(
                ingress=operator_ingress.local_suspension_control("acknowledge_unknown_scopes"))
            self.result_label.setText("Channels first seen from now on start unsuspended.")
        except S.SuspensionError as exc:
            self.result_label.setText(f"Not changed: {exc}")
        self.refresh()
