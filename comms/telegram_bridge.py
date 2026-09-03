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
from comms import telegram_origin_routing as origin_routing
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


def _same_numeric_chat_id(actual, configured) -> bool:
    if isinstance(actual, bool) or isinstance(configured, bool):
        return False
    try:
        return int(str(actual).strip()) == int(str(configured).strip())
    except (TypeError, ValueError):
        return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    owner_id = _owner_chat_id()

    if not owner_id or not _same_numeric_chat_id(chat_id, owner_id):
        log.warning(f"[TELEGRAM] Rejected message from unauthorized chat_id={chat_id}")
        return  # silent drop — no reply, no acknowledgment

    # 3A.2 Part G blast door: covers the brief window between an emergency
    # stop being requested and the polling loop actually noticing
    # _stop_event and tearing itself down. Silent drop, same as the
    # unauthorized-sender case above — no reply, because the whole point of
    # an E-stop is closing ingress, not conversing through it while it
    # closes.
    from core import emergency_stop
    if emergency_stop.is_latched():
        log.info("[TELEGRAM] Dropped inbound message — emergency stop is active.")
        return

    text = update.message.text
    if not text:
        return

    reply_to = getattr(update.message, "reply_to_message", None)
    reply_to_message_id = getattr(reply_to, "message_id", None)
    resolution = origin_routing.resolve(
        destination_chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
    )
    fallback_note = None
    if resolution.reason == "ambiguous":
        fallback_note = "[No unique Telegram origin; telegram-owner fallback.]"
    elif resolution.reason == "expired":
        fallback_note = "[Telegram origin route expired; telegram-owner fallback.]"
    if resolution.route is not None:
        routed = origin_routing.dispatch(resolution.route, text)
        if routed is not None:
            try:
                reply = await asyncio.wrap_future(routed)
            except asyncio.CancelledError:
                # A local emergency stop may win after the initial blast-door
                # check but before Qt admits the queued turn. Ingress remains
                # silent and closed in that race too.
                return
            except Exception:
                if emergency_stop.is_latched():
                    return
                fallback_note = "[Origin unavailable; telegram-owner fallback.]"
            else:
                if emergency_stop.is_latched():
                    return
                await update.message.reply_text(reply)
                return
        else:
            if emergency_stop.is_latched():
                return
            fallback_note = "[Origin unavailable; telegram-owner fallback.]"

    # Offloaded to a thread — run_headless_turn() is a blocking synchronous
    # call (agent.chat() -> requests.post() to llama-server). Without this,
    # a slow generation freezes the entire polling event loop for its full
    # duration, which can cause Telegram to redeliver the update once the
    # loop finally thaws (observed live: a single message producing two
    # separate agent turns). Same fix already applied to Discord in S36b.
    if emergency_stop.is_latched():
        return
    result = await asyncio.to_thread(run_headless_turn, task=text, channel_id=CHANNEL_ID, owner=True)
    reply = result["response"] if result["success"] else f"[Lumina error: {result['error']}]"
    if fallback_note:
        reply = f"{fallback_note}\n{reply}"
    if not emergency_stop.is_latched():
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

    from core import emergency_stop
    if emergency_stop.is_latched():
        return False, "Emergency stop is active — the bridge cannot start until it's re-armed."

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
    """Called from the GUI Settings toggle — blocking, with a bounded
    join(). On a clean stop within the timeout this is unchanged from
    before: clears the thread handle and reports "Bridge stopped."

    Hardened (3A.2 Part G): if join(timeout=5) expires but the thread is
    still genuinely alive, this must NOT clear _thread/_stop_event —
    doing so used to make is_running() falsely report False for a bridge
    that was actually still shutting down (or never even stopping at
    all), which is exactly the kind of lie an emergency-stop readiness
    check (is_running()) can't tolerate. Report the truthful still-
    shutting-down state instead and leave enough state for a later
    stop_bridge()/request_stop_bridge() call, or is_running(), to remain
    correct."""
    global _thread, _stop_event

    if _thread is None or not _thread.is_alive():
        return False, "Bridge not running."

    _stop_event.set()
    _thread.join(timeout=5)

    if _thread.is_alive():
        return False, "Stop requested — bridge is still shutting down."

    _thread = None
    _stop_event = None
    return True, "Bridge stopped."


def request_stop_bridge() -> bool:
    """Non-blocking stop request for the OH SHIT path (3A.2 Part G) —
    sets the existing stop Event and returns immediately without ever
    join()ing the bridge thread, so the emergency activation method never
    blocks the Qt main thread waiting on network I/O to unwind.
    is_running() remains the truthful signal for when the bridge has
    actually finished stopping. Returns True if a running bridge was
    signalled, False if there was nothing to signal."""
    if _thread is None or not _thread.is_alive():
        return False
    if _stop_event is not None:
        _stop_event.set()
    return True


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
