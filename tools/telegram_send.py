"""
Telegram outbound tool — push a file or message to the owner via the Bot
API directly. Independent of the polling bridge process.
"""
import requests
import config
from core.secrets import get_secret

API_BASE = "https://api.telegram.org/bot{token}"


def send_telegram_file(path: str, caption: str = "") -> str:
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
        return f"[Sent '{path}' to Telegram.]"
    except FileNotFoundError:
        return f"[File not found: {path}]"
    except Exception as e:
        return f"[Telegram send error: {e}]"


def send_telegram_message(text: str) -> str:
    """Proactive notification — e.g. a completed task alert."""
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
        return "[Message sent.]"
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