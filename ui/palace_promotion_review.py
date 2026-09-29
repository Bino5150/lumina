"""Trusted Qt owner review of ONE Palace promotion (PALACE-GUARD-01B-4).

Reachable only from Settings > Tools > Pending Actions. The Pending Actions
entry that leads here is a REQUEST -- forgeable JSON -- so nothing on this dialog
comes from it except the pointer (kind, id, destination layer). The text the
owner approves is rendered from a LIVE database read taken when the dialog is
built, and the Approve click is the one and only place an approval is minted:

    click -> mint_owner_promotion_approval(snapshot) -> promote_with_owner_approval()

The service then re-reads the record inside its own write transaction and refuses
if anything the owner was shown has changed. Programmatic accept()/Enter is never
an approval (same rule as ui/site_action_review.py); Cancel is the default
button. No model, tool, queue entry, audit line or piece of Palace text can
click this button.
"""
import uuid

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

from core.palace_promotion import (
    PromotionRefused, describe_refusal, mint_owner_promotion_approval,
    promote_with_owner_approval, render_review_text,
)


class PalacePromotionReviewDialog(QDialog):
    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Palace promotion")
        self.setModal(True)
        self.resize(720, 520)
        self.snapshot = snapshot
        self._rendered = render_review_text(snapshot)
        self._decided = False
        self.result_receipt = None
        self.result_error = None

        # Plain text only: the record's own text is model-authored data.
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setPlainText(self._rendered)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(self)
        self.approve_btn = self.buttons.addButton("Approve promotion", QDialogButtonBox.AcceptRole)
        self.cancel_btn = self.buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self.approve_btn.setEnabled(not snapshot.blocked_reason)
        self.approve_btn.setAutoDefault(False)
        self.approve_btn.setDefault(False)
        self.cancel_btn.setAutoDefault(True)
        self.cancel_btn.setDefault(True)
        self.approve_btn.clicked.connect(self._approve_clicked)
        self.cancel_btn.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

    def _approve_clicked(self):
        if self._decided:
            return
        self._decided = True
        self.approve_btn.setEnabled(False)  # one click, one attempt
        try:
            if not self.isVisible() or self.text.toPlainText() != self._rendered:
                raise PromotionRefused("review_not_presented")
            approval = mint_owner_promotion_approval(
                self.snapshot,
                owner_event=f"settings.pending_actions.palace_promotion_review:{uuid.uuid4().hex}",
            )
            self.result_receipt = promote_with_owner_approval(approval)
        except Exception as e:  # PromotionRefused, or a database error: fail closed, tell the owner
            self.result_error = e
            self.status.setText(describe_refusal(e))
            self.cancel_btn.setText("Close")
            return
        super().accept()

    def accept(self):
        # Programmatic accept()/Enter is not an approval mechanism.
        self.reject()

    def reject(self):
        self._decided = True
        super().reject()
