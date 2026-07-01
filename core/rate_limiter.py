"""
Rate Limiter — per-user sliding window, for public/unattended comms
channels (Discord today; anything else owner=False in the future).

Deliberately keyed on the SENDER's user ID, not the channel_id used
elsewhere for agent caching and PIN state. One person spamming a channel
must not lock out everyone else in it. See
LUMINA_SECURITY_HARDENING_BLUEPRINT_3.md, Part 6 / 2G-2H.

In-memory, same lifecycle as pin_gate.py's session state - resets on
restart by design. A restart-persistent abuse list is a different,
larger feature (ban list, moderation tooling) and not what this is.
"""
import time

WINDOW_SECONDS = 60
MAX_PER_WINDOW = 8          # messages per user per window before throttling
COOLDOWN_SECONDS = 30       # how long a throttled user waits after tripping it

_hits: dict = {}            # user_id -> [timestamps]
_throttled_until: dict = {}  # user_id -> unix timestamp


def check(user_id) -> tuple:
    """Call BEFORE doing any work for an inbound message.
    Returns (allowed: bool, retry_after_seconds: float).
    Records the hit only when allowed=True — a throttled user's repeated
    attempts during cooldown don't extend their own cooldown further,
    same fail-closed-but-not-punitive shape as pin_gate's lockout."""
    user_id = str(user_id)
    now = time.time()

    until = _throttled_until.get(user_id, 0)
    if now < until:
        return False, until - now

    hits = [t for t in _hits.get(user_id, []) if now - t < WINDOW_SECONDS]
    if len(hits) >= MAX_PER_WINDOW:
        _throttled_until[user_id] = now + COOLDOWN_SECONDS
        _hits[user_id] = hits
        return False, COOLDOWN_SECONDS

    hits.append(now)
    _hits[user_id] = hits
    return True, 0.0


def reset(user_id):
    """Owner/moderation escape hatch — clear a user's window and any
    active throttle. Not wired to anything yet; exists for when a mod
    tool or command needs it."""
    user_id = str(user_id)
    _hits.pop(user_id, None)
    _throttled_until.pop(user_id, None)
