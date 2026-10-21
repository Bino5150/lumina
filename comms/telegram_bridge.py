"""
Telegram bridge — trusted single-user channel: owner=True, no PIN gate, no
provenance tagging on inbound text. The ENTIRE justification for that trust
rests on the chat_id check below. Without it, anyone who finds this bot's
username gets owner=True treatment — full toolset, no gate, nothing.
Do not remove it. Do not make it conditional. Do not "simplify" it later.

Run standalone: python -m comms.telegram_bridge
Run embedded: see start_bridge() / stop_bridge() below.

Every start, every inbound update and the polling loop itself obey the
persistent Telegram remote-access switch (comms/telegram_access.py,
TELEGRAM-MASTER-SWITCH-01): while it is OFF nothing starts and a running
listener shuts itself down.

TELEGRAM-OWNER-INBOUND-WAKE-01: the desktop listens whenever it runs and
remote access is ON. The listener runs exactly when the GUI is running, the
switch is ON, the owner configuration is valid, the emergency stop is not
latched, and this process holds the single-consumer lock
(comms/telegram_poller_lock.py). run_gui() starts it at launch, the Settings
switch starts it when flipped ON, and the polling loop below survives an
offline launch, reports its health truthfully and yields to another poller.
Every inbound update passes one gate (comms/telegram_ingress_policy.py)
before routing, any turn or any model.
"""
import os
from data_directory import require_data_directory
require_data_directory(base_dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import collections
import logging
import threading
import time
from telegram import Update
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import config
from comms import telegram_access
from comms import telegram_ingress_policy as ingress_policy
from comms import telegram_origin_routing as origin_routing
from comms import telegram_log_safety
from comms import telegram_poller_lock
from comms import telegram_recovery
from core import suspension, suspension_runtime
from core.headless import (
    headless_reply_text,
    headless_result_delivery_text,
    mark_headless_result_presented,
    run_headless_turn,
)
from core.operator_ingress import RELAYED_COMMAND_REFUSAL, looks_like_operator_command
from core.secrets import get_secret
from core.persistence import load as load_prefs

logging.basicConfig(level=logging.INFO)
# TELEGRAM-BOT-TOKEN-LOG-01: the token rides in every Bot API URL; keep it
# out of httpx's request lines and anything else that reaches the handler.
telegram_log_safety.install()
log = logging.getLogger("telegram_bridge")
# Polling errors keep python-telegram-bot's own log line and traceback (the
# root handler's TELEGRAM-BOT-TOKEN-LOG-01 filter redacts the token).
_ptb_updater_log = logging.getLogger("telegram.ext.Updater")

CHANNEL_ID = "telegram-owner"

# Long-poll timeout passed to getUpdates (R2 D-G: measured in live acceptance).
POLL_TIMEOUT_S = 10
# Bootstrap retry while Telegram is unreachable at start (offline launch).
BOOTSTRAP_BACKOFF_S = (2, 4, 8, 16)
BACKOFF_CAP_S = 30
# Another consumer on this bot: this many 409s within the window -> yield.
CONFLICT_LIMIT = 3
CONFLICT_WINDOW_S = 120
# The name python-telegram-bot 22.x gives its polling task. Its retry loop
# aborts on InvalidToken while Updater.running stays True, so the loop below
# watches the task itself.
_PTB_POLLING_TASK_NAME = "Updater:start_polling:polling_task"

# ── Embedded lifecycle state (module-level, single instance by design —
# there's only ever one Telegram bridge for one owner) ──
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_start_lock = threading.Lock()
# Set by begin_shutdown() while a listener thread is alive: the desktop is
# closing, so nothing new may run (no headless turn after Prime closes).
_closing = False
_refusals = ingress_policy.RefusalBudget()
_health_lock = threading.Lock()
_health = {"state": "stopped"}


def health() -> dict:
    """A copy of the listener's health snapshot, for Settings and status:
    state is one of stopped, starting, retrying, listening, not_configured,
    token_malformed, token_rejected, lock_held, conflict, error."""
    with _health_lock:
        return dict(_health)


def _set_health(state: str, **detail) -> None:
    with _health_lock:
        _health.clear()
        _health.update(state=state, since=time.time(), **detail)


def is_closing() -> bool:
    return _closing


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


def _sent_while_access_was_off(message) -> bool:
    """True when the message predates the owner's last OFF→ON flip. A
    message without a date (real updates always carry one) is not judged
    stale."""
    return ingress_policy.sent_before(getattr(message, "date", None), telegram_access.on_since())


def freshness_refusal(message) -> str | None:
    """The fixed refusal for a message that must not run because of when it
    was sent, or None. Shared by text and /resume, so the two can never
    drift apart (R2 F13): the previous tenant's mail first, then transport
    staleness and clock skew."""
    if _sent_while_access_was_off(message):
        return telegram_access.STALE_BACKLOG_REPLY
    date = getattr(message, "date", None)
    verdict = ingress_policy.message_age_verdict(date)
    if verdict == "stale":
        return ingress_policy.stale_reply(date)
    if verdict == "clock_suspect":
        return ingress_policy.CLOCK_SUSPECT_REPLY
    return None


async def send_refusal(message, text: str) -> None:
    """A capped refusal reply (at most MAX_REFUSAL_REPLIES per listener
    start, then one summary, then silence)."""
    capped = _refusals.next_reply(text)
    if capped is not None:
        await message.reply_text(capped)


def _claim_inbound(event_id: str) -> bool:
    """Durable at-most-once admission for one inbound text event: True only
    for the first delivery, across redeliveries and restarts. A claim-store
    failure fails closed -- an instruction like "stop the print" must never
    run twice, and the owner can resend."""
    from core import idempotency
    try:
        return idempotency.claim(ingress_policy.inbound_claim_id(event_id),
                                 ttl_hours=ingress_policy.DUPLICATE_CLAIM_TTL_HOURS)
    except Exception as exc:
        log.error(f"[TELEGRAM] Inbound claim store failed ({type(exc).__name__}); not running {event_id}.")
        return False


def _remember_headless_answer(chat_id, sent) -> None:
    """Best effort: if it fails, a later swipe-reply to this answer is
    refused as unknown (the safe direction), never run elsewhere."""
    message_id = getattr(sent, "message_id", None)
    if message_id is None:
        return
    try:
        origin_routing.record_headless_reply(destination_chat_id=chat_id, telegram_message_id=message_id)
    except Exception as exc:
        log.info(f"[TELEGRAM] Could not remember a headless answer ({type(exc).__name__}).")


def _admitted_headless_turn(authority, text: str, approval_event_id: str, suspension_token=None):
    """Final admission for the headless channel, on the worker thread right
    before the turn starts (R3 C-1, C-4). None means it never started.
    suspension_token is the one captured at intake (AGENT-RUNAWAY-SUSPEND-01),
    carried unchanged -- a fallback never mints a fresh one."""
    if _closing or not telegram_access.ingress_authority_valid(authority):
        log.info(f"[TELEGRAM] Headless turn not admitted for {approval_event_id}: "
                 "remote access authority changed or Lumina is closing.")
        return None
    return run_headless_turn(
        task=text, channel_id=CHANNEL_ID, owner=True,
        approval_event_id=approval_event_id,
        suspension_token=suspension_token,
    )


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

    # TELEGRAM-MASTER-SWITCH-01 -- remote access OFF closes ingress even in
    # the moment before the listener notices and shuts down. Silent, like
    # the E-stop drop above.
    if not telegram_access.remote_access_enabled():
        log.info("[TELEGRAM] Dropped inbound message — Telegram remote access is off.")
        return

    # TELEGRAM-OWNER-INBOUND-WAKE-01 -- only a new message ever runs. An
    # edit (update.message is None) keeps the original message_id and date
    # and must never re-run anything. Telegram is also asked for "message"
    # updates only, and the handler filter excludes edits.
    message = update.message
    if message is None:
        log.info("[TELEGRAM] Ignored a non-message update (an edit or similar).")
        return

    # Lumina Prime is closing: nothing new runs, not even headless.
    if _closing:
        log.info("[TELEGRAM] Refused an inbound message — Lumina is closing.")
        await message.reply_text(ingress_policy.CLOSING_REFUSAL)
        return

    # Telegram holds undelivered updates (~24 h) and hands them over when a
    # listener next polls. A message sent while remote access was OFF must
    # never run after the owner turns it back on ("the previous tenant's
    # mail"), and one sent while the desktop was closed with remote access
    # ON must not run hours later either. Telegram dates are whole seconds.
    refusal = freshness_refusal(message)
    if refusal is not None:
        log.info("[TELEGRAM] Refused a late or misdated inbound message.")
        await send_refusal(message, refusal)
        return

    text = message.text
    if not text:
        return

    # R3 C-1 -- the remote-access authority this update is admitted under.
    # Re-checked at final turn admission (Prime's origin queue, or the
    # headless worker thread) and before any reply.
    authority = telegram_access.capture_ingress_authority()
    if authority is None:
        return

    # CASTLE-WALLS-REPAIR-04 / CANNON-11 -- a stable identity for THIS
    # exact inbound Telegram event: Telegram's own server-assigned
    # message_id, unique per chat and unchanged across a transport-level
    # redelivery of the same update (see this function's own to_thread
    # comment below about observed live redelivery). Built only from
    # already-authenticated update/chat metadata above -- never from
    # `text` -- so nothing in the message body, including a spoofed or
    # quoted/forwarded "event id", can influence it. chat_id is folded in
    # too, even though this bridge only ever has one valid owner chat, so
    # the identity space can never migrate across a future multi-chat
    # change. Threaded through both the routed-dispatch and headless-
    # fallback branches below -- whichever one this delivery actually
    # takes -- all the way to core.agent's approval word-match hook.
    approval_event_id = f"telegram:{chat_id}:{message.message_id}"

    # TELEGRAM-OWNER-INBOUND-WAKE-01 -- durable at-most-once admission: a
    # redelivered update (shutdown, network fault, restart) never runs a
    # second turn, on either branch below.
    if not _claim_inbound(approval_event_id):
        log.info(f"[TELEGRAM] Dropped a duplicate delivery of {approval_event_id}.")
        return

    # OPERATOR-INGRESS-PROVENANCE-01 -- operator commands are desktop
    # authority. `~filters.COMMAND` (see _register_handlers) only excludes
    # text with a bot_command entity at offset 0, so a whitespace-prefixed or
    # entity-less " /btw" gets here. Anything the desktop parser would read
    # as a command gets a fixed refusal: no origin routing, no headless turn,
    # no model.
    if looks_like_operator_command(text):
        log.info("[TELEGRAM] Refused owner text that parses as an operator command.")
        await message.reply_text(RELAYED_COMMAND_REFUSAL)
        return

    def still_authorized() -> bool:
        return telegram_access.ingress_authority_valid(authority)

    # AGENT-RUNAWAY-SUSPEND-01 -- capture the owner execution token HERE:
    # after every WAKE-01 gate (owner, E-stop, switch, edit, closing,
    # freshness, at-most-once claim, command refusal) and BEFORE the turn
    # waits on Prime or a worker thread. The routed turn and the headless
    # fallback both carry exactly this token; a suspension that lands while
    # the request waits makes it stale, and it never runs. A suspension never
    # stops polling -- only admission.
    try:
        suspension_token = suspension_runtime.capture(suspension.OWNER_SCOPE)
    except suspension_runtime.SuspensionRefused as refused:
        log.info(f"[TELEGRAM] Refused {approval_event_id}: runaway suspension ({refused.refusal}).")
        if still_authorized():
            await message.reply_text(refused.text)
        return

    reply_to = getattr(message, "reply_to_message", None)
    reply_to_message_id = getattr(reply_to, "message_id", None)
    is_swipe_reply = reply_to_message_id is not None
    resolution = origin_routing.resolve(
        destination_chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
    )

    # R3 C-2 -- a swipe-reply runs only at its known origin. Unknown or
    # expired: an explicit refusal, never a silent headless conversation.
    if resolution.reason == "unknown_reply":
        log.info(f"[TELEGRAM] Refused {approval_event_id}: reply to an unknown message.")
        if still_authorized():
            await message.reply_text(ingress_policy.UNKNOWN_REPLY_REFUSAL)
        return
    if resolution.reason == "expired":
        log.info(f"[TELEGRAM] Refused {approval_event_id}: reply to an expired route.")
        if still_authorized():
            await message.reply_text(ingress_policy.EXPIRED_REPLY_REFUSAL)
        return

    fallback_note = None
    if resolution.reason == "ambiguous":
        fallback_note = "[No unique Telegram origin; telegram-owner fallback.]"
    if resolution.route is not None:
        routed = origin_routing.dispatch(resolution.route, text, approval_event_id=approval_event_id,
                                         admission_check=still_authorized,
                                         suspension_token=suspension_token)
        failure = None
        if routed is not None:
            try:
                reply = await asyncio.wrap_future(routed)
            except asyncio.CancelledError:
                # A local emergency stop may win after the initial blast-door
                # check but before Qt admits the queued turn. Ingress remains
                # silent and closed in that race too.
                return
            except Exception as exc:
                failure = exc
            else:
                if still_authorized():
                    await message.reply_text(reply)
                return
        if not still_authorized():
            log.info(f"[TELEGRAM] {approval_event_id} not run: remote access authority changed.")
            return
        if _closing or "origin window closed" in str(failure or ""):
            await message.reply_text(ingress_policy.CLOSING_REFUSAL)
            return
        if is_swipe_reply:
            log.info(f"[TELEGRAM] Refused {approval_event_id}: its Prime origin is unavailable.")
            await message.reply_text(ingress_policy.ORIGIN_UNAVAILABLE_REFUSAL)
            return
        fallback_note = "[Origin unavailable; telegram-owner fallback.]"

    # Offloaded to a thread — run_headless_turn() is a blocking synchronous
    # call (agent.chat() -> requests.post() to llama-server). Without this,
    # a slow generation freezes the entire polling event loop for its full
    # duration, which can cause Telegram to redeliver the update once the
    # loop finally thaws (observed live: a single message producing two
    # separate agent turns). Same fix already applied to Discord in S36b.
    if _closing:
        await message.reply_text(ingress_policy.CLOSING_REFUSAL)
        return
    if not still_authorized():
        return
    result = await asyncio.to_thread(_admitted_headless_turn, authority, text, approval_event_id,
                                     suspension_token)
    if result is None:
        if _closing and still_authorized():
            await message.reply_text(ingress_policy.CLOSING_REFUSAL)
        return
    reply = headless_reply_text(result)
    reply = headless_result_delivery_text(result, reply)
    if fallback_note:
        reply = f"{fallback_note}\n{reply}"
    if still_authorized():
        sent = await message.reply_text(reply)
        _remember_headless_answer(chat_id, sent)
        # The exact estimate becomes approvable only after Telegram confirms
        # this real owner-facing send completed successfully.
        mark_headless_result_presented(result, channel_id=CHANNEL_ID)


def _register_handlers(application) -> None:
    """Every inbound handler, in one place. /resume is the recovery-handler
    skeleton (comms/telegram_recovery.py, not active yet): new messages only,
    never edits. Every other slash command stays unhandled (dropped), as
    before; ordinary text -- new messages only, never edits -- goes to
    handle_message."""
    application.add_handler(CommandHandler(
        telegram_recovery.RECOVERY_COMMAND, telegram_recovery.handle_recovery_command,
        filters=filters.UpdateType.MESSAGE,
    ))
    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message,
    ))


def _stop_requested(stop_event: threading.Event) -> bool:
    from core import emergency_stop
    return (stop_event.is_set() or emergency_stop.is_latched()
            or not telegram_access.remote_access_enabled())


async def _wait_unless_stopped(stop_event: threading.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; True as soon as a stop, OFF or E-stop arrives."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop_requested(stop_event):
            return True
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return _stop_requested(stop_event)


def _backoff(attempt: int) -> float:
    if attempt < len(BOOTSTRAP_BACKOFF_S):
        return BOOTSTRAP_BACKOFF_S[attempt]
    return BACKOFF_CAP_S


async def _shutdown_application(application) -> None:
    """Stop whatever is running, then shut down, each step guarded: a
    polling task that died on InvalidToken re-raises from updater.stop(), a
    half-finished bootstrap leaves some parts running, and nothing here may
    leave the lock or the thread behind."""
    updater = getattr(application, "updater", None)
    steps = []
    if updater is not None and getattr(updater, "running", True):
        steps.append(("updater.stop", updater.stop))
    if getattr(application, "running", True):
        steps.append(("stop", application.stop))
    steps.append(("shutdown", application.shutdown))
    for name, step in steps:
        try:
            await step()
        except Exception as exc:
            detail = telegram_log_safety.redacted_exception_text(exc).strip().splitlines()
            log.info(f"[TELEGRAM] Bridge {name} during shutdown: {(detail or [type(exc).__name__])[-1]}")


def _find_polling_task():
    for task in asyncio.all_tasks():
        if task.get_name() == _PTB_POLLING_TASK_NAME:
            return task
    return None


async def _run_until_stopped(stop_event: threading.Event, token: str):
    """The polling supervisor, stoppable from another thread. It does what
    run_polling() does internally, broken apart so it can:
      * retry bootstrap with backoff while Telegram is unreachable (an
        offline launch never kills the listener for the whole session);
      * stop promptly on a stop request, remote access OFF (from any process
        or Kali) or an E-stop -- including while waiting to retry;
      * report a rejected token (at start or later) and stop, never retry;
      * yield to another poller on repeated 409 Conflict;
      * keep its health snapshot truthful throughout.
    Returns False when it stopped on a fault (rejected token, a 409 yield,
    polling dying), True on a requested stop."""
    attempt = 0
    while not _stop_requested(stop_event):
        application = Application.builder().token(token).build()
        _register_handlers(application)
        conflicts = collections.deque()

        def _on_poll_error(exc):
            # Must not be a coroutine function, and must not raise. Logs
            # exactly as python-telegram-bot's default callback would.
            if isinstance(exc, Conflict):
                conflicts.append(time.monotonic())
            try:
                _ptb_updater_log.error("Exception happened while polling for updates.", exc_info=exc)
            except Exception:
                pass

        _set_health("starting" if attempt == 0 else "retrying")
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(
                timeout=POLL_TIMEOUT_S,
                allowed_updates=["message"],
                drop_pending_updates=False,
                error_callback=_on_poll_error,
            )
        except InvalidToken as exc:
            _report_token_rejected(exc)
            await _shutdown_application(application)
            return False
        except NetworkError as exc:
            await _shutdown_application(application)
            delay = _backoff(attempt)
            attempt += 1
            _set_health("retrying", error=type(exc).__name__)
            log.warning(f"[TELEGRAM] Telegram unreachable ({type(exc).__name__}); retrying in {delay}s.")
            if await _wait_unless_stopped(stop_event, delay):
                return True
            continue

        _refusals.reset()
        _set_health("listening")
        log.info("[TELEGRAM] Bridge started (embedded).")
        polling_task = _find_polling_task()
        clean = True
        try:
            while not stop_event.is_set():
                # TELEGRAM-MASTER-SWITCH-01 -- OFF wins, including a flip made by
                # another process or runtime on the shared data dir.
                if not telegram_access.remote_access_enabled():
                    log.info("[TELEGRAM] Remote access turned off — stopping the bridge.")
                    origin_routing.clear_routes()
                    break
                now = time.monotonic()
                while conflicts and now - conflicts[0] > CONFLICT_WINDOW_S:
                    conflicts.popleft()
                if len(conflicts) >= CONFLICT_LIMIT:
                    _set_health("conflict")
                    log.warning("[TELEGRAM] Another poller is using this bot — this listener yields.")
                    clean = False
                    break
                if polling_task is not None and polling_task.done():
                    failure = None if polling_task.cancelled() else polling_task.exception()
                    if isinstance(failure, InvalidToken):
                        _report_token_rejected(failure)
                    else:
                        _set_health("error", error=type(failure).__name__ if failure else "stopped")
                        log.error("[TELEGRAM] Polling stopped unexpectedly; the listener stopped.")
                    clean = False
                    break
                await asyncio.sleep(0.5)
        finally:
            await _shutdown_application(application)
            log.info("[TELEGRAM] Bridge stopped (embedded).")
        return clean
    return True


def _report_token_rejected(exc) -> None:
    """A rejected token stops the listener for good (no retry). Logged the
    way the bridge always reported failures -- the redacted traceback keeps
    the diagnosis visible while the token never is."""
    _set_health("token_rejected")
    log.error(
        "[TELEGRAM] Bridge stopped on an error:\n%s",
        telegram_log_safety.redacted_exception_text(exc).rstrip(),
    )
    log.error("[TELEGRAM] Telegram rejected the bot token; the listener stopped.")


def _run_bridge(stop_event: threading.Event, token: str) -> bool:
    """Run the polling loop to completion. A failure (rejected token,
    unrecoverable network error) is reported as an ERROR log record with the
    full traceback, token redacted -- an uncaught exception would print
    python-telegram-bot's InvalidToken message, which embeds the token, to
    stderr. Returns False on failure."""
    try:
        return asyncio.run(_run_until_stopped(stop_event, token)) is not False
    except Exception as exc:
        _set_health("error", error=type(exc).__name__)
        log.error(
            "[TELEGRAM] Bridge stopped on an error:\n%s",
            telegram_log_safety.redacted_exception_text(exc).rstrip(),
        )
        return False


def start_bridge(reason: str = "manual") -> tuple[bool, str]:
    """The single start primitive for the embedded listener. Returns
    (success, message). `reason` (startup, switch_on, config_saved, manual)
    is for logging only: every reason passes the same gates. Refused while
    the emergency stop is latched or Telegram remote access is OFF
    (TELEGRAM-MASTER-SWITCH-01), without a valid owner configuration, or
    while another poller holds this bot's lock."""
    global _thread, _stop_event, _closing

    from core import emergency_stop
    if emergency_stop.is_latched():
        return False, "Emergency stop is active — the bridge cannot start until it's re-armed."
    if not telegram_access.remote_access_enabled():
        return False, telegram_access.ACCESS_OFF_MESSAGE

    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return False, "Bridge already running."

        token = get_secret("telegram_bot_token")
        if not token:
            _set_health("not_configured")
            return False, "Bot token not set — configure it above first."
        bot_id = ingress_policy.parse_bot_id(token)
        if bot_id is None:
            _set_health("token_malformed")
            return False, "Bot token looks malformed — paste it again from BotFather."
        if not ingress_policy.valid_owner_chat_id(_owner_chat_id()):
            _set_health("not_configured")
            return False, "Owner chat ID not set — configure it above first."

        try:
            lock, holder = telegram_poller_lock.acquire(bot_id)
        except OSError as exc:
            _set_health("error", error=type(exc).__name__)
            return False, f"Could not take the Telegram listener lock ({type(exc).__name__})."
        if lock is None:
            _set_health("lock_held", pid=holder)
            who = f" (pid {holder})" if holder else ""
            return False, f"Telegram is already being handled by another Lumina{who}."

        stop_event = threading.Event()
        _stop_event = stop_event
        _closing = False
        _refusals.reset()
        _set_health("starting")

        def _runner():
            try:
                _run_bridge(stop_event, token)
            finally:
                lock.release()
                with _health_lock:
                    if _health.get("state") in ("starting", "retrying", "listening"):
                        _health.clear()
                        _health.update(state="stopped", since=time.time())

        thread = threading.Thread(target=_runner, daemon=True, name="telegram-listener")
        try:
            thread.start()
        except BaseException:
            lock.release()
            _set_health("error", error="thread_start")
            raise
        _thread = thread
    log.info(f"[TELEGRAM] Listener starting ({reason}).")
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


def begin_shutdown() -> None:
    """The desktop is closing (LuminaWindow.closeEvent, before the origin
    runtime is unregistered). Non-blocking: marks the process as closing --
    so nothing new runs, not even a headless fallback -- and asks the
    listener to stop. Applies only while a listener thread is actually
    alive (reads the thread itself, not is_running(), which tests and
    callers may stand in for)."""
    global _closing
    thread = _thread
    if thread is None or not thread.is_alive():
        return
    _closing = True
    if _stop_event is not None:
        _stop_event.set()


def shutdown_for_exit(timeout: float = 3.0) -> bool:
    """run_gui(), after the Qt event loop returns: stop the listener and
    join it for at most `timeout` seconds, so python-telegram-bot can
    confirm the update offset (no redelivery at the next start). Never
    blocks exit beyond the bound; the daemon thread dies with the process
    and the kernel releases its lock. True when nothing is left running."""
    begin_shutdown()
    thread = _thread
    if thread is None or not thread.is_alive():
        return True
    thread.join(timeout=timeout)
    if thread.is_alive():
        log.warning(f"[TELEGRAM] Listener still stopping after {timeout}s; exiting anyway.")
        return False
    return True


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _reset_for_tests() -> None:
    global _thread, _stop_event, _closing
    _thread = None
    _stop_event = None
    _closing = False
    _refusals.reset()
    _set_health("stopped")


def main():
    """Standalone terminal entry point — unchanged behavior for anyone who
    still wants to run this as a separate script instead of toggling it
    from the GUI. Refused while Telegram remote access is OFF; stops on its
    own if the switch goes OFF while it runs. Takes the same single-consumer
    lock as the desktop listener."""
    if not telegram_access.remote_access_enabled():
        log.error(f"[TELEGRAM] {telegram_access.ACCESS_OFF_MESSAGE}")
        return
    token = get_secret("telegram_bot_token")
    if not token:
        log.error("telegram_bot_token not set — see core/secrets.py, set_secret().")
        return
    bot_id = ingress_policy.parse_bot_id(token)
    if bot_id is None:
        log.error("telegram_bot_token looks malformed — paste it again from BotFather.")
        return
    if not ingress_policy.valid_owner_chat_id(_owner_chat_id()):
        log.error("Owner chat ID not set — refusing to start unsafe.")
        return
    # AGENT-RUNAWAY-SUSPEND-01 -- a standalone bridge opens the desktop's
    # store and never creates one. Not ready means every message gets the
    # fixed refusal until the owner resolves it at the desktop.
    import config
    suspension_status = suspension_runtime.start(config.DATA_DIR, role="telegram_bridge")
    if suspension_status.state is not suspension_runtime.State.ACTIVE:
        log.error(f"[TELEGRAM] Runaway suspension is {suspension_status.state.value}; "
                  "messages will be refused until the desktop owner initializes or recovers it.")
    lock, holder = telegram_poller_lock.acquire(bot_id)
    if lock is None:
        log.error(f"[TELEGRAM] Telegram is already being handled by another Lumina (pid {holder or '?'}).")
        return
    try:
        stop_event = threading.Event()
        if not _run_bridge(stop_event, token):
            raise SystemExit(1)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
