"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- Settings surface for Sign in with ChatGPT.

Mounted as a self-contained section of the General page (below Save, with
its own controls): connecting a ChatGPT account is session custody, not
backend selection, and nothing here takes part in General's Save. The `openai_chatgpt_plan`
lane stays unselectable (General's selector never lists it) and admits no
operation until a later slice earns it, so this page says exactly that and
claims nothing else -- no model picker, no usage meter, no "ready to chat".

Every session-manager call runs on a worker thread (sign-in waits on the
browser; refresh/disconnect do network I/O); results come back through a
queued Qt signal as sanitized DTOs. The page never receives a token, code,
URL or identity claim -- only state, plan permission and a display label.
"""
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from ._widgets import _btn, _lbl, _sec

_STATE_TEXT = {
    "disconnected": "Not connected",
    "authorizing": "Waiting for you to finish signing in in your browser…",
    "connected_no_plan_permission": "Connected to ChatGPT",
    "ready": "Connected to ChatGPT",
    "refreshing": "Connected to ChatGPT (renewing session…)",
    "reauth_required": "Connection expired — reconnect required",
    "disconnecting": "Disconnecting…",
    "local_disconnected_remote_unconfirmed": "Signed out on this computer",
    "corrupt_session": "The saved ChatGPT connection could not be read safely",
}
_SCOPE_NOTE = ("Signing in stores a ChatGPT session on this computer only. Using your "
               "ChatGPT plan for chat is not available in this version of Lumina yet.")


class ChatGPTPlanCard(QWidget):
    _worker_done = Signal(object)

    def __init__(self, agent=None, c: dict = None, parent=None, manager=None):
        super().__init__(parent)
        self.c = c or {}
        self._manager = manager
        self._busy = False
        self._status = None
        self._profiles = []
        self._pending = None
        self._worker_done.connect(self._on_worker_done)
        self._build()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cancel_pending_sign_in)
        self.refresh()

    # -- plumbing ------------------------------------------------------------

    def _mgr(self):
        if self._manager is None:
            from core.chatgpt_auth import get_session_manager
            self._manager = get_session_manager()
        return self._manager

    def _run(self, action: str, fn) -> None:
        """Run `fn(manager)` off the Qt main thread; deliver (action, outcome)."""
        self._set_busy(True)

        def work():
            try:
                outcome = ("ok", fn(self._mgr()))
            except Exception as exc:          # typed auth errors carry a safe message
                outcome = ("error", getattr(exc, "user_message",
                                            "Temporary OpenAI authentication error. Try again shortly."))
            try:
                status = self._mgr().get_session_state()
                profiles = self._mgr().list_profiles()
            except Exception:
                status, profiles = None, []
            self._worker_done.emit((action, outcome, status, profiles))

        threading.Thread(target=work, name=f"chatgpt-settings-{action}", daemon=True).start()

    def _cancel_pending_sign_in(self) -> None:
        if self._manager is not None:
            try:
                self._manager.cancel_sign_in()
            except Exception:
                pass

    # -- UI ------------------------------------------------------------------

    def _build(self) -> None:
        c = self.c
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(_sec("CHATGPT PLAN — SIGN-IN", c))
        note = _lbl(_SCOPE_NOTE, c)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self._on_account_picked)
        layout.addWidget(self.account_combo)

        self.account_label = _lbl("", c)
        self.permission_label = _lbl("", c)
        self.session_label = _lbl("", c)
        self.message_label = _lbl("", c)
        self.code_label = _lbl("", c)
        self.message_label.setWordWrap(True)
        for w in (self.account_label, self.permission_label, self.session_label,
                  self.code_label, self.message_label):
            layout.addWidget(w)

        row = QHBoxLayout()
        self.continue_btn = _btn("Continue with ChatGPT", c, accent=True)
        self.reconnect_btn = _btn("Reconnect", c)
        self.enable_btn = _btn("Enable plan permission", c)
        self.add_btn = _btn("Add another account", c)
        self.cancel_btn = _btn("Cancel sign-in", c)
        self.confirm_btn = _btn("Confirm this account", c, accent=True)
        self.disconnect_btn = _btn("Disconnect", c, danger=True)
        for b in (self.continue_btn, self.reconnect_btn, self.enable_btn, self.add_btn,
                  self.cancel_btn, self.confirm_btn, self.disconnect_btn):
            row.addWidget(b)
        row.addStretch()
        layout.addLayout(row)

        self.continue_btn.clicked.connect(lambda: self._sign_in())
        self.add_btn.clicked.connect(lambda: self._sign_in(new_account=True))
        self.reconnect_btn.clicked.connect(lambda: self._sign_in(reconnect=True))
        self.enable_btn.clicked.connect(lambda: self._sign_in(reconnect=True, consent=True))
        self.cancel_btn.clicked.connect(self._cancel_and_refresh)
        self.confirm_btn.clicked.connect(self._confirm_sign_in)
        self.disconnect_btn.clicked.connect(lambda: self._run("disconnect", lambda m: m.disconnect()))
        self._render()

    def refresh(self) -> None:
        self._run("refresh", lambda m: None)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._render()

    def _active_profile_id(self):
        return None if self._status is None else self._status.profile_id

    def _sign_in(self, new_account=False, reconnect=False, consent=False) -> None:
        pid = self._active_profile_id() if reconnect else None

        def flow(m):
            pending = m.begin_sign_in(profile_id=pid, new_account=new_account,
                                      request_plan_consent=consent)
            self._pending = pending
            self._worker_done.emit(("authorizing", ("ok", None), m.get_session_state(), m.list_profiles()))
            return m.complete_sign_in(pending)
        self._run("sign_in", flow)

    def _confirm_sign_in(self) -> None:
        pending = self._pending
        if pending is not None:
            self._run("confirm", lambda m: m.confirm_sign_in(pending))

    def _cancel_and_refresh(self) -> None:
        self._cancel_pending_sign_in()
        self._pending = None
        self.refresh()

    def _on_account_picked(self, index: int) -> None:
        if self._busy or index < 0 or index >= len(self._profiles):
            return
        pid = self._profiles[index].profile_id
        if pid != self._active_profile_id():
            self._run("select", lambda m: m.select_profile(pid))

    def _on_worker_done(self, payload) -> None:
        action, (kind, value), status, profiles = payload
        if action != "authorizing":
            self._busy = False
        if status is not None:
            self._status = status
            self._profiles = list(profiles)
        if kind == "error":
            if action in ("sign_in", "confirm"):
                self._pending = None
            self.message_label.setText(value)
        elif action == "sign_in" and value is not None and value.state.value == "authorizing":
            self.message_label.setText("Check the ChatGPT account shown here, then confirm it in Lumina.")
        elif action == "sign_in" and value is not None:
            self._pending = None
            if status is not None and status.state.value == "ready":
                self.message_label.setText("Connected to ChatGPT. Plan permission enabled.")
        elif action == "confirm":
            self._pending = None
            self.message_label.setText("Connected to ChatGPT.")
        elif action == "disconnect" and value is not None:
            self.message_label.setText(
                "Disconnected." if value.remote_revocation_confirmed is not False else
                "Remote disconnect could not be confirmed. Local credentials were removed; "
                "you can also disconnect Lumina in ChatGPT Settings.")
        elif action in ("refresh", "select"):
            self.message_label.setText(status.message if status is not None and status.message else "")
        self._render()

    def _render(self) -> None:
        status = self._status
        state = None if status is None else status.state.value
        authorizing = state == "authorizing"
        self.session_label.setText("Session: " + (_STATE_TEXT.get(state, "Checking…") if state else "Checking…"))
        if status is not None and status.account_display and state not in ("disconnected", None):
            candidate = authorizing and self._pending is not None and status.profile_id is None
            prefix = "Account to confirm: " if candidate else "Connected account: "
            self.account_label.setText(prefix + status.account_display)
        elif status is not None and status.profile_label:
            self.account_label.setText(f"Saved account: {status.profile_label}")
        else:
            self.account_label.setText("")
        perm = None if status is None else status.plan_permission.value
        self.permission_label.setText(
            {"granted": "Plan permission: Enabled", "not_granted": "Plan permission: Not enabled"}.get(perm, ""))
        self.code_label.setText(
            "Enter this one-time code in your browser: " + self._pending.start_code
            if authorizing and self._pending is not None and self._pending.start_code
            and (status is None or status.profile_id is not None or not status.account_display) else "")

        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for p in self._profiles:
            self.account_combo.addItem(p.account_display or p.label)
            if p.active:
                self.account_combo.setCurrentIndex(self.account_combo.count() - 1)
        self.account_combo.blockSignals(False)
        self.account_combo.setVisible(len(self._profiles) > 1)
        self.account_combo.setEnabled(not self._busy)

        has_profile = status is not None and status.profile_id is not None
        idle = not self._busy and not authorizing
        self.continue_btn.setVisible(idle and state in ("disconnected", "local_disconnected_remote_unconfirmed")
                                     and not has_profile_needing_reconnect(state, has_profile))
        self.reconnect_btn.setVisible(idle and has_profile and state in (
            "reauth_required", "disconnected", "local_disconnected_remote_unconfirmed"))
        self.enable_btn.setVisible(idle and state == "connected_no_plan_permission")
        self.add_btn.setVisible(idle and has_profile and state not in ("corrupt_session", "disconnecting"))
        self.cancel_btn.setVisible(authorizing)
        self.confirm_btn.setVisible(not self._busy and self._pending is not None and authorizing
                                    and status is not None and status.profile_id is None
                                    and bool(status.account_display))
        self.disconnect_btn.setVisible(idle and state in (
            "ready", "connected_no_plan_permission", "refreshing", "reauth_required", "disconnecting"))


def has_profile_needing_reconnect(state, has_profile) -> bool:
    """A saved, signed-out registration is resumed with Reconnect (its issued
    client ID), not a fresh registration."""
    return bool(has_profile and state in ("disconnected", "local_disconnected_remote_unconfirmed"))
