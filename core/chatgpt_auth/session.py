"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- ChatGPT session manager: the one
boundary through which anything in Lumina touches a ChatGPT sign-in.

01C ends at READY. This manager authenticates, holds and renews a session
and signs it out. It never sends an inference request, never lists models,
never reads an API key, and never makes the `openai_chatgpt_plan` lane
constructible or selectable (core/backend_identity.py still admits no
operation for it). Session state and backend selectability are different
things.

Interface for 01D (everything else here is internal):
    get_active_profile()          -> ProfileView | None
    list_profiles()               -> [ProfileView]
    get_session_state(pid=None)   -> SessionStatus
    get_plan_permission_state()   -> PlanPermission
    get_valid_access_token(pid=None) -> AccessGrant   (refreshes safely, or a typed ChatGPTAuthError)
    is_grant_current(grant)       -> bool
    disconnect(pid=None)          -> DisconnectOutcome
The caller never sees PKCE, nonces, ID tokens, the file format, locking or
the callback listener; DTOs carry no credentials (AccessGrant's token is
excluded from repr()).

Session states (derived; only the persisted parts live in store.py):
  DISCONNECTED                       no active profile, or signed out with remote revocation confirmed
  AUTHORIZING                        browser authorization or owner confirmation is pending (memory only)
  CONNECTED_NO_PLAN_PERMISSION       identity verified, `chatgpt.tokens.use.direct` not granted
  READY                              identity verified + plan scope granted + renewable credentials
  REFRESHING                         a received rotation is persisted but not yet published
  REAUTH_REQUIRED                    terminal failure, or unknown refresh outcome requiring fresh authorization
  DISCONNECTING                      sign-out started; new use refused; tokens not yet cleared
  LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED  tokens cleared; remote revocation NOT confirmed
  CORRUPT_SESSION                    store unreadable/unsafe; nothing is used, nothing is auto-deleted
A token existing never means READY.

Refresh protocol (single authoritative generation per profile):
  1. refresh lock (interprocess, held across the network call);
  2. store lock: re-read; if another process already refreshed, return its
     token (double-checked); otherwise capture generation G + refresh token;
  3. network refresh;
  4. store lock: if the profile is still at generation G and connected,
     persist the response as a promotable pending rotation (the vendor has
     already retired the old refresh token -- this is now the only usable
     one); if a disconnect started meanwhile, persist it NON-promotable so
     that disconnect revokes the newest token; any other change -> the
     stale refresher loses and revokes its own orphan, best effort;
  5. verify the refreshed ID token, if one came back, against the saved
     subject (a JWKS outage leaves the rotation pending and resumable, even
     across a restart -- the consumed refresh token is never re-sent);
  6. store lock: compare-and-swap G -> G+1; only the authoritative
     generation can publish.
Disconnect: mark DISCONNECTING (generation+1, so any in-flight refresh
loses its CAS) -> take the refresh lock (waits out an in-flight refresh) ->
revoke the newest refresh token (bounded retry) -> clear all token material
-> DISCONNECTED or LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED, never lying about
which.
"""
from __future__ import annotations

import dataclasses
import enum
import inspect
import re
import threading
import time
import uuid
import webbrowser
from typing import Callable, Optional

from core.chatgpt_auth import oauth
from core.chatgpt_auth.callback import CallbackCancelled, CallbackTimeout, LoopbackCallback
from core.chatgpt_auth.store import (
    STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_DISCONNECTED_REMOTE_UNCONFIRMED,
    STATUS_DISCONNECTING, STATUS_REAUTH_REQUIRED, Credentials,
    PendingRotation, Profile, SessionStore, StoreBusy, StoreCorrupt, StoreError, StoreUnsafe,
    new_record_id,
)

CHATGPT_PLAN_LANE = "openai_chatgpt_plan"   # core.backend_identity.OPENAI_CHATGPT_PLAN_LANE

ATTEMPT_TTL_SECONDS = 600            # pending browser authorization lifetime
REFRESH_SKEW_SECONDS = 120           # refresh this long before the access token expires
DISCONNECT_REFRESH_WAIT_SECONDS = 90 # how long disconnect waits for an in-flight refresh


class SessionState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    AUTHORIZING = "authorizing"
    CONNECTED_NO_PLAN_PERMISSION = "connected_no_plan_permission"
    READY = "ready"
    REFRESHING = "refreshing"
    REAUTH_REQUIRED = "reauth_required"
    DISCONNECTING = "disconnecting"
    LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED = "local_disconnected_remote_unconfirmed"
    CORRUPT_SESSION = "corrupt_session"


class PlanPermission(str, enum.Enum):
    GRANTED = "granted"
    NOT_GRANTED = "not_granted"
    UNKNOWN = "unknown"          # no verified grant to inspect


# ---------------------------------------------------------------------------
# Typed failures (owner-facing category + safe categorical class only)
# ---------------------------------------------------------------------------

class ErrorCategory(str, enum.Enum):
    AUTHORIZATION_CANCELLED = "authorization_cancelled"
    PLAN_PERMISSION_NOT_GRANTED = "plan_permission_not_granted"
    REAUTH_REQUIRED = "reauth_required"
    IDENTITY_NOT_VERIFIED = "identity_not_verified"
    TEMPORARY = "temporary"
    REMOTE_DISCONNECT_UNCONFIRMED = "remote_disconnect_unconfirmed"
    SIGN_IN_REQUIRED = "sign_in_required"
    ACCOUNT_MISMATCH = "account_mismatch"
    STORAGE = "storage"
    BUSY = "busy"
    BROWSER_UNAVAILABLE = "browser_unavailable"


USER_MESSAGES = {
    ErrorCategory.AUTHORIZATION_CANCELLED: "Authorization cancelled.",
    ErrorCategory.PLAN_PERMISSION_NOT_GRANTED: "Plan permission not granted.",
    ErrorCategory.REAUTH_REQUIRED: "Connection expired — reconnect required.",
    ErrorCategory.IDENTITY_NOT_VERIFIED: "Could not verify OpenAI identity.",
    ErrorCategory.TEMPORARY: "Temporary OpenAI authentication error. Try again shortly.",
    ErrorCategory.REMOTE_DISCONNECT_UNCONFIRMED: (
        "Remote disconnect could not be confirmed. Local credentials were removed; "
        "you can also disconnect Lumina in ChatGPT Settings."),
    ErrorCategory.SIGN_IN_REQUIRED: "Sign in with ChatGPT to continue.",
    ErrorCategory.ACCOUNT_MISMATCH: (
        "That sign-in returned a different ChatGPT account. Choose the original account, "
        "or add it as a separate account."),
    ErrorCategory.STORAGE: "The saved ChatGPT connection could not be read safely.",
    ErrorCategory.BUSY: "Another Lumina window is updating the ChatGPT connection. Try again.",
    ErrorCategory.BROWSER_UNAVAILABLE: "The system browser could not be opened.",
}


class ChatGPTAuthError(Exception):
    """Every failure this boundary raises. Carries no token, code, URL,
    identity or provider body -- only a category and a categorical class."""
    category = ErrorCategory.TEMPORARY
    retryable = False

    def __init__(self, error_class: str = "unspecified"):
        super().__init__(USER_MESSAGES[self.category])
        self.error_class = error_class

    @property
    def user_message(self) -> str:
        return USER_MESSAGES[self.category]


class AuthorizationCancelled(ChatGPTAuthError):
    category = ErrorCategory.AUTHORIZATION_CANCELLED


class PlanPermissionNotGranted(ChatGPTAuthError):
    category = ErrorCategory.PLAN_PERMISSION_NOT_GRANTED


class ReauthRequired(ChatGPTAuthError):
    category = ErrorCategory.REAUTH_REQUIRED


class IdentityNotVerified(ChatGPTAuthError):
    category = ErrorCategory.IDENTITY_NOT_VERIFIED


class TemporaryAuthError(ChatGPTAuthError):
    category = ErrorCategory.TEMPORARY
    retryable = True


class SignInRequired(ChatGPTAuthError):
    category = ErrorCategory.SIGN_IN_REQUIRED


class AccountMismatch(ChatGPTAuthError):
    category = ErrorCategory.ACCOUNT_MISMATCH


class SessionStorageError(ChatGPTAuthError):
    category = ErrorCategory.STORAGE


class SessionBusy(ChatGPTAuthError):
    category = ErrorCategory.BUSY
    retryable = True


class BrowserUnavailable(ChatGPTAuthError):
    category = ErrorCategory.BROWSER_UNAVAILABLE


# ---------------------------------------------------------------------------
# DTOs (sanitized: safe for UI threads, logs-free by construction)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    plan_permission: PlanPermission
    profile_id: Optional[str] = None
    profile_label: Optional[str] = None
    account_display: Optional[str] = dataclasses.field(default=None, repr=False)
    error_category: Optional[ErrorCategory] = None

    @property
    def message(self) -> Optional[str]:
        return None if self.error_category is None else USER_MESSAGES[self.error_category]


@dataclasses.dataclass(frozen=True)
class ProfileView:
    profile_id: str
    label: str
    account_display: Optional[str] = dataclasses.field(repr=False)
    state: SessionState
    plan_permission: PlanPermission
    active: bool


@dataclasses.dataclass(frozen=True)
class AccessGrant:
    """What 01D will receive: a bearer token for exactly one profile and
    generation. The token is excluded from repr()/str()."""
    profile_id: str
    generation: int
    access_token: str = dataclasses.field(repr=False)
    expires_at: float


@dataclasses.dataclass(frozen=True)
class DisconnectOutcome:
    state: SessionState
    remote_revocation_confirmed: Optional[bool]   # None => there was nothing to revoke


@dataclasses.dataclass(frozen=True)
class PendingSignIn:
    attempt_id: str
    mode: str
    start_code: Optional[str] = dataclasses.field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Telemetry: allowlisted categorical fields only
# ---------------------------------------------------------------------------

_TELEMETRY_KEYS = frozenset({
    "backend_lane", "operation", "mode", "from_state", "to_state", "error_class",
    "plan_permission", "remote_revocation",
})
_CATEGORICAL_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
# None of the keys above may be eaten by core/redaction.SECRET_KEY_MARKERS
# ("auth", "token", "secret", "credential", ...): they are pinned by a test.


def sanitize_telemetry_fields(fields: dict) -> dict:
    """Allowlist, not redaction: unknown keys and non-categorical values are
    dropped, so free text (a token, an email, a URL, a provider body) has no
    path into a recorded event."""
    clean = {}
    for key, value in fields.items():
        if key not in _TELEMETRY_KEYS:
            continue
        if isinstance(value, enum.Enum):
            value = value.value
        if isinstance(value, str) and _CATEGORICAL_RE.match(value):
            clean[key] = value
    return clean


def _default_recorder(event_type: str, fields: dict) -> None:
    try:
        from core.flight_recorder import record_machine_event
        record_machine_event(event_type, fields=fields, backend=CHATGPT_PLAN_LANE)
    except Exception:
        pass   # telemetry never interrupts credential custody


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

def _account_display(profile: Profile) -> str:
    return profile.email or profile.label


def _plan_permission(creds: Optional[Credentials]) -> PlanPermission:
    if creds is None:
        return PlanPermission.UNKNOWN
    return PlanPermission.GRANTED if oauth.PLAN_SCOPE in creds.scopes else PlanPermission.NOT_GRANTED


def derive_state(profile: Optional[Profile]) -> SessionState:
    """Persisted record -> session state. READY requires the complete
    validated contract; a token's mere presence never suffices."""
    if profile is None:
        return SessionState.DISCONNECTED
    if profile.status == STATUS_DISCONNECTING:
        return SessionState.DISCONNECTING
    if profile.status == STATUS_DISCONNECTED_REMOTE_UNCONFIRMED:
        return SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED
    if profile.status == STATUS_REAUTH_REQUIRED:
        return SessionState.REAUTH_REQUIRED
    if profile.status == STATUS_DISCONNECTED or profile.credentials is None:
        return SessionState.DISCONNECTED
    if profile.pending_rotation is not None and profile.pending_rotation.promotable:
        return SessionState.REFRESHING
    if profile.refresh_outcome_unknown:
        return SessionState.REAUTH_REQUIRED
    if oauth.PLAN_SCOPE not in profile.credentials.scopes:
        return SessionState.CONNECTED_NO_PLAN_PERMISSION
    if "offline_access" not in profile.credentials.scopes or not profile.credentials.refresh_token:
        return SessionState.REAUTH_REQUIRED
    return SessionState.READY


class _Attempt:
    """In-process bookkeeping for one pending browser authorization."""

    def __init__(self, auth: oauth.AuthorizationAttempt, listener: LoopbackCallback):
        self.auth = auth
        self.listener = listener
        self.cancelled = False
        self.committing = False
        self.candidate = None  # (client_id, verified identity, credentials), process-local only


class ChatGPTSessionManager:

    def __init__(self, store: Optional[SessionStore] = None,
                 provider: Optional[oauth.ProviderClient] = None, *,
                 clock: Callable[[], float] = time.time,
                 open_browser: Optional[Callable[[str], bool]] = None,
                 recorder: Optional[Callable[[str, dict], None]] = None,
                 attempt_ttl: float = ATTEMPT_TTL_SECONDS,
                 disconnect_refresh_wait: float = DISCONNECT_REFRESH_WAIT_SECONDS):
        self.store = store or SessionStore()
        self.provider = provider or oauth.ProviderClient()
        self._clock = clock
        self._open_browser = open_browser or (lambda url: webbrowser.open(url, new=1, autoraise=True))
        self._recorder = recorder or _default_recorder
        self._attempt_ttl = attempt_ttl
        self._disconnect_refresh_wait = disconnect_refresh_wait
        self._attempt: Optional[_Attempt] = None
        self._attempt_lock = threading.Lock()
        self._refreshing = set()        # profile ids refreshing in THIS process
        self._mem_lock = threading.Lock()

    # -- telemetry -----------------------------------------------------------

    def _emit(self, event_type: str, **fields) -> None:
        fields.setdefault("backend_lane", CHATGPT_PLAN_LANE)
        try:
            self._recorder(event_type, sanitize_telemetry_fields(fields))
        except Exception:
            pass

    # -- store helpers -------------------------------------------------------

    def _guard_store(self, fn):
        try:
            return fn()
        except StoreBusy:
            raise SessionBusy("store_busy") from None
        except (StoreCorrupt, StoreUnsafe) as exc:
            raise SessionStorageError(f"store_{exc.reason}") from None
        except StoreError as exc:
            raise SessionStorageError(f"store_{exc.reason}") from None
        except OSError:
            raise SessionStorageError("store_io") from None

    def _read_doc(self):
        def run():
            with self.store.locked():
                return self.store.read()
        return self._guard_store(run)

    def _resolve_profile_id(self, doc, profile_id: Optional[str]) -> Optional[str]:
        return profile_id if profile_id is not None else doc.active_profile_id

    # -- read-only views -----------------------------------------------------

    def _view(self, doc, profile: Profile) -> ProfileView:
        state = derive_state(profile)
        with self._mem_lock:
            if profile.profile_id in self._refreshing and state is SessionState.READY:
                state = SessionState.REFRESHING
        return ProfileView(
            profile_id=profile.profile_id, label=profile.label,
            account_display=_account_display(profile), state=state,
            plan_permission=_plan_permission(profile.credentials),
            active=(doc.active_profile_id == profile.profile_id),
        )

    def list_profiles(self) -> list:
        doc = self._read_doc()
        return [self._view(doc, p) for p in doc.profiles]

    def get_active_profile(self) -> Optional[ProfileView]:
        doc = self._read_doc()
        profile = doc.profile(doc.active_profile_id)
        return None if profile is None else self._view(doc, profile)

    def get_session_state(self, profile_id: Optional[str] = None) -> SessionStatus:
        with self._attempt_lock:
            attempt = self._attempt
            authorizing = attempt is not None and not attempt.auth.finished
            candidate = attempt.candidate if authorizing else None
        try:
            doc = self._read_doc()
        except SessionStorageError:
            return SessionStatus(SessionState.CORRUPT_SESSION, PlanPermission.UNKNOWN,
                                 error_category=ErrorCategory.STORAGE)
        pid = self._resolve_profile_id(doc, profile_id)
        profile = doc.profile(pid)
        if candidate is not None and profile_id is None:
            _client_id, identity, creds = candidate
            return SessionStatus(SessionState.AUTHORIZING, _plan_permission(creds),
                                 account_display=identity.email or identity.name or "Verified ChatGPT account")
        if authorizing and profile_id is None:
            return SessionStatus(SessionState.AUTHORIZING, _plan_permission(
                profile.credentials if profile else None),
                profile_id=pid, profile_label=profile.label if profile else None,
                account_display=_account_display(profile) if profile else None)
        if profile is None:
            return SessionStatus(SessionState.DISCONNECTED, PlanPermission.UNKNOWN)
        view = self._view(doc, profile)
        category = {
            SessionState.REAUTH_REQUIRED: ErrorCategory.REAUTH_REQUIRED,
            SessionState.LOCAL_DISCONNECTED_REMOTE_UNCONFIRMED: ErrorCategory.REMOTE_DISCONNECT_UNCONFIRMED,
            SessionState.CONNECTED_NO_PLAN_PERMISSION: ErrorCategory.PLAN_PERMISSION_NOT_GRANTED,
        }.get(view.state)
        return SessionStatus(view.state, view.plan_permission, profile_id=profile.profile_id,
                             profile_label=profile.label, account_display=view.account_display,
                             error_category=category)

    def get_plan_permission_state(self, profile_id: Optional[str] = None) -> PlanPermission:
        return self.get_session_state(profile_id).plan_permission

    def select_profile(self, profile_id: str) -> SessionStatus:
        """Switches the active registration. Touches no credentials."""
        def run():
            with self.store.locked():
                doc = self.store.read()
                if doc.profile(profile_id) is None:
                    raise SignInRequired("profile_not_found")
                doc.active_profile_id = profile_id
                self.store.write(doc)
        self._guard_store(run)
        return self.get_session_state()

    # -- sign-in -------------------------------------------------------------

    def begin_sign_in(self, profile_id: Optional[str] = None, *, new_account: bool = False,
                      request_plan_consent: bool = False) -> PendingSignIn:
        """Starts one browser authorization. `profile_id` reconnects that
        saved registration (its issued client ID, its verified subject must
        come back); otherwise a new registration starts with the bootstrap
        client ID. Only one attempt is pending per process: starting a new
        one cancels the previous one."""
        self.cancel_sign_in()
        now = self._clock()

        def prepare():
            with self.store.locked():
                doc = self.store.read()
                host_id = self.store.get_or_create_host_id()
                # Legacy failed first registrations are never trusted or reused.
                if doc.pending_registrations:
                    doc.pending_registrations = []
                    self.store.write(doc)
                if profile_id is not None:
                    profile = doc.profile(profile_id)
                    if profile is None:
                        raise SignInRequired("profile_not_found")
                    if profile.status == STATUS_DISCONNECTING:
                        raise SessionBusy("disconnect_in_progress")
                    if profile.registration_invalid:
                        # OpenAI rejected this client: register a fresh one for
                        # the same account (its subject must come back).
                        return host_id, oauth.MODE_NEW_REGISTRATION, None, profile.profile_id, \
                            profile.subject, None
                    return host_id, oauth.MODE_REAUTHORIZE, profile.client_id, profile.profile_id, \
                        profile.subject, None
                return host_id, oauth.MODE_NEW_REGISTRATION, None, None, None, None

        host_id, mode, client_id, expected_pid, expected_subject, reg_id = self._guard_store(prepare)
        try:
            endpoints = self.provider.endpoints()
        except oauth.ProtocolError as exc:
            self._emit("chatgpt.auth.failed", operation="sign_in", error_class=exc.error_class)
            raise TemporaryAuthError(exc.error_class) from None

        state = oauth.new_state()
        try:
            listener = LoopbackCallback(state=state, expected_client_id=client_id)
        except OSError:
            self._emit("chatgpt.auth.failed", operation="sign_in", error_class="callback_bind_failed")
            raise TemporaryAuthError("callback_bind_failed") from None
        auth = oauth.AuthorizationAttempt(
            attempt_id=uuid.uuid4().hex, created_at=now, expires_at=now + self._attempt_ttl,
            redirect_uri=listener.redirect_uri, state=state, nonce=oauth.new_nonce(),
            verifier=oauth.new_pkce_verifier(), mode=mode, host_id=host_id, client_id=client_id,
            expected_profile_id=expected_pid, expected_subject=expected_subject,
            pending_registration_id=reg_id, request_consent=request_plan_consent,
        )
        attempt = _Attempt(auth, listener)
        with self._attempt_lock:
            self._attempt = attempt
        self._emit("chatgpt.auth.started", operation="sign_in", mode=mode)
        # The browser is handed only a one-shot local start URL; the real
        # authorize URL (state, nonce, challenge, host ID) reaches it as a
        # redirect, never as a launcher process argument.
        start_url, start_code = listener.arm_start(
            oauth.build_authorization_url(endpoints.authorization_endpoint, auth),
            owner_code=(mode == oauth.MODE_NEW_REGISTRATION))
        try:
            try:
                inspect.signature(self._open_browser).bind(start_url, start_code)
            except (TypeError, ValueError):
                opened = self._open_browser(start_url)
            else:
                opened = self._open_browser(start_url, start_code)
        except Exception:
            opened = False
        if opened is False:
            self._end_attempt(attempt)
            self._emit("chatgpt.auth.failed", operation="sign_in", error_class="browser_unavailable")
            raise BrowserUnavailable("browser_unavailable")
        return PendingSignIn(attempt_id=auth.attempt_id, mode=mode, start_code=start_code)

    def cancel_sign_in(self) -> None:
        with self._attempt_lock:
            attempt = self._attempt
            if attempt is None or attempt.auth.finished or attempt.committing:
                return
            attempt.cancelled = True
        if attempt is not None:
            attempt.listener.close()
            if attempt.candidate is not None:
                client_id, identity, creds = attempt.candidate
                self._end_attempt(attempt)
                self._revoke_unless_live_grant(client_id, identity.subject, creds.refresh_token)

    def _end_attempt(self, attempt: _Attempt) -> None:
        attempt.listener.close()
        attempt.auth.scrub()
        with self._attempt_lock:
            if self._attempt is attempt:
                self._attempt = None

    def complete_sign_in(self, pending: PendingSignIn, timeout: Optional[float] = None) -> SessionStatus:
        """Blocks (worker thread, never the Qt main thread) until the browser
        returns, the attempt expires, or it is cancelled; then exchanges,
        verifies and -- only if everything holds -- commits the registration."""
        with self._attempt_lock:
            attempt = self._attempt
        if (attempt is None or attempt.auth.attempt_id != pending.attempt_id
                or attempt.auth.finished or attempt.candidate is not None):
            raise AuthorizationCancelled("attempt_not_pending")
        auth = attempt.auth
        result = None
        try:
            remaining = auth.expires_at - self._clock()
            wait = remaining if timeout is None else min(timeout, remaining)
            try:
                result = attempt.listener.wait(max(0.0, wait))
            except CallbackTimeout:
                raise AuthorizationCancelled("attempt_expired") from None
            except CallbackCancelled:
                raise AuthorizationCancelled("cancelled") from None
            # The listener stays open until the attempt ends: a late second
            # hit on the one-shot start URL still marks it compromised.
            if attempt.cancelled or result.error == "cancelled":
                raise AuthorizationCancelled("cancelled")
            if self._clock() > auth.expires_at:
                raise AuthorizationCancelled("attempt_expired")
            if attempt.listener.compromised:
                raise IdentityNotVerified("attempt_compromised")
            if result.error is not None:
                if result.error == "access_denied":
                    raise AuthorizationCancelled("access_denied")
                if result.error == "unauthorized_client" and auth.expected_profile_id is not None:
                    self._mark_registration_invalid(auth.expected_profile_id)
                if result.error in ("registration_incomplete", "client_id_mismatch", "callback_malformed"):
                    raise IdentityNotVerified(result.error)
                raise TemporaryAuthError(result.error)
            status = self._finish_sign_in(
                auth, result.code, result.client_id,
                is_cancelled=lambda: attempt.cancelled or attempt.listener.compromised,
                stage_candidate=lambda client_id, identity, creds: self._stage_candidate(
                    attempt, client_id, identity, creds))
            if status.state is not SessionState.AUTHORIZING:
                self._emit("chatgpt.auth.completed", operation="sign_in", mode=auth.mode,
                           to_state=status.state, plan_permission=status.plan_permission)
            return status
        except ChatGPTAuthError as exc:
            if attempt.listener.compromised and auth.mode == oauth.MODE_NEW_REGISTRATION \
                    and result is not None and result.client_id:
                # Whatever client ID came back on a compromised attempt is
                # never kept for reuse.
                self._drop_pending_registration(result.client_id)
            self._emit("chatgpt.auth.failed", operation="sign_in", mode=auth.mode,
                       error_class=exc.error_class)
            raise
        finally:
            if attempt.candidate is None:
                self._end_attempt(attempt)

    def _stage_candidate(self, attempt: _Attempt, client_id: str, identity, creds: Credentials) -> SessionStatus:
        with self._attempt_lock:
            if self._attempt is not attempt or attempt.cancelled or attempt.listener.compromised:
                raise AuthorizationCancelled("attempt_compromised")
            attempt.candidate = (client_id, identity, creds)
        return SessionStatus(SessionState.AUTHORIZING, _plan_permission(creds),
                             account_display=identity.email or identity.name or "Verified ChatGPT account")

    def confirm_sign_in(self, pending: PendingSignIn) -> SessionStatus:
        """Owner confirmation of the verified account shown in Settings.
        First-registration credentials stay attempt-scoped until this call."""
        with self._attempt_lock:
            attempt = self._attempt
            if (attempt is None or attempt.auth.attempt_id != pending.attempt_id
                    or attempt.candidate is None or attempt.cancelled):
                raise AuthorizationCancelled("attempt_not_pending")
            client_id, identity, creds = attempt.candidate
            safe = attempt.listener.seal_start()
            if safe:
                attempt.committing = True
        if not safe:
            self._end_attempt(attempt)
            self._revoke_unless_live_grant(client_id, identity.subject, creds.refresh_token)
            raise IdentityNotVerified("attempt_compromised")
        try:
            status = self._commit_sign_in(attempt.auth, client_id, identity, creds,
                                          is_cancelled=lambda: attempt.cancelled)
            self._emit("chatgpt.auth.completed", operation="sign_in", mode=attempt.auth.mode,
                       to_state=status.state, plan_permission=status.plan_permission)
            return status
        finally:
            self._end_attempt(attempt)

    def _finish_sign_in(self, auth: oauth.AuthorizationAttempt, code: str, client_id: str,
                        is_cancelled: Callable[[], bool] = lambda: False,
                        stage_candidate=None) -> SessionStatus:
        # An issued client ID is attempt-scoped through exchange and identity verification.
        if auth.mode == oauth.MODE_NEW_REGISTRATION:
            doc = self._read_doc()
            known = next((p for p in doc.profiles if p.client_id == client_id), None)
            if known is not None:
                auth.expected_profile_id = known.profile_id
                auth.expected_subject = known.subject
        # 1. Exchange with this attempt's PKCE verifier and exact redirect URI.
        try:
            token = self.provider.exchange_code(client_id=client_id, code=code, verifier=auth.verifier,
                                                redirect_uri=auth.redirect_uri, now=self._clock())
        except oauth.ProviderRejected as exc:
            if exc.error_class in ("invalid_client", "unauthorized_client"):
                # The client ID the callback named is not a usable issued
                # client (or a forged callback named someone else's): never
                # keep it for reuse, and a saved profile on it must register
                # afresh next time.
                self._drop_pending_registration(client_id)
                if auth.expected_profile_id is not None:
                    self._mark_registration_invalid(auth.expected_profile_id)
            if exc.error_class == "invalid_grant":
                raise AuthorizationCancelled("authorization_code_rejected") from None
            raise IdentityNotVerified(exc.error_class) from None
        except oauth.ProviderUnavailable as exc:
            raise TemporaryAuthError(exc.error_class) from None
        except oauth.ProtocolError as exc:
            raise IdentityNotVerified(exc.error_class) from None

        # 2. Verify the ID token: signature/issuer/audience=issued client/
        #    expiry/nonce/subject. A token response alone is not proof.
        try:
            identity = self.provider.verify_id_token(token.id_token, client_id=client_id,
                                                     nonce=auth.nonce, now=self._clock())
        except oauth.IdentityVerificationUnavailable as exc:
            raise TemporaryAuthError(exc.error_class) from None
        except oauth.IdentityInvalid as exc:
            raise IdentityNotVerified(exc.error_class) from None

        # 3. Reconnecting account A must not become account B.
        if auth.expected_subject is not None and identity.subject != auth.expected_subject:
            # The other account's freshly issued session is never kept.
            self._revoke_unless_live_grant(client_id, identity.subject, token.credentials.refresh_token)
            raise AccountMismatch("subject_mismatch")

        token.credentials.id_token = token.id_token
        if auth.mode == oauth.MODE_NEW_REGISTRATION:
            try:
                return stage_candidate(client_id, identity, token.credentials)
            except ChatGPTAuthError:
                self._revoke_unless_live_grant(client_id, identity.subject,
                                               token.credentials.refresh_token)
                raise
        # Plan permission is read from the granted scopes; a missing
        # refresh token (offline_access not granted) can never be READY.
        return self._commit_sign_in(auth, client_id, identity, token.credentials, is_cancelled)

    def _mark_registration_invalid(self, profile_id: str) -> None:
        def run():
            with self.store.locked():
                doc = self.store.read()
                profile = doc.profile(profile_id)
                if profile is not None and not profile.registration_invalid:
                    profile.registration_invalid = True
                    self.store.write(doc)
        try:
            self._guard_store(run)
        except ChatGPTAuthError:
            pass

    def _revoke_unless_live_grant(self, client_id: str, subject: str, refresh_token: Optional[str]) -> None:
        """Best-effort revocation of a token Lumina will not keep -- unless a
        saved profile still holds a live session for the same (client,
        subject). OAuth revocation may end every token of the same grant
        (RFC 7009), so revoking a sibling would also end that live session."""
        if not refresh_token:
            return
        try:
            doc = self._read_doc()
        except ChatGPTAuthError:
            return
        for p in doc.profiles:
            if p.client_id == client_id and p.subject == subject and (
                    p.credentials is not None or p.pending_rotation is not None):
                return
        self.provider.revoke(client_id=client_id, refresh_token=refresh_token)

    def _drop_pending_registration(self, client_id: str) -> None:
        def run():
            with self.store.locked():
                doc = self.store.read()
                kept = [r for r in doc.pending_registrations if r.client_id != client_id]
                if len(kept) != len(doc.pending_registrations):
                    doc.pending_registrations = kept
                    self.store.write(doc)
        try:
            self._guard_store(run)
        except ChatGPTAuthError:
            pass

    def _commit_sign_in(self, auth, client_id, identity, creds: Credentials,
                        is_cancelled: Callable[[], bool] = lambda: False) -> SessionStatus:
        now = self._clock()

        def commit():
            with self.store.refresh_locked():
                with self.store.locked():
                    # A cancel (or a newer attempt) that landed while the code
                    # was being exchanged wins: nothing is committed.
                    if is_cancelled():
                        raise AuthorizationCancelled("cancelled")
                    doc = self.store.read()
                    if auth.expected_profile_id is not None:
                        profile = doc.profile(auth.expected_profile_id)
                        rebinding = (profile is not None and profile.registration_invalid
                                     and auth.mode == oauth.MODE_NEW_REGISTRATION)
                        if (profile is None or (profile.client_id != client_id and not rebinding)
                                or profile.subject != identity.subject
                                or profile.issuer != identity.issuer):
                            raise AccountMismatch("registration_changed")
                        if profile.status == STATUS_DISCONNECTING:
                            raise SessionBusy("disconnect_in_progress")
                        if rebinding:
                            if any(p.client_id == client_id for p in doc.profiles if p is not profile):
                                raise AccountMismatch("registration_changed")
                            profile.client_id = client_id
                            profile.registration_invalid = False
                    else:
                        profile = next((p for p in doc.profiles
                                        if p.registration_key == (identity.issuer, identity.subject, client_id)),
                                       None)
                    if profile is None:
                        profile = Profile(
                            profile_id=new_record_id(), label=self._next_label(doc),
                            issuer=identity.issuer, subject=identity.subject, client_id=client_id,
                            email=identity.email, display_name=identity.name, status=STATUS_CONNECTED,
                            generation=0, credentials=None, pending_rotation=None,
                            last_error_class=None, created_at=now, updated_at=now)
                        doc.profiles.append(profile)
                    # The replaced token set is NOT revoked: under the same
                    # issued client and account it may belong to the same
                    # OAuth grant as the session just issued (RFC 7009 lets a
                    # revocation end the whole grant). It simply stops being
                    # used; it expires on its own.
                    profile.credentials = creds
                    profile.pending_rotation = None
                    profile.status = STATUS_CONNECTED
                    profile.generation += 1
                    profile.last_error_class = None
                    profile.refresh_outcome_unknown = False
                    profile.updated_at = now
                    if identity.email:
                        profile.email = identity.email
                    if identity.name:
                        profile.display_name = identity.name
                    doc.pending_registrations = [
                        r for r in doc.pending_registrations
                        if r.client_id != client_id and r.registration_id != auth.pending_registration_id]
                    doc.active_profile_id = profile.profile_id
                    self.store.write(doc)
                    return profile.profile_id
        try:
            pid = self._guard_store(commit)
        except ChatGPTAuthError:
            # Not committed: the freshly issued session must not stay alive --
            # unless revoking it could also end a live saved session.
            self._revoke_unless_live_grant(client_id, identity.subject, creds.refresh_token)
            raise
        return self.get_session_state(pid)

    @staticmethod
    def _next_label(doc) -> str:
        used = {p.label for p in doc.profiles}
        n = 1
        while f"ChatGPT account {n}" in used:
            n += 1
        return f"ChatGPT account {n}"

    # -- tokens --------------------------------------------------------------

    def _needs_refresh(self, creds: Credentials, now: float) -> bool:
        return creds.expires_at - REFRESH_SKEW_SECONDS <= now

    @staticmethod
    def _refuse_unusable(profile: Optional[Profile]) -> None:
        state = derive_state(profile)
        if state in (SessionState.READY, SessionState.REFRESHING):
            return
        if state is SessionState.CONNECTED_NO_PLAN_PERMISSION:
            raise PlanPermissionNotGranted("plan_scope_missing")
        if state is SessionState.REAUTH_REQUIRED:
            raise ReauthRequired(profile.last_error_class or "reauth_required")
        if state is SessionState.DISCONNECTING:
            raise SignInRequired("disconnect_in_progress")
        raise SignInRequired("not_connected")

    def _grant(self, profile: Profile) -> AccessGrant:
        # Only a fully READY record yields a token: published credentials,
        # plan scope granted, renewable, no unpublished rotation.
        if derive_state(profile) is not SessionState.READY:
            self._refuse_unusable(profile)
            raise TemporaryAuthError("refresh_incomplete")
        return AccessGrant(profile_id=profile.profile_id, generation=profile.generation,
                           access_token=profile.credentials.access_token,
                           expires_at=profile.credentials.expires_at)

    def get_valid_access_token(self, profile_id: Optional[str] = None) -> AccessGrant:
        """A bearer token for the given (default: active) profile, refreshing
        it first when needed and allowed. Raises a typed ChatGPTAuthError
        otherwise. Never falls back to any other credential or lane, and
        never returns a token that is already expired."""
        doc = self._read_doc()
        pid = self._resolve_profile_id(doc, profile_id)
        profile = doc.profile(pid)
        if profile is not None and profile.refresh_outcome_unknown and profile.pending_rotation is None:
            # Another process may currently hold the refresh lock. Wait for
            # its settled result; if it lost the response, _refresh_locked
            # refuses the old token without sending it again.
            grant = self._refresh(pid)
        else:
            self._refuse_unusable(profile)
            if profile.pending_rotation is None and not self._needs_refresh(profile.credentials, self._clock()):
                return self._checked_grant(profile, profile_id)
            grant = self._refresh(pid)
        if grant.expires_at - REFRESH_SKEW_SECONDS <= self._clock():
            # A resumed rotation (after an outage or restart) can publish a
            # token that has meanwhile expired: renew it once more.
            grant = self._refresh(pid)
        if grant.expires_at <= self._clock():
            raise TemporaryAuthError("access_token_expired")
        return self._checked_grant_by_id(grant, profile_id)

    def _checked_grant(self, profile: Profile, requested: Optional[str]) -> AccessGrant:
        grant = self._grant(profile)
        return self._checked_grant_by_id(grant, requested)

    def _checked_grant_by_id(self, grant: AccessGrant, requested: Optional[str]) -> AccessGrant:
        if requested is None:
            # The caller asked for "the active account": if the owner switched
            # accounts meanwhile, this token belongs to the wrong one.
            doc = self._read_doc()
            if doc.active_profile_id != grant.profile_id:
                raise TemporaryAuthError("active_profile_changed")
        return grant

    def is_grant_current(self, grant: AccessGrant) -> bool:
        """False once the grant's generation has been superseded, revoked or
        cleared -- lets a holder of an older grant notice it went stale."""
        try:
            doc = self._read_doc()
        except ChatGPTAuthError:
            return False
        profile = doc.profile(grant.profile_id)
        return (profile is not None and derive_state(profile) is SessionState.READY
                and profile.generation == grant.generation)

    def _refresh(self, pid: str) -> AccessGrant:
        with self._mem_lock:
            self._refreshing.add(pid)
        try:
            return self._guard_store(lambda: self._refresh_locked(pid))
        finally:
            with self._mem_lock:
                self._refreshing.discard(pid)

    # ID-token failures on a refreshed token that say more about this host's
    # clock or a key-set propagation delay than about the token. They keep
    # the persisted rotation (it is the only usable refresh token) and retry.
    _ENVIRONMENTAL_IDENTITY_FAILURES = frozenset({
        "id_token_issued_in_future", "id_token_not_yet_valid", "id_token_expired",
    })

    def _refresh_locked(self, pid: str) -> AccessGrant:
        with self.store.refresh_locked():
            # (2) re-read under the lock: another process may have refreshed.
            with self.store.locked():
                doc = self.store.read()
                profile = doc.profile(pid)
                self._refuse_unusable(profile)
                now = self._clock()
                base_gen = profile.generation
                client_id = profile.client_id
                subject = profile.subject
                resume = profile.pending_rotation if (
                    profile.pending_rotation is not None and profile.pending_rotation.promotable) else None
                if resume is None:
                    if not self._needs_refresh(profile.credentials, now):
                        return self._grant(profile)
                    earliest = profile.credentials.earliest_refresh_at
                    if earliest is not None:
                        # A vendor hint past expiry would wedge the session.
                        earliest = min(earliest, profile.credentials.expires_at)
                    if earliest is not None and now < earliest:
                        if profile.credentials.expires_at > now:
                            return self._grant(profile)
                        raise TemporaryAuthError("refresh_not_ready")
                    refresh_token = profile.credentials.refresh_token
                    previous_scopes = profile.credentials.scopes
                    previous_id_token = profile.credentials.id_token
                    # From here until the outcome is known, the vendor may
                    # rotate without this store seeing the successor.
                    profile.refresh_outcome_unknown = True
                    self.store.write(doc)

            if resume is None:
                self._emit("chatgpt.refresh.started", operation="refresh")
                # (3) the network round trip, refresh lock still held.
                try:
                    token = self.provider.refresh(client_id=client_id, refresh_token=refresh_token,
                                                  previous_scopes=previous_scopes, now=self._clock())
                except oauth.ProviderRejected as exc:
                    if exc.terminal_for_refresh:
                        self._invalidate(pid, base_gen, exc.error_class,
                                         registration_invalid=(exc.error_class == oauth.INVALID_CLIENT_CODE))
                        raise ReauthRequired(exc.error_class) from None
                    self._settle_outcome(pid, base_gen)
                    self._emit("chatgpt.refresh.failed", operation="refresh", error_class=exc.error_class)
                    raise TemporaryAuthError(exc.error_class) from None
                except oauth.ProviderUnavailable as exc:
                    if exc.status == 429:
                        # Rate-limited before processing: nothing rotated. A
                        # 5xx (possibly from a gateway in front of the origin)
                        # proves nothing about whether the origin rotated.
                        self._settle_outcome(pid, base_gen)
                    self._emit("chatgpt.refresh.failed", operation="refresh", error_class=exc.error_class)
                    raise TemporaryAuthError(exc.error_class) from None
                except oauth.InvalidTokenResponse as exc:
                    # A 2xx that broke the contract: the vendor has most likely
                    # retired the old refresh token already, so keeping it would
                    # only get the whole family killed as "reused" later.
                    self._invalidate(pid, base_gen, exc.error_class,
                                     revoke=[t for t in [exc.salvage_refresh_token] if t])
                    raise ReauthRequired(exc.error_class) from None
                except oauth.ProtocolError as exc:
                    self._settle_outcome(pid, base_gen)
                    self._emit("chatgpt.refresh.failed", operation="refresh", error_class=exc.error_class)
                    raise TemporaryAuthError(exc.error_class) from None
                new_creds = token.credentials
                if new_creds.id_token is None:
                    new_creds.id_token = previous_id_token
                    needs_identity_check = False
                else:
                    needs_identity_check = True
                # (4) checkpoint the rotation before anything else can fail.
                try:
                    outcome = self._checkpoint_rotation(pid, base_gen, new_creds)
                except BaseException:
                    # Could not persist the only usable refresh token (disk
                    # full, I/O error): end that session remotely rather than
                    # leave it live and unreachable.
                    self.provider.revoke(client_id=client_id, refresh_token=new_creds.refresh_token)
                    raise
                if outcome != "pending":
                    raise TemporaryAuthError("refresh_superseded")
                pending_creds = new_creds
            else:
                pending_creds = resume.credentials
                needs_identity_check = pending_creds.id_token is not None and \
                    pending_creds.id_token != self._current_id_token(pid)

            # (5) identity of a refreshed ID token must still be this profile.
            if needs_identity_check:
                try:
                    identity = self.provider.verify_id_token(
                        pending_creds.id_token, client_id=client_id, nonce=None,
                        now=pending_creds.received_at)
                except oauth.IdentityVerificationUnavailable as exc:
                    self._emit("chatgpt.refresh.failed", operation="refresh", error_class=exc.error_class)
                    raise TemporaryAuthError(exc.error_class) from None
                except oauth.IdentityInvalid as exc:
                    if exc.error_class in self._ENVIRONMENTAL_IDENTITY_FAILURES:
                        self._emit("chatgpt.refresh.failed", operation="refresh", error_class=exc.error_class)
                        raise TemporaryAuthError(exc.error_class) from None
                    self._invalidate(pid, base_gen, exc.error_class)
                    raise IdentityNotVerified(exc.error_class) from None
                if identity.subject != subject:
                    self._invalidate(pid, base_gen, "subject_mismatch")
                    raise AccountMismatch("subject_mismatch")

            # (6) compare-and-swap publish.
            with self.store.locked():
                doc = self.store.read()
                profile = doc.profile(pid)
                pending = None if profile is None else profile.pending_rotation
                if (profile is None or profile.status != STATUS_CONNECTED
                        or profile.generation != base_gen or pending is None or not pending.promotable
                        or pending.credentials.refresh_token != pending_creds.refresh_token):
                    raise TemporaryAuthError("refresh_superseded")
                profile.credentials = pending.credentials
                profile.pending_rotation = None
                profile.generation = base_gen + 1
                profile.last_error_class = None
                profile.refresh_outcome_unknown = False
                profile.updated_at = self._clock()
                self.store.write(doc)
                self._emit("chatgpt.refresh.completed", operation="refresh",
                           plan_permission=_plan_permission(profile.credentials))
                return self._grant(profile)

    def _current_id_token(self, pid: str) -> Optional[str]:
        with self.store.locked():
            profile = self.store.read().profile(pid)
            return None if profile is None or profile.credentials is None else profile.credentials.id_token

    def _settle_outcome(self, pid: str, base_gen: int) -> None:
        """The refresh got a definite answer that rotated nothing."""
        with self.store.locked():
            doc = self.store.read()
            profile = doc.profile(pid)
            if profile is not None and profile.generation == base_gen and profile.refresh_outcome_unknown:
                profile.refresh_outcome_unknown = False
                self.store.write(doc)

    def _checkpoint_rotation(self, pid: str, base_gen: int, creds: Credentials) -> str:
        """Returns 'pending' (promotable checkpoint written), 'lost_to_disconnect'
        (kept non-promotable for the disconnect, and revoked here too) or
        'stale' (the orphan was revoked best-effort and never written)."""
        with self.store.locked():
            doc = self.store.read()
            profile = doc.profile(pid)
            if profile is not None and profile.status == STATUS_CONNECTED and profile.generation == base_gen:
                profile.pending_rotation = PendingRotation(creds, base_gen, promotable=True)
                profile.refresh_outcome_unknown = False
                profile.updated_at = self._clock()
                self.store.write(doc)
                return "pending"
            client_id = None if profile is None else profile.client_id
            lost_to_disconnect = profile is not None and profile.status == STATUS_DISCONNECTING
            if lost_to_disconnect:
                profile.pending_rotation = PendingRotation(creds, base_gen, promotable=False)
                profile.refresh_outcome_unknown = False
                profile.updated_at = self._clock()
                self.store.write(doc)
        # Nothing authoritative will use this token set. A disconnect also
        # revokes it, but one that already gave up waiting may not see it.
        if client_id is not None:
            self.provider.revoke(client_id=client_id, refresh_token=creds.refresh_token)
        if lost_to_disconnect:
            self._emit("chatgpt.refresh.failed", operation="refresh", error_class="lost_to_disconnect")
            return "lost_to_disconnect"
        self._emit("chatgpt.refresh.failed", operation="refresh", error_class="stale_generation")
        return "stale"

    def _invalidate(self, pid: str, base_gen: int, error_class: str, *, revoke=(),
                    registration_invalid: bool = False) -> None:
        """Terminal credential failure: clear token material (keep the
        registration so reauthorization reuses its issued client ID), but
        only if nothing newer replaced the generation this decision was
        about. A received-but-unpublished rotation being discarded is still
        a live renewable session at the vendor: it is revoked, best effort."""
        to_revoke = list(revoke)
        with self.store.locked():
            doc = self.store.read()
            profile = doc.profile(pid)
            if profile is None or profile.generation != base_gen or profile.status != STATUS_CONNECTED:
                client_id = None
            else:
                client_id = profile.client_id
                if profile.pending_rotation is not None:
                    to_revoke.append(profile.pending_rotation.credentials.refresh_token)
                profile.credentials = None
                profile.pending_rotation = None
                profile.status = STATUS_REAUTH_REQUIRED
                profile.generation += 1
                profile.refresh_outcome_unknown = False
                profile.registration_invalid = profile.registration_invalid or registration_invalid
                profile.last_error_class = (error_class if re.match(r"^[a-z0-9_]{1,64}$", error_class)
                                            else "terminal")
                profile.updated_at = self._clock()
                self.store.write(doc)
        if client_id is not None:
            for token in to_revoke:
                self.provider.revoke(client_id=client_id, refresh_token=token)
            self._emit("chatgpt.refresh.failed", operation="refresh", error_class=error_class,
                       to_state=SessionState.REAUTH_REQUIRED)

    # -- disconnect ----------------------------------------------------------

    def disconnect(self, profile_id: Optional[str] = None) -> DisconnectOutcome:
        """Stop new use, revoke the newest renewable session, clear tokens.
        Resumable: a disconnect interrupted by a crash leaves the profile
        DISCONNECTING (unusable) and finishes on the next call."""
        return self._guard_store(lambda: self._disconnect(profile_id))

    def _disconnect(self, profile_id: Optional[str]) -> DisconnectOutcome:
        with self.store.locked():
            doc = self.store.read()
            pid = self._resolve_profile_id(doc, profile_id)
            profile = doc.profile(pid)
            if profile is None:
                return DisconnectOutcome(SessionState.DISCONNECTED, None)
            if profile.credentials is None and profile.pending_rotation is None:
                if profile.status in (STATUS_CONNECTED, STATUS_DISCONNECTING, STATUS_REAUTH_REQUIRED):
                    profile.status = STATUS_DISCONNECTED
                    profile.generation += 1
                    profile.refresh_outcome_unknown = False
                    profile.updated_at = self._clock()
                    self.store.write(doc)
                return DisconnectOutcome(derive_state(profile), None)
            from_state = derive_state(profile)
            if profile.status != STATUS_DISCONNECTING:
                profile.status = STATUS_DISCONNECTING
                profile.generation += 1          # any in-flight refresh now loses its CAS
                if profile.pending_rotation is not None and profile.pending_rotation.promotable:
                    # A received rotation awaiting identity verification can
                    # no longer be published; it stays only to be revoked.
                    profile.pending_rotation.promotable = False
                profile.updated_at = self._clock()
                self.store.write(doc)
        self._emit("chatgpt.disconnect.started", operation="disconnect", from_state=from_state)

        waited_out_refresh = True
        try:
            refresh_lock = self.store.refresh_locked(timeout=self._disconnect_refresh_wait)
            refresh_lock.__enter__()
        except StoreBusy:
            refresh_lock = None
            waited_out_refresh = False
        try:
            with self.store.locked():
                profile = self.store.read().profile(pid)
                if profile is None or profile.status != STATUS_DISCONNECTING:
                    # Another disconnect finished it.
                    return DisconnectOutcome(derive_state(profile), None)
                client_id = profile.client_id
                outcome_unknown = profile.refresh_outcome_unknown
                tokens = []
                if profile.pending_rotation is not None and profile.pending_rotation.credentials.refresh_token:
                    tokens.append(profile.pending_rotation.credentials.refresh_token)
                if (profile.credentials is not None and profile.credentials.refresh_token
                        and profile.credentials.refresh_token not in tokens):
                    tokens.append(profile.credentials.refresh_token)

            # Newest first; confirmation is about the newest renewable session.
            had_tokens = bool(tokens)
            confirmed = not tokens
            for index, token in enumerate(tokens):
                ok = self.provider.revoke(client_id=client_id, refresh_token=token)
                if index == 0:
                    confirmed = ok
            # Never claim the renewable session is gone when a newer refresh
            # token may exist that this store never saw: an in-flight refresh
            # we could not wait out, or one whose response was lost.
            confirmed = confirmed and waited_out_refresh and not outcome_unknown

            with self.store.locked():
                doc = self.store.read()
                profile = doc.profile(pid)
                if profile is None or profile.status != STATUS_DISCONNECTING:
                    return DisconnectOutcome(derive_state(profile), None)
                profile.credentials = None
                profile.pending_rotation = None
                profile.refresh_outcome_unknown = False
                profile.status = STATUS_DISCONNECTED if confirmed else STATUS_DISCONNECTED_REMOTE_UNCONFIRMED
                profile.generation += 1
                profile.updated_at = self._clock()
                self.store.write(doc)
                final = derive_state(profile)
        finally:
            if refresh_lock is not None:
                refresh_lock.__exit__(None, None, None)
        if confirmed:
            self._emit("chatgpt.disconnect.completed", operation="disconnect",
                       remote_revocation="confirmed" if had_tokens else "not_applicable", to_state=final)
        else:
            self._emit("chatgpt.disconnect.remote_unconfirmed", operation="disconnect",
                       remote_revocation="unconfirmed", to_state=final)
        return DisconnectOutcome(final, confirmed if had_tokens else None)


# ---------------------------------------------------------------------------
# Process-wide default (the Settings UI and, later, 01D use this one)
# ---------------------------------------------------------------------------

_default_manager: Optional[ChatGPTSessionManager] = None
_default_lock = threading.Lock()


def get_session_manager() -> ChatGPTSessionManager:
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = ChatGPTSessionManager()
        return _default_manager
