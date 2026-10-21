"""ui/suspension_dialog.py -- the desktop's runaway-suspension controls
(AGENT-RUNAWAY-SUSPEND-01, Gatling Slice C1b).

The one place a suspension store is initialized, repaired, recovered or
resumed. Every button mints a fresh local suspension-control envelope at the
moment of the click (core.operator_ingress.local_suspension_control); no
text, Telegram message or model output can reach these controls.

  * No store yet: the owner chooses Initialize (first launch with runaway
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

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)

from core import operator_ingress, suspension as S, suspension_runtime as R


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


class SuspensionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Runaway suspension")
        self.setMinimumWidth(560)
        self._views = ()
        self._blocked_runtimes = ()

        layout = QVBoxLayout(self)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.incidents = QListWidget()
        self.incidents.currentRowChanged.connect(self._show_incident)
        layout.addWidget(self.incidents)
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.first_run_confirm = QCheckBox(
            "This is the first launch with runaway suspension for this data folder")
        self.first_run_confirm.toggled.connect(lambda on: self._exclusive(self.first_run_confirm, on))
        layout.addWidget(self.first_run_confirm)
        self.unknown_prior_state = QCheckBox(
            "I can't confirm this is the first launch. Earlier suspensions, other Lumina processes "
            "and the outcome of earlier work are unknown; start with my work and every channel "
            "suspended for review")
        self.unknown_prior_state.toggled.connect(
            lambda on: self._exclusive(self.unknown_prior_state, on))
        layout.addWidget(self.unknown_prior_state)
        self.ack_unknown = QCheckBox(
            "I accept that work whose process stopped may or may not have finished (outcome unknown)")
        layout.addWidget(self.ack_unknown)
        self.ack_runtimes = QCheckBox(
            "Other Lumina processes listed above are stopped or may be ignored")
        layout.addWidget(self.ack_runtimes)
        self.unknown_scopes_clear = QCheckBox(
            "After recovery, let channels whose state can't be read start unsuspended")
        layout.addWidget(self.unknown_scopes_clear)

        buttons = QHBoxLayout()
        self.initialize_btn = QPushButton("Initialize")
        self.initialize_btn.clicked.connect(self._initialize)
        self.repair_btn = QPushButton("Repair identity")
        self.repair_btn.clicked.connect(self._repair)
        self.recover_btn = QPushButton("Recover…")
        self.recover_btn.clicked.connect(self._recover)
        self.resume_btn = QPushButton("Resume selected")
        self.resume_btn.clicked.connect(self._resume)
        self.ack_scopes_btn = QPushButton("Unknown channels may start unsuspended")
        self.ack_scopes_btn.clicked.connect(self._acknowledge_unknown_scopes)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        for button in (self.initialize_btn, self.repair_btn, self.recover_btn, self.resume_btn,
                       self.ack_scopes_btn, self.close_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.refresh()

    # -- state ---------------------------------------------------------------

    def refresh(self) -> None:
        status = R.status()
        self._status = status
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
                head = ("No runaway-suspension store exists for this data folder, so nothing is "
                        "admitted yet. A missing store can't be told apart from a deleted one. "
                        "If this is the first launch with runaway suspension here, confirm and "
                        "choose Initialize. If Lumina has run with it here before, or you're "
                        "not sure, tick the acknowledgement and choose Recover: no earlier "
                        "records survive, so nothing is restored. It starts a new record with "
                        "your work and every channel suspended so you can review before resuming.")
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
        if not self._views:
            self.detail_label.setText("")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        status = self._status
        absent_clean = self._absent_clean()
        if not absent_clean:
            for box in (self.first_run_confirm, self.unknown_prior_state):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        self.first_run_confirm.setVisible(absent_clean)
        self.unknown_prior_state.setVisible(absent_clean)
        self.initialize_btn.setVisible(absent_clean)
        self.initialize_btn.setEnabled(absent_clean and self.first_run_confirm.isChecked())
        self.repair_btn.setVisible(status.state is R.State.FAULT and status.fault in (
            S.Fault.IDENTITY_MISSING, S.Fault.IDENTITY_INVALID))
        recovering = status.state in (R.State.UNINITIALIZED, R.State.FAULT)
        self.recover_btn.setVisible(recovering)
        self.recover_btn.setEnabled(not absent_clean or self.unknown_prior_state.isChecked())
        # With no trace there is no old registry or scope table to acknowledge:
        # the no-trace acknowledgement covers both, and channels always start
        # suspended.
        self.ack_runtimes.setVisible(recovering and not absent_clean)
        self.unknown_scopes_clear.setVisible(recovering and not absent_clean)
        self.resume_btn.setVisible(status.state is R.State.ACTIVE)
        self.resume_btn.setEnabled(bool(self._views) and self.incidents.currentRow() >= 0)
        self.ack_unknown.setVisible(status.state is R.State.ACTIVE and bool(self._views))
        self.ack_scopes_btn.setVisible(status.state is R.State.ACTIVE and any(
            v.trigger == S.RECOVERY_TRIGGER for v in self._views))

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
        if not self.first_run_confirm.isChecked():
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
        if no_trace and not self.unknown_prior_state.isChecked():
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
