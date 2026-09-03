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
    future: Future = field(default_factory=Future)


class OriginUnavailable(RuntimeError):
    pass


_origin: ContextVar[tuple[Optional[str], Optional[int]]] = ContextVar(
    "telegram_outbound_origin", default=(None, None),
)
_lock = threading.Lock()
_routes: dict[tuple[str, int], OriginRoute] = {}
_runtimes: dict[str, object] = {}


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


def resolve(*, destination_chat_id, reply_to_message_id=None,
            now: float | None = None) -> RouteResolution:
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
                return RouteResolution(None, "cold")
            key = (chat_id, message_id)
            route = _routes.get(key)
            if route is None:
                return RouteResolution(None, "cold")
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


def dispatch(route: OriginRoute, text: str) -> Future | None:
    with _lock:
        dispatcher = _live_dispatcher_locked(route.runtime_token)
    if dispatcher is None:
        return None

    request = OriginDispatch(route=route, text=text)
    try:
        accepted = dispatcher(request)
    except Exception:
        return None
    if accepted is False:
        return None
    return request.future


def _reset_for_tests() -> None:
    with _lock:
        _routes.clear()
        _runtimes.clear()
    _origin.set((None, None))
