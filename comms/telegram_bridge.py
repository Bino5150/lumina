"""
Telegram bridge — trusted single-user channel: owner=True, no PIN gate, no
provenance tagging on inbound text. The ENTIRE justification for that trust
rests on the chat_id check below. Without it, anyone who finds this bot's
username gets owner=True treatment — full toolset, no gate, nothing.
Do not remove it. Do not make it conditional. Do not "simplify" it later.

Run standalone: python -m comms.telegram_bridge
"""
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

import config
from core.headless import run_headless_turn
from core.secrets import get_secret

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram_bridge")

CHANNEL_ID = "telegram-owner"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id != config.TELEGRAM_OWNER_CHAT_ID:
        log.warning(f"[TELEGRAM] Rejected message from unauthorized chat_id={chat_id}")
        return  # silent drop — no reply, no acknowledgment

    text = update.message.text
    if not text:
        return

    result = run_headless_turn(task=text, channel_id=CHANNEL_ID, owner=True)
    reply = result["response"] if result["success"] else f"[Lumina error: {result['error']}]"
    await update.message.reply_text(reply)


def main():
    token = get_secret("telegram_bot_token")
    if not token:
        log.error("telegram_bot_token not set — see core/secrets.py, set_secret().")
        return
    if not config.TELEGRAM_OWNER_CHAT_ID:
        log.error("TELEGRAM_OWNER_CHAT_ID not set in config.py — refusing to start unsafe.")
        return
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Telegram bridge starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()