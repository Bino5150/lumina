"""OPERATOR-INGRESS-PROVENANCE-01 (Gatling Slice C, sub-slice C0).

Operator commands (/status, /btw, /compact, /stop, /stop all, /context ...)
are desktop authority. Before this slice the only thing keeping
Telegram-relayed text out of the desktop dispatcher was Telegram's own
`filters.TEXT & ~filters.COMMAND`, which keys on a bot_command entity at
offset 0. A whitespace-prefixed or entity-less " /btw test" passes that
filter, is relayed into the desktop's text handler, and the desktop parser
(which strips whitespace) dispatched it -- reproduced read-only during the
AGENT-RUNAWAY-SUSPEND-01 R2 review.

Covered here (no Qt):
  * runtime-minted ingress envelopes: only a local text submission may
    dispatch operator commands; nothing text-shaped or caller-shaped can
    stand in for one;
  * Telegram ingress refuses any text the desktop parser would read as a
    command, with a fixed reply, before routing or a headless turn;
  * the /resume recovery-handler skeleton: owner/private/unedited/
    unforwarded/exact-grammar verification on real python-telegram-bot
    objects, registered as a CommandHandler, with NO active resume.

The real desktop window paths are in test_operator_ingress_provenance_01_qt.py.
"""
import asyncio
import datetime
import types

import pytest

# TELEGRAM-MASTER-SWITCH-01: these tests model the owner having Telegram
# remote access ON (it defaults to OFF); the switch has its own tests.
pytestmark = pytest.mark.usefixtures("telegram_remote_access_on")
from telegram import Chat, Message, MessageEntity, MessageOriginUser, Update, User
from telegram.ext import CommandHandler, MessageHandler

import comms.telegram_bridge as telegram_bridge
import comms.telegram_recovery as recovery
import core.operator_ingress as ingress
from core import emergency_stop
from core.operator_commands import parse_operator_command


@pytest.fixture(autouse=True)
def _clean_emergency_stop():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


# ── Envelopes ────────────────────────────────────────────────────────────

def test_only_a_local_text_submission_may_dispatch_operator_commands():
    assert ingress.may_dispatch_operator_command(ingress.local_text_submission()) is True
    assert ingress.may_dispatch_operator_command(ingress.telegram_relayed_text("telegram:42:1")) is False
    assert ingress.may_dispatch_operator_command(ingress.local_runtime_action("approve_button")) is False


@pytest.mark.parametrize("impostor", [
    None, "LOCAL_TEXT_SUBMISSION", "local_text_submission", True, 1,
    {"kind": "LOCAL_TEXT_SUBMISSION"},
    types.SimpleNamespace(kind=ingress.IngressKind.LOCAL_TEXT_SUBMISSION),
    ingress.IngressKind.LOCAL_TEXT_SUBMISSION,
])
def test_nothing_text_or_caller_shaped_stands_in_for_an_envelope(impostor):
    assert ingress.may_dispatch_operator_command(impostor) is False


def test_envelopes_cannot_be_constructed_without_the_runtime_factories():
    with pytest.raises(TypeError):
        ingress.OperatorIngress(kind=ingress.IngressKind.LOCAL_TEXT_SUBMISSION)
    with pytest.raises(TypeError):
        ingress.OperatorIngress(ingress.IngressKind.LOCAL_TEXT_SUBMISSION, None, object())


def test_a_subclass_or_a_mutated_envelope_is_not_local_authority():
    relayed = ingress.telegram_relayed_text("telegram:42:1")
    with pytest.raises(Exception):
        relayed.kind = ingress.IngressKind.LOCAL_TEXT_SUBMISSION      # frozen

    class Sub(ingress.OperatorIngress):
        pass

    forged = object.__new__(Sub)
    object.__setattr__(forged, "kind", ingress.IngressKind.LOCAL_TEXT_SUBMISSION)
    object.__setattr__(forged, "event_id", None)
    object.__setattr__(forged, "_mint", ingress._MINT)
    assert ingress.may_dispatch_operator_command(forged) is False


def test_an_envelope_built_around_the_factories_is_not_local_authority():
    forged = object.__new__(ingress.OperatorIngress)               # skips __init__/__post_init__
    object.__setattr__(forged, "kind", ingress.IngressKind.LOCAL_TEXT_SUBMISSION)
    object.__setattr__(forged, "event_id", None)
    object.__setattr__(forged, "_mint", object())
    assert ingress.may_dispatch_operator_command(forged) is False


@pytest.mark.parametrize("text", [
    "/status", " /btw test", "\t/compact", "\n/stop all", "   /context reforge", "/unknown thing",
])
def test_command_detection_mirrors_the_desktop_parser_exactly(text):
    assert parse_operator_command(text) is not None
    assert ingress.looks_like_operator_command(text) is True


@pytest.mark.parametrize("text", ["hello", "what does /btw do?", "​/stop all", "", "   "])
def test_text_the_desktop_would_not_parse_as_a_command_is_ordinary(text):
    assert parse_operator_command(text) is None
    assert ingress.looks_like_operator_command(text) is False


# ── Telegram ingress refuses command-shaped text before any turn ─────────

def _update(chat_id, text, *, reply_to_message_id=None):
    replies = []

    async def _reply(value):
        replies.append(value)

    reply_to = (types.SimpleNamespace(message_id=reply_to_message_id)
                if reply_to_message_id is not None else None)
    message = types.SimpleNamespace(text=text, message_id=9001, reply_to_message=reply_to,
                                    reply_text=_reply)
    return types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=chat_id), message=message), replies


def _no_turns(monkeypatch):
    def _fail(*a, **k):
        pytest.fail("command-shaped relayed text must not reach routing or a model turn")

    monkeypatch.setattr(telegram_bridge, "origin_routing",
                        types.SimpleNamespace(resolve=_fail, dispatch=_fail), raising=False)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn", _fail)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: "42")


@pytest.mark.parametrize("text", [
    " /btw test", " /stop all", " /compact", "\t/context reforge", "/status", "/unknown x",
])
def test_command_shaped_owner_text_gets_the_fixed_refusal_and_no_turn(monkeypatch, text):
    _no_turns(monkeypatch)
    update, replies = _update(42, text, reply_to_message_id=7001)

    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == [ingress.RELAYED_COMMAND_REFUSAL]


def test_refusal_text_is_fixed_machine_text_that_echoes_nothing():
    assert "/btw" not in ingress.RELAYED_COMMAND_REFUSAL
    assert ingress.RELAYED_COMMAND_REFUSAL.startswith("[Lumina:")


def test_unauthorized_chat_with_command_text_stays_silent(monkeypatch):
    _no_turns(monkeypatch)
    update, replies = _update(999, " /stop all")

    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == []


def test_command_text_while_emergency_stopped_stays_silent(monkeypatch):
    _no_turns(monkeypatch)
    emergency_stop.latch(source="test", reason="ingress")
    update, replies = _update(42, " /btw test")

    asyncio.run(telegram_bridge.handle_message(update, context=None))

    assert replies == []


def test_ordinary_owner_text_still_reaches_its_turn(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram_bridge, "origin_routing", types.SimpleNamespace(
        resolve=lambda **kw: types.SimpleNamespace(route=None, reason="cold")), raising=False)
    monkeypatch.setattr(telegram_bridge, "_owner_chat_id", lambda: 42)
    monkeypatch.setattr(telegram_bridge, "run_headless_turn",
                        lambda **kw: calls.append(kw) or {"success": True, "response": "hi"})
    for text in ("hello there", "what does /btw do?", "​/stop all"):
        update, replies = _update(42, text)
        asyncio.run(telegram_bridge.handle_message(update, context=None))
        assert replies == ["hi"]
    assert [c["task"] for c in calls] == ["hello there", "what does /btw do?", "​/stop all"]


# ── /resume recovery-handler skeleton (no active resume) ────────────────

OWNER = 42
NOW = datetime.datetime(2026, 10, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _owner():
    return User(id=OWNER, first_name="owner", is_bot=False)


def _msg(text, *, chat=None, user="owner", **extra):
    chat = chat or Chat(id=OWNER, type=Chat.PRIVATE)
    from_user = _owner() if user == "owner" else user
    head = text.split(" ", 1)[0]
    entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(head))] if text.startswith("/") else []
    return Message(message_id=777, date=NOW, chat=chat, from_user=from_user, text=text,
                   entities=entities, **extra)


def _upd(message=None, *, edited=None):
    return Update(update_id=1, message=message, edited_message=edited)


def _verify(update, bot="LuminaBot", owner=OWNER):
    return recovery.verify_recovery_update(update, owner_chat_id=owner, bot_username=bot)


def test_a_verified_owner_recovery_event_carries_only_runtime_identity():
    v = _verify(_upd(_msg("/resume ABCD2345")))
    assert isinstance(v, recovery.VerifiedTelegramRecovery)
    assert v.event_id == "telegram:42:777"
    assert v.selector == "ABCD2345" and v.ack_unknown is False
    assert v.message_date == NOW


def test_bot_mention_lowercase_selector_and_ack_unknown_are_accepted_exactly():
    v = _verify(_upd(_msg("/resume@LuminaBot abcd2345 ack-unknown")))
    assert v.selector == "ABCD2345" and v.ack_unknown is True
    v = _verify(_upd(_msg("/resume@luminabot ABCD2345")))
    assert v.selector == "ABCD2345"


def _rejected(update, reason, **kw):
    with pytest.raises(recovery.RecoveryRejected) as exc:
        _verify(update, **kw)
    assert exc.value.reason == reason


@pytest.mark.parametrize("update, reason", [
    (_upd(_msg("/resume ABCD2345", chat=Chat(id=OWNER, type=Chat.GROUP))), "not_private"),
    (_upd(_msg("/resume ABCD2345", chat=Chat(id=43, type=Chat.PRIVATE))), "owner_mismatch"),
    (_upd(_msg("/resume ABCD2345", user=User(id=43, first_name="x", is_bot=False))), "owner_mismatch"),
    (_upd(_msg("/resume ABCD2345", user=User(id=OWNER, first_name="x", is_bot=True))), "owner_mismatch"),
    (_upd(_msg("/resume ABCD2345", user=None)), "owner_mismatch"),
    (_upd(None, edited=_msg("/resume ABCD2345")), "edited"),
    (_upd(_msg("/resume ABCD2345", edit_date=NOW)), "edited"),
    (_upd(_msg("/resume ABCD2345", forward_origin=MessageOriginUser(NOW, _owner()))), "forwarded"),
    (_upd(_msg("/resume ABCD2345", is_automatic_forward=True)), "forwarded"),
    (_upd(_msg("/resume ABCD2345", via_bot=User(id=7, first_name="b", is_bot=True))), "via_bot"),
    (_upd(_msg("/resume ABCD2345", sender_chat=Chat(id=-100, type=Chat.CHANNEL))), "sender_chat"),
    (_upd(_msg("/resume")), "malformed"),
    (_upd(_msg("/resume ABCD234")), "malformed"),
    (_upd(_msg("/resume ABCD23456")), "malformed"),
    (_upd(_msg("/resume ABCD2345 extra")), "malformed"),
    (_upd(_msg("/resume  ABCD2345")), "malformed"),
    (_upd(_msg("/resume ABCD2345\n")), "malformed"),
    (_upd(_msg("/resume ABCDI345")), "malformed"),            # I is not Crockford base32
    (_upd(_msg("/resumeABCD2345")), "malformed"),
    (_upd(_msg("/resume@OtherBot ABCD2345")), "wrong_bot"),
])
def test_every_recovery_check_rejects_its_case(update, reason):
    _rejected(update, reason)


def test_leading_whitespace_is_not_a_recovery_command():
    m = Message(message_id=777, date=NOW, chat=Chat(id=OWNER, type=Chat.PRIVATE), from_user=_owner(),
                text=" /resume ABCD2345", entities=[MessageEntity(MessageEntity.BOT_COMMAND, 1, 7)])
    _rejected(_upd(m), "malformed")


def test_no_configured_owner_rejects_everything():
    _rejected(_upd(_msg("/resume ABCD2345")), "owner_unset", owner=None)
    _rejected(_upd(_msg("/resume ABCD2345")), "owner_unset", owner="")


def test_handlers_register_a_resume_command_that_ignores_edits_beside_the_unchanged_text_handler():
    added = []
    app = types.SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h))

    telegram_bridge._register_handlers(app)

    commands = [h for h in added if isinstance(h, CommandHandler)]
    texts = [h for h in added if isinstance(h, MessageHandler)]
    assert len(commands) == 1 and commands[0].commands == frozenset({"resume"})
    assert commands[0].callback is recovery.handle_recovery_command
    assert len(texts) == 1 and texts[0].callback is telegram_bridge.handle_message
    bot = types.SimpleNamespace(username="LuminaBot")
    good = _upd(_msg("/resume ABCD2345"))
    good.message.set_bot(bot)
    assert commands[0].check_update(good)
    edited = _upd(None, edited=_msg("/resume ABCD2345"))
    edited.edited_message.set_bot(bot)
    assert not commands[0].check_update(edited)
    assert not texts[0].check_update(good)                      # commands never reach the text path


@pytest.fixture
def replies(monkeypatch):
    sent = []

    async def _reply(self, text, *args, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "reply_text", _reply)
    monkeypatch.setattr(recovery, "_owner_chat_id", lambda: OWNER)
    return sent


def _handle(update):
    context = types.SimpleNamespace(bot=types.SimpleNamespace(username="LuminaBot"))
    asyncio.run(recovery.handle_recovery_command(update, context))


def test_verified_owner_resume_gets_the_not_active_reply_and_changes_nothing(replies):
    _handle(_upd(_msg("/resume ABCD2345")))
    assert replies == [recovery.RECOVERY_NOT_ACTIVE_REPLY]
    assert "ABCD2345" not in recovery.RECOVERY_NOT_ACTIVE_REPLY


@pytest.mark.parametrize("update", [
    _upd(_msg("/resume ABCD2345", chat=Chat(id=43, type=Chat.PRIVATE))),
    _upd(_msg("/resume ABCD2345", forward_origin=MessageOriginUser(NOW, _owner()))),
    _upd(_msg("/resume")),
    _upd(None, edited=_msg("/resume ABCD2345")),
])
def test_rejected_recovery_events_are_dropped_silently(replies, update):
    _handle(update)
    assert replies == []


def test_recovery_events_are_dropped_while_emergency_stopped(replies):
    emergency_stop.latch(source="test", reason="recovery")
    _handle(_upd(_msg("/resume ABCD2345")))
    assert replies == []


def test_the_skeleton_has_no_resume_capability_to_call():
    assert not hasattr(recovery, "resume")
    assert not any(name.startswith(("clear", "rearm", "unlatch", "suspend")) for name in dir(recovery))
