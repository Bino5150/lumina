"""Turn-scoped provenance for the first Chrome navigation action.

Only a fresh, authenticated owner ingress event can supply a destination.
The model can select that destination, but cannot supply a different one by
quoting a page, an old turn, or a tool result. This module stores no URL past
the lifetime of the admitting chat() call.
"""
from __future__ import annotations

import contextvars
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from chrome_companion import policy, protocol

_OWNER_COMMAND = re.compile(
    r"^\s*(?:please\s+)?(?:open|visit|go\s+to|navigate\s+to)\s+(\S+)", re.IGNORECASE
)
_BARE_HOST = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z0-9-]+(?::[0-9]{1,5})?(?:[/?#].*)?$", re.I)
_current: contextvars.ContextVar["OwnerURLGrant | None"] = contextvars.ContextVar(
    "chrome_companion_owner_url", default=None
)


def normalize_owner_url(raw: str) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > protocol.MAX_TAB_URL_CHARS:
        return None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in raw) or "\\" in raw:
        return None
    value = raw if "://" in raw else f"https://{raw}" if _BARE_HOST.fullmatch(raw) else raw
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None or parts.fragment:
            return None
        verdict = policy.classify_url(value)
    except ValueError:
        return None
    return value if verdict.readable else None


@dataclass
class OwnerURLGrant:
    event_id: str
    url: str
    nonce: str
    consumed: bool = False

    def claim(self, value: str) -> bool:
        if self.consumed or normalize_owner_url(value) != self.url:
            return False
        self.consumed = True
        # A transport replay of one authenticated owner event must not mint
        # another URL action after a process restart. This durable namespace
        # is distinct from image-approval and other idempotency claims.
        try:
            from core import idempotency
            request_id = hashlib.sha256(
                ("chrome_companion_owner_url_event\0" + self.event_id).encode("utf-8")
            ).hexdigest()
            return idempotency.claim(request_id, ttl_hours=24 * 365 * 10)
        except Exception:
            return False  # ledger loss cannot grant browser authority


def begin_turn(user_input, *, source: str, owner: bool, event_id: str):
    """Install a one-use URL grant only for an explicit owner command.

    The command must begin the owner's text. Quoted webpage instructions and
    incidental links elsewhere in a message do not mint navigation authority.
    """
    grant = None
    if owner and source == "OWNER_DIRECT" and isinstance(user_input, str) and event_id:
        match = _OWNER_COMMAND.match(user_input)
        if match:
            url = normalize_owner_url(match.group(1))
            if url is not None:
                grant = OwnerURLGrant(event_id=event_id, url=url, nonce=secrets.token_hex(16))
    return _current.set(grant)


def end_turn(token) -> None:
    _current.reset(token)


def claim_owner_url(value: str) -> tuple[str, str] | None:
    grant = _current.get()
    if grant is None or not grant.claim(value):
        return None
    return grant.url, grant.nonce
