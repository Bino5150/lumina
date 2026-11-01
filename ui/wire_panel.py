"""WIRE-WAKE-01 W1 — passive Wire wake/task inbox panel for the Lumina GUI.

A display surface, not an actuator. It renders:

- Wire connection state (listening / disconnected / offline),
- the GUI session's identity, generation and lease liveness,
- every wake/task row the listener surfaces, with full sender provenance and
  an explicit ``peer / owner=false`` authority marker on each row,
- the runaway-suspension and emergency-stop state next to task state, so the
  owner sees the whole admission truth at a glance.

Interaction is limited to two owner buttons per pending row:

- **Ack** — records the adapter receipt ("this GUI received the intent").
  Never implies a model resumed or work ran.
- **Claim** — an explicit owner click that coordination-claims the wake's
  task through the W0 op (server-side lease/task/exactly-once fencing). It
  does NOT execute work; actual work happens only through the owner's
  existing channels with the full admission stack.

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
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

__all__ = ["WireWakePanel"]

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

_CONNECTION_LABELS = {
    "connecting": "connecting…",
    "listening": "connected",
    "disconnected": "disconnected — retrying",
    "offline": "offline",
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
    """One wake/task row. Pure display + two owner buttons."""

    ack_requested = Signal(str)
    claim_requested = Signal(str)

    def __init__(self, wake: dict[str, Any], colors: dict[str, str], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._wake_id = str(wake.get("wake_id", ""))
        self._state = str(wake.get("state", "pending"))

        self.setObjectName("wireWakeRow")
        self.setStyleSheet(
            f"#wireWakeRow {{ background: {colors['bg_card']}; "
            f"border: 1px solid {colors['border']}; border-radius: 6px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        state_badge = QLabel(_STATE_LABELS.get(self._state, self._state.upper()))
        state_badge.setTextFormat(Qt.TextFormat.PlainText)
        state_badge.setStyleSheet(
            f"color: {_STATE_COLORS.get(self._state, colors['text_primary'])}; "
            f"font-weight: bold; font-size: 11px;"
        )
        top.addWidget(state_badge)
        task_label = QLabel(f"task {wake.get('task_ref', '?')}")
        task_label.setTextFormat(Qt.TextFormat.PlainText)
        task_label.setStyleSheet(
            f"color: {colors['text_primary']}; font-weight: bold;"
        )
        top.addWidget(task_label, 1)
        created = QLabel(_fmt_time(wake.get("created_at")))
        created.setTextFormat(Qt.TextFormat.PlainText)
        created.setStyleSheet(f"color: {colors['text_muted']}; font-size: 11px;")
        top.addWidget(created)
        layout.addLayout(top)

        provenance = QLabel(
            "from {seat} · peer · owner=false · attempts {attempts}".format(
                seat=wake.get("requested_by", "?"),
                attempts=wake.get("attempts", 0),
            )
        )
        provenance.setTextFormat(Qt.TextFormat.PlainText)
        provenance.setStyleSheet(f"color: {colors['text_muted']}; font-size: 11px;")
        layout.addWidget(provenance)

        note = wake.get("last_error") or ""
        if note:
            note_label = QLabel(f"note: {note}")
            note_label.setTextFormat(Qt.TextFormat.PlainText)
            note_label.setWordWrap(True)
            note_label.setStyleSheet(f"color: {colors['text_dim']}; font-size: 11px;")
            layout.addWidget(note_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addStretch(1)
        actionable = self._state in ("pending", "delivered")
        self._ack_btn = QPushButton("Ack")
        self._ack_btn.setToolTip(
            "Record this GUI's receipt of the wake intent. "
            "Does NOT resume a model or execute anything."
        )
        self._ack_btn.setEnabled(actionable)
        self._claim_btn = QPushButton("Claim")
        self._claim_btn.setToolTip(
            "Owner-only coordination claim of this wake's task (exactly-once, "
            "server-side fencing). Does NOT execute work."
        )
        self._claim_btn.setEnabled(self._state == "pending" or self._state == "delivered")
        for btn in (self._ack_btn, self._claim_btn):
            btn.setStyleSheet(
                f"QPushButton {{ background: {colors['bg_input']}; "
                f"color: {colors['text_primary']}; border: 1px solid {colors['border']}; "
                f"border-radius: 4px; padding: 2px 10px; font-size: 11px; }}"
                f"QPushButton:disabled {{ color: {colors['text_dim']}; }}"
            )
        self._ack_btn.clicked.connect(lambda: self.ack_requested.emit(self._wake_id))
        self._claim_btn.clicked.connect(lambda: self.claim_requested.emit(self._wake_id))
        buttons.addWidget(self._ack_btn)
        buttons.addWidget(self._claim_btn)
        layout.addLayout(buttons)

    def update_wake(self, wake: dict[str, Any]) -> None:
        """Replace this row's content in place (same wake_id)."""
        new_state = str(wake.get("state", self._state))
        if new_state != self._state:
            self._state = new_state
            self._ack_btn.setEnabled(new_state in ("pending", "delivered"))
            self._claim_btn.setEnabled(new_state in ("pending", "delivered"))
        # Full re-render of the text labels would require rebuilding the row;
        # state + button truth is what changes in practice, and the panel
        # rebuilds rows wholesale on catch-up refreshes.


class WireWakePanel(QWidget):
    """Compact wake inbox strip; expands to the full list."""

    ack_requested = Signal(str)
    claim_requested = Signal(str)

    def __init__(self, colors: dict[str, str], parent=None):
        super().__init__(parent)
        self._colors = colors
        self._expanded = False
        self._wakes: dict[str, dict[str, Any]] = {}
        self._row_order: list[str] = []
        self._rows: dict[str, _WakeRow] = {}
        self._session: dict[str, Any] | None = None
        self._connection = "connecting"

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
        }.get(state, self._colors["text_muted"])
        self._dot.setStyleSheet(f"color: {dot_color};")
        self._refresh_strip()

    def set_session(self, session: dict[str, Any] | None) -> None:
        self._session = session
        self._refresh_strip()

    def apply_wake_event(self, event: dict[str, Any]) -> None:
        """Merge one listener event ({source, wake}) into the inbox model."""
        wake = event.get("wake")
        if not isinstance(wake, dict):
            return
        wake_id = wake.get("wake_id")
        if not isinstance(wake_id, str) or not wake_id:
            return
        known = self._wakes.get(wake_id)
        if known is not None and known.get("state") in ("completed", "failed",
                                                        "expired", "cancelled"):
            # Terminal states never regress from a replayed event.
            return
        self._wakes[wake_id] = wake
        if wake_id not in self._row_order:
            self._row_order.append(wake_id)
        self._rebuild_rows()
        self._refresh_strip()

    def apply_action_result(self, result: dict[str, Any]) -> None:
        """Apply an ack/claim outcome: update the row or surface the error."""
        wake_id = str(result.get("wake_id", ""))
        if result.get("ok"):
            wake = result.get("wake")
            if isinstance(wake, dict) and wake_id in self._wakes:
                self._wakes[wake_id] = wake
                self._rebuild_rows()
        else:
            error = str(result.get("error", "unknown error"))
            row = self._rows.get(wake_id)
            if row is not None:
                # Honest refusal display: the row's note area shows why.
                record = dict(self._wakes.get(wake_id, {}))
                record["last_error"] = f"action refused: {error}"
                self._wakes[wake_id] = record
                self._rebuild_rows()

    def set_admission_note(self, note: str) -> None:
        """Display the current suspension/E-stop admission truth."""
        self._admission_note = note
        self._refresh_strip()

    _admission_note: str = ""

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

    def _rebuild_rows(self) -> None:
        # Newest first; terminal rows drop to the bottom and fade naturally
        # via their state badge.
        order = sorted(
            self._row_order,
            key=lambda wid: (
                self._wakes[wid].get("state") in ("completed", "failed",
                                                  "expired", "cancelled"),
                str(self._wakes[wid].get("created_at", "")),
            ),
            reverse=False,
        )
        # Clear and rebuild (bounded by the wake population the listener
        # surfaces; the relay's own state machine retires terminal rows).
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._row_order = order
        # Insert before the trailing stretch.
        stretch_index = self._detail_layout.count() - 1
        for index, wake_id in enumerate(order):
            row = _WakeRow(self._wakes[wake_id], self._colors)
            row.ack_requested.connect(self.ack_requested)
            row.claim_requested.connect(self.claim_requested)
            self._rows[wake_id] = row
            self._detail_layout.insertWidget(stretch_index + index, row)
