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
