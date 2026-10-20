"""TELEGRAM-OWNER-INBOUND-WAKE-01 -- pure inbound policy for the Telegram
listener. Qt-free and network-free, so its tests block in the main CI gate.

What it decides:
  * whether the owner configuration is valid enough to start a listener
    (a well-formed bot token, a positive private chat id);
  * whether an inbound message is too late, or dated in the future, to run;
  * the fixed replies Lumina sends when it refuses something (never echoing
    the inbound text), and the per-start cap on how many it sends;
  * the durable duplicate-claim key for an inbound event.

The bridge (comms/telegram_bridge.py) applies these, in a fixed order,
before routing, any turn, or any model sees an update.
"""
from __future__ import annotations

import datetime
import math
import re
import threading
import time

# A message older than this when it reaches Lumina is never run (it was sent
# while the desktop was closed with remote access ON). Telegram dates are
# whole UTC seconds, so a message can look up to 1 s older than it is.
STALE_AFTER_S = 180
# A message dated further than this in the future means this computer's
# clock is wrong, and a wrong clock could make a stale message look fresh.
CLOCK_SKEW_S = 120
# At most this many refusal replies per listener start; then one summary.
MAX_REFUSAL_REPLIES = 5
# Longer than Telegram's ~24 h update retention plus the staleness window.
DUPLICATE_CLAIM_TTL_HOURS = 48

_TOKEN_SHAPE = re.compile(r"(\d{1,20}):[A-Za-z0-9_-]{30,}")

STALE_INBOUND_REPLY = (
    "[Lumina: this message was sent at {sent_hhmm} UTC and reached Lumina {age_min} min later, "
    "so it was not run. Nothing was changed. Send it again if you still want it.]"
)
CLOCK_SUSPECT_REPLY = (
    "[Lumina: this message's timestamp is ahead of this computer's clock, so it was not run. "
    "Nothing was changed. Check the computer's clock.]"
)
BACKLOG_SUMMARY_REPLY = (
    "[Lumina: more older messages were not run either; they won't each get a reply.]"
)
UNKNOWN_REPLY_REFUSAL = (
    "[Lumina: this is a reply to a message this Lumina session doesn't know (it may be from "
    "before a restart), so it was not run. Nothing was changed. Send it as a new message, not a "
    "reply, if you still want it.]"
)
EXPIRED_REPLY_REFUSAL = (
    "[Lumina: the message you replied to is too old to route back to its conversation, so this "
    "was not run. Nothing was changed. Send it as a new message if you still want it.]"
)
ORIGIN_UNAVAILABLE_REFUSAL = (
    "[Lumina: the conversation you replied to isn't available right now, so this was not run. "
    "Nothing was changed. Send it as a new message if you still want it.]"
)
CLOSING_REFUSAL = (
    "[Lumina closed before this could run, so it was not run. Nothing was changed. "
    "Send it again once Lumina is running.]"
)


def parse_bot_id(token) -> str | None:
    """The token's public numeric bot id, or None when the token is missing
    or not shaped like a Bot API token. Never returns the secret half."""
    if not isinstance(token, str):
        return None
    match = _TOKEN_SHAPE.fullmatch(token)
    return match.group(1) if match else None


def valid_owner_chat_id(value) -> bool:
    """The owner chat is a private chat: a positive integer id."""
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


def _epoch_seconds(date) -> int | None:
    if date is None:
        return None
    try:
        return int(date.timestamp())
    except Exception:
        return None


def message_age_verdict(date, now: float | None = None) -> str:
    """"fresh", "stale" or "clock_suspect". A message without a date (real
    updates always carry one) is not judged, matching the previous-tenant
    rule."""
    sent = _epoch_seconds(date)
    if sent is None:
        return "fresh"
    age = math.floor(time.time() if now is None else now) - sent
    if age > STALE_AFTER_S:
        return "stale"
    if age < -CLOCK_SKEW_S:
        return "clock_suspect"
    return "fresh"


def sent_before(date, since) -> bool:
    """True when the message predates `since` (unix seconds)."""
    sent = _epoch_seconds(date)
    if sent is None or since is None:
        return False
    try:
        return date.timestamp() < int(since)
    except Exception:
        return False


def stale_reply(date, now: float | None = None) -> str:
    sent = _epoch_seconds(date) or 0
    current = math.floor(time.time() if now is None else now)
    sent_hhmm = datetime.datetime.fromtimestamp(sent, datetime.timezone.utc).strftime("%H:%M")
    return STALE_INBOUND_REPLY.format(sent_hhmm=sent_hhmm, age_min=max(0, (current - sent) // 60))


class RefusalBudget:
    """Caps refusal replies per listener start: the first
    MAX_REFUSAL_REPLIES go out as-is, the next one is a single summary, and
    after that refusals are silent (still logged by the caller)."""

    def __init__(self, limit: int = MAX_REFUSAL_REPLIES):
        self._limit = limit
        self._sent = 0
        self._lock = threading.Lock()

    def next_reply(self, text: str) -> str | None:
        with self._lock:
            self._sent += 1
            if self._sent <= self._limit:
                return text
            if self._sent == self._limit + 1:
                return BACKLOG_SUMMARY_REPLY
            return None

    def reset(self) -> None:
        with self._lock:
            self._sent = 0


def inbound_claim_id(event_id: str) -> str:
    """The durable at-most-once key for one inbound text event. Its own
    namespace: never the image-approval hook's raw event id, and never
    anything /resume (Gatling C1) will consume."""
    from core.idempotency import make_request_id
    return make_request_id("telegram_inbound", event_id)
