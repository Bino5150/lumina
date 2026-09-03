"""Telegram outbound tools — send through the Bot API and keep replies live."""
import threading

import requests
import config
from core.secrets import get_secret
from core.idempotency import make_request_id, check, record
from core.persistence import load as load_prefs

API_BASE = "https://api.telegram.org/bot{token}"
_bridge_start_lock = threading.Lock()


def _owner_chat_id():
    """prefs.json (settable via the Communications tab) takes priority;
    config.py is the fallback for anyone who set it there manually before
    the UI field existed. Mirrors comms/telegram_bridge.py's resolution —
    duplicated rather than imported so this module doesn't pull in
    python-telegram-bot (an optional dep) just to send a file."""
    from_prefs = load_prefs().get("telegram_owner_chat_id")
    if from_prefs:
        return from_prefs
    return config.TELEGRAM_OWNER_CHAT_ID


def _ensure_reply_bridge() -> str | None:
    """Start the established inbound bridge once when an outbound send needs it.

    Returns a safe warning when the reply path could not be established.  The
    caller still performs the existing outbound send: bridge startup and Bot
    API delivery are independent, and a failed listener must not silently
    discard an otherwise deliverable alert.
    """
    try:
        from comms.telegram_bridge import is_running, start_bridge
    except Exception as e:
        return f"could not load ({type(e).__name__})"

    try:
        if is_running():
            return None
        # Tool dispatch is sequential inside one LuminaAgent today, but other
        # owner agents/background tasks can call this shared module from
        # different threads.  Serialize only the check/start transition, then
        # re-check after acquiring the lock so one listener wins the race.
        with _bridge_start_lock:
            if is_running():
                return None
            started, message = start_bridge()
            if started or is_running():
                return None
            return f"did not start: {message}"
    except Exception as e:
        # Exception bodies can contain request/config details.  The type is
        # enough to represent failure honestly without leaking diagnostics.
        return f"failed with {type(e).__name__}"


def _with_bridge_warning(result: str, warning: str | None) -> str:
    if warning is None:
        return result
    return f"{result} [Telegram reply bridge unavailable — {warning}]"


def send_telegram_file(path: str, caption: str = "") -> str:
    request_id = make_request_id("send_telegram_file", path, caption)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = _owner_chat_id()
    if not token or not chat_id:
        return "[Telegram not configured — missing bot token or owner chat id.]"
    bridge_warning = _ensure_reply_bridge()
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                API_BASE.format(token=token) + "/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": f}, timeout=30,
            )
        resp.raise_for_status()
        result = f"[Sent '{path}' to Telegram.]"
        record(request_id, result)
        return _with_bridge_warning(result, bridge_warning)
    except FileNotFoundError:
        return _with_bridge_warning(f"[File not found: {path}]", bridge_warning)
    except Exception as e:
        return _with_bridge_warning(f"[Telegram send error: {e}]", bridge_warning)


def send_telegram_message(text: str) -> str:
    """Proactive notification — e.g. a completed task alert."""
    request_id = make_request_id("send_telegram_message", text)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = _owner_chat_id()
    if not token or not chat_id:
        return "[Telegram not configured.]"
    bridge_warning = _ensure_reply_bridge()
    try:
        resp = requests.post(
            API_BASE.format(token=token) + "/sendMessage",
            data={"chat_id": chat_id, "text": text[:4096]}, timeout=15,
        )
        resp.raise_for_status()
        result = "[Message sent.]"
        record(request_id, result)
        return _with_bridge_warning(result, bridge_warning)
    except Exception as e:
        return _with_bridge_warning(f"[Telegram send error: {e}]", bridge_warning)


def register_telegram_tools(registry):
    registry.register(
        name="send_telegram_file", fn=send_telegram_file,
        description="Send a file from Skynet to the owner's Telegram.",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
                    "required": ["path"]}
    )
    registry.register(
        name="send_telegram_message", fn=send_telegram_message,
        description="Send a proactive text notification to the owner's Telegram.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    )
