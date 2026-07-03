"""
Telegram bridge — trusted single-user channel: owner=True, no PIN gate, no
provenance tagging on inbound text. The ENTIRE justification for that trust
rests on the chat_id check below. Without it, anyone who finds this bot's
username gets owner=True treatment — full toolset, no gate, nothing.
Do not remove it. Do not make it conditional. Do not "simplify" it later.

Run standalone: python -m comms.telegram_bridge
Run embedded (GUI toggle): see start_bridge() / stop_bridge() below.
"""
import asyncio
import logging
import threading
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

import config
from core.headless import run_headless_turn
from core.secrets import get_secret
from core.persistence import load as load_prefs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram_bridge")

CHANNEL_ID = "telegram-owner"

# ── Embedded lifecycle state (module-level, single instance by design —
# there's only ever one Telegram bridge for one owner) ──
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def _owner_chat_id():
    """prefs.json (settable via the Communications tab) takes priority;
    config.py is the fallback for anyone who set it there manually before
    the UI field existed. Returns None if neither is set."""
    from_prefs = load_prefs().get("telegram_owner_chat_id")
    if from_prefs:
        return from_prefs
    return config.TELEGRAM_OWNER_CHAT_ID


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    owner_id = _owner_chat_id()

    if not owner_id or str(chat_id) != str(owner_id):
        log.warning(f"[TELEGRAM] Rejected message from unauthorized chat_id={chat_id}")
        return  # silent drop — no reply, no acknowledgment

    text = update.message.text
    if not text:
        return

    # Offloaded to a thread — run_headless_turn() is a blocking synchronous
    # call (agent.chat() -> requests.post() to llama-server). Without this,
    # a slow generation freezes the entire polling event loop for its full
    # duration, which can cause Telegram to redeliver the update once the
    # loop finally thaws (observed live: a single message producing two
    # separate agent turns). Same fix already applied to Discord in S36b.
    result = await asyncio.to_thread(run_headless_turn, task=text, channel_id=CHANNEL_ID, owner=True)
    reply = result["response"] if result["success"] else f"[Lumina error: {result['error']}]"
    await update.message.reply_text(reply)


async def _run_until_stopped(stop_event: threading.Event, token: str):
    """Non-blocking-equivalent polling loop, stoppable from another thread.
    This is what run_polling() does internally, broken apart so we can
    check stop_event between initialize/start/updater.start_polling and
    the eventual clean shutdown, instead of blocking forever."""
    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    log.info("[TELEGRAM] Bridge started (embedded).")

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        log.info("[TELEGRAM] Bridge stopped (embedded).")


def start_bridge() -> tuple[bool, str]:
    """Called from the GUI toggle. Returns (success, message)."""
    global _thread, _stop_event

    if _thread is not None and _thread.is_alive():
        return False, "Bridge already running."

    token = get_secret("telegram_bot_token")
    if not token:
        return False, "Bot token not set — configure it above first."
    if not _owner_chat_id():
        return False, "Owner chat ID not set — configure it above first."

    _stop_event = threading.Event()

    def _runner():
        asyncio.run(_run_until_stopped(_stop_event, token))

    _thread = threading.Thread(target=_runner, daemon=True)
    _thread.start()
    return True, "Bridge started."


def stop_bridge() -> tuple[bool, str]:
    """Called from the GUI toggle. Returns (success, message)."""
    global _thread, _stop_event

    if _thread is None or not _thread.is_alive():
        return False, "Bridge not running."

    _stop_event.set()
    _thread.join(timeout=5)
    _thread = None
    _stop_event = None
    return True, "Bridge stopped."


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def main():
    """Standalone terminal entry point — unchanged behavior for anyone who
    still wants to run this as a separate script instead of toggling it
    from the GUI."""
    token = get_secret("telegram_bot_token")
    if not token:
        log.error("telegram_bot_token not set — see core/secrets.py, set_secret().")
        return
    if not _owner_chat_id():
        log.error("Owner chat ID not set — refusing to start unsafe.")
        return
    stop_event = threading.Event()
    asyncio.run(_run_until_stopped(stop_event, token))


if __name__ == "__main__":
    main()