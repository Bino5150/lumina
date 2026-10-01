"""
SUBSCRIPTION-PLAN-BACKENDS-01C -- Sign in with ChatGPT: authentication and
OS-local session custody for the reserved `openai_chatgpt_plan` lane.

Authentication only. This package sends no inference request, lists no
models and makes no backend constructible or selectable; 01D builds the
plan transport on top of the interface re-exported here.

  store.py    OS-local owner-only files, locks, schema/checksum, host ID
  oauth.py    PKCE/state/nonce, discovery, token/refresh/revoke, ID-token verification
  callback.py single-attempt 127.0.0.1 loopback listener
  session.py  state machine, serialized refresh, disconnect, sanitized DTOs/telemetry
"""
from core.chatgpt_auth.session import (
    AccessGrant,
    AccountMismatch,
    AuthorizationCancelled,
    BrowserUnavailable,
    ChatGPTAuthError,
    ChatGPTSessionManager,
    DisconnectOutcome,
    ErrorCategory,
    IdentityNotVerified,
    PendingSignIn,
    PlanPermission,
    PlanPermissionNotGranted,
    ProfileView,
    ReauthRequired,
    SessionBusy,
    SessionState,
    SessionStatus,
    SessionStorageError,
    SignInRequired,
    TemporaryAuthError,
    get_session_manager,
)

__all__ = [
    "AccessGrant", "AccountMismatch", "AuthorizationCancelled", "BrowserUnavailable",
    "ChatGPTAuthError", "ChatGPTSessionManager", "DisconnectOutcome", "ErrorCategory",
    "IdentityNotVerified", "PendingSignIn", "PlanPermission", "PlanPermissionNotGranted",
    "ProfileView", "ReauthRequired", "SessionBusy", "SessionState", "SessionStatus",
    "SessionStorageError", "SignInRequired", "TemporaryAuthError", "get_session_manager",
]
