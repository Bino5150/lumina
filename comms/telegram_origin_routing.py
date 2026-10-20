"""Process-local routing for replies to Lumina-originated Telegram messages.

The route ledger contains identifiers and bounded timestamps only. Telegram
message content and bot credentials never enter it. Runtime dispatchers are
opaque callbacks: the GUI owns the only callback that can enqueue work onto
its serialized foreground turn path.
"""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time
import uuid
import weakref
from typing import Callable, Optional


ROUTE_TTL_SECONDS = 24 * 60 * 60
MAX_ROUTES = 256


@dataclass(frozen=True)
class OriginRoute:
    destination_chat_id: str
    telegram_message_id: int
    conversation_id: int
    runtime_token: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class RouteResolution:
    route: Optional[OriginRoute]
    reason: str


@dataclass
class OriginDispatch:
    """Transient delivery envelope, deliberately separate from route metadata."""
    route: OriginRoute
    text: str
    # CASTLE-WALLS-REPAIR-04 / CANNON-11 -- the stable identity of the
    # specific inbound Telegram event being routed, carried alongside
    # `text` all the way to core.agent's approval word-match hook. Never
    # derived from `text` itself. None only for callers that predate this
    # field (kept optional so nothing upstream is forced to change).
    approval_event_id: Optional[str] = None
    future: Future = field(default_factory=Future)
    # TELEGRAM-OWNER-INBOUND-WAKE-01 (R3 C-1) -- re-checked by the runtime at
    # final turn admission: False once remote-access authority changed since
    # the update was admitted (OFF, an E-stop, or an OFF->ON flip), and the
    # request must then never run. None only for callers that predate it.
    admission_check: Optional[Callable[[], bool]] = None


class OriginUnavailable(RuntimeError):
    pass


_origin: ContextVar[tuple[Optional[str], Optional[int]]] = ContextVar(
    "telegram_outbound_origin", default=(None, None),
)
_lock = threading.Lock()
_routes: dict[tuple[str, int], OriginRoute] = {}
_runtimes: dict[str, object] = {}
# TELEGRAM-OWNER-INBOUND-WAKE-01 (R3 C-2) -- message ids of answers the
# headless telegram-owner channel sent in this process, so a swipe-reply to
# one of them is a known origin ("headless_reply") rather than unknown.
# Identifiers and expiry times only; never content.
_headless_replies: dict[tuple[str, int], float] = {}


def _chat_key(value) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not a Telegram chat id")
    return str(int(str(value).strip()))


def _dispatcher_ref(dispatcher: Callable[[OriginDispatch], bool]):
    if getattr(dispatcher, "__self__", None) is not None:
        return weakref.WeakMethod(dispatcher)
    # Module functions and short test callbacks do not create an ownership
    # cycle, and retaining them avoids a weak-reference disappearing between
    # registration and first dispatch.
    return lambda: dispatcher


def register_runtime(dispatcher: Callable[[OriginDispatch], bool], *,
                     token: str | None = None) -> str:
    runtime_token = token or uuid.uuid4().hex
    with _lock:
        _runtimes[runtime_token] = _dispatcher_ref(dispatcher)
    return runtime_token


def unregister_runtime(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _runtimes.pop(token, None)


@contextmanager
def origin_scope(runtime_token: str | None, conversation_id: int | None):
    marker = _origin.set((runtime_token, conversation_id))
    try:
        yield
    finally:
        _origin.reset(marker)


def _live_dispatcher_locked(runtime_token: str):
    ref = _runtimes.get(runtime_token)
    dispatcher = ref() if ref is not None else None
    if dispatcher is None:
        _runtimes.pop(runtime_token, None)
    return dispatcher


def _prune_locked(current: float) -> None:
    for key, route in list(_routes.items()):
        if route.expires_at <= current:
            _routes.pop(key, None)
    while len(_routes) >= MAX_ROUTES:
        oldest_key = min(_routes, key=lambda key: _routes[key].created_at)
        _routes.pop(oldest_key, None)


def record_outbound(*, destination_chat_id, telegram_message_id,
                    now: float | None = None) -> OriginRoute | None:
    runtime_token, conversation_id = _origin.get()
    if not runtime_token or conversation_id is None or telegram_message_id is None:
        return None
    try:
        chat_id = _chat_key(destination_chat_id)
        if isinstance(telegram_message_id, bool):
            return None
        message_id = int(telegram_message_id)
        if message_id <= 0:
            return None
        conversation = int(conversation_id)
    except (TypeError, ValueError):
        return None

    created_at = time.monotonic() if now is None else float(now)
    route = OriginRoute(
        destination_chat_id=chat_id,
        telegram_message_id=message_id,
        conversation_id=conversation,
        runtime_token=runtime_token,
        created_at=created_at,
        expires_at=created_at + ROUTE_TTL_SECONDS,
    )
    with _lock:
        if _live_dispatcher_locked(runtime_token) is None:
            return None
        _prune_locked(created_at)
        _routes[(chat_id, message_id)] = route
    return route


def record_headless_reply(*, destination_chat_id, telegram_message_id,
                          now: float | None = None) -> bool:
    """Remember that the headless telegram-owner channel sent this message
    (TELEGRAM-OWNER-INBOUND-WAKE-01). Never a Prime origin: it only lets a
    swipe-reply to it continue with the headless channel explicitly."""
    try:
        chat_id = _chat_key(destination_chat_id)
        if isinstance(telegram_message_id, bool):
            return False
        message_id = int(telegram_message_id)
        if message_id <= 0:
            return False
    except (TypeError, ValueError):
        return False
    created_at = time.monotonic() if now is None else float(now)
    with _lock:
        for key, expires_at in list(_headless_replies.items()):
            if expires_at <= created_at:
                _headless_replies.pop(key, None)
        while len(_headless_replies) >= MAX_ROUTES:
            _headless_replies.pop(min(_headless_replies, key=_headless_replies.get), None)
        _headless_replies[(chat_id, message_id)] = created_at + ROUTE_TTL_SECONDS
    return True


def resolve(*, destination_chat_id, reply_to_message_id=None,
            now: float | None = None) -> RouteResolution:
    """A swipe-reply (reply_to_message_id set) resolves to its exact origin:
    "exact_reply" (Prime), "headless_reply" (an answer the headless channel
    sent), "expired", or "unknown_reply" -- never a silent "cold"
    (TELEGRAM-OWNER-INBOUND-WAKE-01 R3 C-2). A plain message resolves to
    "sole_recent", "ambiguous" or "cold" as before."""
    try:
        chat_id = _chat_key(destination_chat_id)
    except (TypeError, ValueError):
        return RouteResolution(None, "cold")
    current = time.monotonic() if now is None else float(now)

    with _lock:
        if reply_to_message_id is not None:
            try:
                message_id = int(reply_to_message_id)
            except (TypeError, ValueError):
                return RouteResolution(None, "unknown_reply")
            key = (chat_id, message_id)
            route = _routes.get(key)
            if route is None:
                headless_expiry = _headless_replies.get(key)
                if headless_expiry is None:
                    return RouteResolution(None, "unknown_reply")
                if headless_expiry <= current:
                    _headless_replies.pop(key, None)
                    return RouteResolution(None, "expired")
                return RouteResolution(None, "headless_reply")
            if route.expires_at <= current:
                _routes.pop(key, None)
                return RouteResolution(None, "expired")
            return RouteResolution(route, "exact_reply")

        _prune_locked(current)
        active = [
            route for route in _routes.values()
            if route.destination_chat_id == chat_id
        ]

        if not active:
            return RouteResolution(None, "cold")
        origins = {(r.runtime_token, r.conversation_id) for r in active}
        if len(origins) != 1:
            return RouteResolution(None, "ambiguous")
        return RouteResolution(max(active, key=lambda r: r.created_at), "sole_recent")


def dispatch(route: OriginRoute, text: str, *,
             approval_event_id: Optional[str] = None,
             admission_check: Optional[Callable[[], bool]] = None) -> Future | None:
    with _lock:
        dispatcher = _live_dispatcher_locked(route.runtime_token)
    if dispatcher is None:
        return None

    request = OriginDispatch(route=route, text=text, approval_event_id=approval_event_id,
                             admission_check=admission_check)
    try:
        accepted = dispatcher(request)
    except Exception:
        return None
    if accepted is False:
        return None
    return request.future


def clear_routes() -> None:
    """Forget every return route (TELEGRAM-MASTER-SWITCH-01: remote access
    turned OFF). Registered runtimes stay; only recorded routes go, so no
    old route can capture a reply after access comes back on."""
    with _lock:
        _routes.clear()
        _headless_replies.clear()


def _reset_for_tests() -> None:
    with _lock:
        _routes.clear()
        _runtimes.clear()
        _headless_replies.clear()
    _origin.set((None, None))
