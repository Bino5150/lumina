"""Telegram /resume recovery-handler skeleton (OPERATOR-INGRESS-PROVENANCE-01).

Runaway-suspension recovery (AGENT-RUNAWAY-SUSPEND-01) will accept
`/resume <selector>` from exactly two authority channels: the local desktop
chat and the owner's verified Telegram. This module is the Telegram side's
ingress check, built ahead of the suspension store so the boundary exists
and is tested before anything relies on it.

**There is no resume capability here.** A fully verified owner event gets a
fixed "not active" reply and changes nothing. Freshness against an incident
and claim-once event consumption need the suspension store and arrive with
it (Slice C1/C3).

A recovery event is accepted only when every check passes; any failure is a
silent drop with a logged reason code (never the message text):
  * owner configured, and chat id AND sender id both equal it;
  * private chat; sender is not a bot; no sender_chat (channel/anonymous);
  * not edited (an edited_message update, or an edit_date);
  * not forwarded (forward_origin, automatic forward) and not via a bot;
  * exact grammar: "/resume[@<this bot>] <8 Crockford base32> [ack-unknown]",
    with no leading or extra whitespace, and any bot mention equal to the
    running bot's username.
"""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("telegram_recovery")

RECOVERY_COMMAND = "resume"
SELECTOR_LENGTH = 8

# Crockford base32: digits plus letters without I, L, O, U. Case-insensitive.
_GRAMMAR = re.compile(
    r"/resume(?:@(?P<bot>[A-Za-z0-9_]{1,64}))? "
    r"(?P<selector>[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{%d})"
    r"(?P<ack> ack-unknown)?" % SELECTOR_LENGTH
)

RECOVERY_NOT_ACTIVE_REPLY = (
    "[Lumina: /resume is reserved for runaway-suspension recovery, which is not "
    "active in this version. Nothing was changed.]"
)


class RecoveryRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedTelegramRecovery:
    """Runtime-created after every check passed. Carries only identity the
    runtime derived from authenticated update metadata, plus the parsed
    selector."""
    event_id: str
    selector: str
    ack_unknown: bool
    message_date: datetime.datetime


def _same_id(actual, configured) -> bool:
    if isinstance(actual, bool) or isinstance(configured, bool):
        return False
    try:
        return int(str(actual).strip()) == int(str(configured).strip())
    except (TypeError, ValueError):
        return False


def verify_recovery_update(update, *, owner_chat_id, bot_username) -> VerifiedTelegramRecovery:
    """Return the verified envelope, or raise RecoveryRejected(reason)."""
    if owner_chat_id in (None, ""):
        raise RecoveryRejected("owner_unset")
    if getattr(update, "edited_message", None) is not None:
        raise RecoveryRejected("edited")
    message = getattr(update, "message", None)
    if message is None:
        raise RecoveryRejected("malformed")
    chat = message.chat
    if getattr(chat, "type", None) != "private":
        raise RecoveryRejected("not_private")
    sender = message.from_user
    if (sender is None or sender.is_bot
            or not _same_id(chat.id, owner_chat_id) or not _same_id(sender.id, owner_chat_id)):
        raise RecoveryRejected("owner_mismatch")
    if message.sender_chat is not None:
        raise RecoveryRejected("sender_chat")
    if message.edit_date is not None:
        raise RecoveryRejected("edited")
    if message.forward_origin is not None or message.is_automatic_forward:
        raise RecoveryRejected("forwarded")
    if message.via_bot is not None:
        raise RecoveryRejected("via_bot")
    text = message.text if isinstance(message.text, str) else ""
    match = _GRAMMAR.fullmatch(text)
    if match is None:
        raise RecoveryRejected("malformed")
    mention = match.group("bot")
    if mention is not None and (not bot_username or mention.lower() != str(bot_username).lower()):
        raise RecoveryRejected("wrong_bot")
    return VerifiedTelegramRecovery(
        event_id=f"telegram:{chat.id}:{message.message_id}",
        selector=match.group("selector").upper(),
        ack_unknown=match.group("ack") is not None,
        message_date=message.date,
    )


def _owner_chat_id():
    from comms.telegram_bridge import _owner_chat_id as owner
    return owner()


async def handle_recovery_command(update, context):
    """CommandHandler callback for /resume. Skeleton: verified owner events
    get RECOVERY_NOT_ACTIVE_REPLY; everything else is dropped silently."""
    from core import emergency_stop
    if emergency_stop.is_latched():
        log.info("[TELEGRAM] Dropped recovery command — emergency stop is active.")
        return
    from comms import telegram_access
    if not telegram_access.remote_access_enabled():
        log.info("[TELEGRAM] Dropped recovery command — Telegram remote access is off.")
        return
    try:
        verify_recovery_update(update, owner_chat_id=_owner_chat_id(),
                               bot_username=getattr(getattr(context, "bot", None), "username", None))
    except RecoveryRejected as rejected:
        log.warning(f"[TELEGRAM] Rejected recovery command ({rejected.reason}).")
        return
    await update.message.reply_text(RECOVERY_NOT_ACTIVE_REPLY)
