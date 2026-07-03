"""
Telegram outbound tool — push a file or message to the owner via the Bot
API directly. Independent of the polling bridge process.
"""
import requests
import config
from core.secrets import get_secret
from core.idempotency import make_request_id, check, record

API_BASE = "https://api.telegram.org/bot{token}"


def send_telegram_file(path: str, caption: str = "") -> str:
    request_id = make_request_id("send_telegram_file", path, caption)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = config.TELEGRAM_OWNER_CHAT_ID
    if not token or not chat_id:
        return "[Telegram not configured — missing bot token or owner chat id.]"
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
        return result
    except FileNotFoundError:
        return f"[File not found: {path}]"
    except Exception as e:
        return f"[Telegram send error: {e}]"


def send_telegram_message(text: str) -> str:
    """Proactive notification — e.g. a completed task alert."""
    request_id = make_request_id("send_telegram_message", text)
    cached = check(request_id)
    if cached:
        return f"[Duplicate suppressed — already sent: {cached}]"

    token = get_secret("telegram_bot_token")
    chat_id = config.TELEGRAM_OWNER_CHAT_ID
    if not token or not chat_id:
        return "[Telegram not configured.]"
    try:
        resp = requests.post(
            API_BASE.format(token=token) + "/sendMessage",
            data={"chat_id": chat_id, "text": text[:4096]}, timeout=15,
        )
        resp.raise_for_status()
        result = "[Message sent.]"
        record(request_id, result)
        return result
    except Exception as e:
        return f"[Telegram send error: {e}]"


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