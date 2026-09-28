"""Trusted Qt review of an immutable site-action snapshot. No chat authority."""
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

from chrome_companion.site_actions.model import ContractError, canonical_json


def render_snapshot(snapshot):
    """One literal, complete representation; no rich text or URL activation."""
    return canonical_json({
        "runtime": snapshot.specification.runtime.value,
        "capability": snapshot.specification.manifest.capability_id,
        "consequence": snapshot.specification.consequence.value,
        "principal": snapshot.principal,
        "principal_assurance": "machine_verified" if snapshot.principal is not None else "unavailable",
        "specification": snapshot.specification.to_data(),
        "scope": snapshot.scope_digest, "boot": snapshot.boot_digest,
        "surface": snapshot.surface_digest, "control_epoch": snapshot.control_epoch,
        "parent_event": snapshot.parent_event_digest, "expires_ns": snapshot.expires_ns,
    }).decode("utf-8")


class SiteActionReviewDialog(QDialog):
    approved = Signal(object)

    def __init__(self, broker, delivery, presenter, current_scope, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Companion action")
        self.setModal(True)
        self.resize(680, 480)
        self._broker = broker
        self._delivery = delivery
        self._presenter = presenter
        self._current_scope = current_scope
        self._snapshot = broker.review_snapshot(delivery, presenter)
        self._rendered = render_snapshot(self._snapshot)
        self._presented = False
        self._decided = False
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setPlainText(self._rendered)
        self.buttons = QDialogButtonBox(self)
        self.approve = self.buttons.addButton("Approve", QDialogButtonBox.AcceptRole)
        self.cancel = self.buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self.approve.setEnabled(False)
        self.approve.clicked.connect(self._approve_clicked)
        self.cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addWidget(self.buttons)

    def _current(self):
        if (self._decided or not self.isVisible() or not self._current_scope()
                or self.text.toPlainText() != self._rendered
                or self._broker.review_snapshot(self._delivery, self._presenter) is not self._snapshot):
            raise ContractError("hidden/stale/changed review")

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._acknowledge_presentation)

    def _acknowledge_presentation(self):
        try:
            self._current()
            self._broker.present_review(self._delivery, self._presenter, self._snapshot.fingerprint)
            self._presented = True
            self.approve.setEnabled(True)
        except ContractError:
            self.reject()

    def _approve_clicked(self):
        try:
            self._current()
            if not self._presented:
                raise ContractError("review not successfully presented")
            approval = self._broker.approve_review(self._delivery, self._presenter, self._snapshot.fingerprint)
        except ContractError:
            self.reject()
            return
        self._decided = True
        self.approve.setEnabled(False)
        self.approved.emit(approval)
        super().accept()

    def reject(self):
        if not self._decided:
            self._decided = True
            self._broker.cancel_review(self._delivery, self._presenter)
        super().reject()

    def hideEvent(self, event):
        # A hidden dialog cannot be re-presented as its old approval surface.
        if not self._decided:
            self._decided = True
            self._broker.cancel_review(self._delivery, self._presenter)
        super().hideEvent(event)

    def accept(self):
        # Programmatic accept/Enter is not a second approval mechanism.
        self.reject()
