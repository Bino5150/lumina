"""WIRE-WAKE-01 W1 — passive Wire wake/task inbox panel for the Lumina GUI (R2).

A display surface, not an actuator. It renders:

- Wire connection state (listening / disconnected / offline / off),
- the GUI session's identity, generation and lease liveness,
- wake/task rows with full sender provenance and an explicit
  ``peer / owner=false`` authority marker on each row,
- the runaway-suspension and emergency-stop admission truth next to task
  state, so the owner sees the whole admission picture at a glance.

Interaction is limited to three owner controls, each behind an explicit
confirmation dialog stating exactly what the Wire will record:

- **Ack** — records the adapter receipt ("this GUI received the intent").
  Never implies a model resumed or work ran.
- **Claim** — an explicit owner click that coordination-claims the wake's
  task through the W0 op (server-side lease/task/exactly-once fencing). It
  does NOT execute work; the dialog says so verbatim. After a claim the row
  shows the task's title/scope from the relay's own response and offers
  **Release**.
- **Release** — the W0 protocol's only unclaim mechanism is a terminal
  completion: the dialog states verbatim that releasing records
  ``outcome=failed`` with the note ``released by owner without execution``
  and is permanent.

R2 repairs (Sonnet review of 75d2b98): rows update incrementally in place
(no O(N²) full rebuild), rendered rows are capped (newest
:data:`MAX_RENDERED_ROWS` plus an honest "+N more" marker), active rows
sort newest-first, terminal rows sink, and dead code is gone.

INJECTION SAFETY: every string arriving over the Wire is untrusted
coordination data. All Wire-sourced text is rendered with
``Qt.TextFormat.PlainText`` explicitly — note bodies that look like markup,
HTML or instructions are inert pixels, and no code path connects display to
execution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

__all__ = ["WireWakePanel", "MAX_RENDERED_ROWS"]

#: R2/H3: rendered-row cap. Older rows drop off the widget (counted honestly
#: in the "+N more" marker) but stay in the model for state truth.
MAX_RENDERED_ROWS = 50

_STATE_COLORS = {
    "pending": "#ffa502",      # warning: awaiting receipt
    "delivered": "#00b4cc",    # tool_text: receipted, unclaimed
    "claimed": "#2ed573",      # success: coordination claim held
    "completed": "#2ed573",
    "failed": "#ff4757",       # danger
    "expired": "#6b7280",      # muted
    "cancelled": "#6b7280",
    "ambiguous": "#ffa502",
}

_STATE_LABELS = {
    "pending": "PENDING",
    "delivered": "RECEIPTED",
    "claimed": "CLAIMED",
    "completed": "COMPLETED",
    "failed": "FAILED",
    "expired": "EXPIRED",
    "cancelled": "CANCELLED",
    "ambiguous": "AMBIGUOUS",
}

_TERMINAL_STATES = ("completed", "failed", "expired", "cancelled")

_CONNECTION_LABELS = {
    "connecting": "connecting…",
    "listening": "connected",
    "disconnected": "disconnected — retrying",
    "offline": "offline",
    "off": "off",
}


def _fmt_time(value: Any) -> str:
    """Compact local-time rendering of an ISO-8601 Z timestamp; raw on parse failure."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return value


class _WakeRow(QFrame):
    """One wake/task row. Pure display + owner buttons behind confirmations."""

    ack_requested = Signal(str)
    claim_requested = Signal(str)
    release_requested = Signal(str, str)  # (wake_id, task_ref)

    def __init__(self, wake: dict[str, Any], colors: dict[str, str], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._wake_id = str(wake.get("wake_id", ""))
        self._state = str(wake.get("state", "pending"))
        self._task_ref = str(wake.get("task_ref", "?"))
        self._task: dict[str, Any] | None = None

        self.setObjectName("wireWakeRow")
        self.setStyleSheet(
            f"#wireWakeRow {{ background: {colors['bg_card']}; "
            f"border: 1px solid {colors['border']}; border-radius: 6px; }}"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 6, 8, 6)
        self._layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._state_badge = QLabel()
        self._state_badge.setTextFormat(Qt.TextFormat.PlainText)
        self._state_badge.setStyleSheet(
            f"color: {_STATE_COLORS.get(self._state, colors['text_primary'])}; "
            f"font-weight: bold; font-size: 11px;"
        )
        top.addWidget(self._state_badge)
        self._task_label = QLabel()
        self._task_label.setTextFormat(Qt.TextFormat.PlainText)
        self._task_label.setStyleSheet(
            f"color: {colors['text_primary']}; font-weight: bold;"
        )
        top.addWidget(self._task_label, 1)
        self._created_label = QLabel(_fmt_time(wake.get("created_at")))
        self._created_label.setTextFormat(Qt.TextFormat.PlainText)
        self._created_label.setStyleSheet(
            f"color: {colors['text_muted']}; font-size: 11px;"
        )
        top.addWidget(self._created_label)
        self._layout.addLayout(top)

        self._provenance_label = QLabel()
        self._provenance_label.setTextFormat(Qt.TextFormat.PlainText)
        self._provenance_label.setStyleSheet(
            f"color: {colors['text_muted']}; font-size: 11px;"
        )
        self._layout.addWidget(self._provenance_label)

        self._note_label = QLabel()
        self._note_label.setTextFormat(Qt.TextFormat.PlainText)
        self._note_label.setWordWrap(True)
        self._note_label.setStyleSheet(f"color: {colors['text_dim']}; font-size: 11px;")
        self._note_label.hide()
        self._layout.addWidget(self._note_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self._ack_btn = QPushButton("Ack")
        self._ack_btn.setToolTip(
            "Record this GUI's receipt of the wake intent. "
            "Does NOT resume a model or execute anything."
        )
        self._claim_btn = QPushButton("Claim…")
        self._claim_btn.setToolTip(
            "Owner-only coordination claim of this wake's task (exactly-once, "
            "server-side fencing). Shows a confirmation first. Does NOT execute work."
        )
        self._release_btn = QPushButton("Release…")
        self._release_btn.setToolTip(
            "Release this claimed task: records outcome=failed with the note "
            "'released by owner without execution'. Permanent."
        )
        self._release_btn.hide()
        for btn in (self._ack_btn, self._claim_btn, self._release_btn):
            btn.setStyleSheet(
                f"QPushButton {{ background: {colors['bg_input']}; "
                f"color: {colors['text_primary']}; border: 1px solid {colors['border']}; "
                f"border-radius: 4px; padding: 2px 10px; font-size: 11px; }}"
                f"QPushButton:disabled {{ color: {colors['text_dim']}; }}"
            )
        self._ack_btn.clicked.connect(lambda: self.ack_requested.emit(self._wake_id))
        self._claim_btn.clicked.connect(self._on_claim_clicked)
        self._release_btn.clicked.connect(self._on_release_clicked)
        buttons.addWidget(self._ack_btn)
        buttons.addWidget(self._claim_btn)
        buttons.addWidget(self._release_btn)
        self._layout.addLayout(buttons)

        self.update_wake(wake)

    # ── state ─────────────────────────────────────────────────────────────

    @property
    def wake_id(self) -> str:
        return self._wake_id

    @property
    def state(self) -> str:
        return self._state

    def update_wake(self, wake: dict[str, Any]) -> None:
        """Update this row in place (R2/H3: no rebuild)."""
        self._state = str(wake.get("state", self._state))
        self._task_ref = str(wake.get("task_ref", self._task_ref))
        self._state_badge.setText(_STATE_LABELS.get(self._state, self._state.upper()))
        self._state_badge.setStyleSheet(
            f"color: {_STATE_COLORS.get(self._state, self._colors['text_primary'])}; "
            f"font-weight: bold; font-size: 11px;"
        )
        title = None
        scope = None
        if isinstance(self._task, dict):
            title = self._task.get("title")
            scope = self._task.get("scope")
        if title:
            self._task_label.setText(f"task {self._task_ref} — {title}")
        else:
            self._task_label.setText(f"task {self._task_ref}")
        if scope:
            self._task_label.setToolTip(f"scope: {scope}")
        self._created_label.setText(_fmt_time(wake.get("created_at")))
        self._provenance_label.setText(
            "from {seat} · peer · owner=false · attempts {attempts}".format(
                seat=wake.get("requested_by", "?"),
                attempts=wake.get("attempts", 0),
            )
        )
        note = wake.get("last_error") or ""
        if note:
            self._note_label.setText(f"note: {note}")
            self._note_label.setVisible(True)
        else:
            self._note_label.setVisible(False)
        actionable = self._state in ("pending", "delivered")
        self._ack_btn.setEnabled(actionable)
        self._claim_btn.setVisible(actionable)
        self._release_btn.setVisible(self._state == "claimed")

    def set_task(self, task: dict[str, Any]) -> None:
        """Attach the relay's task record (from the claim response)."""
        self._task = task if isinstance(task, dict) else None
        self._refresh_title_only()

    def _refresh_title_only(self) -> None:
        title = self._task.get("title") if isinstance(self._task, dict) else None
        scope = self._task.get("scope") if isinstance(self._task, dict) else None
        if title:
            self._task_label.setText(f"task {self._task_ref} — {title}")
        if scope:
            self._task_label.setToolTip(f"scope: {scope}")

    # ── owner actions (each behind an explicit confirmation) ──────────────

    def _on_claim_clicked(self) -> None:
        if not self._confirm_claim():
            return
        self.claim_requested.emit(self._wake_id)

    def _confirm_claim(self) -> bool:
        """Confirmation gate for Claim (monkeypatchable in tests)."""
        task_bit = f"task {self._task_ref}"
        if isinstance(self._task, dict) and self._task.get("title"):
            task_bit += f" — {self._task.get('title')}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Claim Wire task?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            f"Claim {task_bit} for this Lumina session?\n\n"
            "• Lumina becomes the EXCLUSIVE claimant on the Wire.\n"
            "• No work will start — claiming is coordination only.\n"
            "• The relay enforces lease/task fencing; a stale claim is refused."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _on_release_clicked(self) -> None:
        if not self._confirm_release():
            return
        self.release_requested.emit(self._wake_id, self._task_ref)

    def _confirm_release(self) -> bool:
        """Confirmation gate for Release (monkeypatchable in tests)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Release claimed task?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            f"Release task {self._task_ref}?\n\n"
            "The Wire protocol's only unclaim mechanism records the task as\n"
            "FAILED with the note 'released by owner without execution'.\n"
            "This is PERMANENT — the task cannot be reclaimed afterwards."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes


class WireWakePanel(QWidget):
    """Compact wake inbox strip; expands to the bounded row list."""

    ack_requested = Signal(str)
    claim_requested = Signal(str)
    release_requested = Signal(str, str)  # (wake_id, task_ref)

    def __init__(self, colors: dict[str, str], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._expanded = False
        self._wakes: dict[str, dict[str, Any]] = {}
        self._rows: dict[str, _WakeRow] = {}
        self._order: list[str] = []  # display order, newest-active first
        self._hidden_count = 0
        self._session: dict[str, Any] | None = None
        self._connection = "connecting"
        self._admission_note = ""

        self.setStyleSheet(
            f"WireWakePanel {{ background: {colors['bg_panel']}; "
            f"border-top: 1px solid {colors['border']}; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 3, 6, 3)
        outer.setSpacing(3)

        strip = QHBoxLayout()
        strip.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setTextFormat(Qt.TextFormat.PlainText)
        self._dot.setFixedWidth(12)
        strip.addWidget(self._dot)
        self._title = QLabel("Wire")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setStyleSheet(
            f"color: {colors['text_primary']}; font-weight: bold; font-size: 11px;"
        )
        strip.addWidget(self._title)
        self._summary = QLabel("starting…")
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        self._summary.setStyleSheet(f"color: {colors['text_muted']}; font-size: 11px;")
        strip.addWidget(self._summary, 1)
        self._toggle_btn = QPushButton("▾")
        self._toggle_btn.setFixedWidth(24)
        self._toggle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {colors['text_muted']}; "
            f"border: none; font-size: 12px; }}"
        )
        self._toggle_btn.clicked.connect(self._toggle_expanded)
        strip.addWidget(self._toggle_btn)
        outer.addLayout(strip)

        self._detail = QScrollArea()
        self._detail.setWidgetResizable(True)
        self._detail.setFrameShape(QFrame.Shape.NoFrame)
        self._detail.setVisible(False)
        self._detail.setMaximumHeight(220)
        detail_host = QWidget()
        self._detail_layout = QVBoxLayout(detail_host)
        self._detail_layout.setContentsMargins(2, 2, 2, 2)
        self._detail_layout.setSpacing(4)
        self._more_label = QLabel("")
        self._more_label.setTextFormat(Qt.TextFormat.PlainText)
        self._more_label.setStyleSheet(
            f"color: {colors['text_muted']}; font-size: 11px;"
        )
        self._more_label.hide()
        self._detail_layout.addWidget(self._more_label)
        self._detail_layout.addStretch(1)
        self._detail.setWidget(detail_host)
        outer.addWidget(self._detail)

        self._refresh_strip()

    # ── public update API (called from GUI-thread slots) ──────────────────

    def set_connection_state(self, state: str) -> None:
        self._connection = state
        dot_color = {
            "listening": self._colors["success"],
            "connecting": self._colors["warning"],
            "disconnected": self._colors["danger"],
            "offline": self._colors["text_dim"],
            "off": self._colors["text_dim"],
        }.get(state, self._colors["text_muted"])
        self._dot.setStyleSheet(f"color: {dot_color};")
        self._refresh_strip()

    def set_session(self, session: dict[str, Any] | None) -> None:
        self._session = session
        self._refresh_strip()

    def apply_wake_event(self, event: dict[str, Any]) -> None:
        """Merge one listener event into the inbox model (R2/H3: incremental)."""
        if event.get("truncated"):
            self._more_label.setText(
                f"⚠ catch-up truncated: {event.get('note', 'backlog too large')}"
            )
            self._more_label.setVisible(True)
            return
        wake = event.get("wake")
        if not isinstance(wake, dict):
            return
        wake_id = wake.get("wake_id")
        if not isinstance(wake_id, str) or not wake_id:
            return
        known = self._wakes.get(wake_id)
        if known is not None and known.get("state") in _TERMINAL_STATES:
            # Terminal states never regress from a replayed event.
            return
        was_state = known.get("state") if known else None
        self._wakes[wake_id] = wake
        row = self._rows.get(wake_id)
        if row is not None:
            row.update_wake(wake)
            if row.state in _TERMINAL_STATES and was_state not in _TERMINAL_STATES:
                self._sink_row(wake_id)
        else:
            self._insert_row(wake_id, wake)
        self._refresh_strip()

    def apply_action_result(self, result: dict[str, Any]) -> None:
        """Apply an ack/claim/release outcome: update the row or surface the error."""
        wake_id = str(result.get("wake_id", ""))
        if result.get("ok"):
            wake = result.get("wake")
            if isinstance(wake, dict) and wake_id in self._wakes:
                self._wakes[wake_id] = wake
                row = self._rows.get(wake_id)
                if row is not None:
                    row.update_wake(wake)
                    if row.state in _TERMINAL_STATES:
                        self._sink_row(wake_id)
            task = result.get("task")
            row = self._rows.get(wake_id)
            if isinstance(task, dict) and row is not None:
                row.set_task(task)
        else:
            error = str(result.get("error", "unknown error"))
            row = self._rows.get(wake_id)
            if row is not None:
                record = dict(self._wakes.get(wake_id, {}))
                record["last_error"] = f"action refused: {error}"
                self._wakes[wake_id] = record
                row.update_wake(record)
            else:
                self._more_label.setText(f"⚠ action refused: {error}")
                self._more_label.setVisible(True)

    def set_admission_note(self, note: str) -> None:
        """Display the current suspension/E-stop admission truth."""
        self._admission_note = note
        self._refresh_strip()

    # ── internals ─────────────────────────────────────────────────────────

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)
        self._toggle_btn.setText("▴" if self._expanded else "▾")

    def _pending_count(self) -> int:
        return sum(
            1 for w in self._wakes.values() if w.get("state") in ("pending", "delivered")
        )

    def _refresh_strip(self) -> None:
        conn = _CONNECTION_LABELS.get(self._connection, self._connection)
        session_bit = ""
        if isinstance(self._session, dict):
            lease = "lease✓" if self._session.get("lease_live") else "lease✗"
            session_bit = (
                f" · session {str(self._session.get('session_id', ''))[:8]}"
                f" gen{self._session.get('generation', '?')} {lease}"
            )
        pending = self._pending_count()
        pending_bit = f" · {pending} pending" if pending else ""
        admission = f" · {self._admission_note}" if self._admission_note else ""
        self._summary.setText(f"{conn}{session_bit}{pending_bit}{admission}")
        self._summary.setTextFormat(Qt.TextFormat.PlainText)

    def _insert_row(self, wake_id: str, wake: dict[str, Any]) -> None:
        row = _WakeRow(wake, self._colors)
        row.ack_requested.connect(self.ack_requested)
        row.claim_requested.connect(self.claim_requested)
        row.release_requested.connect(self.release_requested)
        self._rows[wake_id] = row
        if wake.get("state") in _TERMINAL_STATES:
            self._detail_layout.insertWidget(self._detail_layout.count() - 1, row)
        else:
            # Active rows enter at the top (below the "+N more" marker).
            self._detail_layout.insertWidget(1, row)
        # _order is pure insertion order — the cap evicts oldest-inserted.
        self._order.append(wake_id)
        self._enforce_cap()

    def _sink_row(self, wake_id: str) -> None:
        """Move a terminal row to the bottom of the list (one-time, O(1))."""
        row = self._rows.get(wake_id)
        if row is None:
            return
        self._detail_layout.removeWidget(row)
        self._detail_layout.insertWidget(self._detail_layout.count() - 1, row)
        if wake_id in self._order:
            self._order.remove(wake_id)
        self._order.append(wake_id)

    def _enforce_cap(self) -> None:
        """Keep at most MAX_RENDERED_ROWS row widgets; count the rest honestly."""
        rendered = [wid for wid in self._order if wid in self._rows]
        excess = len(rendered) - MAX_RENDERED_ROWS
        while excess > 0:
            oldest = rendered.pop(0)
            row = self._rows.pop(oldest, None)
            if row is not None:
                row.setParent(None)
                row.deleteLater()
            excess -= 1
        hidden = len(self._wakes) - len(self._rows)
        if hidden > 0:
            self._more_label.setText(f"+ {hidden} older wake(s) not rendered")
            self._more_label.setVisible(True)
        elif not self._more_label.text().startswith("⚠"):
            self._more_label.setVisible(False)
