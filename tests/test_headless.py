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
