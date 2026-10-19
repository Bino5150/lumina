"""Telegram outbound tools — send through the Bot API and keep replies live."""
import requests
import config
from comms import telegram_access
from comms import telegram_origin_routing as origin_routing
from core.redaction import redact_telegram_bot_tokens
from core.secrets import get_secret
from core.idempotency import make_request_id, check, record
from core.persistence import load as load_prefs

API_BASE = "https://api.telegram.org/bot{token}"
# TELEGRAM-MASTER-SWITCH-01 -- remote access OFF blocks sending too: a lost
# phone must not receive screenshots or reports.
ACCESS_OFF_RESULT = "[Telegram remote access is off — nothing was sent.]"


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


def _listener_running() -> bool:
    """Whether an inbound listener is up to receive replies. Read only:
    an outbound send never starts, restarts or enables listening
    (TELEGRAM-MASTER-SWITCH-01 removed the old outbound auto-start; automatic
    listening belongs to TELEGRAM-OWNER-INBOUND-WAKE-01)."""
    try:
        from comms.telegram_bridge import is_running
        return bool(is_running())
    except Exception:
        return False


def _reply_path_warning() -> str | None:
    return None if _listener_running() else "no Telegram listener is running, so replies can't come back yet"


def _with_bridge_warning(result: str, warning: str | None) -> str:
    if warning is None:
        return result
    return f"{result} [Telegram reply bridge unavailable — {warning}]"


def _send_error(exc) -> str:
    """requests puts the request URL in HTTPError/ConnectionError messages,
    and the Bot API URL carries the bot token. This text becomes a tool
    result (model context, chat history, Flight Recorder), so the token is
    redacted; the rest of the error stays (TELEGRAM-BOT-TOKEN-LOG-01)."""
    return f"[Telegram send error: {redact_telegram_bot_tokens(str(exc))}]"


def _message_id(resp):
    """Extract the Bot API delivery identity without making send success
    depend on an optional or malformed response body."""
    try:
        payload = resp.json()
        message_id = payload["result"]["message_id"]
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        return message_id
    except Exception:
        return None


def _record_origin(resp, chat_id) -> None:
    message_id = _message_id(resp)
    if message_id is not None:
        try:
            origin_routing.record_outbound(
                destination_chat_id=chat_id,
                telegram_message_id=message_id,
            )
        except Exception:
            # Routing is an additive return path. It must never rewrite an
            # already-successful Bot API delivery into a send failure.
            pass


def send_telegram_file(path: str, caption: str = "") -> str:
    if not telegram_access.remote_access_enabled():
        return ACCESS_OFF_RESULT
    request_id = make_request_id("send_telegram_file", path, caption)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = _owner_chat_id()
    if not token or not chat_id:
        return "[Telegram not configured — missing bot token or owner chat id.]"
    bridge_warning = _reply_path_warning()
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                API_BASE.format(token=token) + "/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": f}, timeout=30,
            )
        resp.raise_for_status()
        if bridge_warning is None:
            _record_origin(resp, chat_id)
        result = f"[Sent '{path}' to Telegram.]"
        record(request_id, result)
        return _with_bridge_warning(result, bridge_warning)
    except FileNotFoundError:
        return _with_bridge_warning(f"[File not found: {path}]", bridge_warning)
    except Exception as e:
        return _with_bridge_warning(_send_error(e), bridge_warning)


def send_telegram_message(text: str) -> str:
    """Proactive notification — e.g. a completed task alert."""
    if not telegram_access.remote_access_enabled():
        return ACCESS_OFF_RESULT
    request_id = make_request_id("send_telegram_message", text)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = _owner_chat_id()
    if not token or not chat_id:
        return "[Telegram not configured.]"
    bridge_warning = _reply_path_warning()
    try:
        resp = requests.post(
            API_BASE.format(token=token) + "/sendMessage",
            data={"chat_id": chat_id, "text": text[:4096]}, timeout=15,
        )
        resp.raise_for_status()
        if bridge_warning is None:
            _record_origin(resp, chat_id)
        result = "[Message sent.]"
        record(request_id, result)
        return _with_bridge_warning(result, bridge_warning)
    except Exception as e:
        return _with_bridge_warning(_send_error(e), bridge_warning)


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
