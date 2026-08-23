"""core/headless.py — FE-18: _reap_idle() used to fire the idle callback
while still holding _lock. Harmless while the callback is unset (today),
but the moment something wires real work to it (Discord-Lite's planned
summarization LLM call), every channel's cache access -- and therefore
every inbound message, since get_headless_agent() takes this same lock --
would freeze for the callback's full duration. This confirms the lock is
actually free by the time the callback fires, and that reaping still
happens correctly.
"""
import threading
import time
import types
import core.headless as headless
import pytest


# ── CODING-06R.1 Repair A: cached-agent owner authority ────────────────────
#
# get_headless_agent()'s force_tools_profile branch used to apply the
# CALL-TIME owner argument to an already-cached agent's registry, instead
# of the cached agent's own (immutable) agent.owner. A cache-hit call with
# owner=True for a channel that was genuinely established as owner=False
# could therefore re-enable owner-only tools (run_tests,
# save_coding_checkpoint, read_coding_checkpoint, ...) on that non-owner's
# registry even though agent.owner itself never changed -- confirmed 06R
# privilege-elevation finding. Mirrors core/agent.py's apply_persona(),
# which already gates its own apply_tool_profile() call on self.owner
# rather than any external argument.
class TestCachedOwnerAuthority:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self):
        saved = (dict(headless._agents), dict(headless._last_used), dict(headless._is_owner))
        headless._agents.clear()
        headless._last_used.clear()
        headless._is_owner.clear()
        yield
        headless._agents.clear()
        headless._last_used.clear()
        headless._is_owner.clear()
        headless._agents.update(saved[0])
        headless._last_used.update(saved[1])
        headless._is_owner.update(saved[2])

    def test_non_owner_cache_hit_with_mismatched_owner_true_stays_non_owner(self):
        from core.tool_profiles import OWNER_ONLY_TOOLS
        channel = "test-cached-owner-mismatch-nonowner"
        agent = headless.get_headless_agent(channel, owner=False)
        assert agent.owner is False

        agent2 = headless.get_headless_agent(
            channel, owner=True, force_tools_profile="Coding",
        )
        assert agent2 is agent, "cache hit must return the same instance"
        assert agent.owner is False, "agent.owner must never be mutated by a later call"

        enabled = set(agent.registry.list_enabled())
        for tool in ("run_tests", "save_coding_checkpoint", "read_coding_checkpoint"):
            assert tool not in enabled, (
                f"{tool} was enabled on a genuine non-owner's cached agent via a "
                f"mismatched call-time owner=True argument"
            )
        assert enabled.isdisjoint(OWNER_ONLY_TOOLS)

    def test_owner_cache_hit_with_mismatched_owner_false_not_demoted(self):
        channel = "test-cached-owner-mismatch-owner"
        agent = headless.get_headless_agent(channel, owner=True)
        assert agent.owner is True

        agent2 = headless.get_headless_agent(
            channel, owner=False, force_tools_profile="Coding",
        )
        assert agent2 is agent
        assert agent.owner is True, "agent.owner must never be mutated by a later call"

        enabled = set(agent.registry.list_enabled())
        assert "run_tests" in enabled
        assert "save_coding_checkpoint" in enabled
        assert "read_coding_checkpoint" in enabled

    def test_same_owner_repeated_calls_unaffected(self):
        channel = "test-cached-owner-mismatch-same"
        agent = headless.get_headless_agent(channel, owner=False)
        agent2 = headless.get_headless_agent(
            channel, owner=False, force_tools_profile="Coding",
        )
        assert agent2 is agent
        enabled = set(agent.registry.list_enabled())
        assert "run_python" in enabled  # Coding profile still applies for a real non-owner
        assert "run_tests" not in enabled  # but its owner-only member stays stripped

    def test_owner_mismatch_logs_but_does_not_raise(self, capsys):
        channel = "test-cached-owner-mismatch-log"
        headless.get_headless_agent(channel, owner=False)
        headless.get_headless_agent(channel, owner=True, force_tools_profile="Coding")
        out = capsys.readouterr().out
        assert channel in out
        assert "owner" in out.lower()

    def test_fresh_construction_owner_true_unaffected(self):
        channel = "test-cached-owner-mismatch-fresh-owner"
        agent = headless.get_headless_agent(
            channel, owner=True, force_tools_profile="Coding",
        )
        assert agent.owner is True
        enabled = set(agent.registry.list_enabled())
        assert "run_tests" in enabled

    def test_fresh_construction_owner_false_unaffected(self):
        channel = "test-cached-owner-mismatch-fresh-guest"
        agent = headless.get_headless_agent(
            channel, owner=False, force_tools_profile="Coding",
        )
        assert agent.owner is False
        enabled = set(agent.registry.list_enabled())
        assert "run_tests" not in enabled


def test_idle_callback_fires_after_lock_released(monkeypatch):
    # Swap in a plain (non-reentrant) Lock so .locked() reflects whether
    # ANYTHING -- including this same thread -- currently holds it. The
    # real module uses an RLock, which would let the same thread re-enter
    # even with the old bug present, masking the regression.
    monkeypatch.setattr(headless, "_lock", threading.Lock())
    monkeypatch.setattr(headless, "_agents", {"chan-1": object()})
    monkeypatch.setattr(
        headless, "_last_used",
        {"chan-1": time.time() - headless.IDLE_TIMEOUT_SECONDS - 1},
    )
    monkeypatch.setattr(headless, "_is_owner", {"chan-1": False})

    seen = []

    def callback(cid):
        seen.append(cid)
        assert not headless._lock.locked(), (
            "FE-18 regression: idle callback fired while _lock was still held"
        )

    monkeypatch.setattr(headless, "_on_idle_callback", callback)

    headless._reap_idle()

    assert seen == ["chan-1"]
    assert "chan-1" not in headless._agents
    assert "chan-1" not in headless._last_used
    assert "chan-1" not in headless._is_owner


def test_reap_idle_skips_owner_true_channels(monkeypatch):
    monkeypatch.setattr(headless, "_lock", threading.Lock())
    monkeypatch.setattr(headless, "_agents", {"telegram-owner": object()})
    monkeypatch.setattr(
        headless, "_last_used",
        {"telegram-owner": time.time() - headless.IDLE_TIMEOUT_SECONDS - 1},
    )
    monkeypatch.setattr(headless, "_is_owner", {"telegram-owner": True})
    monkeypatch.setattr(headless, "_on_idle_callback", None)

    headless._reap_idle()

    # owner=True channels (Telegram) are never reaped on a timer.
    assert "telegram-owner" in headless._agents


# MB-06: run_headless_turn(trace=True) captures structured tool-call data
# instead of only console-logging it. Monkeypatches get_headless_agent
# itself (rather than building a fake `self` and calling LuminaAgent.chat
# unbound, as test_agent_tool_budget.py does) so this exercises
# run_headless_turn exactly as production callers do, without constructing
# a real LuminaAgent.
def _fake_agent(response="fake response"):
    ns = types.SimpleNamespace()
    ns.registry = types.SimpleNamespace(all_tool_names=lambda: ["tool_a", "tool_b"])
    ns.on_tool_call = lambda name, args: None
    ns.on_tool_result = lambda name, result: None

    def _chat(task, source="OWNER_DIRECT"):
        # Drive the callbacks the same way LuminaAgent.chat() really does,
        # around a single simulated tool call.
        ns.on_tool_call("tool_a", {"x": 1})
        ns.on_tool_result("tool_a", "ok")
        return response

    ns.chat = _chat
    return ns


def test_trace_false_is_byte_identical_to_pre_mb06_shape(monkeypatch):
    fake = _fake_agent()
    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: fake)

    result = headless.run_headless_turn("hi", "chan-1", owner=True)

    assert result == {"success": True, "response": "fake response"}
    assert "tool_calls" not in result
    assert "available_tools" not in result


def test_trace_true_captures_tool_calls_and_available_tools(monkeypatch):
    fake = _fake_agent()
    monkeypatch.setattr(headless, "get_headless_agent", lambda *a, **k: fake)

    result = headless.run_headless_turn("hi", "chan-1", owner=True, trace=True)

    assert result["success"] is True
    assert result["response"] == "fake response"
    assert result["tool_calls"] == [{"name": "tool_a", "args": {"x": 1}, "result": "ok"}]
    assert result["available_tools"] == ["tool_a", "tool_b"]
