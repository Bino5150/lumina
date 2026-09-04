"""
tests/test_context_rebuild_ui.py -- CONTEXT-GC-01/A6I: ui/main_window.py
wiring and authority proof for core/context_rebuild.py.

Companion to tests/test_context_rebuild.py (which covers the coordinator
itself against a neutral _FakeGenerationOwner, no Qt at all). This file
proves the things that live only in ui/main_window.py and the surrounding
codebase:

1. /context, /context status, /context rebuild are desktop-owner-only --
   never reachable from a Telegram-relayed dispatch, a malformed/unknown
   command form, or (structurally) any model/tool/headless path.
2. /context status is truly read-only.
3. _command_context_rebuild()'s synchronous admission checks match
   _context_rebuild_eligibility() exactly and never start work when
   ineligible.
4. The full spawn -> coordinator -> signal-back pipeline is wired
   correctly, and _on_context_rebuild_finished() renders a truthful
   receipt.
5. No model-facing context-rebuild tool exists anywhere in ToolRegistry.

Same lightweight-fake convention as tests/test_context_transaction_ui.py
and tests/test_manual_compaction_ui.py: unbound LuminaWindow methods bound
via types.MethodType onto a types.SimpleNamespace shaped like enough of a
real window -- no QApplication/real LuminaWindow construction needed.
"""
import inspect
import os
import threading
import types

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import core.context_checkpoints as cc
import core.context_rebuild as context_rebuild_module
import tools.memory as memory
import tools.palace as palace
import ui.main_window as main_window
from core import emergency_stop
from core.context_transaction import ContextGeneration
from core.operator_commands import parse_operator_command
from ui.main_window import LuminaWindow


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    memory.init_chat_db()
    palace.init_palace_db()
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_emergency_stop():
    emergency_stop._reset_for_tests()
    yield
    emergency_stop._reset_for_tests()


def _seed_chat(messages=()):
    chat_id = memory.create_chat("test chat")
    for role, content in messages:
        memory.save_chat_message(chat_id, role, content)
    return chat_id


class _ChatWidget:
    def __init__(self):
        self.notices = []

    def add_operator_message(self, text):
        self.notices.append(text)


class _Ctx:
    def __init__(self, history=None, compacting=False):
        self.history = list(history or [])
        self._compacting = compacting
        self._last_usage_snapshot = None


class _Signals:
    def __init__(self):
        self.emitted = []
        self.context_rebuild_finished = types.SimpleNamespace(emit=self.emitted.append)


class _RunningWorker:
    def isRunning(self):
        return True


def _fake_window(chat_id=None, ctx=None, telegram_active=None, worker=None):
    fake = types.SimpleNamespace(
        _current_chat_id=chat_id,
        agent=types.SimpleNamespace(ctx=ctx if ctx is not None else _Ctx(), llm=object(),
                                     get_context_usage=lambda chat_id=None, refresh=False: {
                                         "used_tokens": 100, "max_tokens": 1000, "percent": 10.0,
                                     }),
        chat_widget=_ChatWidget(),
        worker=worker,
        _telegram_active_dispatch=telegram_active,
        _manual_compaction_thread=None,
        _context_rebuild_thread=None,
        _context_rebuild_cancel=None,
        _context_rebuild_started_at=None,
        _context_generation=ContextGeneration(),
        signals=_Signals(),
        _refresh_operator_telemetry=lambda refresh_context=False: None,
    )
    for name in (
        "current_chat_id", "current_generation", "bump", "live_ctx",
        "_dispatch_operator_command", "_command_context", "_command_context_status",
        "_command_context_rebuild", "_context_rebuild_eligibility", "_on_context_rebuild_finished",
    ):
        setattr(fake, name, types.MethodType(getattr(LuminaWindow, name), fake))
    return fake


# ── Authority: desktop-only, never Telegram ──────────────────────────────

@pytest.mark.parametrize("argument", ["", "status", "rebuild"])
def test_context_family_rejected_when_telegram_dispatch_active(argument, monkeypatch):
    spy_calls = []
    monkeypatch.setattr(
        context_rebuild_module, "run_rebuild",
        lambda *a, **k: spy_calls.append((a, k)),
    )
    fake = _fake_window(chat_id=_seed_chat([("user", "u1")]), telegram_active=object())

    fake._command_context(argument)

    assert not spy_calls
    assert fake.chat_widget.notices
    assert "desktop-only" in fake.chat_widget.notices[-1] or "not available over Telegram" in fake.chat_widget.notices[-1]
    assert fake._context_rebuild_thread is None


def test_context_rebuild_via_full_dispatch_from_telegram_never_starts_thread(monkeypatch):
    """End-to-end through _dispatch_operator_command(), exactly the path a
    Telegram-relayed '/context rebuild' would take per _on_user_message()."""
    fake = _fake_window(chat_id=_seed_chat([("user", "u1")]), telegram_active=object())
    command = parse_operator_command("/context rebuild")
    assert command.known

    fake._dispatch_operator_command(command)

    assert fake._context_rebuild_thread is None
    assert fake.chat_widget.notices


def test_context_family_admitted_from_desktop_dispatch(monkeypatch):
    """Sanity: with no Telegram dispatch active, /context IS reachable --
    proving the rejection above is a real, meaningful gate, not a check
    that would reject everything."""
    fake = _fake_window(chat_id=_seed_chat([("user", "u1")]), telegram_active=None)
    command = parse_operator_command("/context status")

    fake._dispatch_operator_command(command)

    assert fake.chat_widget.notices
    assert "desktop-only" not in fake.chat_widget.notices[-1]


# ── Command grammar: malformed / unknown forms rejected, no side effects ──

@pytest.mark.parametrize("text", [
    "/context foo", "/context rebuild now", "/context --force", "/context Rebuild Now",
])
def test_context_malformed_subcommand_never_starts_rebuild(text, monkeypatch):
    spy_calls = []
    monkeypatch.setattr(
        context_rebuild_module, "run_rebuild",
        lambda *a, **k: spy_calls.append((a, k)),
    )
    fake = _fake_window(chat_id=_seed_chat([("user", "u1")]))
    command = parse_operator_command(text)
    assert command.known  # "context" itself is known; the subcommand is what's malformed

    fake._dispatch_operator_command(command)

    assert not spy_calls
    assert fake._context_rebuild_thread is None
    assert "Unknown /context subcommand" in fake.chat_widget.notices[-1]


def test_reconstruct_synonym_is_not_a_known_command():
    command = parse_operator_command("/reconstruct")
    assert command.known is False


def test_unknown_command_dispatch_never_reaches_context_handler(monkeypatch):
    calls = []
    fake = _fake_window(chat_id=_seed_chat([("user", "u1")]))
    fake._command_context = types.MethodType(
        lambda self, arg: calls.append(arg), fake,
    )
    command = parse_operator_command("/reconstruct")

    fake._dispatch_operator_command(command)

    assert not calls
    assert "Unknown command" in fake.chat_widget.notices[-1]


# ── No model-facing reconstruction tool anywhere ─────────────────────────

_BANNED_TOOL_NAMES = {
    "rebuild_context", "context_gc", "context_reconstruct",
    "flush_context", "optimize_context",
}


def test_no_model_facing_context_rebuild_tool_registered():
    from core.agent import LuminaAgent
    agent = LuminaAgent(owner=True, channel_id="context-gc-01-a6i-tool-audit", backend="llamacpp")
    names = set(agent.registry.all_tool_names())
    assert not (names & _BANNED_TOOL_NAMES)
    assert not any("rebuild" in n.lower() and "context" in n.lower() for n in names)
    assert not any("reconstruct" in n.lower() for n in names)


def _imported_module_names(module):
    """Actual `import x` / `from x import y` module names only -- never a
    raw substring search over source text, which would false-positive on
    any comment or docstring that merely mentions another module by name
    (this codebase's docstrings do that constantly, e.g. to cross-reference
    design lineage)."""
    import ast
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_agent_module_never_imports_operator_commands_or_context_rebuild_coordinator():
    """Structural proof that the model's only reach into tools (core.agent.
    LuminaAgent.registry.call()) is disjoint from the /context dispatch
    path -- core/agent.py itself never imports the operator-command parser
    or this coordinator, so there is no code path by which a tool call
    could invoke either."""
    import core.agent as agent_module
    imported = _imported_module_names(agent_module)
    assert not any("operator_commands" in n for n in imported)
    assert not any("context_rebuild" in n for n in imported)


# ── Headless / Discord / non-owner channels structurally excluded ───────

def test_headless_module_never_imports_operator_commands_or_main_window():
    import core.headless as headless_module
    imported = _imported_module_names(headless_module)
    assert not any("operator_commands" in n for n in imported)
    assert not any("main_window" in n for n in imported)
    assert not any("context_rebuild" in n for n in imported)


def test_discord_bridge_never_imports_operator_commands_or_main_window():
    import comms.discord_bridge as discord_module
    imported = _imported_module_names(discord_module)
    assert not any("operator_commands" in n for n in imported)
    assert not any("main_window" in n for n in imported)
    assert not any("context_rebuild" in n for n in imported)


# ── /context status: read-only ───────────────────────────────────────────

def test_status_no_active_chat_reports_truthfully_and_touches_nothing():
    fake = _fake_window(chat_id=None)
    fake._command_context_status()
    assert "no active chat" in fake.chat_widget.notices[-1].lower()


def test_status_never_mutates_history_or_creates_a_checkpoint(monkeypatch):
    chat_id = _seed_chat([("user", "u1"), ("assistant", "a1")])
    ctx = _Ctx(history=[{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}])
    fake = _fake_window(chat_id=chat_id, ctx=ctx)
    before = list(ctx.history)

    def _boom(*a, **k):
        raise AssertionError("/context status must never compile/create a checkpoint")

    monkeypatch.setattr(context_rebuild_module, "run_rebuild", _boom)
    monkeypatch.setattr("core.continuity_compiler.compile_continuity_checkpoint", _boom)
    monkeypatch.setattr(cc, "begin_checkpoint", _boom)

    fake._command_context_status()

    assert ctx.history == before
    assert cc.list_checkpoints(chat_id) == []
    assert fake.chat_widget.notices


@pytest.mark.parametrize("setup,expected_fragment", [
    (lambda fake: setattr(fake, "worker", _RunningWorker()), "foreground turn"),
    (lambda fake: setattr(fake.agent.ctx, "_compacting", True), "compaction"),
    (lambda fake: setattr(fake, "_manual_compaction_thread", object()), "compact"),
    (lambda fake: setattr(fake, "_context_rebuild_thread", object()), "already in progress"),
])
def test_status_reports_truthful_ineligibility_reason(setup, expected_fragment):
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    setup(fake)

    fake._command_context_status()

    notice = fake.chat_widget.notices[-1]
    assert "Rebuild eligible: no" in notice
    assert expected_fragment in notice.lower()


def test_status_reports_eligible_when_nothing_blocks():
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)

    fake._command_context_status()

    assert "Rebuild eligible: yes" in fake.chat_widget.notices[-1]


def test_status_reports_latched_emergency_stop():
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    emergency_stop.latch(source="test", reason="test")

    fake._command_context_status()

    notice = fake.chat_widget.notices[-1]
    assert "Rebuild eligible: no" in notice
    assert "emergency stop" in notice.lower()


# ── _command_context_rebuild(): admission mirrors eligibility exactly ───

@pytest.mark.parametrize("setup,expected_fragment", [
    (lambda fake: setattr(fake, "_current_chat_id", None), "no active chat"),
    (lambda fake: setattr(fake, "worker", _RunningWorker()), "foreground turn"),
    (lambda fake: setattr(fake.agent.ctx, "_compacting", True), "compaction"),
    (lambda fake: setattr(fake, "_manual_compaction_thread", object()), "compact"),
    (lambda fake: setattr(fake, "_context_rebuild_thread", object()), "already in progress"),
])
def test_rebuild_rejected_synchronously_when_ineligible_never_starts_thread(setup, expected_fragment, monkeypatch):
    spy_calls = []
    monkeypatch.setattr(
        context_rebuild_module, "run_rebuild",
        lambda *a, **k: spy_calls.append((a, k)),
    )
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    setup(fake)

    fake._command_context_rebuild()

    assert not spy_calls
    notice = fake.chat_widget.notices[-1]
    assert notice.startswith("Rebuild rejected:")
    assert expected_fragment in notice.lower()


def test_rebuild_rejected_while_latched():
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    emergency_stop.latch(source="test", reason="test")

    fake._command_context_rebuild()

    notice = fake.chat_widget.notices[-1]
    assert notice.startswith("Rebuild rejected:")
    assert "emergency stop" in notice.lower()
    assert fake._context_rebuild_thread is None


# ── Eligible rebuild: full spawn -> coordinator -> signal pipeline ───────

def test_eligible_rebuild_spawns_thread_calls_coordinator_and_emits_receipt(monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    ctx = _Ctx(history=[{"role": "user", "content": "u1"}])
    fake = _fake_window(chat_id=chat_id, ctx=ctx)

    captured = {}

    def _fake_run_rebuild(cid, generation_owner, backend, ephemeral_history, **kwargs):
        captured["chat_id"] = cid
        captured["generation_owner"] = generation_owner
        captured["backend"] = backend
        captured["ephemeral_history"] = ephemeral_history
        captured["kwargs"] = kwargs
        return context_rebuild_module.RebuildReceipt(
            status=context_rebuild_module.STATUS_SUCCESS, reason="Rebuild complete.",
            chat_id=cid, checkpoint_id=1, pre_history_count=1, post_history_count=1,
            pre_estimated_tokens=10, post_estimated_tokens=5,
        )

    monkeypatch.setattr(context_rebuild_module, "run_rebuild", _fake_run_rebuild)

    fake._command_context_rebuild()
    assert fake._context_rebuild_thread is not None
    fake._context_rebuild_thread.join(timeout=5)

    assert captured["chat_id"] == chat_id
    assert captured["generation_owner"] is fake
    assert captured["backend"] is fake.agent.llm
    assert captured["ephemeral_history"] == [{"role": "user", "content": "u1"}]
    assert "cancel_event" in captured["kwargs"]
    assert isinstance(captured["kwargs"]["cancel_event"], threading.Event)
    assert "expected_epoch" in captured["kwargs"]

    assert len(fake.signals.emitted) == 1
    assert fake.signals.emitted[0].status == context_rebuild_module.STATUS_SUCCESS


def test_coordinator_exception_is_caught_and_reported_never_crashes_thread(monkeypatch):
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)

    def _boom(*a, **k):
        raise RuntimeError("simulated coordinator crash")

    monkeypatch.setattr(context_rebuild_module, "run_rebuild", _boom)

    fake._command_context_rebuild()
    fake._context_rebuild_thread.join(timeout=5)

    assert len(fake.signals.emitted) == 1
    receipt = fake.signals.emitted[0]
    assert receipt.status == context_rebuild_module.STATUS_ERROR
    assert "simulated coordinator crash" in receipt.reason


# ── _on_context_rebuild_finished(): truthful receipt rendering ──────────

def test_on_finished_success_renders_receipt_and_resets_bookkeeping():
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    fake._context_rebuild_thread = object()
    fake._context_rebuild_cancel = object()
    fake._context_rebuild_started_at = 123.0
    receipt = context_rebuild_module.RebuildReceipt(
        status=context_rebuild_module.STATUS_SUCCESS, reason="Rebuild complete.",
        chat_id=chat_id, checkpoint_id=42, pre_history_count=10, post_history_count=3,
        pre_estimated_tokens=500, post_estimated_tokens=120,
    )

    fake._on_context_rebuild_finished(receipt)

    assert fake._context_rebuild_thread is None
    assert fake._context_rebuild_cancel is None
    assert fake._context_rebuild_started_at is None
    notice = fake.chat_widget.notices[-1]
    assert "42" in notice
    assert "10" in notice and "3" in notice
    assert "durable transcript unchanged" in notice


def test_on_finished_failure_renders_reason_verbatim_and_resets_bookkeeping():
    chat_id = _seed_chat([("user", "u1")])
    fake = _fake_window(chat_id=chat_id)
    fake._context_rebuild_thread = object()
    receipt = context_rebuild_module.RebuildReceipt(
        status=context_rebuild_module.STATUS_CANCELLED,
        reason="Rebuild cancelled. Live context unchanged.",
        chat_id=chat_id,
    )

    fake._on_context_rebuild_finished(receipt)

    assert fake._context_rebuild_thread is None
    assert fake.chat_widget.notices[-1] == "/context rebuild: Rebuild cancelled. Live context unchanged."


def test_on_finished_for_a_chat_no_longer_active_does_not_touch_current_chat_display():
    chat_id = _seed_chat([("user", "u1")])
    other_chat_id = _seed_chat([("user", "u2")])
    fake = _fake_window(chat_id=other_chat_id)  # user switched away already
    receipt = context_rebuild_module.RebuildReceipt(
        status=context_rebuild_module.STATUS_SUCCESS, reason="Rebuild complete.",
        chat_id=chat_id, checkpoint_id=1, pre_history_count=1, post_history_count=1,
        pre_estimated_tokens=1, post_estimated_tokens=1,
    )

    fake._on_context_rebuild_finished(receipt)

    notice = fake.chat_widget.notices[-1]
    assert "no longer the active chat" in notice
    assert "durable transcript unchanged" in notice
